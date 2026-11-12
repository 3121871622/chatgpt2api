"""O(1) account selection index for very large account pools.

The legacy selector scanned every account in the pool on every request, built a
fresh candidate list, then re-scanned it while waiting for capacity.  At 7k
accounts that already cost seconds; at 1M accounts it is unusable.

This module keeps the *selection topology* of the pool in memory instead of
recomputing it:

* accounts are bucketed by ``(mode, plan, source)``; each bucket keeps a
  ``deque`` of tokens that currently have spare capacity, so taking the next
  account is a ``popleft`` instead of a full scan;
* every deque entry carries an epoch.  Re-classifying an account only needs a
  new epoch, which invalidates the old entry lazily instead of paying an
  ``O(n)`` removal;
* saturated tokens leave the deque and are re-queued when capacity is
  released, so a busy pool never burns CPU skipping busy accounts;
* writing a balance is O(1) amortised and read/acquire never touches the whole
  pool.

The index is deliberately free of business policy: the caller supplies a
``classifier`` mapping an account dict to ``(image_ready, text_ready, plan,
source)``, and the shape of the readiness rules stays owned by
:mod:`services.account_service`.
"""

from __future__ import annotations

from collections import deque
from threading import RLock
from typing import Callable, Iterable, Iterator, Mapping

IMAGE_MODE = "image"
TEXT_MODE = "text"

MODES = (IMAGE_MODE, TEXT_MODE)

UNKNOWN_KEY = "unknown"

BucketKey = tuple[str, str]

_Classifier = Callable[[Mapping[str, object]], tuple[bool, bool, str, str]]


def normalize_bucket_part(value: object) -> str:
    """Normalise one bucket dimension (plan / source) for key stability."""
    text = str(value or "").strip().lower()
    return text or UNKNOWN_KEY


class AccountSelectionIndex:
    """Incrementally maintained candidate buckets for image and text selection."""

    def __init__(
        self,
        classifier: _Classifier,
        *,
        scan_budget: int = 32,
    ) -> None:
        self._lock = RLock()
        self._classifier = classifier
        self._scan_budget = max(1, int(scan_budget))
        # token -> (epoch, image bucket key | None, text bucket key | None)
        self._meta: dict[str, tuple[int, BucketKey | None, BucketKey | None]] = {}
        self._queues: dict[str, dict[BucketKey, deque[tuple[str, int]]]] = {
            mode: {} for mode in MODES
        }
        self._sizes: dict[str, dict[BucketKey, int]] = {mode: {} for mode in MODES}
        self._order: dict[str, list[BucketKey]] = {mode: [] for mode in MODES}
        self._cursor: dict[str, int] = {mode: 0 for mode in MODES}
        # Smooth weighted round-robin credit per bucket.  Rotating buckets
        # without weights gives a 2-account Plus bucket the same share as an
        # 8,000-account free bucket; this ledger makes each *account* roughly
        # equally likely instead.
        self._current_weight: dict[str, dict[BucketKey, int]] = {
            mode: {} for mode in MODES
        }
        self._inflight: dict[str, int] = {}
        self._saturated: set[str] = set()
        self._epoch_seq = 0
        self._adds = 0
        self._removals = 0
        self._acquires = 0
        self._scanned = 0

    # ---------------------------------------------------------------- writing

    def _classify(
        self,
        account: Mapping[str, object],
    ) -> tuple[BucketKey | None, BucketKey | None]:
        try:
            image_ready, text_ready, plan, source = self._classifier(account)
        except Exception:
            return (None, None)
        plan_key = normalize_bucket_part(plan)
        source_key = normalize_bucket_part(source)
        image_bucket: BucketKey | None = (plan_key, source_key) if image_ready else None
        text_bucket: BucketKey | None = (plan_key, source_key) if text_ready else None
        return image_bucket, text_bucket

    def add(self, token: str, account: Mapping[str, object]) -> bool:
        """Insert or re-classify one account.  Returns True when state changed."""
        normalized = str(token or "").strip()
        if not normalized:
            return False
        image_bucket, text_bucket = self._classify(account)
        with self._lock:
            previous = self._meta.get(normalized)
            if previous is not None:
                if previous[1] == image_bucket and previous[2] == text_bucket:
                    return False
                self._dec_sizes(previous[1], previous[2])
            self._epoch_seq += 1
            epoch = self._epoch_seq
            self._meta[normalized] = (epoch, image_bucket, text_bucket)
            self._inc_sizes(image_bucket, text_bucket)
            self._enqueue(normalized, epoch, image_bucket, text_bucket)
            self._adds += 1
            return True

    def remove(self, token: str) -> bool:
        normalized = str(token or "").strip()
        if not normalized:
            return False
        with self._lock:
            previous = self._meta.pop(normalized, None)
            if previous is None:
                return False
            self._dec_sizes(previous[1], previous[2])
            self._inflight.pop(normalized, None)
            self._saturated.discard(normalized)
            self._removals += 1
            return True

    def rekey(self, old_token: str, new_token: str) -> None:
        """Move runtime state when a credential rotation renames a token."""
        source = str(old_token or "").strip()
        target = str(new_token or "").strip()
        if not source or not target or source == target:
            return
        with self._lock:
            meta = self._meta.get(source)
            inflight = self._inflight.pop(source, 0)
            saturated = source in self._saturated
            self._saturated.discard(source)
            if meta is None:
                return
            self._meta.pop(source, None)
            self._epoch_seq += 1
            epoch = self._epoch_seq
            self._meta[target] = (epoch, meta[1], meta[2])
            if inflight:
                self._inflight[target] = inflight
            if saturated:
                self._saturated.add(target)
            self._enqueue(target, epoch, meta[1], meta[2])

    def sync(self, accounts: Mapping[str, Mapping[str, object]]) -> int:
        """Reconcile the whole index with ``accounts``; returns changed rows."""
        changed = 0
        seen: set[str] = set()
        for token, account in accounts.items():
            normalized = str(token or "").strip()
            if not normalized:
                continue
            seen.add(normalized)
            if self.add(normalized, account):
                changed += 1
        if len(seen) != len(self._meta):
            with self._lock:
                stale = [token for token in self._meta if token not in seen]
            for token in stale:
                if self.remove(token):
                    changed += 1
        return changed

    def rebucket(self, token: str, account: Mapping[str, object]) -> bool:
        """Alias for :meth:`add` used by in-place account updates."""
        return self.add(token, account)

    # --------------------------------------------------------------- capacity

    def acquire(
        self,
        mode: str,
        *,
        plans: Iterable[str] | None = None,
        sources: Iterable[str] | None = None,
        excluded: Iterable[str] | None = None,
        max_concurrency: int = 1,
    ) -> str | None:
        """Take the next account with spare capacity, or None.

        ``plans`` / ``sources`` narrow the eligible buckets; an empty or
        ``None`` filter means "any".  ``excluded`` is per-request and never
        evicts a token from the index.
        """
        limit = max(1, int(max_concurrency or 1))
        plan_filter = self._filter_set(plans)
        source_filter = self._filter_set(sources)
        excluded_set = set(excluded or ())
        with self._lock:
            candidates = self._eligible_buckets(mode, plan_filter, source_filter)
            while candidates:
                chosen_index, bucket, _ = self._pick_weighted_bucket(mode, candidates)
                token = self._take_from(
                    mode,
                    bucket,
                    self._queues.get(mode, {}).get(bucket),
                    limit,
                    excluded_set,
                )
                if token is not None:
                    self._cursor[mode] = (chosen_index + 1) % max(
                        1, len(self._order.get(mode) or [])
                    )
                    self._acquires += 1
                    return token
                # The bucket was counted as ready but its deque only held stale
                # or saturated entries; exclude it from this acquire pass.
                candidates = [item for item in candidates if item[1] != bucket]
            self._cursor[mode] = (self._cursor.get(mode, 0) + 1) % max(
                1, len(self._order.get(mode) or [])
            )
            return None

    def _eligible_buckets(
        self,
        mode: str,
        plan_filter: set[str] | None,
        source_filter: set[str] | None,
    ) -> list[tuple[int, BucketKey, int]]:
        order = self._order.get(mode) or []
        sizes = self._sizes.get(mode) or {}
        eligible: list[tuple[int, BucketKey, int]] = []
        for index, bucket in enumerate(order):
            if plan_filter is not None and bucket[0] not in plan_filter:
                continue
            if source_filter is not None and bucket[1] not in source_filter:
                continue
            size = int(sizes.get(bucket, 0) or 0)
            if size > 0:
                eligible.append((index, bucket, size))
        return eligible

    def _pick_weighted_bucket(
        self,
        mode: str,
        candidates: list[tuple[int, BucketKey, int]],
    ) -> tuple[int, BucketKey, int]:
        """Smooth weighted round-robin by ready-account count."""
        total = sum(size for _, _, size in candidates)
        current = self._current_weight.setdefault(mode, {})
        for _, bucket, size in candidates:
            current[bucket] = int(current.get(bucket, 0)) + size
        chosen = max(candidates, key=lambda item: (current.get(item[1], 0), -item[0]))
        current[chosen[1]] = int(current.get(chosen[1], 0)) - total
        return chosen

    def _take_from(
        self,
        mode: str,
        bucket: BucketKey,
        queue: deque[tuple[str, int]] | None,
        limit: int,
        excluded: set[str],
        *,
        lease: bool = True,
    ) -> str | None:
        if not queue:
            return None
        for _ in range(min(len(queue), self._scan_budget)):
            if not queue:
                return None
            token, epoch = queue.popleft()
            self._scanned += 1
            meta = self._meta.get(token)
            if meta is None or meta[0] != epoch:
                # stale entry: account deleted, re-bucketed or re-queued
                continue
            if meta[1 if mode == IMAGE_MODE else 2] != bucket:
                continue
            if token in excluded:
                queue.append((token, epoch))
                continue
            if not lease:
                queue.append((token, epoch))
                return token
            inflight = int(self._inflight.get(token, 0)) + 1
            self._inflight[token] = inflight
            if inflight >= limit:
                self._saturated.add(token)
            else:
                queue.append((token, epoch))
            return token
        return None

    def next_ready(
        self,
        mode: str,
        *,
        plans: Iterable[str] | None = None,
        sources: Iterable[str] | None = None,
        excluded: Iterable[str] | None = None,
    ) -> str | None:
        """Rotate to the next ready token without leasing capacity.

        Used by text selection, which is request-scoped and does not hold a
        per-account slot.  The token stays in its bucket.
        """
        plan_filter = self._filter_set(plans)
        source_filter = self._filter_set(sources)
        excluded_set = set(excluded or ())
        with self._lock:
            candidates = self._eligible_buckets(mode, plan_filter, source_filter)
            while candidates:
                chosen_index, bucket, _ = self._pick_weighted_bucket(mode, candidates)
                token = self._take_from(
                    mode,
                    bucket,
                    self._queues.get(mode, {}).get(bucket),
                    1,
                    excluded_set,
                    lease=False,
                )
                if token is not None:
                    self._cursor[mode] = (chosen_index + 1) % max(
                        1, len(self._order.get(mode) or [])
                    )
                    return token
                candidates = [item for item in candidates if item[1] != bucket]
            self._cursor[mode] = (self._cursor.get(mode, 0) + 1) % max(
                1, len(self._order.get(mode) or [])
            )
            return None

    def release(self, token: str, *, max_concurrency: int = 1) -> None:
        normalized = str(token or "").strip()
        if not normalized:
            return
        limit = max(1, int(max_concurrency or 1))
        with self._lock:
            inflight = int(self._inflight.get(normalized, 0))
            if inflight <= 1:
                self._inflight.pop(normalized, None)
                remaining = 0
            else:
                self._inflight[normalized] = inflight - 1
                remaining = inflight - 1
            if normalized not in self._saturated:
                return
            if remaining >= limit:
                return
            self._saturated.discard(normalized)
            meta = self._meta.get(normalized)
            if meta is None:
                return
            self._enqueue(normalized, meta[0], meta[1], meta[2])

    # ------------------------------------------------------------------ reads

    def has_ready(
        self,
        mode: str,
        *,
        plans: Iterable[str] | None = None,
        sources: Iterable[str] | None = None,
    ) -> bool:
        plan_filter = self._filter_set(plans)
        source_filter = self._filter_set(sources)
        with self._lock:
            for bucket, size in (self._sizes.get(mode) or {}).items():
                if size <= 0:
                    continue
                if plan_filter is not None and bucket[0] not in plan_filter:
                    continue
                if source_filter is not None and bucket[1] not in source_filter:
                    continue
                return True
            return False

    def count_ready(
        self,
        mode: str,
        *,
        plans: Iterable[str] | None = None,
        sources: Iterable[str] | None = None,
    ) -> int:
        plan_filter = self._filter_set(plans)
        source_filter = self._filter_set(sources)
        total = 0
        with self._lock:
            for bucket, size in (self._sizes.get(mode) or {}).items():
                if size <= 0:
                    continue
                if plan_filter is not None and bucket[0] not in plan_filter:
                    continue
                if source_filter is not None and bucket[1] not in source_filter:
                    continue
                total += size
        return total

    def iter_ready(
        self,
        mode: str,
        *,
        plans: Iterable[str] | None = None,
        sources: Iterable[str] | None = None,
        excluded: Iterable[str] | None = None,
        with_capacity_only: bool = False,
        max_concurrency: int = 1,
    ) -> Iterator[str]:
        """Enumerate ready tokens without leasing them (used by previews).

        Only tokens are snapshotted, so enumerating a million-account pool costs
        a few megabytes rather than a copy of every account record.
        """
        plan_filter = self._filter_set(plans)
        source_filter = self._filter_set(sources)
        excluded_set = set(excluded or ())
        limit = max(1, int(max_concurrency or 1))
        with self._lock:
            snapshot: list[str] = [
                token
                for token, meta in self._meta.items()
                if (bucket := self._bucket_for(meta, mode)) is not None
                and (plan_filter is None or bucket[0] in plan_filter)
                and (source_filter is None or bucket[1] in source_filter)
                and token not in excluded_set
                and (
                    not with_capacity_only
                    or int(self._inflight.get(token, 0)) < limit
                )
            ]
        for token in snapshot:
            yield token

    def stats(self) -> dict[str, object]:
        with self._lock:
            image_sizes = self._sizes.get(IMAGE_MODE) or {}
            text_sizes = self._sizes.get(TEXT_MODE) or {}
            return {
                "tracked": len(self._meta),
                "image_ready": sum(image_sizes.values()),
                "text_ready": sum(text_sizes.values()),
                "image_buckets": len(self._order.get(IMAGE_MODE) or []),
                "text_buckets": len(self._order.get(TEXT_MODE) or []),
                "inflight": len(self._inflight),
                "saturated": len(self._saturated),
                "adds": self._adds,
                "removals": self._removals,
                "acquires": self._acquires,
                "scanned": self._scanned,
            }

    # --------------------------------------------------------------- internal

    @staticmethod
    def _filter_set(values: Iterable[str] | None) -> set[str] | None:
        if values is None:
            return None
        normalized = {normalize_bucket_part(value) for value in values}
        normalized.discard("")
        return normalized or None

    @staticmethod
    def _bucket_for(
        meta: tuple[int, BucketKey | None, BucketKey | None],
        mode: str,
    ) -> BucketKey | None:
        return meta[1] if mode == IMAGE_MODE else meta[2]

    def _inc_sizes(self, image_bucket: BucketKey | None, text_bucket: BucketKey | None) -> None:
        if image_bucket is not None:
            sizes = self._sizes[IMAGE_MODE]
            sizes[image_bucket] = sizes.get(image_bucket, 0) + 1
            self._ensure_bucket(IMAGE_MODE, image_bucket)
        if text_bucket is not None:
            sizes = self._sizes[TEXT_MODE]
            sizes[text_bucket] = sizes.get(text_bucket, 0) + 1
            self._ensure_bucket(TEXT_MODE, text_bucket)

    def _dec_sizes(self, image_bucket: BucketKey | None, text_bucket: BucketKey | None) -> None:
        for mode, bucket in ((IMAGE_MODE, image_bucket), (TEXT_MODE, text_bucket)):
            if bucket is None:
                continue
            sizes = self._sizes[mode]
            remaining = sizes.get(bucket, 0) - 1
            if remaining > 0:
                sizes[bucket] = remaining
            else:
                sizes.pop(bucket, None)

    def _ensure_bucket(self, mode: str, bucket: BucketKey) -> None:
        queues = self._queues[mode]
        if bucket not in queues:
            queues[bucket] = deque()
            self._order[mode] = sorted(queues)

    def _enqueue(
        self,
        token: str,
        epoch: int,
        image_bucket: BucketKey | None,
        text_bucket: BucketKey | None,
    ) -> None:
        if token in self._saturated:
            # The token keeps its membership but waits for a release.
            return
        for mode, bucket in ((IMAGE_MODE, image_bucket), (TEXT_MODE, text_bucket)):
            if bucket is None:
                continue
            self._ensure_bucket(mode, bucket)
            self._queues[mode][bucket].append((token, epoch))
