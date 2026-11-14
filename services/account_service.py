from __future__ import annotations

import hashlib
import json
import random
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Condition, Lock, Thread

from services.lock_diagnostics import InstrumentedLock
from services import gateway_client as _gw
from typing import Any, Callable, Iterator
from uuid import uuid4

from services.account_credentials import (
    ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
    access_token_expires_in_seconds,
    access_token_issued_at,
    decode_access_token_payload,
    project_upstream_credential_availability,
)
from services.account_index import IMAGE_MODE, TEXT_MODE, AccountSelectionIndex
from services.account_scoring import (
    candidate_score,
    last_used_epoch,
    record_egress_result,
    record_image_health,
)
from services.upstream_circuit import UPSTREAM_FAILURE_CODES, upstream_circuit
from services.account_processing import (
    account_processing_batch,
    account_processing_priority_slot,
    account_processing_slot,
    account_processing_worker_count,
)
from services.account_operation_events import (
    ACCOUNT_OPERATION_EVENT_LIMIT,
    normalize_account_operation_event,
)
from services.config import config
from services.image_failure import ImageFailure, classify_image_exception, image_failure
from services.log_service import (
    LOG_TYPE_ACCOUNT,
    log_service,
)
from services.storage.base import (
    StorageBackend,
    StorageMutation,
    StorageRevisionConflictError,
)
from utils.diagnostics import diagnostic_excerpt, sanitize_diagnostic_text
from utils.helper import anonymize_token
from utils.log import logger

_RemoteCheckMarker = tuple[str, str, str, str, bool | None, str, str]
_CredentialGeneration = tuple[str, str, str]

_MISSING = object()


def _index_plan_key(account: dict) -> str:
    value = AccountService._normalize_account_type(account.get("type"))
    return str(value or "unknown").strip().lower() or "unknown"


def _index_source_key(account: dict) -> str:
    return AccountService._normalize_source_type(account.get("source_type"))


def classify_account_for_index(account: dict) -> tuple[bool, bool, str, str]:
    """Selection topology of one account for :mod:`services.account_index`.

    Credential availability is computed once and reused for both the image and
    text readiness checks: it decodes the access token, so asking twice per
    write (and twice per row projection) dominated bulk operations.
    """
    availability = project_upstream_credential_availability(
        str(account.get("access_token") or ""),
        str(account.get("refresh_token") or ""),
        access_confirmed_invalid=(
            str(account.get("last_remote_check_result") or "").strip().lower() == "invalid"
        ),
        refresh_confirmed_invalid=bool(account.get("refresh_token_invalid_at")),
    )
    credentials_usable = availability.status != "unavailable"
    check_ready = (
        account.get("last_remote_check_result") != "pending"
        or account.get("pending_auth_scope") == "image"
    )
    status = account.get("status")
    # A known zero quota is already rejected by _is_image_account_available.
    # Keep the selection index consistent with it: otherwise the index hands
    # out spent accounts, and every image request pays a full remote validation
    # round-trip per exhausted account before finding a usable one.
    try:
        image_quota_ready = bool(account.get("image_quota_unknown")) or int(
            account.get("quota") or 0
        ) > 0
    except (TypeError, ValueError):
        image_quota_ready = False
    image_ready = (
        credentials_usable
        and check_ready
        and status == "正常"
        and image_quota_ready
    )
    text_ready = credentials_usable and check_ready and status in {"正常", "限流"}
    return (image_ready, text_ready, _index_plan_key(account), _index_source_key(account))


class _AccountRecordIterator:
    """Read-only iterator over the live account map in insertion order."""

    __slots__ = ("_accounts", "_order", "_index")

    def __init__(self, accounts: dict[str, dict], order: list[str]) -> None:
        self._accounts = accounts
        self._order = order
        self._index = 0

    def __iter__(self) -> "_AccountRecordIterator":
        return self

    def __next__(self) -> dict:
        order = self._order
        accounts = self._accounts
        while self._index < len(order):
            token = order[self._index]
            self._index += 1
            account = accounts.get(token)
            if account is not None:
                return account
        raise StopIteration


class _AccountStore(dict):
    """Account map that keeps the runtime indexes in sync on every write.

    The account service mutates ``self._accounts`` from a dozen code paths.
    Intercepting the mapping operations updates the selection index, the
    management-id lookup and the insertion order exactly once per mutation, so
    no caller has to remember to do it.
    """

    def __init__(self, on_change: Callable[[str, str, object, object], None]) -> None:
        super().__init__()
        self._on_change = on_change

    def __setitem__(self, key: str, value: dict) -> None:
        previous = super().get(key, _MISSING)
        super().__setitem__(key, value)
        self._on_change("set", key, None if previous is _MISSING else previous, value)

    def __delitem__(self, key: str) -> None:
        previous = super().__getitem__(key)
        super().__delitem__(key)
        self._on_change("del", key, previous, None)

    def pop(self, key: str, *default: object) -> object:
        if not super().__contains__(key):
            if default:
                return default[0]
            raise KeyError(key)
        previous = super().pop(key)
        self._on_change("del", key, previous, None)
        return previous

    def clear(self) -> None:
        keys = list(super().keys())
        super().clear()
        for key in keys:
            self._on_change("del", key, None, None)

    def update(self, *args: object, **kwargs: object) -> None:
        merged: dict[str, dict] = {}
        for value in args:
            if isinstance(value, dict):
                merged.update(value)
        merged.update(kwargs)  # type: ignore[arg-type]
        for key, item in merged.items():
            self[key] = item

    def setdefault(self, key: str, default: dict | None = None) -> dict:  # type: ignore[override]
        if super().__contains__(key):
            return super().__getitem__(key)
        value = {} if default is None else default
        self[key] = value
        return value

    def __deepcopy__(self, memo: dict[int, object]) -> dict[str, dict]:
        """Snapshot as a plain mapping: the change hook is not data."""
        return {key: deepcopy(value, memo) for key, value in dict.items(self)}



class ImageAccountSelectionError(RuntimeError):
    """图片账号调度失败。

    这是“本次请求为什么没拿到账号”的错误，不等同于账号持久状态。
    账号是否限流/异常仍由远程配额确认或鉴权结果决定。
    """

    # 控制流只认两个 pool outcomes；deadline_exceeded is request-scoped and
    # never changes a persisted Upstream Account state.
    #   quota_exhausted -> 远程确认额度耗尽，告诉客户端别重试（429）
    #   unavailable     -> 其它一切（没号/全忙/预检失败/上游波动），可重试（503）
    DEFAULTS: dict[str, tuple[int, str, str]] = {
        "quota_exhausted": (429, "insufficient_quota", "insufficient_quota"),
        "unavailable": (503, "server_error", "no_available_account"),
        "deadline_exceeded": (503, "server_error", "task_interrupted"),
    }

    def __init__(self, kind: str, message: str = "") -> None:
        defaults = self.DEFAULTS.get(kind, self.DEFAULTS["unavailable"])
        self.kind = kind if kind in self.DEFAULTS else "unavailable"
        self.status_code, self.error_type, self.code = defaults
        detail = message or self.kind.replace("_", " ")
        super().__init__(f"image_account_selection:{self.kind}; {detail}")


class OAuthRefreshError(RuntimeError):
    """Structured OAuth token refresh failure."""

    def __init__(self, status_code: int, error_code: str = "", description: str = "") -> None:
        self.status_code = int(status_code or 0)
        self.error_code = str(error_code or "").strip()
        self.description = str(description or "").strip()[:300]
        details = [f"oauth_refresh_http_{self.status_code}"]
        if self.error_code:
            details.append(self.error_code)
        if self.description and self.description.casefold() != self.error_code.casefold():
            details.append(self.description)
        super().__init__(": ".join(details))


class TerminalRefreshTokenError(OAuthRefreshError):
    """The refresh credential is revoked, expired, or otherwise unusable."""

    def __init__(self, status_code: int, error_code: str = "", description: str = "") -> None:
        super().__init__(status_code, error_code, description)
        self.failure = image_failure("auth_invalid", raw_detail=str(self))


class RefreshCredentialsChangedError(RuntimeError):
    """Refresh credentials changed while an OAuth request was in flight."""

    def __init__(self) -> None:
        message = "OAuth refresh credentials changed during refresh."
        self.failure = image_failure("upstream_unavailable", raw_detail=message)
        super().__init__(message)


class AccountService:
    """账号池服务，使用 token -> account 的 dict 保存账号。"""

    _ACCESS_TOKEN_REFRESH_SKEW_SECONDS = ACCESS_TOKEN_REFRESH_SKEW_SECONDS
    _TOKEN_REFRESH_ERROR_BACKOFF_SECONDS = 5 * 60
    _POOL_HEALTH_REFRESH_BATCH_SIZE = 10
    _IMAGE_FAILURE_REFRESH_DEDUP_SECONDS = 30
    _ACCESS_TOKEN_FINGERPRINT_LIMIT = 8
    _REFRESH_PROGRESS_COMPLETED_TTL_SECONDS = 10 * 60
    _REFRESH_PROGRESS_ACTIVE_TTL_SECONDS = 60 * 60
    _REFRESH_PROGRESS_PRUNE_INTERVAL_SECONDS = 60
    _REFRESH_PROGRESS_EVENT_LIMIT = ACCOUNT_OPERATION_EVENT_LIMIT
    _STORAGE_MUTATION_MAX_ATTEMPTS = 4
    _ACCOUNT_SNAPSHOT_TTL_SECONDS = 5.0
    _GIT_ACCOUNT_SNAPSHOT_TTL_SECONDS = 60.0
    # Operational totals only; resettable state such as invalid_count stays LWW.
    _ADDITIVE_ACCOUNT_COUNTER_FIELDS = frozenset({"success", "fail"})
    _OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
    _OAUTH_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
    _OAUTH_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36"
    )
    _TERMINAL_REFRESH_ERROR_CODES = frozenset({
        "invalid_grant",
        "invalid_refresh_token",
        "refresh_token_invalidated",
        # The upstream returns this once a refresh token was already consumed.
        # Without it the account stayed "expiring" forever and the lifecycle
        # watcher retried (and logged) it every cycle.
        "refresh_token_reused",
        "refresh_token_expired",
        "refresh_token_revoked",
        "invalid_token",
        "token_expired",
    })
    _TERMINAL_REFRESH_MESSAGE_FRAGMENTS = (
        "session has ended",
        "refresh token has already been used",
        "refresh token has expired",
        "refresh token is invalid",
        "refresh token was revoked",
    )
    # 刷新进度追踪
    _refresh_progress: dict[str, dict] = {}
    _refresh_progress_lock = Lock()
    _refresh_progress_last_pruned_at = 0.0

    def __init__(
        self,
        storage_backend: StorageBackend,
        *,
        proxy_reference_mutation: Callable[
            [list[tuple[object, object]], Callable[[list[str]], Any]],
            Any,
        ] | None = None,
    ):
        self.storage = storage_backend
        self._proxy_reference_mutation = proxy_reference_mutation
        self._lock = InstrumentedLock(
            "account",
            slow_seconds=1.0,
            on_slow=self._on_account_lock_slow,
        )
        self._oauth_refresh_flights_lock = Lock()
        self._oauth_refresh_flights: dict[_CredentialGeneration, Future[str]] = {}
        self._image_slot_condition = Condition(self._lock)
        self._account_snapshot_refresh_lock = Lock()
        self._index = 0
        self._persisted_accounts: dict[str, dict] = {}
        self._accounts_revision = ""
        self._selection_index = AccountSelectionIndex(classify_account_for_index)
        self._by_management_id: dict[str, str] = {}
        self._token_order: list[str] = []
        self._token_holes = 0
        self._group_counts: dict[str, int] = {}
        self._group_counts_dirty = False
        self._accounts = self._build_account_store(self._load_accounts())
        self._account_snapshot_checked_at = time.monotonic()
        # Kick off a background sync to the Go gateway so it has accounts ready
        # from the first request onward.  Non-blocking and best-effort.
        self._sync_gateway_accounts_async()
        self._image_inflight: dict[str, int] = {}
        self._image_failure_refresh_lock = Lock()
        self._image_failure_refresh_active: set[str] = set()
        self._image_failure_refresh_active_scopes: dict[str, str] = {}
        self._image_failure_refresh_rerun: set[str] = set()
        self._image_failure_refresh_pending: deque[str] = deque()
        self._image_failure_refresh_pending_set: set[str] = set()
        self._image_failure_refresh_pending_scopes: dict[str, str] = {}
        self._image_failure_refresh_started_at: dict[str, float] = {}
        self._token_aliases: dict[str, str] = {}
        self._cumulative_total = self._load_cumulative_total()

    def _build_account_store(self, accounts: dict[str, dict]) -> "_AccountStore":
        """Wrap a plain account mapping and rebuild every derived index."""
        store = _AccountStore(self._on_account_store_change)
        dict.update(store, accounts)
        self._rebuild_runtime_indexes(store)
        return store

    def _rebuild_runtime_indexes(self, store: dict[str, dict]) -> None:
        by_id: dict[str, str] = {}
        order: list[str] = []
        group_counts: dict[str, int] = {}
        for token, account in store.items():
            management_id = str(account.get("management_id") or "").strip().lower()
            if management_id:
                by_id[management_id] = token
            group_id = str(account.get("group_id") or "").strip()
            if group_id:
                group_counts[group_id] = group_counts.get(group_id, 0) + 1
            order.append(token)
        self._by_management_id = by_id
        self._token_order = order
        self._token_holes = 0
        self._group_counts = group_counts
        self._group_counts_dirty = False
        self._selection_index.sync(self._least_recently_used_view(store))

    @staticmethod
    def _least_recently_used_view(store: dict[str, dict]) -> dict[str, dict]:
        """Order accounts never-used first, then oldest use first.

        The selection queues are FIFO and are rebuilt from this mapping on
        every restart/snapshot reload.  Feeding them in database insertion
        order reset the rotation to the same head of the pool each time, so a
        frequently redeployed 10k pool kept spending the first ~7k accounts
        while ~2.8k healthy accounts were never reached.  A random tie-break
        spreads never-used accounts instead of re-creating a fixed head.
        """
        keyed = [
            (last_used_epoch(account), random.random(), token, account)
            for token, account in store.items()
        ]
        keyed.sort(key=lambda item: (item[0], item[1]))
        return {token: account for _, _, token, account in keyed}

    def group_counts(self) -> dict[str, int]:
        """Account count per group, cached until a group membership changes."""
        if self._group_counts_dirty:
            counts: dict[str, int] = {}
            for account in self._accounts.values():
                group_id = str(account.get("group_id") or "").strip()
                if group_id:
                    counts[group_id] = counts.get(group_id, 0) + 1
            self._group_counts = counts
            self._group_counts_dirty = False
        return self._group_counts

    @staticmethod
    def _selection_affecting_fields(account: dict | None) -> tuple:
        if not isinstance(account, dict):
            return ()
        return (
            account.get("status"),
            account.get("quota"),
            account.get("image_quota_unknown"),
            account.get("type"),
            account.get("source_type"),
            account.get("access_token"),
            bool(account.get("refresh_token")),
            account.get("last_remote_check_result"),
            account.get("pending_auth_scope"),
            bool(account.get("refresh_token_invalid_at")),
        )

    def _on_account_store_change(
        self,
        operation: str,
        token: str,
        previous: object,
        current: object,
    ) -> None:
        """Keep derived indexes consistent with an in-place account write."""
        if operation == "set" and isinstance(current, dict):
            previous_account = previous if isinstance(previous, dict) else None
            old_id = str((previous_account or {}).get("management_id") or "").strip().lower()
            new_id = str(current.get("management_id") or "").strip().lower()
            if old_id and old_id != new_id and self._by_management_id.get(old_id) == token:
                self._by_management_id.pop(old_id, None)
            if new_id:
                self._by_management_id[new_id] = token
            if previous_account is None:
                self._token_order.append(token)
            if str((previous_account or {}).get("group_id") or "") != str(
                current.get("group_id") or ""
            ):
                self._group_counts_dirty = True
            if (
                previous_account is None
                or self._selection_affecting_fields(previous_account)
                != self._selection_affecting_fields(current)
            ):
                self._selection_index.add(token, current)
            return
        if operation == "del":
            previous_account = previous if isinstance(previous, dict) else None
            old_id = str((previous_account or {}).get("management_id") or "").strip().lower()
            if old_id and self._by_management_id.get(old_id) == token:
                self._by_management_id.pop(old_id, None)
            if str((previous_account or {}).get("group_id") or ""):
                self._group_counts_dirty = True
            self._selection_index.remove(token)
            self._token_holes += 1
            if self._token_holes > max(1024, len(self._token_order) // 4):
                self._token_order = [
                    item for item in self._token_order if item in self._accounts
                ]
                self._token_holes = 0

    def _get_cumulative_file(self) -> Path:
        storage_path = getattr(self.storage, "file_path", None)
        if isinstance(storage_path, Path):
            return storage_path.with_name(".cumulative_total")
        from services.config import DATA_DIR
        return DATA_DIR / ".cumulative_total"

    def _load_cumulative_total(self) -> int:
        try:
            f = self._get_cumulative_file()
            if f.exists():
                return int(f.read_text().strip())
        except Exception:
            pass
        return len(self._accounts)

    def _save_cumulative_total(self) -> None:
        try:
            self._get_cumulative_file().write_text(str(self._cumulative_total))
        except Exception:
            pass

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _decode_jwt_payload(token: str) -> dict:
        return decode_access_token_payload(token)

    @staticmethod
    def _management_id_for_token(access_token: str) -> str:
        digest = hashlib.sha256(str(access_token or "").encode("utf-8")).hexdigest()[:24]
        return f"acct_{digest}"

    @staticmethod
    def _access_token_fingerprint(access_token: str) -> str:
        return hashlib.sha256(str(access_token or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _scrub_diagnostic_secrets(
        account: dict,
        values: list[object],
    ) -> None:
        secrets = [str(value or "").strip() for value in values]
        secrets = [secret for secret in secrets if secret]
        if not secrets:
            return
        for key in (
            "last_remote_check_error",
            "last_refresh_error",
            "last_token_refresh_error",
        ):
            value = account.get(key)
            if not isinstance(value, str):
                continue
            account[key] = sanitize_diagnostic_text(
                value,
                sensitive_values=secrets,
            )

    @classmethod
    def _normalize_access_token_fingerprints(
        cls,
        value: object,
        access_token: str,
    ) -> list[str]:
        candidates = value if isinstance(value, list) else []
        fingerprints = [
            str(item or "").strip().lower()
            for item in candidates
            if len(str(item or "").strip()) == 64
            and all(char in "0123456789abcdef" for char in str(item or "").strip().lower())
        ]
        current = cls._access_token_fingerprint(access_token)
        fingerprints = list(dict.fromkeys(fingerprints))
        first = fingerprints[0] if fingerprints else current
        recent = [fingerprint for fingerprint in fingerprints[1:] if fingerprint != current]
        if current != first:
            recent.append(current)
        return [first, *recent[-(cls._ACCESS_TOKEN_FINGERPRINT_LIMIT - 1):]]

    @classmethod
    def _normalize_management_id(cls, value: object, access_token: str) -> str:
        candidate = str(value or "").strip().lower()
        suffix = candidate[5:] if candidate.startswith("acct_") else ""
        if len(suffix) == 24 and all(char in "0123456789abcdef" for char in suffix):
            return candidate
        return cls._management_id_for_token(access_token)


    @classmethod
    def _unique_management_id(
        cls,
        access_token: str,
        preferred: object,
        used_ids: set[str],
    ) -> str:
        candidate = cls._normalize_management_id(preferred, access_token)
        if candidate not in used_ids:
            return candidate
        counter = 1
        while True:
            digest = hashlib.sha256(
                f"{access_token}\0{counter}".encode("utf-8")
            ).hexdigest()[:24]
            candidate = f"acct_{digest}"
            if candidate not in used_ids:
                return candidate
            counter += 1

    @staticmethod
    def _parse_time(value: object) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except Exception:
            try:
                parsed = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
            except Exception:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _timestamp_to_iso(value: object) -> str:
        try:
            ts = int(value)
        except (TypeError, ValueError):
            return ""
        tz = timezone(timedelta(hours=8))
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(tz).isoformat()

    def _normalize_loaded_accounts(
        self,
        accounts: list[Any],
        *,
        recover_interrupted_checks: bool,
    ) -> tuple[dict[str, dict], bool]:
        loaded: dict[str, dict] = {}
        used_management_ids: set[str] = set()
        changed = False
        for item in accounts:
            normalized = self._normalize_account(item)
            if normalized is None:
                changed = True
                continue
            access_token = str(normalized.get("access_token") or "").strip()
            previous = loaded.get(access_token)
            if previous is not None:
                used_management_ids.discard(
                    str(previous.get("management_id") or "").strip().lower()
                )
            management_id = self._unique_management_id(
                access_token,
                normalized.get("management_id"),
                used_management_ids,
            )
            if management_id != normalized.get("management_id"):
                normalized["management_id"] = management_id
                changed = True
            if (
                recover_interrupted_checks
                and normalized.get("last_remote_check_result") == "pending"
                and normalized.get("pending_auth_scope") != "image"
            ):
                normalized["last_remote_check_result"] = "error"
                normalized["last_remote_check_error"] = (
                    normalized.get("last_remote_check_error")
                    or "Account verification was interrupted by a service restart."
                )
                normalized["last_remote_check_error_at"] = datetime.now(timezone.utc).isoformat()
                normalized["pending_auth_remove_invalid"] = None
                normalized["pending_auth_scope"] = None
                normalized.pop("pending_auth_verification_id", None)
                changed = True
            if normalized != item:
                changed = True
            used_management_ids.add(management_id)
            loaded[normalized["access_token"]] = normalized
        return loaded, changed

    def _load_accounts(self) -> dict[str, dict]:
        last_conflict: StorageRevisionConflictError | None = None
        for _ in range(self._STORAGE_MUTATION_MAX_ATTEMPTS):
            loaded, revision, changed = self._read_accounts_snapshot(
                recover_interrupted_checks=True,
            )
            if changed:
                try:
                    result = self.storage.replace_accounts(
                        list(loaded.values()),
                        expected_revision=revision,
                    )
                except StorageRevisionConflictError as exc:
                    last_conflict = exc
                    continue
                revision = result.revision
            self._persisted_accounts = deepcopy(loaded)
            self._accounts_revision = revision
            return loaded
        assert last_conflict is not None
        raise last_conflict

    def _read_accounts_snapshot(
        self,
        *,
        recover_interrupted_checks: bool = False,
    ) -> tuple[dict[str, dict], str, bool]:
        snapshot = self.storage.load_accounts_snapshot()
        loaded, changed = self._normalize_loaded_accounts(
            snapshot.items,
            recover_interrupted_checks=recover_interrupted_checks,
        )
        return loaded, snapshot.revision, changed

    def _account_snapshot_ttl_seconds(self) -> float:
        ttl = max(0.0, float(self._ACCOUNT_SNAPSHOT_TTL_SECONDS))
        # Reloading the whole account table is O(pool).  The control plane owns
        # the database in a single process, so a longer window for large pools
        # removes the periodic 0.5-1s stall without changing local correctness;
        # selection still re-validates credentials before leasing an account.
        try:
            count = len(self._accounts)
        except TypeError:
            count = 0
        if count > 250_000:
            ttl = max(ttl, 120.0)
        elif count > 50_000:
            ttl = max(ttl, 60.0)
        elif count > 10_000:
            ttl = max(ttl, 30.0)
        elif count > 2_000:
            ttl = max(ttl, 15.0)
        try:
            backend_type = str(self.storage.get_backend_info().get("type") or "").strip().lower()
        except Exception:
            return ttl
        if backend_type == "git":
            return max(ttl, float(self._GIT_ACCOUNT_SNAPSHOT_TTL_SECONDS))
        return ttl

    def _apply_account_view_locked(
        self,
        accounts: dict[str, dict],
        *,
        persisted_accounts: dict[str, dict],
        revision: str,
        snapshot_checked: bool = True,
        preserve_index: bool = False,
        prepared_persisted: dict[str, dict] | None = None,
    ) -> None:
        token_rotations = self._passive_token_rotations_locked(accounts)
        self._accounts = self._build_account_store(accounts)
        # deepcopy of the whole pool is pure (it only reads the argument), so a
        # caller may hand in an already-copied mapping to keep this copy out of
        # the lock hold.  Falling back to deepcopy keeps every other call site
        # unchanged.
        self._persisted_accounts = (
            prepared_persisted
            if prepared_persisted is not None
            else deepcopy(persisted_accounts)
        )
        self._accounts_revision = revision
        for new_token, alias_sources in token_rotations:
            self._move_account_runtime_token_locked(new_token, alias_sources)
        if not preserve_index:
            self._index = self._index % len(self._accounts) if self._accounts else 0
        if snapshot_checked:
            self._account_snapshot_checked_at = time.monotonic()
        self._image_slot_condition.notify_all()
        # Keep the Go gateway index in sync whenever the account view changes.
        self._sync_gateway_accounts_async()

    def _refresh_accounts_snapshot_if_stale(
        self,
        *,
        wait_for_refresh: bool = False,
    ) -> bool:
        now = time.monotonic()
        ttl = self._account_snapshot_ttl_seconds()
        with self._lock:
            if (
                now - self._account_snapshot_checked_at
                < ttl
            ):
                return False

        if not self._account_snapshot_refresh_lock.acquire(blocking=wait_for_refresh):
            return False
        try:
            with self._lock:
                if (
                    time.monotonic() - self._account_snapshot_checked_at
                    < ttl
                ):
                    return False
                expected_revision = self._accounts_revision

            # O(1) revision probe: a 7k-account reload costs seconds, so ask the
            # adapter whether anything changed before paying for it.
            revision_probe = getattr(self.storage, "load_accounts_revision", None)
            if callable(revision_probe):
                try:
                    external_revision = revision_probe()
                except Exception:
                    external_revision = None
                if external_revision is not None and external_revision == expected_revision:
                    with self._lock:
                        if self._accounts_revision == expected_revision:
                            self._account_snapshot_checked_at = time.monotonic()
                    return False

            try:
                loaded, revision, _ = self._read_accounts_snapshot()
            except Exception:
                with self._lock:
                    if self._accounts_revision == expected_revision:
                        self._account_snapshot_checked_at = time.monotonic()
                return False

            # Pay for the full-pool deep copy before taking the account lock.
            # It only reads `loaded` (a private dict returned by the adapter),
            # and doing it inside the lock held the pool for seconds on every
            # revision bump, which queued account updates and selections.
            prepared_persisted = deepcopy(loaded)

            with self._image_slot_condition:
                if self._accounts_revision != expected_revision:
                    return False
                self._account_snapshot_checked_at = time.monotonic()
                if revision == expected_revision:
                    return False
                self._apply_account_view_locked(
                    loaded,
                    persisted_accounts=loaded,
                    revision=revision,
                    prepared_persisted=prepared_persisted,
                )
                return True
        finally:
            self._account_snapshot_refresh_lock.release()

    @staticmethod
    def _account_mutation(
        baseline: dict[str, dict],
        desired: dict[str, dict],
        revision: str,
    ) -> StorageMutation:
        return StorageMutation(
            upserts=tuple(
                deepcopy(account)
                for token, account in desired.items()
                if token not in baseline or baseline[token] != account
            ),
            delete_keys=tuple(token for token in baseline if token not in desired),
            expected_revision=revision,
        )

    @staticmethod
    def _integer_field(value: object) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _merge_additive_account_value(
        cls,
        field: str,
        baseline: dict,
        local: dict,
        remote: dict,
    ) -> int | None:
        if field not in baseline or field not in local or field not in remote:
            return None
        baseline_value = cls._integer_field(baseline[field])
        local_value = cls._integer_field(local[field])
        remote_value = cls._integer_field(remote[field])
        if baseline_value is None or local_value is None or remote_value is None:
            return None
        return max(0, remote_value + (local_value - baseline_value))

    @classmethod
    def _is_local_quota_consumption(cls, baseline: dict, local: dict) -> bool:
        # quota is also refreshed as an absolute upstream snapshot. A changed
        # last_used_at distinguishes an image-result decrement from that refresh.
        baseline_quota = cls._integer_field(baseline.get("quota"))
        local_quota = cls._integer_field(local.get("quota"))
        return bool(
            baseline_quota is not None
            and local_quota is not None
            and local_quota < baseline_quota
            and local.get("last_used_at") != baseline.get("last_used_at")
        )

    @classmethod
    def _merge_account_fields(
        cls,
        baseline: dict,
        local: dict,
        remote: dict,
    ) -> dict:
        merged = deepcopy(remote)
        for field in baseline.keys() | local.keys():
            baseline_has = field in baseline
            local_has = field in local
            if baseline_has == local_has and (
                not local_has or baseline[field] == local[field]
            ):
                continue
            if local_has:
                if field in cls._ADDITIVE_ACCOUNT_COUNTER_FIELDS:
                    additive_value = cls._merge_additive_account_value(
                        field,
                        baseline,
                        local,
                        remote,
                    )
                    if additive_value is not None:
                        merged[field] = additive_value
                        continue
                if field == "quota" and cls._is_local_quota_consumption(
                    baseline,
                    local,
                ):
                    additive_value = cls._merge_additive_account_value(
                        field,
                        baseline,
                        local,
                        remote,
                    )
                    if additive_value is not None:
                        merged[field] = additive_value
                        continue
                merged[field] = deepcopy(local[field])
            else:
                merged.pop(field, None)
        return merged

    @staticmethod
    def _rotated_account_token(
        accounts: dict[str, dict],
        baseline: dict[str, dict],
        old_token: str,
        old_account: dict,
    ) -> str | None:
        management_id = str(old_account.get("management_id") or "").strip()
        if not management_id:
            return None
        return next(
            (
                token
                for token, account in accounts.items()
                if token != old_token
                and token not in baseline
                and str(account.get("management_id") or "").strip() == management_id
            ),
            None,
        )

    @classmethod
    def _merge_accounts_after_conflict(
        cls,
        baseline: dict[str, dict],
        local: dict[str, dict],
        remote: dict[str, dict],
    ) -> dict[str, dict]:
        merged = deepcopy(remote)
        handled_local_tokens: set[str] = set()

        for token, baseline_account in baseline.items():
            if token in local:
                continue
            local_rotation = cls._rotated_account_token(
                local,
                baseline,
                token,
                baseline_account,
            )
            remote_rotation = cls._rotated_account_token(
                remote,
                baseline,
                token,
                baseline_account,
            )
            if local_rotation is None:
                # A local deletion is an explicit command. It also deletes a
                # concurrently rotated form of the same logical account.
                merged.pop(token, None)
                if remote_rotation is not None:
                    merged.pop(remote_rotation, None)
                continue

            handled_local_tokens.add(local_rotation)
            if token not in remote:
                # A remote deletion beats a stale token refresh. When both
                # processes rotated the account, retain the remote winner.
                continue
            merged.pop(token, None)
            merged[local_rotation] = cls._merge_account_fields(
                baseline_account,
                local[local_rotation],
                remote[token],
            )

        for token, local_account in local.items():
            if token in handled_local_tokens:
                continue
            baseline_account = baseline.get(token)
            if baseline_account is None:
                remote_account = remote.get(token)
                merged[token] = (
                    {**deepcopy(remote_account), **deepcopy(local_account)}
                    if remote_account is not None
                    else deepcopy(local_account)
                )
                continue
            if local_account == baseline_account:
                continue
            remote_account = remote.get(token)
            if remote_account is None:
                remote_rotation = cls._rotated_account_token(
                    remote,
                    baseline,
                    token,
                    baseline_account,
                )
                if remote_rotation is not None:
                    merged[remote_rotation] = cls._merge_account_fields(
                        baseline_account,
                        local_account,
                        remote[remote_rotation],
                    )
                    merged.pop(token, None)
                    continue
                # A remote deletion beats a stale background update. Keeping it
                # deleted also removes it from this process's in-memory view.
                merged.pop(token, None)
                continue
            merged[token] = cls._merge_account_fields(
                baseline_account,
                local_account,
                remote_account,
            )
        return merged

    def _prune_token_aliases_locked(self) -> None:
        aliases = dict(getattr(self, "_token_aliases", {}))
        compacted: dict[str, str] = {}
        for source in aliases:
            token = source
            seen: set[str] = set()
            while token not in self._accounts and token in aliases and token not in seen:
                seen.add(token)
                token = aliases[token]
            if token in self._accounts and token != source:
                compacted[source] = token
        self._token_aliases = compacted

    def _remove_account_runtime_state_locked(self, tokens: set[str]) -> None:
        removed_tokens = {str(token or "").strip() for token in tokens if token}
        if not removed_tokens:
            return
        for token in removed_tokens:
            self._image_inflight.pop(token, None)
            self._selection_index.remove(token)
        self._token_aliases = {
            source: target
            for source, target in self._token_aliases.items()
            if source not in removed_tokens and target not in removed_tokens
        }
        self._index = self._index % len(self._accounts) if self._accounts else 0

    def _restore_accounts_after_save_error(self) -> None:
        fallback = deepcopy(self._persisted_accounts)
        try:
            loaded, revision, _ = self._read_accounts_snapshot()
        except Exception:
            self._apply_account_view_locked(
                fallback,
                persisted_accounts=fallback,
                revision=self._accounts_revision,
                snapshot_checked=False,
                preserve_index=True,
            )
        else:
            self._apply_account_view_locked(
                loaded,
                persisted_accounts=loaded,
                revision=revision,
                preserve_index=True,
            )

    def _save_accounts(
        self,
        *,
        expected_credential_generation: _CredentialGeneration | None = None,
        conflict_existing_tokens: set[str] | None = None,
    ) -> bool:
        last_conflict: StorageRevisionConflictError | None = None
        for attempt in range(self._STORAGE_MUTATION_MAX_ATTEMPTS):
            mutation = self._account_mutation(
                self._persisted_accounts,
                self._accounts,
                self._accounts_revision,
            )
            if not mutation.upserts and not mutation.delete_keys:
                self._prune_token_aliases_locked()
                return True
            try:
                result = self.storage.mutate_accounts(mutation)
            except StorageRevisionConflictError as exc:
                last_conflict = exc
                if attempt + 1 >= self._STORAGE_MUTATION_MAX_ATTEMPTS:
                    self._restore_accounts_after_save_error()
                    raise
                try:
                    snapshot = self.storage.load_accounts_snapshot()
                    remote, _ = self._normalize_loaded_accounts(
                        snapshot.items,
                        recover_interrupted_checks=False,
                    )
                    if conflict_existing_tokens is not None:
                        conflict_existing_tokens.update(
                            token
                            for token in self._accounts
                            if token not in self._persisted_accounts and token in remote
                        )
                    if expected_credential_generation is not None:
                        expected_access_token = expected_credential_generation[0]
                        remote_account = remote.get(expected_access_token)
                        if (
                            remote_account is None
                            or self._credential_generation(
                                expected_access_token,
                                remote_account,
                            )
                            != expected_credential_generation
                        ):
                            self._apply_account_view_locked(
                                remote,
                                persisted_accounts=remote,
                                revision=snapshot.revision,
                            )
                            return False
                    merged = self._merge_accounts_after_conflict(
                        self._persisted_accounts,
                        self._accounts,
                        remote,
                    )
                    self._apply_account_view_locked(
                        merged,
                        persisted_accounts=remote,
                        revision=snapshot.revision,
                    )
                except Exception:
                    self._restore_accounts_after_save_error()
                    raise
                continue
            except Exception:
                self._restore_accounts_after_save_error()
                raise
            # Only the rows that were actually written need their persisted
            # mirror refreshed.  Deep-copying the whole pool here made every
            # single-account save O(pool) — with a background watcher that ran
            # inside the account lock it stalled list/preview for seconds.
            for item in mutation.upserts:
                token = str(item.get("access_token") or "").strip()
                if token:
                    self._persisted_accounts[token] = deepcopy(item)
            for key in mutation.delete_keys:
                self._persisted_accounts.pop(str(key or "").strip(), None)
            self._accounts_revision = result.revision
            self._account_snapshot_checked_at = time.monotonic()
            self._prune_token_aliases_locked()
            return True
        assert last_conflict is not None
        raise last_conflict

    def _save_account_row_locked(self, access_token: str) -> bool:
        """Persist one account row without diffing the whole pool.

        ``mark_text_used`` runs on every chat request; going through the full
        ``_save_accounts`` diff means comparing every account in the pool.  A
        single-row mutation keeps that path O(1) for large pools.
        """
        for attempt in range(self._STORAGE_MUTATION_MAX_ATTEMPTS):
            account = self._accounts.get(access_token)
            if account is None:
                return False
            mutation = StorageMutation(
                upserts=(deepcopy(account),),
                delete_keys=(),
                expected_revision=self._accounts_revision,
            )
            try:
                result = self.storage.mutate_accounts(mutation)
            except StorageRevisionConflictError:
                if attempt + 1 >= self._STORAGE_MUTATION_MAX_ATTEMPTS:
                    self._restore_accounts_after_save_error()
                    return False
                try:
                    loaded, revision, _ = self._read_accounts_snapshot()
                except Exception:
                    self._restore_accounts_after_save_error()
                    return False
                self._apply_account_view_locked(
                    loaded,
                    persisted_accounts=loaded,
                    revision=revision,
                )
                continue
            except Exception:
                self._restore_accounts_after_save_error()
                raise
            self._persisted_accounts[access_token] = deepcopy(account)
            self._accounts_revision = result.revision
            self._account_snapshot_checked_at = time.monotonic()
            return True
        return False

    @staticmethod
    def _is_account_selectable(
        account: dict,
        *,
        allow_limited: bool,
        allow_image_pending: bool = False,
    ) -> bool:
        if not isinstance(account, dict):
            return False
        allowed_statuses = {"正常", "限流"} if allow_limited else {"正常"}
        if account.get("status") not in allowed_statuses:
            return False
        credential_availability = project_upstream_credential_availability(
            str(account.get("access_token") or ""),
            str(account.get("refresh_token") or ""),
            access_confirmed_invalid=(
                str(account.get("last_remote_check_result") or "").strip().lower() == "invalid"
            ),
            refresh_confirmed_invalid=bool(account.get("refresh_token_invalid_at")),
        )
        if credential_availability.status == "unavailable":
            return False
        if account.get("last_remote_check_result") != "pending":
            return True
        return bool(
            allow_image_pending
            and account.get("pending_auth_scope") == "image"
        )

    @classmethod
    def _is_image_account_available(cls, account: dict) -> bool:
        if not cls._is_account_selectable(account, allow_limited=False):
            return False
        # An account that just produced a >60s image stream is temporarily
        # deprioritized so the next request is less likely to sit in the same
        # slow upstream stream.
        try:
            slow_until = float(account.get("image_slow_until_ts") or 0)
        except (TypeError, ValueError):
            slow_until = 0.0
        if slow_until > time.time():
            return False
        # An account that just returned no image is temporarily skipped so the
        # next request does not immediately land on the same failing account.
        try:
            fail_until = float(account.get("image_fail_until_ts") or 0)
        except (TypeError, ValueError):
            fail_until = 0.0
        if fail_until > time.time():
            return False
        if bool(account.get("image_quota_unknown")):
            return True
        # quota 是展示/预估值，不能作为持久调度开关。
        if bool(account.get("image_quota_unknown")):
            return account.get("status") == "正常"
        # quota is a local estimate, but a known zero means the account already
        # spent today's image quota. Treating it as selectable made a single
        # image request serially re-validate up to 20 exhausted accounts
        # (observed 9 minutes). Only remote-confirmed 限流 was previously
        # excluded; require a positive known quota instead.
        return account.get("status") == "正常" and int(account.get("quota") or 0) > 0

    @classmethod
    def _is_unlimited_image_quota_account(cls, account: dict) -> bool:
        if not isinstance(account, dict) or not bool(account.get("image_quota_unknown")):
            return False
        account_type = (cls._normalize_account_type(account.get("type")) or "").lower()
        return account_type in {"pro", "prolite"}

    @classmethod
    def _remote_check_fresh(cls, account: dict, *, max_age_seconds: float = 120.0) -> bool:
        """True when a very recent successful /me check still proves availability.

        Re-validating every candidate over the network turned single image
        requests into 20 serial round-trips (~60s) while a batch refresh was
        running. A 20s freshness window keeps selection instant without
        weakening terminal state: only ``ok`` results are reused, and any
        upstream 401/429 still flips the account through the normal path.
        """
        if not isinstance(account, dict):
            return False
        if str(account.get("last_remote_check_result") or "").strip().lower() != "ok":
            return False
        if not cls._is_image_account_available(account):
            return False
        stamp = str(account.get("last_remote_checked_at") or "").strip()
        if not stamp:
            return False
        try:
            checked = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            return False
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - checked).total_seconds()
        return 0 <= age <= max(0.0, float(max_age_seconds))

    @classmethod
    def _account_matches_plan_type(cls, account: dict, plan_type: str | None = None) -> bool:
        if not plan_type:
            return True
        normalized_plan = cls._normalize_account_type(plan_type)
        normalized_account = cls._normalize_account_type(account.get("type"))
        if not normalized_plan or not normalized_account:
            return False
        return normalized_plan.lower() == normalized_account.lower()

    @classmethod
    def _account_matches_source_type(cls, account: dict, source_type: str | None = None) -> bool:
        if not source_type:
            return True
        return cls._normalize_source_type(account.get("source_type")) == cls._normalize_source_type(source_type)

    @classmethod
    def _account_matches_any_plan_type(cls, account: dict, plan_types: set[str] | tuple[str, ...] | None = None) -> bool:
        if not plan_types:
            return True
        normalized_account = cls._normalize_account_type(account.get("type"))
        normalized_plans = {
            normalized
            for plan_type in plan_types
            if (normalized := cls._normalize_account_type(plan_type))
        }
        return bool(normalized_account and normalized_account in normalized_plans)

    @staticmethod
    def _normalize_source_type(value: object) -> str:
        raw = str(value or "").strip().lower()
        if raw in {"codex", "cpa", "cpa_json", "remote_cpa", "sub2api"}:
            return "codex"
        return "web"

    @staticmethod
    def _normalize_account_type(value: object) -> str | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        key = raw.lower().replace("-", "_").replace(" ", "_")
        compact = key.replace("_", "")
        aliases = {
            "free": "free",
            "plus": "Plus",
            "pro": "Pro",
            "prolite": "ProLite",
            "team": "Team",
            "business": "Team",
            "enterprise": "Enterprise",
        }
        return aliases.get(compact) or aliases.get(key)

    @staticmethod
    def _has_value(value: object) -> bool:
        return value is not None and str(value).strip() != ""

    @staticmethod
    def _bool_value(value: object, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        raw = str(value or "").strip().lower()
        if raw in {"1", "true", "yes", "y", "on"}:
            return True
        if raw in {"0", "false", "no", "n", "off", "none", "null", ""}:
            return False
        return default

    @classmethod
    def _normalize_account_status(cls, value: object, account: dict) -> str:
        if cls._bool_value(account.get("auto_disabled"), False):
            return "禁用"
        if account.get("enabled") is not None and not cls._bool_value(account.get("enabled"), True):
            return "禁用"
        raw = str(value or "").strip()
        if not raw:
            return "正常"
        aliases = {
            "正常": "正常",
            "normal": "正常",
            "ready": "正常",
            "限流": "限流",
            "limited": "限流",
            "rate_limited": "限流",
            "cooling": "限流",
            "backoff": "限流",
            "异常": "异常",
            "abnormal": "异常",
            "invalid": "异常",
            "error": "异常",
            "incomplete": "异常",
            "禁用": "禁用",
            "disabled": "禁用",
            "auto_disabled": "禁用",
        }
        return aliases.get(raw.lower(), aliases.get(raw, "正常"))

    @classmethod
    def _quota_value(cls, value: object, default: int = 0) -> int:
        if not cls._has_value(value):
            return max(0, int(default or 0))
        try:
            return max(0, int(float(str(value).strip())))
        except (TypeError, ValueError):
            return max(0, int(default or 0))

    @classmethod
    def _extract_image_quota_from_limits(cls, limits_progress: object) -> tuple[int | None, str | None, bool | None]:
        if not isinstance(limits_progress, list):
            return None, None, None
        if not limits_progress:
            return None, None, None

        for item in limits_progress:
            if not isinstance(item, dict):
                continue
            feature = str(
                item.get("feature_name")
                or item.get("feature")
                or item.get("name")
                or item.get("type")
                or ""
            ).strip().lower()
            if feature in {"image_gen", "image_generation", "image", "images"}:
                restore_at = str(
                    item.get("reset_after")
                    or item.get("restore_at")
                    or item.get("reset_at")
                    or ""
                ).strip() or None
                return cls._quota_value(item.get("remaining"), 0), restore_at, False

        return None, None, True

    def _normalize_account(self, item: dict) -> dict | None:
        if not isinstance(item, dict):
            return None
        access_token = item.get("access_token") or item.get("accessToken") or ""
        if not access_token:
            return None
        normalized = dict(item)
        normalized.pop("accessToken", None)
        normalized["access_token"] = access_token
        normalized["management_id"] = self._normalize_management_id(
            normalized.get("management_id"),
            access_token,
        )
        normalized["access_token_fingerprints"] = self._normalize_access_token_fingerprints(
            normalized.get("access_token_fingerprints"),
            access_token,
        )
        if str(normalized.get("type") or "").strip().lower() == "codex":
            normalized["export_type"] = "codex"
            normalized.pop("type", None)
        limits_progress = normalized.get("limits_progress")
        limits_progress = limits_progress if isinstance(limits_progress, list) else []
        derived_quota, derived_restore_at, derived_unknown = self._extract_image_quota_from_limits(limits_progress)
        has_explicit_quota = self._has_value(normalized.get("quota"))
        normalized["type"] = self._normalize_account_type(normalized.get("type"))
        normalized["status"] = self._normalize_account_status(normalized.get("status"), normalized)
        normalized.pop("enabled", None)
        normalized.pop("auto_disabled", None)
        normalized["email"] = normalized.get("email") or None
        normalized["user_id"] = normalized.get("user_id") or None
        normalized["proxy"] = str(normalized.get("proxy") or "").strip()
        source_type = normalized.get("source_type")
        if not source_type and str(normalized.get("export_type") or "").strip().lower() == "codex":
            source_type = "codex"
        normalized["source_type"] = self._normalize_source_type(source_type)
        if not has_explicit_quota and derived_quota is not None:
            normalized["quota"] = derived_quota
        normalized["quota"] = self._quota_value(normalized.get("quota"), 0)
        has_explicit_quota_state = self._has_value(normalized.get("image_quota_unknown"))
        if derived_unknown is not None and not has_explicit_quota_state:
            normalized["image_quota_unknown"] = derived_unknown
        elif not has_explicit_quota_state:
            normalized["image_quota_unknown"] = True
        normalized["image_quota_unknown"] = self._bool_value(normalized.get("image_quota_unknown"), True)
        has_confirmed_quota = (
            derived_unknown is False
            or (
                bool(normalized.get("last_remote_checked_at"))
                and not normalized["image_quota_unknown"]
            )
        )
        if normalized["status"] == "正常" and (not has_confirmed_quota or normalized["quota"] == 0):
            normalized["image_quota_unknown"] = True
        elif normalized["status"] == "限流":
            normalized["quota"] = 0
            normalized["image_quota_unknown"] = False
        normalized["limits_progress"] = limits_progress
        normalized["default_model_slug"] = normalized.get("default_model_slug") or None
        if derived_restore_at and not normalized.get("restore_at"):
            normalized["restore_at"] = derived_restore_at
        normalized["restore_at"] = normalized.get("restore_at") or None
        normalized["success"] = int(normalized.get("success") or 0)
        normalized["fail"] = int(normalized.get("fail") or 0)
        normalized["invalid_count"] = int(normalized.get("invalid_count") or 0)
        normalized["last_used_at"] = normalized.get("last_used_at")
        normalized["last_invalid_at"] = normalized.get("last_invalid_at") or None
        normalized["last_refresh_error"] = normalized.get("last_refresh_error") or None
        normalized["last_refresh_error_at"] = normalized.get("last_refresh_error_at") or None
        normalized["last_remote_checked_at"] = normalized.get("last_remote_checked_at") or None
        normalized["last_remote_check_attempt_at"] = normalized.get("last_remote_check_attempt_at") or None
        normalized["last_remote_check_error"] = normalized.get("last_remote_check_error") or None
        normalized["last_remote_check_error_at"] = normalized.get("last_remote_check_error_at") or None
        normalized["last_remote_check_event"] = normalized.get("last_remote_check_event") or None
        remote_check_result = str(normalized.get("last_remote_check_result") or "").strip().lower()
        normalized["last_remote_check_result"] = (
            remote_check_result
            if remote_check_result in {"pending", "ok", "error", "invalid"}
            else None
        )
        pending_remove = normalized.get("pending_auth_remove_invalid")
        normalized["pending_auth_remove_invalid"] = (
            self._bool_value(pending_remove)
            if normalized["last_remote_check_result"] == "pending" and pending_remove is not None
            else None
        )
        pending_scope = str(normalized.get("pending_auth_scope") or "").strip().lower()
        normalized["pending_auth_scope"] = (
            "image" if pending_scope == "image" else "account"
        ) if normalized["last_remote_check_result"] == "pending" else None
        verification_id = str(normalized.get("pending_auth_verification_id") or "").strip()
        if normalized["last_remote_check_result"] == "pending" and verification_id:
            normalized["pending_auth_verification_id"] = verification_id
        else:
            normalized.pop("pending_auth_verification_id", None)

        normalized["last_token_refresh_at"] = normalized.get("last_token_refresh_at") or None
        normalized["last_token_refresh_error"] = normalized.get("last_token_refresh_error") or None
        normalized["last_token_refresh_error_at"] = normalized.get("last_token_refresh_error_at") or None
        normalized["refresh_token_invalid_at"] = normalized.get("refresh_token_invalid_at") or None
        diagnostic_secrets = (
            normalized.get("access_token"),
            normalized.get("refresh_token"),
            normalized.get("id_token"),
        )
        diagnostic_proxies = (normalized.get("proxy"),)
        for key in (
            "last_remote_check_error",
            "last_refresh_error",
            "last_token_refresh_error",
        ):
            if normalized.get(key):
                normalized[key] = sanitize_diagnostic_text(
                    normalized[key],
                    sensitive_values=diagnostic_secrets,
                    proxy_values=diagnostic_proxies,
                )
        for key in (
            "capability_cooldowns",
            "capability_failure_counts",
            "capability_failure_codes",
            "capability_failed_at",
        ):
            normalized.pop(key, None)
        normalized["created_at"] = normalized.get("created_at") or AccountService._now()
        return normalized

    @staticmethod
    def _jwt_exp(access_token: str) -> int:
        try:
            return int(AccountService._decode_jwt_payload(access_token).get("exp") or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _token_expires_in(cls, access_token: str) -> int | None:
        return access_token_expires_in_seconds(access_token)

    @classmethod
    def _token_needs_refresh(cls, access_token: str, *, force: bool = False) -> bool:
        if force:
            return True
        remaining = cls._token_expires_in(access_token)
        return remaining is not None and remaining <= cls._ACCESS_TOKEN_REFRESH_SKEW_SECONDS

    @classmethod
    def _token_issued_at(cls, access_token: str) -> datetime | None:
        issued_at = access_token_issued_at(access_token)
        if issued_at is None:
            return None
        return datetime.fromtimestamp(issued_at, tz=timezone.utc)

    @staticmethod
    def _safe_response_text(response: object, limit: int = 300) -> str:
        try:
            return str(getattr(response, "text", "") or "")[:limit]
        except Exception:
            return ""

    @staticmethod
    def _oauth_refresh_error_fields(data: object) -> tuple[str, str]:
        if not isinstance(data, dict):
            return "", ""
        error = data.get("error")
        nested = error if isinstance(error, dict) else {}
        code = str(
            nested.get("code")
            or data.get("code")
            or (error if isinstance(error, str) else "")
            or ""
        ).strip()
        description = str(
            data.get("error_description")
            or nested.get("message")
            or data.get("message")
            or nested.get("description")
            or ""
        ).strip()
        return code, description

    @classmethod
    def _is_terminal_refresh_error(cls, status_code: int, error_code: str, description: str) -> bool:
        if status_code in {408, 429} or status_code >= 500:
            return False
        normalized_code = str(error_code or "").strip().casefold()
        normalized_description = str(description or "").strip().casefold()
        if normalized_code in cls._TERMINAL_REFRESH_ERROR_CODES:
            return True
        return 400 <= status_code < 500 and any(
            fragment in normalized_description
            for fragment in cls._TERMINAL_REFRESH_MESSAGE_FRAGMENTS
        )

    def _resolve_access_token_locked(self, access_token: str) -> str:
        token = str(access_token or "").strip()
        seen: set[str] = set()
        while token and token not in self._accounts and token in self._token_aliases and token not in seen:
            seen.add(token)
            token = self._token_aliases.get(token, token)
        return token

    def resolve_access_token(self, access_token: str) -> str:
        if not access_token:
            return ""
        with self._lock:
            return self._resolve_access_token_locked(access_token)

    def _get_account_for_token(self, access_token: str) -> tuple[str, dict | None]:
        with self._lock:
            resolved = self._resolve_access_token_locked(access_token)
            account = self._accounts.get(resolved)
            return resolved, dict(account) if account else None

    def _credential_snapshot(self, access_token: str) -> tuple[str, str, dict | None]:
        resolved, account = self._get_account_for_token(access_token)
        if not account:
            return resolved, "", None
        active_token = str(account.get("access_token") or resolved or access_token).strip()
        refresh_token = str(account.get("refresh_token") or "").strip()
        return active_token, refresh_token, account

    @staticmethod
    def _credential_generation(
        access_token: str,
        account: dict | None,
    ) -> _CredentialGeneration:
        item = account or {}
        return (
            str(item.get("access_token") or access_token or "").strip(),
            str(item.get("refresh_token") or "").strip(),
            str(item.get("last_token_refresh_at") or "").strip(),
        )

    def _record_token_refresh_error(
        self,
        access_token: str,
        event: str,
        error: str,
        *,
        expected_access_token: str | None = None,
        expected_refresh_token: str | None = None,
        expected_last_token_refresh_at: str | None = None,
        terminal: bool = False,
    ) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            resolved = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(resolved)
            if current is None:
                return False
            expected_generation = None
            if expected_access_token is not None and expected_refresh_token is not None:
                expected_generation = (
                    str(expected_access_token or "").strip(),
                    str(expected_refresh_token or "").strip(),
                    str(expected_last_token_refresh_at or "").strip(),
                )
                if self._credential_generation(resolved, current) != expected_generation:
                    return False
            next_item = dict(current)
            next_item["last_token_refresh_error"] = str(error or "refresh token failed")
            next_item["last_token_refresh_error_at"] = now
            if terminal:
                next_item["refresh_token_invalid_at"] = now
            account = self._normalize_account(next_item)
            if account is None:
                return False
            self._accounts[resolved] = account
            saved = self._save_accounts(
                expected_credential_generation=expected_generation,
            )
            if not saved:
                return False
            resolved = self._resolve_access_token_locked(resolved)
            persisted = self._accounts.get(resolved)
            if (
                persisted is None
                or persisted.get("last_token_refresh_error_at") != now
            ):
                return False
        log_service.add(
            LOG_TYPE_ACCOUNT,
            "refresh_token 刷新 access_token 失败",
            {
                "source": event,
                "token": anonymize_token(resolved),
                "error": str(persisted.get("last_token_refresh_error") or ""),
            },
        )
        return True

    @staticmethod
    def _credential_error_text(
        error: object,
        account: dict | None,
        *,
        access_token: str = "",
    ) -> str:
        account = account or {}
        return sanitize_diagnostic_text(
            error,
            sensitive_values=(
                access_token,
                account.get("access_token"),
                account.get("refresh_token"),
                account.get("id_token"),
            ),
            proxy_values=(account.get("proxy"),),
            limit=2000,
        )

    def _recent_token_refresh_error(self, account: dict) -> bool:
        last_error_at = self._parse_time(account.get("last_token_refresh_error_at"))
        if last_error_at is None:
            return False
        return (datetime.now(timezone.utc) - last_error_at).total_seconds() < self._TOKEN_REFRESH_ERROR_BACKOFF_SECONDS

    def _request_access_token_refresh(
        self,
        refresh_token: str,
        account: dict | None = None,
        *,
        image_scope: bool = False,
    ) -> dict[str, str]:
        from curl_cffi import requests
        from services.proxy_service import proxy_settings

        session = requests.Session(**proxy_settings.build_session_kwargs(account=account, impersonate="chrome110", verify=True))
        try:
            with account_processing_slot():
                response = session.post(
                    self._OAUTH_TOKEN_URL,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": self._OAUTH_USER_AGENT,
                    },
                    data={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": self._OAUTH_CLIENT_ID,
                    },
                    timeout=60,
                )
            raw_text = self._safe_response_text(response)
            try:
                data = response.json() if raw_text else {}
            except Exception:
                data = {}
            status_code = int(getattr(response, "status_code", 0) or 0)
            if status_code != 200 or not isinstance(data, dict) or not data.get("access_token"):
                error_code, description = self._oauth_refresh_error_fields(data)
                description = description or raw_text
                error_type = (
                    TerminalRefreshTokenError
                    if self._is_terminal_refresh_error(status_code, error_code, description)
                    else OAuthRefreshError
                )
                raise error_type(status_code, error_code, description)
            return {
                "access_token": str(data.get("access_token") or "").strip(),
                "refresh_token": str(data.get("refresh_token") or refresh_token).strip(),
                "id_token": str(data.get("id_token") or "").strip(),
            }
        finally:
            session.close()

    def _move_account_runtime_token_locked(
        self,
        new_token: str,
        alias_sources: set[str],
    ) -> None:
        for source in alias_sources:
            if source != new_token:
                self._token_aliases[source] = new_token

        old_inflight = sum(
            int(self._image_inflight.pop(source, 0))
            for source in alias_sources
            if source != new_token
        )
        if old_inflight:
            self._image_inflight[new_token] = (
                int(self._image_inflight.get(new_token, 0)) + old_inflight
            )
        for source in alias_sources:
            if source != new_token:
                self._selection_index.rekey(source, new_token)

        with self._image_failure_refresh_lock:
            old_scopes = [
                self._image_failure_refresh_pending_scopes.pop(source)
                for source in alias_sources
                if source in self._image_failure_refresh_pending_scopes
            ]
            pending = (
                new_token if token in alias_sources else token
                for token in self._image_failure_refresh_pending
            )
            self._image_failure_refresh_pending = deque(dict.fromkeys(pending))
            self._image_failure_refresh_pending_set = set(
                self._image_failure_refresh_pending
            )
            if old_scopes:
                current_scope = self._image_failure_refresh_pending_scopes.get(new_token)
                self._image_failure_refresh_pending_scopes[new_token] = (
                    "account"
                    if "account" in {*old_scopes, current_scope}
                    else "image"
                )
            old_started_at = max(
                (
                    self._image_failure_refresh_started_at.pop(source)
                    for source in alias_sources
                    if source in self._image_failure_refresh_started_at
                ),
                default=None,
            )
            if old_started_at is not None:
                self._image_failure_refresh_started_at[new_token] = max(
                    old_started_at,
                    self._image_failure_refresh_started_at.get(new_token, 0.0),
                )
            # Active workers keep their original key until their finally block.

    def _passive_token_rotations_locked(
        self,
        loaded: dict[str, dict],
    ) -> list[tuple[str, set[str]]]:
        loaded_by_management_id = {
            str(account.get("management_id") or "").strip(): token
            for token, account in loaded.items()
            if str(account.get("management_id") or "").strip()
        }
        rotations: list[tuple[str, set[str]]] = []
        for old_token, old_account in self._accounts.items():
            if old_token in loaded:
                continue
            management_id = str(old_account.get("management_id") or "").strip()
            new_token = loaded_by_management_id.get(management_id)
            if not new_token or new_token == old_token:
                continue
            alias_sources = {
                source
                for source in {old_token, *self._token_aliases}
                if self._resolve_access_token_locked(source) == old_token
            }
            rotations.append((new_token, alias_sources))
        return rotations

    def _apply_refreshed_tokens(
        self,
        old_access_token: str,
        token_data: dict,
        event: str,
        *,
        expected_access_token: str | None = None,
        expected_refresh_token: str,
        expected_last_token_refresh_at: str | None = None,
    ) -> str:
        now = datetime.now(timezone.utc).isoformat()
        expected_access_token = expected_access_token or old_access_token
        expected_generation = (
            str(expected_access_token or "").strip(),
            str(expected_refresh_token or "").strip(),
            str(expected_last_token_refresh_at or "").strip(),
        )
        with self._image_slot_condition:
            old_token = self._resolve_access_token_locked(old_access_token)
            current = self._accounts.get(old_token)
            if current is None:
                raise RefreshCredentialsChangedError()
            if self._credential_generation(old_token, current) != expected_generation:
                raise RefreshCredentialsChangedError()
            new_token = str(token_data.get("access_token") or old_token).strip()
            if not new_token:
                return old_token
            if new_token != old_token and new_token in self._accounts:
                raise RefreshCredentialsChangedError()

            next_item = dict(current)
            next_item["access_token"] = new_token
            if token_data.get("refresh_token"):
                next_item["refresh_token"] = str(token_data.get("refresh_token") or "").strip()
            if token_data.get("id_token"):
                next_item["id_token"] = str(token_data.get("id_token") or "").strip()
            self._scrub_diagnostic_secrets(
                next_item,
                [
                    current.get("access_token"),
                    current.get("refresh_token"),
                    current.get("id_token"),
                ],
            )
            next_item["last_token_refresh_at"] = now
            next_item["last_token_refresh_error"] = None
            next_item["last_token_refresh_error_at"] = None
            next_item["refresh_token_invalid_at"] = None
            next_item["invalid_count"] = 0
            next_item["last_invalid_at"] = None
            next_item["last_refresh_error"] = None
            next_item["last_refresh_error_at"] = None
            if current.get("status") == "\u5f02\u5e38":
                next_item["status"] = "\u6b63\u5e38"
            if current.get("last_remote_check_result") == "invalid":
                next_item["last_remote_check_result"] = None
                next_item["last_remote_check_error"] = None
                next_item["last_remote_check_error_at"] = None
                next_item["last_remote_check_event"] = None
                next_item["pending_auth_remove_invalid"] = None
                next_item["pending_auth_scope"] = None

            account = self._normalize_account(next_item)
            if account is None:
                return old_token

            rotated = new_token != old_token
            alias_sources = {
                source
                for source in {old_token, *self._token_aliases}
                if self._resolve_access_token_locked(source) == old_token
            }
            if rotated:
                self._accounts.pop(old_token, None)
            self._accounts[new_token] = account
            saved = self._save_accounts(
                expected_credential_generation=expected_generation,
            )
            final_token = (
                new_token
                if new_token in self._accounts
                else next(
                    (
                        token
                        for token, item in self._accounts.items()
                        if item.get("management_id") == current.get("management_id")
                    ),
                    None,
                )
            )
            if not saved or final_token != new_token:
                if final_token:
                    self._move_account_runtime_token_locked(
                        final_token,
                        alias_sources,
                    )
                else:
                    self._remove_account_runtime_state_locked(alias_sources)
                self._image_slot_condition.notify_all()
                raise RefreshCredentialsChangedError()
            if rotated:
                self._move_account_runtime_token_locked(
                    new_token,
                    alias_sources,
                )
            self._image_slot_condition.notify_all()

        log_service.add(
            LOG_TYPE_ACCOUNT,
            "refresh_token 已刷新 access_token",
            {"source": event, "token": anonymize_token(new_token), "rotated": rotated},
        )
        return new_token

    def _refresh_access_token_owner(
        self,
        active_token: str,
        refresh_token: str,
        account: dict,
        *,
        event: str,
        image_scope: bool,
    ) -> str:
        try:
            if image_scope:
                token_data = self._request_access_token_refresh(
                    refresh_token,
                    account,
                    image_scope=True,
                )
            else:
                token_data = self._request_access_token_refresh(refresh_token, account)
        except TerminalRefreshTokenError as exc:
            exc.expected_access_token = active_token
            exc.expected_refresh_token = refresh_token
            exc.expected_last_token_refresh_at = str(
                account.get("last_token_refresh_at") or ""
            )
            current_token, current_refresh_token, current = self._credential_snapshot(active_token)
            if not current or (current_token, current_refresh_token) != (active_token, refresh_token):
                raise RefreshCredentialsChangedError() from exc
            raise
        except Exception as exc:
            recorded = self._record_token_refresh_error(
                active_token,
                event,
                str(exc or ""),
                expected_access_token=active_token,
                expected_refresh_token=refresh_token,
                expected_last_token_refresh_at=str(
                    account.get("last_token_refresh_at") or ""
                ),
            )
            if recorded:
                raise
            raise RefreshCredentialsChangedError() from exc

        return self._apply_refreshed_tokens(
            active_token,
            token_data,
            event,
            expected_access_token=active_token,
            expected_refresh_token=refresh_token,
            expected_last_token_refresh_at=str(
                account.get("last_token_refresh_at") or ""
            ),
        )

    def _maintain_access_token(
        self,
        access_token: str,
        *,
        force: bool = False,
        event: str = "refresh_access_token",
        raise_on_error: bool = False,
        image_scope: bool = False,
        expected_credentials: _CredentialGeneration | None = None,
        skip_if_image_busy: bool = False,
    ) -> str:
        if not access_token:
            return ""
        for credential_attempt in range(2):
            resolved_token, account = self._get_account_for_token(access_token)
            if not account:
                raise RefreshCredentialsChangedError()
            active_token = str(account.get("access_token") or resolved_token or access_token)
            needs_refresh = self._token_needs_refresh(active_token, force=force)
            refresh_backoff = not force and self._recent_token_refresh_error(account)
            refresh_token = str(account.get("refresh_token") or "").strip()
            current_generation = self._credential_generation(active_token, account)
            if (
                expected_credentials is not None
                and current_generation != expected_credentials
            ):
                raise RefreshCredentialsChangedError()
            if not refresh_token:
                return active_token
            if account.get("refresh_token_invalid_at") and not force:
                remaining = self._token_expires_in(active_token)
                if remaining is None or remaining > 0:
                    return active_token
                raise TerminalRefreshTokenError(
                    400,
                    "invalid_refresh_token",
                    "refresh token is marked invalid",
                )

            key = current_generation
            if skip_if_image_busy:
                with self._image_slot_condition:
                    current_token = self._resolve_access_token_locked(active_token)
                    current = self._accounts.get(current_token)
                    current_generation = self._credential_generation(current_token, current)
                    if not current or current_generation != key or (
                        expected_credentials is not None
                        and current_generation != expected_credentials
                    ):
                        if expected_credentials is not None or credential_attempt > 0:
                            raise RefreshCredentialsChangedError()
                        continue
                    image_busy = int(self._image_inflight.get(current_token, 0)) > 1
                    with self._oauth_refresh_flights_lock:
                        future = self._oauth_refresh_flights.get(key)
                        owner = future is None
                        if future is None:
                            if image_busy or not needs_refresh or refresh_backoff:
                                return active_token
                            future = Future()
                            self._oauth_refresh_flights[key] = future
            else:
                with self._oauth_refresh_flights_lock:
                    future = self._oauth_refresh_flights.get(key)
                    owner = future is None
                    if future is None:
                        if not needs_refresh or refresh_backoff:
                            return active_token
                        future = Future()
                        self._oauth_refresh_flights[key] = future
            if owner:
                try:
                    result = self._refresh_access_token_owner(
                        active_token,
                        refresh_token,
                        account,
                        event=event,
                        image_scope=image_scope,
                    )
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            try:
                return future.result()
            except TerminalRefreshTokenError as exc:
                expected_access_token = str(
                    getattr(exc, "expected_access_token", active_token) or active_token
                )
                expected_refresh_token = str(
                    getattr(exc, "expected_refresh_token", refresh_token) or refresh_token
                )
                expected_last_token_refresh_at = str(
                    getattr(exc, "expected_last_token_refresh_at", "") or ""
                )
                current_token, current_refresh_token, current = self._credential_snapshot(active_token)
                if current and (current_token, current_refresh_token) != (
                    expected_access_token,
                    expected_refresh_token,
                ):
                    if expected_credentials is not None:
                        raise RefreshCredentialsChangedError() from exc
                    if credential_attempt == 0:
                        access_token = current_token
                        continue
                    raise RefreshCredentialsChangedError() from exc
                error_str = str(exc)
                recorded = self._record_token_refresh_error(
                    active_token,
                    event,
                    error_str,
                    expected_access_token=expected_access_token,
                    expected_refresh_token=expected_refresh_token,
                    expected_last_token_refresh_at=expected_last_token_refresh_at,
                    terminal=True,
                )
                if not recorded:
                    current_token, _current_refresh, current = self._credential_snapshot(
                        active_token
                    )
                    if current is not None:
                        if (
                            expected_credentials is not None
                            and self._credential_generation(current_token, current)
                            != expected_credentials
                        ):
                            raise RefreshCredentialsChangedError() from exc
                        return current_token
                    raise RefreshCredentialsChangedError() from exc
                raise
            except RefreshCredentialsChangedError:
                current_token, current = self._get_account_for_token(active_token)
                if current is None:
                    raise
                latest_generation = self._credential_generation(current_token, current)
                if expected_credentials is not None or latest_generation == current_generation:
                    raise
                # A concurrent successful exchange already produced a usable AT.
                if latest_generation[2] != current_generation[2]:
                    return str(current.get("access_token") or current_token or active_token)
                if credential_attempt == 0:
                    access_token = current_token
                    continue
                raise
            except Exception:
                if raise_on_error:
                    raise
                current_token, current = self._get_account_for_token(active_token)
                if current:
                    return str(current.get("access_token") or current_token or active_token)
                raise RefreshCredentialsChangedError()
            finally:
                if owner:
                    with self._oauth_refresh_flights_lock:
                        if self._oauth_refresh_flights.get(key) is future:
                            self._oauth_refresh_flights.pop(key, None)
        raise RefreshCredentialsChangedError()

    def ensure_access_token(
        self,
        access_token: str,
        *,
        event: str = "ensure_access_token",
        raise_on_error: bool = False,
        image_scope: bool = False,
        expected_credentials: _CredentialGeneration | None = None,
        skip_if_image_busy: bool = False,
    ) -> str:
        """Return a usable AT, renewing it through RT only when needed."""
        return self._maintain_access_token(
            access_token,
            force=False,
            event=event,
            raise_on_error=raise_on_error,
            image_scope=image_scope,
            expected_credentials=expected_credentials,
            skip_if_image_busy=skip_if_image_busy,
        )

    def force_refresh_access_token(
        self,
        access_token: str,
        *,
        event: str = "force_refresh_access_token",
        raise_on_error: bool = False,
        image_scope: bool = False,
        expected_credentials: _CredentialGeneration | None = None,
    ) -> str:
        """Force one RT-to-AT exchange for an explicit recovery or admin action."""
        return self._maintain_access_token(
            access_token,
            force=True,
            event=event,
            raise_on_error=raise_on_error,
            image_scope=image_scope,
            expected_credentials=expected_credentials,
        )

    def refresh_access_token(
        self,
        access_token: str,
        *,
        force: bool = False,
        event: str = "refresh_access_token",
        remove_invalid: bool | None = None,
        raise_on_error: bool = False,
        image_scope: bool = False,
        expected_credentials: _CredentialGeneration | None = None,
        skip_if_image_busy: bool = False,
    ) -> str:
        """Compatibility wrapper; new callers use ensure/force methods."""
        _ = remove_invalid
        operation = self.force_refresh_access_token if force else self.ensure_access_token
        kwargs = {
            "event": event,
            "raise_on_error": raise_on_error,
            "image_scope": image_scope,
            "expected_credentials": expected_credentials,
        }
        if not force:
            kwargs["skip_if_image_busy"] = skip_if_image_busy
        return operation(access_token, **kwargs)

    def _token_refresh_backed_off(self, account: dict) -> bool:
        """True while a non-terminal refresh failure is still cooling down."""
        raw = account.get("last_token_refresh_error_at")
        if not raw:
            return False
        parsed = self._parse_time(raw)
        if parsed is None:
            return False
        try:
            failed_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        age = (datetime.now(timezone.utc) - failed_at).total_seconds()
        return 0 <= age < self._TOKEN_REFRESH_ERROR_BACKOFF_SECONDS

    def list_expiring_access_tokens(self) -> list[str]:
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            # Snapshot only: copying ~10k references costs well under a
            # millisecond.  Every per-account check below (JWT expiry decode in
            # _token_needs_refresh, timestamp parsing in
            # _token_refresh_backed_off) is pure and lock-free, so it runs
            # outside the lock.  Doing that work while holding the global
            # account lock blocked _update_account_impl for seconds and made
            # the lock wait grow with pool size and traffic.
            snapshot = list(self._accounts.values())
        return [
            token
            for account in snapshot
            if account.get("status") in {"正常", "限流"}
            and account.get("last_remote_check_result") != "pending"
            and str(account.get("refresh_token") or "").strip()
            and not account.get("refresh_token_invalid_at")
            and not self._token_refresh_backed_off(account)
            and (token := str(account.get("access_token") or "").strip())
            and self._token_needs_refresh(token)
        ]

    def list_tokens(self) -> list[str]:
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            return list(self._accounts)

    def _list_ready_candidate_tokens(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> list[str]:
        excluded = set(excluded_tokens or set())
        return [
            token
            for item in self._accounts.values()
            if self._is_image_account_available(item)
               and self._account_matches_plan_type(item, plan_type)
               and self._account_matches_any_plan_type(item, plan_types)
               and self._account_matches_source_type(item, source_type)
               and (token := item.get("access_token") or "")
               and token not in excluded
        ]

    def _list_available_candidate_tokens(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
    ) -> list[str]:
        max_concurrency = max(1, int(config.image_account_concurrency or 1))
        return [
            token
            for token in self._list_ready_candidate_tokens(
                excluded_tokens,
                plan_type,
                source_type,
                plan_types,
            )
            if int(self._image_inflight.get(token, 0)) < max_concurrency
        ]

    @classmethod
    def _index_plan_filter(
            cls,
            plan_type: str | None,
            plan_types: set[str] | tuple[str, ...] | None,
    ) -> set[str] | None:
        """Translate the public plan filters into index bucket keys."""
        if not plan_type and not plan_types:
            return None
        keys: set[str] = set()
        if plan_type:
            normalized = cls._normalize_account_type(plan_type)
            if not normalized:
                return {"__no_match__"}
            keys.add(normalized.lower())
        for item in plan_types or ():
            normalized = cls._normalize_account_type(item)
            if normalized:
                keys.add(normalized.lower())
        return keys or {"__no_match__"}

    @classmethod
    def _index_source_filter(cls, source_type: str | None) -> set[str] | None:
        if not source_type:
            return None
        return {cls._normalize_source_type(source_type)}

    def probe_access_token_light(self, access_token: str, timeout: float = 5.0) -> bool:
        """Cheap image-selection liveness probe: one ``/backend-api/me`` call.

        Quota comes from the account's cached snapshot; this only proves the
        credential still works, avoiding two extra upstream calls per image.
        """
        if not access_token:
            return False
        from services.openai_backend_api import OpenAIBackendAPI

        with account_processing_priority_slot():
            with OpenAIBackendAPI(access_token) as backend:
                backend.get_user_info_light(timeout=timeout)
        return True

    def _candidate_remote_fresh(
            self,
            token: str,
            plan_type: str | None,
            plan_types: set[str] | tuple[str, ...] | None,
            source_type: str | None,
    ) -> bool:
        """True when a leased candidate needs no network check at all."""
        account = self.get_account(token) or {}
        return (
            self._remote_check_fresh(account)
            and self._account_matches_plan_type(account, plan_type)
            and self._account_matches_any_plan_type(account, plan_types)
            and self._account_matches_source_type(account, source_type)
        )

    def _candidate_light_probe_eligible(
            self,
            token: str,
            plan_type: str | None,
            plan_types: set[str] | tuple[str, ...] | None,
            source_type: str | None,
    ) -> bool:
        """True when one cheap /me probe is enough to validate the candidate."""
        account = self.get_account(token) or {}
        return (
            not bool(account.get("image_quota_unknown"))
            and float(account.get("quota") or 0) > 0
            and self._account_matches_plan_type(account, plan_type)
            and self._account_matches_any_plan_type(account, plan_types)
            and self._account_matches_source_type(account, source_type)
        )

    def _note_selection_probe_failure(self, token: str, exc: BaseException) -> None:
        """A real 401 during selection still removes the dead credential."""
        detail = str(exc or "")
        if any(
            marker in detail.lower()
            for marker in ("401", "token_revoked", "invalidated oauth")
        ):
            try:
                self.handle_invalid_token(
                    token,
                    "image_selection_probe",
                    error=detail[:500],
                )
            except Exception:
                pass

    def _probe_candidates_parallel(
            self,
            tokens: list[str],
            *,
            timeout: float = 5.0,
            deadline: float | None = None,
    ) -> tuple[str | None, dict[str, BaseException]]:
        """Light-probe several leased candidates at once.

        Returns ``(winner, failures)``: the first candidate whose probe
        succeeds, plus the probes that failed before it. Serial probing made a
        request pay ~5s per dead candidate; in parallel the wait is bounded by
        the fastest healthy one. Probes still running when a winner is found
        finish on their own within ``timeout``.
        """
        from concurrent.futures import TimeoutError as FuturesTimeoutError

        failures: dict[str, BaseException] = {}
        if not tokens:
            return None, failures
        wait_timeout = float(timeout) + 1.0
        if deadline is not None:
            wait_timeout = min(wait_timeout, max(0.0, deadline - time.monotonic()))
        executor = ThreadPoolExecutor(
            max_workers=len(tokens),
            thread_name_prefix="image-probe",
        )
        winner: str | None = None
        try:
            futures = {
                executor.submit(self.probe_access_token_light, token, timeout): token
                for token in tokens
            }
            try:
                for future in as_completed(futures, timeout=wait_timeout):
                    token = futures[future]
                    try:
                        future.result()
                    except Exception as exc:
                        failures[token] = exc
                        continue
                    winner = token
                    break
            except FuturesTimeoutError:
                pass
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
        return winner, failures

    def _acquire_next_candidate_token(
            self,
            excluded_tokens: set[str] | None = None,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
            deadline_monotonic: float | None = None,
            *,
            wait: bool = True,
            required_capabilities: set[str] | tuple[str, ...] | None = None,
    ) -> str | None:
        # Capabilities that are cheap to verify locally and expensive to get
        # wrong upstream: an account whose recorded capability map lacks them
        # can only burn an upstream attempt, so steer selection away from it
        # instead of letting the caller reject it afterwards.
        hard_required = {
            str(item).strip()
            for item in (required_capabilities or ())
            if str(item).strip() in {"gif", "file_upload"}
        }
        capability_skipped: set[str] = set()
        # Fast path: delegate to the Go gateway when it is available.
        # The gateway maintains its own in-memory index and returns in <1 ms
        # without touching the Python global lock.  On any failure (network,
        # gateway not running) we fall through to the existing local path.
        if _gw.is_enabled():
            remaining = (
                float(deadline_monotonic) - time.monotonic()
                if deadline_monotonic is not None
                else None
            )
            if remaining is None or remaining > 0:
                gw_timeout = min(2.0, remaining) if remaining is not None else 2.0
                plans_list: list[str] | None = None
                sources_list: list[str] | None = None
                _plans = self._index_plan_filter(plan_type, plan_types)
                if _plans is not None:
                    plans_list = list(_plans)
                _sources = self._index_source_filter(source_type)
                if _sources is not None:
                    sources_list = list(_sources)
                tok = _gw.acquire(
                    plans=plans_list,
                    sources=sources_list,
                    excluded=list(excluded_tokens) if excluded_tokens else None,
                    limit=max(1, int(config.image_account_concurrency or 1)),
                    timeout=gw_timeout,
                )
                if tok:
                    # Lease exists in the gateway; mirror into local inflight
                    # so release_image_slot can find it.
                    with self._image_slot_condition:
                        self._image_inflight[tok] = (
                            int(self._image_inflight.get(tok, 0)) + 1
                        )
                    return tok
                if tok == "" and not wait:
                    # Gateway says pool is exhausted and caller doesn't want to block.
                    return None
                # tok is None → gateway unavailable, fall through to local index.

        plans = self._index_plan_filter(plan_type, plan_types)
        sources = self._index_source_filter(source_type)
        if hard_required:
            # Waiting cannot help when the pool contains no account that either
            # advertises the capability or is still unclassified: fail fast
            # instead of blocking the request until its deadline.
            self._require_hard_capability(hard_required)
        while True:
            with self._image_slot_condition:
                remaining = (
                    float(deadline_monotonic) - time.monotonic()
                    if deadline_monotonic is not None
                    else None
                )
                if remaining is not None and remaining <= 0:
                    raise ImageAccountSelectionError(
                        "deadline_exceeded",
                        "image request deadline exceeded while waiting for an account slot",
                    )
                # Token refresh can rotate an attempted account's access token while
                # this request waits for a slot. Resolve aliases on every pass so the
                # same account cannot be selected again under its refreshed token.
                resolved_excluded_tokens = {
                    self._resolve_access_token_locked(token)
                    for token in (excluded_tokens or set())
                    if token
                }
                # Never hand back an account this call already rejected for a
                # missing hard capability: the caller would only reject it again.
                resolved_excluded_tokens |= capability_skipped
                if not self._selection_index.has_ready(
                    IMAGE_MODE,
                    plans=plans,
                    sources=sources,
                ):
                    if not wait:
                        return None
                    raise self._no_ready_candidate_error(
                        plan_type,
                        source_type,
                        plan_types,
                        resolved_excluded_tokens,
                    )
                max_concurrency = max(1, int(config.image_account_concurrency or 1))
                access_token = self._selection_index.acquire(
                    IMAGE_MODE,
                    plans=plans,
                    sources=sources,
                    excluded=resolved_excluded_tokens,
                    max_concurrency=max_concurrency,
                )
                if access_token:
                    self._image_inflight[access_token] = (
                        int(self._image_inflight.get(access_token, 0)) + 1
                    )
                    if hard_required:
                        known_capabilities = (
                            self._accounts.get(access_token) or {}
                        ).get("image_capabilities")
                        if (
                            isinstance(known_capabilities, dict)
                            and known_capabilities
                            and not hard_required.issubset(known_capabilities)
                        ):
                            # Give the lease straight back (inflight + selection
                            # index) and skip it, instead of returning an
                            # account the caller must reject and re-request.
                            self._release_image_slot_locked(access_token)
                            capability_skipped.add(access_token)
                            if len(capability_skipped) >= 300:
                                raise ImageAccountSelectionError(
                                    "capability_unavailable",
                                    "no account has the required image capability",
                                )
                            continue
                    return access_token
                if not wait:
                    return None
                self._image_slot_condition.wait(
                    timeout=min(1.0, remaining) if remaining is not None else 1.0
                )
            # The wait can outlive the account snapshot TTL. Refresh outside the
            # account lock before considering another candidate so a remote delete
            # or disable cannot be leased from the stale in-memory view.
            self._refresh_accounts_snapshot_if_stale(wait_for_refresh=True)

    def _require_hard_capability(self, required: set[str]) -> None:
        """Raise capability_unavailable when nothing in the pool can serve it.

        An account is a possible candidate when it either advertises every
        required capability, or has no recorded capability map at all (never
        classified, so it may still work). Only when neither exists is waiting
        pointless.
        """
        required = {str(item).strip() for item in (required or ()) if str(item).strip()}
        if not required:
            return
        with self._lock:
            snapshot = list(self._accounts.values())
        for item in snapshot:
            if not self._is_image_account_available(item):
                continue
            known = item.get("image_capabilities")
            if not isinstance(known, dict) or not known:
                return  # unclassified account: give it a chance
            if required.issubset(known):
                return
        raise ImageAccountSelectionError(
            "capability_unavailable",
            "no account has the required image capability",
        )

    def _no_ready_candidate_error(
            self,
            plan_type: str | None,
            source_type: str | None,
            plan_types: set[str] | tuple[str, ...] | None,
            excluded_tokens: set[str] | None,
    ) -> "ImageAccountSelectionError":
        """没有任何 ready 候选时区分两种成因。

        这是“初筛阶段”的判据：只看本地缓存状态，此时还没走远程预检，
        不存在“预检失败”这一维度。匹配账号全部为“限流”（限流只由远程确认写入）
        -> 额度耗尽（429）；否则一律归为可重试的 unavailable（503）。

        注意：get_available_access_token 里的 429 判据更严
        （额外要求 not saw_unavailable_failure），因为那是“预检阶段”，
        会有上游波动等可重试失败混入，不能仅凭限流就下终结性结论。
        """
        excluded = set(excluded_tokens or set())
        matched = 0
        limited = 0
        for item in self._accounts.values():
            token = item.get("access_token") or ""
            if not token or token in excluded:
                continue
            if not (
                self._account_matches_plan_type(item, plan_type)
                and self._account_matches_any_plan_type(item, plan_types)
                and self._account_matches_source_type(item, source_type)
            ):
                continue
            matched += 1
            if str(item.get("status") or "") == "限流":
                limited += 1
        if matched > 0 and limited == matched:
            return ImageAccountSelectionError(
                "quota_exhausted",
                "all matched image accounts are remote-confirmed quota exhausted",
            )
        return ImageAccountSelectionError(
            "unavailable",
            "no image account is ready for current model/status filters",
        )

    def image_inflight_total(self) -> int:
        with self._image_slot_condition:
            return sum(max(0, int(value or 0)) for value in self._image_inflight.values())

    def _sync_gateway_accounts_async(self) -> None:
        """Push all in-memory accounts to the Go gateway in a background thread.

        Called after a full snapshot load or refresh so the gateway index stays
        in sync without blocking the main event loop.  Errors are logged but
        never raised; the gateway path is optional and falls back to the local
        index on failure.
        """
        if not _gw.is_enabled():
            return
        # Take a shallow copy of values while the lock is NOT held so we
        # don't block selection; the dict values are immutable-ish dicts.
        try:
            accounts = list(self._accounts.values())
        except Exception:
            return

        def _run() -> None:
            try:
                sent, errors = _gw.sync_accounts(accounts, request_timeout=10.0)
                if errors:
                    logger.warning({
                        "event": "gateway_sync_errors",
                        "total": len(accounts),
                        "sent": sent,
                        "errors": errors,
                    })
                else:
                    logger.debug({
                        "event": "gateway_sync_done",
                        "sent": sent,
                    })
            except Exception as exc:
                logger.debug("gateway sync thread error: %s", exc)

        t = Thread(target=_run, name="gateway-sync", daemon=True)
        t.start()

    def selection_index_stats(self) -> dict[str, object]:
        """Read-only diagnostics for image/text selection fairness."""
        try:
            return self._selection_index.stats()
        except Exception:
            return {}

    def _on_account_lock_slow(self, event: dict) -> None:
        """Log every account-lock wait or hold that exceeds the slow threshold."""
        logger.warning({"event": "account_lock_slow", **event})

    def lock_diagnostics(self) -> dict[str, object]:
        """Read-only snapshot of account-lock wait/hold timing."""
        try:
            return self._lock.snapshot()
        except Exception:
            return {}

    def release_image_slot(self, access_token: str) -> None:
        if not access_token:
            return
        with self._image_slot_condition:
            access_token = self._resolve_access_token_locked(access_token)
            self._release_image_slot_locked(access_token)
            self._image_slot_condition.notify_all()
        # Notify the Go gateway outside the lock so we don't hold it during I/O.
        # Best-effort: a failure here only means the gateway's lease counter drifts
        # temporarily; it self-corrects on the next acquire/release cycle.
        if _gw.is_enabled():
            _gw.release(access_token, limit=max(1, int(config.image_account_concurrency or 1)))

    def _release_image_slot_locked(self, access_token: str) -> None:
        current_inflight = int(self._image_inflight.get(access_token, 0))
        if current_inflight <= 1:
            self._image_inflight.pop(access_token, None)
        else:
            self._image_inflight[access_token] = current_inflight - 1
        self._selection_index.release(
            access_token,
            max_concurrency=max(1, int(config.image_account_concurrency or 1)),
        )

    def get_available_access_token(
            self,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
            excluded_tokens: set[str] | None = None,
            deadline_monotonic: float | None = None,
            required_capabilities: set[str] | tuple[str, ...] | None = None,
    ) -> str:
        """Select one image account token with a closed failure contract.

        ``ImageAccountSelectionError`` is the only selection-failure type the
        protocol layer can map (``quota_exhausted`` -> 429, everything else ->
        retryable 503). Any other exception escaping the selector used to reach
        the generic request handler and became ``internal_error`` / HTTP 500
        even though it only means "no account for this request". Normalize it
        here, at the owner of the invariant, so a pool miss can never present
        itself as a server bug.
        """
        try:
            return self._select_available_access_token(
                plan_type=plan_type,
                source_type=source_type,
                plan_types=plan_types,
                excluded_tokens=excluded_tokens,
                deadline_monotonic=deadline_monotonic,
                required_capabilities=required_capabilities,
            )
        except ImageAccountSelectionError:
            raise
        except Exception as exc:
            logger.warning({
                "event": "image_account_selection_internal_error",
                "error_type": type(exc).__name__,
                "error": diagnostic_excerpt(exc, 500),
            })
            raise ImageAccountSelectionError(
                "unavailable",
                f"image account selection failed internally ({type(exc).__name__})",
            ) from exc

    def _select_available_access_token(
            self,
            plan_type: str | None = None,
            source_type: str | None = None,
            plan_types: set[str] | tuple[str, ...] | None = None,
            excluded_tokens: set[str] | None = None,
            deadline_monotonic: float | None = None,
            required_capabilities: set[str] | tuple[str, ...] | None = None,
    ) -> str:
        """从候选池中获取一个可用的图片生图 token。

        基于本地缓存做初筛，然后通过 fetch_remote_info 做远程验证（token 有效性、配额等）。
        限制最大尝试次数防止 token rotation 导致无限循环。
        """
        self._refresh_accounts_snapshot_if_stale()
        selection_started = time.monotonic()
        # Never let account selection hold a customer request for minutes while
        # it probes exhausted free accounts one by one. 30s allows a couple of
        # slow upstream /me checks while still failing visibly instead of
        # hanging; 20s occasionally produced no_available_account on healthy
        # pools when the upstream was slow.
        selection_deadline = min(
            deadline_monotonic if deadline_monotonic is not None else float("inf"),
            selection_started + 30.0,
        )
        if time.monotonic() >= selection_deadline:
            raise ImageAccountSelectionError(
                "unavailable",
                "image account selection timed out before trying any account",
            )
        try:
            max_attempts = max(
                1,
                min(12, int(config.image_max_account_attempts or 8)),
            )
        except (AttributeError, TypeError, ValueError):
            max_attempts = 8
        externally_excluded = set(excluded_tokens or set())
        attempted_tokens: set[str] = set()
        remote_checks = 0
        cache_hits = 0
        # 控制流只保留两个出口，但最终是否能说“额度耗尽”必须谨慎：
        # 只要出现过非额度类失败，就说明不能断言全部账号都耗尽，应返回可重试的 unavailable。
        saw_remote_quota_exhausted = False
        saw_unavailable_failure = False
        for _attempt in range(max_attempts):
            if upstream_circuit.is_open():
                raise ImageAccountSelectionError(
                    "unavailable",
                    "upstream circuit open; refusing to burn more accounts",
                )
            try:
                access_token = self._acquire_next_candidate_token(
                    excluded_tokens=externally_excluded | attempted_tokens,
                    plan_type=plan_type,
                    source_type=source_type,
                    plan_types=plan_types,
                    deadline_monotonic=selection_deadline,
                    required_capabilities=required_capabilities,
                )
            except ImageAccountSelectionError as exc:
                if exc.kind == "deadline_exceeded":
                    raise
                if attempted_tokens:
                    break
                raise
            # Power of three choices: lease a few ready candidates and keep the
            # best one. Two choices over a 10k pool kept re-picking the same
            # established accounts; a third candidate gives fresh accounts a
            # realistic chance without sorting the whole pool.
            candidates = [access_token]
            for _ in range(2):
                try:
                    next_token = self._acquire_next_candidate_token(
                        excluded_tokens=(
                            externally_excluded
                            | attempted_tokens
                            | set(candidates)
                        ),
                        plan_type=plan_type,
                        source_type=source_type,
                        plan_types=plan_types,
                        deadline_monotonic=selection_deadline,
                        wait=False,
                        required_capabilities=required_capabilities,
                    )
                except ImageAccountSelectionError as exc:
                    if exc.kind == "deadline_exceeded":
                        for token in candidates:
                            self.release_image_slot(token)
                        raise
                    break
                if not next_token:
                    break
                candidates.append(next_token)
            scored = [
                (
                    candidate_score(
                        self.get_account(token) or {},
                        required_capabilities,
                    ),
                    token,
                )
                for token in candidates
            ]
            if len(candidates) > 1 and all(
                self._candidate_remote_fresh(token, plan_type, plan_types, source_type)
                or self._candidate_light_probe_eligible(
                    token, plan_type, plan_types, source_type
                )
                for token in candidates
            ):
                # Parallel validation: every leased candidate is either
                # fresh or cheap to probe, so validate them together and
                # take the first healthy one instead of paying ~5s per dead
                # candidate in sequence.
                ordered = [
                    token
                    for _, token in sorted(scored, key=lambda item: item[0], reverse=True)
                ]
                for token in candidates:
                    attempted_tokens.add(token)
                failures: dict[str, BaseException] = {}
                # A candidate whose recent remote check is still fresh needs no
                # network call at all; prefer the best-scored one of those.
                winner: str | None = next(
                    (
                        token
                        for token in ordered
                        if self._candidate_remote_fresh(
                            token, plan_type, plan_types, source_type
                        )
                    ),
                    None,
                )
                if winner is None:
                    probe_tokens = [
                        token
                        for token in ordered
                        if self._candidate_light_probe_eligible(
                            token, plan_type, plan_types, source_type
                        )
                    ]
                    winner, failures = self._probe_candidates_parallel(
                        probe_tokens,
                        timeout=5.0,
                        deadline=selection_deadline,
                    )
                    if winner is None:
                        winner = next(
                            (
                                token
                                for token in ordered
                                if token not in failures
                                and self._candidate_remote_fresh(
                                    token, plan_type, plan_types, source_type
                                )
                            ),
                            None,
                        )
                for token, probe_exc in failures.items():
                    saw_unavailable_failure = True
                    self._note_selection_probe_failure(token, probe_exc)
                for token in candidates:
                    if token != winner:
                        self.release_image_slot(token)
                if winner is not None:
                    cache_hits += 1
                    winner_account = self.get_account(winner) or {}
                    wait_ms = int((time.monotonic() - selection_started) * 1000)
                    if wait_ms >= 2000:
                        logger.info({
                            "event": "image_account_selected",
                            "wait_ms": wait_ms,
                            "attempts": len(attempted_tokens),
                            "remote_checks": remote_checks,
                            "cache_hits": cache_hits,
                            "source": "parallel_probe",
                        })
                    return str(winner_account.get("access_token") or winner)
                if time.monotonic() >= selection_deadline:
                    raise ImageAccountSelectionError(
                        "unavailable",
                        "image account selection timed out during remote validation",
                    )
                continue
            _, chosen_token = max(scored, key=lambda item: item[0])
            # 5% exploration keeps a temporarily low-scored account from being
            # frozen out forever; it also gives new accounts a chance to build
            # their EWMA instead of starving behind established ones.
            if len(scored) > 1 and random.random() < 0.05:
                chosen_token = random.choice(scored)[1]
            for token in candidates:
                attempted_tokens.add(token)
                if token != chosen_token:
                    self.release_image_slot(token)
            access_token = chosen_token
            if time.monotonic() >= selection_deadline:
                self.release_image_slot(access_token)
                raise ImageAccountSelectionError(
                    "unavailable",
                    "image account selection timed out before remote validation",
                )
            cached_account = self.get_account(access_token) or {}
            if (
                self._remote_check_fresh(cached_account)
                and self._account_matches_plan_type(cached_account, plan_type)
                and self._account_matches_any_plan_type(cached_account, plan_types)
                and self._account_matches_source_type(cached_account, source_type)
            ):
                cache_hits += 1
                wait_ms = int((time.monotonic() - selection_started) * 1000)
                if wait_ms >= 2000:
                    logger.info({
                        "event": "image_account_selected",
                        "wait_ms": wait_ms,
                        "attempts": len(attempted_tokens),
                        "remote_checks": remote_checks,
                        "cache_hits": cache_hits,
                        "source": "cached_remote_check",
                    })
                return str(cached_account.get("access_token") or access_token)
            # Cached quota is still positive: validate liveness with one /me
            # call instead of paying for /me + init + default-account again.
            if (
                not bool(cached_account.get("image_quota_unknown"))
                and float(cached_account.get("quota") or 0) > 0
                and self._account_matches_plan_type(cached_account, plan_type)
                and self._account_matches_any_plan_type(cached_account, plan_types)
                and self._account_matches_source_type(cached_account, source_type)
            ):
                try:
                    self.probe_access_token_light(access_token, timeout=5.0)
                except Exception as probe_exc:
                    # A failed light probe used to fall through to a full
                    # fetch_remote_info (up to 8s more) on the same account,
                    # so one dead candidate cost ~13s of the customer's wait.
                    # Move on instead; a real 401 still goes through the
                    # normal invalid-token path so the account is removed.
                    saw_unavailable_failure = True
                    detail = str(probe_exc or "")
                    if any(
                        marker in detail.lower()
                        for marker in ("401", "token_revoked", "invalidated oauth")
                    ):
                        try:
                            self.handle_invalid_token(
                                access_token,
                                "image_selection_probe",
                                error=detail[:500],
                            )
                        except Exception:
                            pass
                    self.release_image_slot(access_token)
                    if time.monotonic() >= selection_deadline:
                        raise ImageAccountSelectionError(
                            "unavailable",
                            "image account selection timed out during remote validation",
                        )
                    continue
                else:
                    cache_hits += 1
                    return str(cached_account.get("access_token") or access_token)
            remote_checks += 1
            try:
                account = self.fetch_remote_info(
                    access_token,
                    "get_available_access_token",
                    image_scope=True,
                    request_timeout=8.0,
                )
            except Exception:
                # 预检失败（上游波动/网络/401 等）：这个号这次不可用，换下一个。
                # 401 已在 fetch_remote_info 内部走异常处理，这里不再二次分类。
                saw_unavailable_failure = True
                self.release_image_slot(access_token)
                if time.monotonic() >= selection_deadline:
                    raise ImageAccountSelectionError(
                        "unavailable",
                        "image account selection timed out during remote validation",
                    )
                continue
            if time.monotonic() >= selection_deadline:
                self.release_image_slot(access_token)
                raise ImageAccountSelectionError(
                    "unavailable",
                    "image account selection timed out during remote validation",
                )
            # fetch_remote_info 内部可能因 token rotation 导致 access_token 变化，
            # 把新 token 也加入排除列表，防止重复尝试
            resolved = str((account or {}).get("access_token") or "")
            if resolved and resolved != access_token:
                attempted_tokens.add(resolved)
            if (
                    self._is_image_account_available(account or {})
                    and self._account_matches_plan_type(account or {}, plan_type)
                    and self._account_matches_any_plan_type(account or {}, plan_types)
                    and self._account_matches_source_type(account or {}, source_type)
            ):
                wait_ms = int((time.monotonic() - selection_started) * 1000)
                if wait_ms >= 2000:
                    logger.info({
                        "event": "image_account_selected",
                        "wait_ms": wait_ms,
                        "attempts": len(attempted_tokens),
                        "remote_checks": remote_checks,
                        "cache_hits": cache_hits,
                        "source": "remote_check",
                    })
                return str((account or {}).get("access_token") or access_token)
            if str((account or {}).get("status") or "") == "限流":
                saw_remote_quota_exhausted = True
            else:
                saw_unavailable_failure = True
            self.release_image_slot(access_token)
        wait_ms = int((time.monotonic() - selection_started) * 1000)
        logger.warning({
            "event": "image_account_selection_failed",
            "wait_ms": wait_ms,
            "attempts": len(attempted_tokens),
            "remote_checks": remote_checks,
            "cache_hits": cache_hits,
            "quota_exhausted": saw_remote_quota_exhausted and not saw_unavailable_failure,
        })
        if saw_remote_quota_exhausted and not saw_unavailable_failure:
            raise ImageAccountSelectionError(
                "quota_exhausted",
                f"all usable image accounts remote-confirmed quota exhausted after {len(attempted_tokens)} attempts",
            )
        raise ImageAccountSelectionError(
            "unavailable",
            f"no image account available after {len(attempted_tokens)} attempts",
        )

    def get_text_access_token(self, excluded_tokens: set[str] | None = None) -> str:
        self._refresh_accounts_snapshot_if_stale()
        attempted = set(excluded_tokens or set())
        while True:
            with self._lock:
                access_token = self._selection_index.next_ready(
                    TEXT_MODE,
                    excluded=attempted,
                )
            if not access_token:
                return ""
            attempted.add(access_token)
            try:
                return self.ensure_access_token(access_token, event="get_text_access_token")
            except (TerminalRefreshTokenError, RefreshCredentialsChangedError):
                continue

    def mark_text_used(self, access_token: str) -> None:
        if not access_token:
            return
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return
            next_item = dict(current)
            next_item["last_used_at"] = self._now()
            account = self._normalize_account(next_item)
            if account is None:
                return
            self._accounts[access_token] = account
            self._save_account_row_locked(access_token)

    def remove_invalid_token(
        self,
        access_token: str,
        event: str,
        quiet: bool = False,
        remove: bool | None = None,
        error: str | None = None,
    ) -> bool:
        return self.handle_invalid_token(
            access_token,
            event,
            error=error,
            quiet=quiet,
            remove=remove,
            expected_access_token=access_token,
        )

    def handle_invalid_token(
        self,
        access_token: str,
        event: str,
        error: str | None = None,
        quiet: bool = False,
        remove: bool | None = None,
        expected_access_token: str | None = None,
        expected_refresh_token: str | None = None,
        expected_remote_check_marker: _RemoteCheckMarker | None = None,
        token_refresh_error: str | None = None,
        refresh_token_terminal: bool = False,
    ) -> bool:
        """统一处理鉴权异常账号。

        口径固定为：先记录异常，再按“自动移除异常账号”配置删除或保留异常状态。
        """
        should_remove = config.auto_remove_invalid_accounts if remove is None else remove
        return self._apply_invalid_token_state(
            access_token,
            event,
            str(error or "invalid access token"),
            remove=bool(should_remove),
            expected_access_token=expected_access_token,
            expected_refresh_token=expected_refresh_token,
            expected_remote_check_marker=expected_remote_check_marker,
            token_refresh_error=token_refresh_error,
            refresh_token_terminal=refresh_token_terminal,
            quiet=quiet,
        )

    def get_account(self, access_token: str) -> dict | None:
        if not access_token:
            return None
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            account = self._accounts.get(access_token)
            return dict(account) if account else None

    def get_account_by_token_identity(self, access_token: str) -> dict | None:
        """Resolve a current account from either its current or a rotated access token."""
        token = str(access_token or "").strip()
        if not token:
            return None
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            resolved = self._resolve_access_token_locked(token)
            account = self._accounts.get(resolved)
            if account is not None:
                return dict(account)

            fingerprint = self._access_token_fingerprint(token)
            for candidate in self._accounts.values():
                fingerprints = candidate.get("access_token_fingerprints")
                if isinstance(fingerprints, list) and fingerprint in fingerprints:
                    return dict(candidate)
        return None

    def get_account_by_id(self, account_id: str) -> dict | None:
        management_id = str(account_id or "").strip().lower()
        if not management_id:
            return None
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            token = self._by_management_id.get(management_id)
            if token is None:
                return None
            account = self._accounts.get(token)
            if account is None:
                return None
            result = dict(account)
            access_token = str(result.get("access_token") or "").strip()
            result["image_inflight"] = int(self._image_inflight.get(access_token, 0))
            return result

    def resolve_account_ids(self, account_ids: list[str]) -> tuple[list[str], list[str]]:
        requested = list(dict.fromkeys(str(item or "").strip().lower() for item in account_ids if str(item or "").strip()))
        if not requested:
            return [], []
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            tokens: list[str] = []
            missing: list[str] = []
            for account_id in requested:
                token = self._by_management_id.get(account_id)
                if token:
                    tokens.append(token)
                else:
                    missing.append(account_id)
        return tokens, missing

    def account_count(self) -> int:
        """Pool size without materialising a list of accounts."""
        return len(self._accounts)

    def iter_account_records(self) -> Iterator[dict]:
        """Iterate live account records in insertion order.

        Callers must treat the yielded mappings as read-only and project only
        the page they return; copying the whole pool is what made large-pool
        listing unusable.
        """
        return _AccountRecordIterator(self._accounts, self._token_order)

    def iter_account_window(self, start: int, stop: int) -> list[dict]:
        """Return projected-inflight records for an insertion-order window."""
        if stop <= start:
            return []
        order = self._token_order
        accounts = self._accounts
        window: list[dict] = []
        if self._token_holes == 0:
            # No tombstones: the order list is the live slicing index, so a
            # deep page costs O(page) instead of walking the prefix.
            for token in order[start:stop]:
                account = accounts.get(token)
                if account is None:
                    continue
                item = dict(account)
                item["image_inflight"] = int(
                    self._image_inflight.get(str(account.get("access_token") or ""), 0)
                )
                window.append(item)
            return window
        position = 0
        index = 0
        total = len(order)
        while index < total and position < stop:
            token = order[index]
            index += 1
            account = accounts.get(token)
            if account is None:
                continue
            if position >= start:
                item = dict(account)
                item["image_inflight"] = int(
                    self._image_inflight.get(str(account.get("access_token") or ""), 0)
                )
                window.append(item)
            position += 1
        return window

    @classmethod
    def is_image_account_available(cls, account: dict) -> bool:
        return cls._is_image_account_available(account)

    @classmethod
    def is_unlimited_image_quota_account(cls, account: dict) -> bool:
        return cls._is_unlimited_image_quota_account(account)

    def list_accounts(self) -> list[dict]:
        """返回所有账号的副本，并为每个账号附加当前图片在途数 image_inflight。

        image_inflight 为内存态并发计数(账号正在生成、尚未结束的图片数)。号池空闲时
        若某账号该值持续 > 0，说明其并发槽位泄漏、已被静默排除出调度，可借此在 UI 上诊断。
        """
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            result = []
            for item in self._accounts.values():
                account = dict(item)
                token = account.get("access_token") or ""
                account["image_inflight"] = int(self._image_inflight.get(token, 0))
                result.append(account)
            return result

    # ------------------------------------------------- read-only audit queries

    @staticmethod
    def _credential_projection(account: dict) -> Any:
        return project_upstream_credential_availability(
            str(account.get("access_token") or ""),
            str(account.get("refresh_token") or ""),
            access_confirmed_invalid=(
                str(account.get("last_remote_check_result") or "").strip().lower()
                == "invalid"
            ),
            refresh_confirmed_invalid=bool(account.get("refresh_token_invalid_at")),
        )

    @staticmethod
    def _identity_projection(account: dict) -> dict[str, Any]:
        """Project only identity fields for duplicate analysis.

        The account record is the primary source; tokens are decoded only when
        the record lacks an email or upstream subject. No credential value is
        returned to the caller.
        """
        access_token = str(account.get("access_token") or "").strip()
        id_token = str(account.get("id_token") or "").strip()
        email = str(account.get("email") or "").strip().casefold()
        subject = (
            str(account.get("user_id") or "").strip()
            or str(account.get("account_id") or "").strip()
        )
        if not email or not subject:
            claims = decode_access_token_payload(access_token)
            auth_claim = claims.get("https://api.openai.com/auth")
            auth_claim = auth_claim if isinstance(auth_claim, dict) else {}
            profile_claim = claims.get("https://api.openai.com/profile")
            profile_claim = profile_claim if isinstance(profile_claim, dict) else {}
            if not email:
                email = str(
                    profile_claim.get("email") or claims.get("email") or ""
                ).strip().casefold()
            if not subject:
                subject = str(
                    auth_claim.get("chatgpt_account_id")
                    or auth_claim.get("user_id")
                    or claims.get("sub")
                    or ""
                ).strip()
        if (not email or not subject) and id_token:
            id_claims = decode_access_token_payload(id_token)
            if not email:
                email = str(id_claims.get("email") or "").strip().casefold()
            if not subject:
                subject = str(id_claims.get("sub") or "").strip()
        refresh_token = str(account.get("refresh_token") or "").strip()
        projection = AccountService._credential_projection(account)
        return {
            "management_id": str(account.get("management_id") or "").strip(),
            "email": email,
            "subject": subject,
            "status": str(account.get("status") or "").strip(),
            "created_at": str(account.get("created_at") or "").strip(),
            "source_type": str(account.get("source_type") or "").strip(),
            "credential": projection.status,
            "access": projection.access.status,
            "refresh": projection.refresh_status,
            "has_refresh_token": bool(refresh_token),
            "refresh_token_fingerprint": (
                hashlib.sha256(refresh_token.encode("utf-8")).hexdigest()[:16]
                if refresh_token
                else ""
            ),
            "refresh_token_confirmed_invalid": bool(
                account.get("refresh_token_invalid_at")
            ),
        }

    def _iter_identity_projections(self) -> Iterator[dict[str, Any]]:
        for account in self.iter_account_records():
            yield self._identity_projection(account)

    @staticmethod
    def _duplicate_credential_rank(value: str) -> int:
        return {"usable": 0, "recoverable": 1}.get(value, 2)

    @staticmethod
    def _duplicate_refresh_rank(item: dict[str, Any]) -> int:
        return {"valid": 0, "missing": 1}.get(str(item.get("refresh") or ""), 2)

    @classmethod
    def _sorted_duplicate_members(
        cls,
        members: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        ordered = sorted(
            members,
            key=lambda item: str(item.get("created_at") or ""),
            reverse=True,
        )
        ordered.sort(
            key=lambda item: (
                cls._duplicate_credential_rank(str(item.get("credential") or "")),
                cls._duplicate_refresh_rank(item),
            )
        )
        return ordered

    @classmethod
    def _duplicate_group_payload(
        cls,
        key: str,
        kind: str,
        members: list[dict[str, Any]],
        *,
        signals: list[str],
        max_members: int,
    ) -> dict[str, Any]:
        ordered = cls._sorted_duplicate_members(members)
        visible = ordered[:max_members]
        key_field = {
            "email": "email",
            "subject": "upstream_subject",
            "refresh_token": "refresh_token_fingerprint",
        }[kind]
        return {
            key_field: key,
            "count": len(members),
            "suggested_keep": str(visible[0].get("management_id") or "") if visible else "",
            "suggested_remove_ids": [
                str(item.get("management_id") or "")
                for item in visible[1:]
                if str(item.get("management_id") or "")
            ],
            "signals": signals,
            "members": [
                {
                    "account_id": str(item.get("management_id") or ""),
                    "email": str(item.get("email") or ""),
                    "status": str(item.get("status") or ""),
                    "credential": str(item.get("credential") or ""),
                    "access": str(item.get("access") or ""),
                    "refresh": str(item.get("refresh") or ""),
                    "has_refresh_token": bool(item.get("has_refresh_token")),
                    "refresh_token_invalid": bool(
                        item.get("refresh_token_confirmed_invalid")
                    ),
                    "created_at": str(item.get("created_at") or ""),
                    "source_type": str(item.get("source_type") or ""),
                }
                for item in visible
            ],
            "members_truncated": len(members) > len(visible),
        }

    def scan_account_duplicates(
        self,
        *,
        max_groups: int = 100,
        max_members: int = 20,
    ) -> dict[str, Any]:
        """Report duplicate accounts by email, upstream subject, and RT.

        This is a read-only report: it never mutates the account store and
        never returns access tokens, refresh tokens, or ID tokens. Two passes
        are used so a large pool with mostly unique identities does not keep a
        full member list in memory.
        """
        max_groups = max(1, min(1_000, int(max_groups)))
        max_members = max(1, min(200, int(max_members)))
        email_counts: dict[str, int] = {}
        subject_counts: dict[str, int] = {}
        refresh_counts: dict[str, int] = {}
        total = 0
        with_email = 0
        missing_email = 0
        missing_identity = 0
        missing_email_sample: list[dict[str, Any]] = []
        missing_identity_sample: list[dict[str, Any]] = []

        for item in self._iter_identity_projections():
            total += 1
            email = str(item.get("email") or "")
            subject = str(item.get("subject") or "")
            refresh_fingerprint = str(item.get("refresh_token_fingerprint") or "")
            if email:
                with_email += 1
                email_counts[email] = email_counts.get(email, 0) + 1
            else:
                missing_email += 1
                if len(missing_email_sample) < 20:
                    missing_email_sample.append(
                        {
                            "account_id": str(item.get("management_id") or ""),
                            "status": str(item.get("status") or ""),
                            "has_upstream_subject": bool(subject),
                            "created_at": str(item.get("created_at") or ""),
                        }
                    )
                if not subject:
                    missing_identity += 1
                    if len(missing_identity_sample) < 20:
                        missing_identity_sample.append(
                            {
                                "account_id": str(item.get("management_id") or ""),
                                "status": str(item.get("status") or ""),
                                "created_at": str(item.get("created_at") or ""),
                            }
                        )
            if subject:
                subject_counts[subject] = subject_counts.get(subject, 0) + 1
            if refresh_fingerprint:
                refresh_counts[refresh_fingerprint] = (
                    refresh_counts.get(refresh_fingerprint, 0) + 1
                )

        duplicate_emails = {
            key for key, count in email_counts.items() if count > 1
        }
        duplicate_subjects = {
            key for key, count in subject_counts.items() if count > 1
        }
        duplicate_refresh_tokens = {
            key for key, count in refresh_counts.items() if count > 1
        }

        email_members: dict[str, list[dict[str, Any]]] = {
            key: [] for key in duplicate_emails
        }
        subject_members: dict[str, list[dict[str, Any]]] = {
            key: [] for key in duplicate_subjects
        }
        refresh_members: dict[str, list[dict[str, Any]]] = {
            key: [] for key in duplicate_refresh_tokens
        }
        for item in self._iter_identity_projections():
            email = str(item.get("email") or "")
            subject = str(item.get("subject") or "")
            refresh_fingerprint = str(item.get("refresh_token_fingerprint") or "")
            if email in email_members:
                email_members[email].append(item)
            if subject in subject_members:
                subject_members[subject].append(item)
            if refresh_fingerprint in refresh_members:
                refresh_members[refresh_fingerprint].append(item)

        email_groups: list[dict[str, Any]] = []
        for email, members in email_members.items():
            if len(members) < 2:
                continue
            refresh_fingerprints = {
                str(item.get("refresh_token_fingerprint") or "")
                for item in members
                if str(item.get("refresh_token_fingerprint") or "")
            }
            signals = ["same_email"]
            if len(refresh_fingerprints) == 1:
                signals.append("same_refresh_token")
            elif len(refresh_fingerprints) > 1:
                signals.append("different_refresh_tokens")
            if len({str(item.get("created_at") or "") for item in members}) > 1:
                signals.append("different_created_at")
            if sum(
                1 for item in members if str(item.get("source_type") or "") == "codex"
            ) >= 2:
                signals.append("multiple_codex_imports")
            group = self._duplicate_group_payload(
                email,
                "email",
                members,
                signals=signals,
                max_members=max_members,
            )
            group["suspected_import_duplicate"] = (
                "same_refresh_token" in signals
                or "multiple_codex_imports" in signals
            )
            email_groups.append(group)
        email_groups.sort(key=lambda group: (-int(group["count"]), str(group["email"])))

        subject_groups: list[dict[str, Any]] = []
        for subject, members in subject_members.items():
            if len(members) < 2:
                continue
            refresh_fingerprints = {
                str(item.get("refresh_token_fingerprint") or "")
                for item in members
                if str(item.get("refresh_token_fingerprint") or "")
            }
            signals = ["same_upstream_subject"]
            if len(refresh_fingerprints) == 1:
                signals.append("same_refresh_token")
            if any(not str(item.get("email") or "") for item in members):
                signals.append("missing_email")
            group = self._duplicate_group_payload(
                subject,
                "subject",
                members,
                signals=signals,
                max_members=max_members,
            )
            group["suspected_import_duplicate"] = "same_refresh_token" in signals
            subject_groups.append(group)
        subject_groups.sort(
            key=lambda group: (-int(group["count"]), str(group["upstream_subject"]))
        )

        refresh_groups: list[dict[str, Any]] = []
        for fingerprint, members in refresh_members.items():
            if len(members) < 2:
                continue
            group = self._duplicate_group_payload(
                fingerprint,
                "refresh_token",
                members,
                signals=["shared_refresh_token"],
                max_members=max_members,
            )
            group["suspected_import_duplicate"] = True
            refresh_groups.append(group)
        refresh_groups.sort(
            key=lambda group: (
                -int(group["count"]),
                str(group["refresh_token_fingerprint"]),
            )
        )

        email_items = email_groups[:max_groups]
        subject_items = subject_groups[:max_groups]
        refresh_items = refresh_groups[:max_groups]
        suspected_import_groups = [
            group for group in email_groups if group["suspected_import_duplicate"]
        ]
        return {
            "read_only": True,
            "generated_at": self._now(),
            "total_accounts": total,
            "accounts_with_email": with_email,
            "missing_email": missing_email,
            "missing_identity": missing_identity,
            "duplicate_email": {
                "group_count": len(email_groups),
                "redundant_accounts": sum(
                    int(group["count"]) - 1 for group in email_groups
                ),
                "suspected_import_groups": len(suspected_import_groups),
                "suspected_import_accounts": sum(
                    int(group["count"]) - 1 for group in suspected_import_groups
                ),
                "truncated": len(email_groups) > len(email_items),
                "items": email_items,
            },
            "duplicate_upstream_subject": {
                "group_count": len(subject_groups),
                "redundant_accounts": sum(
                    int(group["count"]) - 1 for group in subject_groups
                ),
                "truncated": len(subject_groups) > len(subject_items),
                "items": subject_items,
            },
            "duplicate_refresh_token": {
                "group_count": len(refresh_groups),
                "redundant_accounts": sum(
                    int(group["count"]) - 1 for group in refresh_groups
                ),
                "truncated": len(refresh_groups) > len(refresh_items),
                "items": refresh_items,
            },
            "missing_email_sample": missing_email_sample,
            "missing_identity_sample": missing_identity_sample,
            "limits": {"max_groups": max_groups, "max_members": max_members},
        }

    def scan_credential_cleanup_candidates(
        self,
        *,
        limit: int = 200,
    ) -> dict[str, Any]:
        """Read-only AT/RT cleanup classification.

        ``strict_cleanup_ids`` contains accounts whose AT is invalid **and**
        whose present RT is confirmed invalid: the only bucket that satisfies
        "both tokens are dead". ``refresh_missing_ids`` contains accounts with
        an invalid AT and no RT at all; they are unrecoverable but deleting
        them is a policy decision, not a confirmed RT failure.
        """
        limit = max(0, min(5_000, int(limit)))
        availability = {"usable": 0, "recoverable": 0, "unavailable": 0}
        total = 0
        access_invalid = 0
        rt_present_unconfirmed = 0
        strict_cleanup = 0
        refresh_missing = 0
        strict_ids: list[str] = []
        missing_ids: list[str] = []
        sample: list[dict[str, Any]] = []

        for account in self.iter_account_records():
            total += 1
            projection = self._credential_projection(account)
            availability[projection.status] += 1
            if projection.access.status != "invalid":
                continue
            access_invalid += 1
            management_id = str(account.get("management_id") or "").strip()
            if projection.refresh_status == "invalid":
                strict_cleanup += 1
                if management_id and len(strict_ids) < limit:
                    strict_ids.append(management_id)
                reason = "at_invalid_rt_invalid"
            elif projection.refresh_status == "missing":
                refresh_missing += 1
                if management_id and len(missing_ids) < limit:
                    missing_ids.append(management_id)
                reason = "at_invalid_rt_missing"
            else:
                rt_present_unconfirmed += 1
                reason = "at_invalid_rt_valid"
            if len(sample) < 20:
                sample.append(
                    {
                        "account_id": management_id,
                        "status": str(account.get("status") or ""),
                        "reason": reason,
                        "access": projection.access.status,
                        "refresh": projection.refresh_status,
                        "has_email": bool(str(account.get("email") or "").strip()),
                    }
                )
        return {
            "read_only": True,
            "generated_at": self._now(),
            "total_accounts": total,
            "availability": availability,
            "access_invalid": access_invalid,
            "rt_present_unconfirmed": rt_present_unconfirmed,
            "strict_cleanup": strict_cleanup,
            "refresh_missing": refresh_missing,
            "strict_cleanup_ids": strict_ids,
            "refresh_missing_ids": missing_ids,
            "id_list_truncated": (
                strict_cleanup > len(strict_ids) or refresh_missing > len(missing_ids)
            ),
            "limit": limit,
            "sample": sample,
        }

    def list_limited_tokens(self) -> list[str]:
        """Limited accounts worth re-checking now.

        An account whose upstream ``restore_at`` is still in the future cannot
        have its image quota back yet. Re-querying it every watcher cycle only
        produced a remote call plus a locked database write per account, which
        showed up as queueing on the account lock.
        """
        self._refresh_accounts_snapshot_if_stale()
        now = datetime.now(timezone.utc)
        with self._lock:
            # Snapshot only; filtering runs outside the lock so a full-pool
            # scan never blocks account updates for seconds.
            snapshot = list(self._accounts.values())
        tokens: list[str] = []
        for item in snapshot:
            if item.get("status") != "限流":
                continue
            if item.get("last_remote_check_result") == "pending":
                continue
            token = item.get("access_token") or ""
            if not token:
                continue
            restore_at = self._parse_time(item.get("restore_at"))
            if restore_at is not None:
                if restore_at.tzinfo is None:
                    restore_at = restore_at.replace(tzinfo=timezone.utc)
                if restore_at > now:
                    continue
            tokens.append(token)
        return tokens

    def list_quota_probe_tokens(self, limit: int = 10) -> list[str]:
        """Pick accounts whose quota is 0 and whose check is stale/never done.

        Image selection skips known quota<=0 accounts, so without this probe an
        account whose daily image quota reset would never be discovered again.
        """
        self._refresh_accounts_snapshot_if_stale()
        now = datetime.now(timezone.utc)
        with self._lock:
            snapshot = list(self._accounts.values())
        tokens: list[str] = []
        for item in snapshot:
            token = str(item.get("access_token") or "").strip()
            if not token or item.get("status") != "正常":
                continue
            if bool(item.get("image_quota_unknown")):
                continue
            try:
                quota = int(item.get("quota") or 0)
            except (TypeError, ValueError):
                quota = 0
            if quota > 0:
                continue
            stamp = str(item.get("last_remote_checked_at") or "").strip()
            if stamp:
                try:
                    checked = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    if checked.tzinfo is None:
                        checked = checked.replace(tzinfo=timezone.utc)
                    if (now - checked).total_seconds() < 12 * 3600:
                        continue
                except ValueError:
                    pass
            tokens.append(token)
            if len(tokens) >= max(1, int(limit)):
                break
        return tokens

    def note_slow_image_stream(
        self,
        token: str,
        stream_ms: int,
        *,
        threshold_ms: int = 60_000,
        cooldown_seconds: int = 1_800,
    ) -> bool:
        """Temporarily cool down an account after a very slow image stream."""
        if int(stream_ms or 0) < threshold_ms:
            return False
        with self._lock:
            account = self._accounts.get(str(token or ""))
            if account is None:
                return False
            account["last_image_stream_ms"] = int(stream_ms)
            account["image_slow_until_ts"] = time.time() + max(60, int(cooldown_seconds))
            try:
                self._save_account_row_locked(str(token))
            except Exception:
                return False
        return True

    def mark_memory_state(
        self,
        access_token: str,
        *,
        settings: dict[str, Any] | None,
        success: bool,
        error: str = "",
        state: str | None = None,
    ) -> dict[str, Any] | None:
        """Persist the per-account memory probe/toggle/clear result."""
        current = self.get_account(access_token) or {}
        now = datetime.now(timezone.utc).isoformat()
        resolved_state = str(state or "").strip().lower()
        if resolved_state not in {"disabled", "enabled", "cleared"}:
            resolved_state = "disabled" if success else "failed"
        updates: dict[str, Any] = {
            "memory_settings": settings or {},
            "memory_disable_error": str(error or ""),
            "memory_state": resolved_state,
            "memory_disable_result": (
                "disabled" if resolved_state == "disabled"
                else "failed" if resolved_state == "failed"
                else resolved_state
            ),
        }
        if resolved_state == "disabled":
            updates["memory_disabled_at"] = now
        elif resolved_state == "enabled":
            updates["memory_enabled_at"] = now
            updates["memory_disabled_at"] = None
        elif resolved_state == "cleared":
            updates["memory_cleared_at"] = now
        elif current.get("memory_disabled_at"):
            updates["memory_disabled_at"] = current.get("memory_disabled_at")
        return self.update_account(access_token, updates, quiet=True)

    def note_image_generation_failure(
        self,
        token: str,
        failure_code: str,
        *,
        cooldown_seconds: int = 600,
    ) -> bool:
        """Temporarily skip an account after a proven image-generation failure."""
        normalized = str(failure_code or "")
        if normalized not in {"no_image_generated", "image_quota_exhausted"}:
            return False
        with self._lock:
            account = self._accounts.get(str(token or ""))
            if account is None:
                return False
            account["last_image_failure"] = normalized
            account["image_fail_until_ts"] = time.time() + max(60, int(cooldown_seconds))
            try:
                self._save_account_row_locked(str(token))
            except Exception:
                return False
        return True

    def list_pending_auth_verification_tokens(self) -> list[str]:
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            return [
                token
                for item in self._accounts.values()
                if item.get("last_remote_check_result") == "pending"
                and item.get("pending_auth_scope") == "image"
                and (token := str(item.get("access_token") or "").strip())
            ]

    def resume_pending_auth_verifications(self) -> int:
        tokens = self.list_pending_auth_verification_tokens()
        scheduled = 0
        for token in tokens:
            if self._schedule_account_refresh_after_image_failure(token):
                scheduled += 1
        return scheduled

    def _auto_remove_tokens_locked(
        self,
        *,
        remove_invalid: bool,
        remove_rate_limited: bool,
    ) -> tuple[list[str], list[str]]:
        invalid_tokens = [
            token
            for item in self._accounts.values()
            if remove_invalid
               and item.get("status") == "异常"
               and (token := item.get("access_token") or "")
        ]
        rate_limited_tokens = [
            token
            for item in self._accounts.values()
            if remove_rate_limited
               and item.get("status") == "限流"
               and (token := item.get("access_token") or "")
        ]
        return invalid_tokens, rate_limited_tokens

    def preview_auto_remove_accounts(
        self,
        *,
        remove_invalid: bool | None = None,
        remove_rate_limited: bool | None = None,
    ) -> dict[str, Any]:
        self._refresh_accounts_snapshot_if_stale()
        remove_invalid = config.auto_remove_invalid_accounts if remove_invalid is None else bool(remove_invalid)
        remove_rate_limited = (
            config.auto_remove_rate_limited_accounts
            if remove_rate_limited is None
            else bool(remove_rate_limited)
        )
        with self._lock:
            invalid_tokens, rate_limited_tokens = self._auto_remove_tokens_locked(
                remove_invalid=remove_invalid,
                remove_rate_limited=remove_rate_limited,
            )
        invalid = len(invalid_tokens)
        rate_limited = len(rate_limited_tokens)
        return {
            "dry_run": True,
            "invalid": invalid,
            "rate_limited": rate_limited,
            "total_removed": invalid + rate_limited,
            "auto_remove_invalid_accounts": remove_invalid,
            "auto_remove_rate_limited_accounts": remove_rate_limited,
        }

    def cleanup_auto_remove_accounts(
        self,
        *,
        remove_invalid: bool | None = None,
        remove_rate_limited: bool | None = None,
    ) -> dict[str, Any]:
        self._refresh_accounts_snapshot_if_stale()
        remove_invalid = config.auto_remove_invalid_accounts if remove_invalid is None else bool(remove_invalid)
        remove_rate_limited = (
            config.auto_remove_rate_limited_accounts
            if remove_rate_limited is None
            else bool(remove_rate_limited)
        )
        with self._lock:
            invalid_tokens, rate_limited_tokens = self._auto_remove_tokens_locked(
                remove_invalid=remove_invalid,
                remove_rate_limited=remove_rate_limited,
            )

        target_tokens = list(dict.fromkeys([*invalid_tokens, *rate_limited_tokens]))
        result = self.delete_accounts(target_tokens, return_items=False) if target_tokens else {"removed": 0}
        removed = int(result.get("removed") or 0)
        if removed:
            log_service.add(
                LOG_TYPE_ACCOUNT,
                "按账号自动移除策略清理账号",
                {
                    "removed": removed,
                    "invalid": len(invalid_tokens),
                    "rate_limited": len(rate_limited_tokens),
                },
            )
        return {
            "dry_run": False,
            "invalid": len(invalid_tokens),
            "rate_limited": len(rate_limited_tokens),
            "total_removed": removed,
            "auto_remove_invalid_accounts": remove_invalid,
            "auto_remove_rate_limited_accounts": remove_rate_limited,
        }

    def list_normal_tokens(self) -> list[str]:
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            snapshot = list(self._accounts.values())
        return [
            token
            for item in snapshot
            if item.get("status") == "正常"
               and item.get("last_remote_check_result") != "pending"
               and (token := item.get("access_token") or "")
        ]

    @classmethod
    def _pool_health_freshness_seconds(cls, freshness_seconds: int | float | None = None) -> int:
        if freshness_seconds is not None:
            try:
                return max(60, int(float(freshness_seconds)))
            except (TypeError, ValueError):
                pass
        return max(60, int(config.refresh_account_interval_minute) * 60)

    @classmethod
    def _remote_check_is_fresh(cls, account: dict, now: datetime, freshness_seconds: int) -> bool:
        checked_at = cls._parse_time(account.get("last_remote_checked_at"))
        return checked_at is not None and (now - checked_at).total_seconds() <= freshness_seconds

    @classmethod
    def _remote_check_attempt_is_recent(cls, account: dict, now: datetime, freshness_seconds: int) -> bool:
        attempted_at = cls._parse_time(account.get("last_remote_check_attempt_at"))
        return attempted_at is not None and (now - attempted_at).total_seconds() <= freshness_seconds

    @classmethod
    def _pool_health_metrics_from_accounts(
        cls,
        accounts: list[dict],
        *,
        now: datetime,
        freshness_seconds: int,
    ) -> dict[str, Any]:
        local_normal = [
            item
            for item in accounts
            if cls._is_account_selectable(item, allow_limited=False)
        ]
        confirmed_normal = [
            item
            for item in local_normal
            if cls._remote_check_is_fresh(item, now, freshness_seconds)
        ]
        latest_checked_at = ""
        for item in accounts:
            checked_at = cls._parse_time(item.get("last_remote_checked_at"))
            if checked_at is None:
                continue
            value = checked_at.isoformat()
            if not latest_checked_at or value > latest_checked_at:
                latest_checked_at = value
        return {
            "current_quota": sum(
                int(item.get("quota") or 0)
                for item in confirmed_normal
                if not item.get("image_quota_unknown")
            ),
            "current_available": len(confirmed_normal),
            "estimated_quota": sum(
                int(item.get("quota") or 0)
                for item in local_normal
                if not item.get("image_quota_unknown")
            ),
            "estimated_available": len(local_normal),
            "unconfirmed_available": max(0, len(local_normal) - len(confirmed_normal)),
            "unknown_quota_count": sum(1 for item in confirmed_normal if item.get("image_quota_unknown")),
            "pool_freshness_seconds": freshness_seconds,
            "pool_last_checked_at": latest_checked_at,
        }

    @classmethod
    def _pool_health_stale_tokens(
        cls,
        accounts: list[dict],
        *,
        now: datetime,
        freshness_seconds: int,
    ) -> list[str]:
        stale: list[tuple[float, str]] = []
        for item in accounts:
            token = str(item.get("access_token") or "").strip()
            if not token or item.get("status") != "正常":
                continue
            if cls._remote_check_is_fresh(item, now, freshness_seconds):
                continue
            if cls._remote_check_attempt_is_recent(item, now, freshness_seconds):
                continue
            checked_at = cls._parse_time(item.get("last_remote_checked_at"))
            sort_key = checked_at.timestamp() if checked_at else 0.0
            stale.append((sort_key, token))
        stale.sort(key=lambda item: item[0])
        return [token for _, token in stale]

    @staticmethod
    def _pool_health_target_reached(
        metrics: dict[str, Any],
        *,
        target_quota: int | None = None,
        target_available: int | None = None,
    ) -> bool:
        if target_quota is not None and int(metrics.get("current_quota") or 0) >= max(1, int(target_quota)):
            return True
        if target_available is not None and int(metrics.get("current_available") or 0) >= max(1, int(target_available)):
            return True
        return False

    def evaluate_account_pool(
        self,
        *,
        refresh_stale: bool = False,
        target_quota: int | None = None,
        target_available: int | None = None,
        freshness_seconds: int | float | None = None,
    ) -> dict[str, Any]:
        """Return registration-facing account pool metrics from remotely confirmed data.

        Local quota is useful for display, but registration stop decisions should not
        trust stale local quota.  This method refreshes only stale normal accounts,
        in small batches, until the requested target is confirmed or no eligible
        stale accounts remain.
        """
        freshness = self._pool_health_freshness_seconds(freshness_seconds)
        refreshed = 0
        refresh_errors: list[dict[str, Any]] = []

        while True:
            now = datetime.now(timezone.utc)
            accounts = self.list_accounts()
            metrics = self._pool_health_metrics_from_accounts(
                accounts,
                now=now,
                freshness_seconds=freshness,
            )
            if not refresh_stale:
                return {
                    **metrics,
                    "pool_refreshed": refreshed,
                    "pool_refresh_errors": refresh_errors,
                }
            if self._pool_health_target_reached(
                metrics,
                target_quota=target_quota,
                target_available=target_available,
            ):
                return {
                    **metrics,
                    "pool_refreshed": refreshed,
                    "pool_refresh_errors": refresh_errors,
                }

            stale_tokens = self._pool_health_stale_tokens(
                accounts,
                now=now,
                freshness_seconds=freshness,
            )
            if not stale_tokens:
                return {
                    **metrics,
                    "pool_refreshed": refreshed,
                    "pool_refresh_errors": refresh_errors,
                }

            batch = stale_tokens[: self._POOL_HEALTH_REFRESH_BATCH_SIZE]
            result = self.sync_accounts_and_quota(batch)
            refreshed += int(result.get("synced") or 0)
            refresh_errors.extend(result.get("errors") or [])

    @staticmethod
    def _account_payload_token(item: dict) -> str:
        return str(item.get("access_token") or item.get("accessToken") or "").strip()

    @staticmethod
    def _refresh_token_aware_updates(
        current: dict[str, Any],
        updates: dict[str, Any],
        *,
        preserve_lifecycle: bool = False,
    ) -> dict[str, Any]:
        normalized = dict(updates)
        if "refresh_token" not in normalized:
            return normalized
        current_refresh_token = str(current.get("refresh_token") or "").strip()
        updated_refresh_token = str(normalized.get("refresh_token") or "").strip()
        if preserve_lifecycle and "refresh_token_invalid_at" in normalized:
            normalized["refresh_token_invalid_at"] = (
                normalized.get("refresh_token_invalid_at") or None
            )
        else:
            normalized["refresh_token_invalid_at"] = (
                None
                if updated_refresh_token != current_refresh_token
                else current.get("refresh_token_invalid_at") or None
            )
        return normalized

    @staticmethod
    def _prepare_account_payload(item: dict, *, restore: bool = False) -> dict | None:
        if not isinstance(item, dict):
            return None
        access_token = AccountService._account_payload_token(item)
        if not access_token:
            return None
        payload = dict(item)
        payload.pop("accessToken", None)
        if not restore:
            payload.pop("management_id", None)
            payload.pop("access_token_fingerprints", None)
        payload["access_token"] = access_token
        # CPA/Codex 导出文件里的 `type=codex` 是导出格式，不是号池套餐类型。
        if str(payload.get("type") or "").strip().lower() == "codex":
            payload["export_type"] = "codex"
            payload["source_type"] = "codex"
            payload.pop("type", None)
        if str(payload.get("export_type") or "").strip().lower() == "codex":
            payload["source_type"] = "codex"
        if payload.get("plan_type") and not payload.get("type"):
            payload["type"] = str(payload.get("plan_type") or "").strip()
        return payload

    def _run_proxy_assignment_mutation(
        self,
        references: list[tuple[object, object]],
        mutation: Callable[[list[str]], Any],
    ) -> Any:
        if self._proxy_reference_mutation is not None:
            return self._proxy_reference_mutation(references, mutation)

        normalized: list[str] = []
        for value, legacy_group_id in references:
            raw = str(value or "").strip()
            if not raw and str(legacy_group_id or "").strip():
                raw = f"group:{str(legacy_group_id).strip()}"
            if raw.lower() == "global":
                raw = ""
            normalized.append(raw)
        return mutation(normalized)

    def _with_normalized_proxy_updates(
        self,
        updates: dict[str, Any],
        mutation: Callable[[dict[str, Any]], Any],
    ) -> Any:
        normalized_updates = dict(updates)
        if "proxy" not in normalized_updates and "proxy_group_id" not in normalized_updates:
            return mutation(normalized_updates)

        return self._run_proxy_assignment_mutation(
            [
                (
                    normalized_updates.get("proxy"),
                    normalized_updates.get("proxy_group_id"),
                )
            ],
            lambda normalized: mutation({
                **normalized_updates,
                "proxy": normalized[0],
            }),
        )

    @account_processing_batch
    def add_account_items(
        self,
        items: list[dict],
        return_items: bool = True,
        *,
        restore: bool = False,
        return_item_results: bool = False,
    ) -> dict:
        source_items = list(items)
        payloads: list[dict] = []
        payload_indices: list[int] = []
        for index, item in enumerate(source_items):
            payload = self._prepare_account_payload(item, restore=restore)
            if payload is None:
                continue
            payloads.append(payload)
            payload_indices.append(index)

        result = self._add_account_payloads(
            payloads,
            return_items=return_items,
            preserve_lifecycle=restore,
            return_item_results=return_item_results,
        )
        if return_item_results:
            aligned_results = ["invalid"] * len(source_items)
            for index, status in zip(
                payload_indices,
                result.get("item_results") or [],
            ):
                aligned_results[index] = status
            result["item_results"] = aligned_results
        return result

    @account_processing_batch
    def add_accounts(self, tokens: list[str], source_type: str = "web", return_items: bool = True) -> dict:
        tokens = list(dict.fromkeys(token for token in tokens if token))
        if not tokens:
            return {"added": 0, "skipped": 0, "items": self.list_accounts() if return_items else []}
        return self._add_account_payloads([
            {"access_token": token, "source_type": self._normalize_source_type(source_type)}
            for token in tokens
        ], return_items=return_items)

    def add_account_payloads(self, payloads: list[dict], *, return_items: bool = False) -> dict:
        """Add full account payloads so refresh_token / identity are preserved."""
        return self._add_account_payloads(payloads, return_items=return_items)

    def backfill_account_credentials(self, payloads: list[dict]) -> int:
        """Fill missing refresh_token/profile fields on already imported rows.

        Early legacy-sync versions copied only the access token, leaving the
        pool without refresh tokens; repair those rows without touching
        credentials that are already present.
        """
        updated = 0
        with self._lock:
            for payload in payloads or []:
                if not isinstance(payload, dict):
                    continue
                token = self._account_payload_token(payload)
                if not token:
                    continue
                account = self._accounts.get(token)
                if account is None:
                    continue
                changed = False
                for field in (
                    "refresh_token",
                    "email",
                    "management_id",
                    "user_id",
                    "type",
                    "plan_type",
                    "source_type",
                ):
                    if not str(account.get(field) or "").strip() and payload.get(field):
                        account[field] = payload[field]
                        changed = True
                if not changed:
                    continue
                self._accounts[token] = account
                try:
                    self._save_account_row_locked(token)
                    updated += 1
                except Exception:
                    continue
        return updated

    def _add_account_payloads(
        self,
        payloads: list[dict],
        return_items: bool = True,
        *,
        preserve_lifecycle: bool = False,
        return_item_results: bool = False,
    ) -> dict:
        proxy_payload_indices = [
            index
            for index, payload in enumerate(payloads)
            if (
                isinstance(payload, dict)
                and self._account_payload_token(payload)
                and ("proxy" in payload or "proxy_group_id" in payload)
            )
        ]
        if not proxy_payload_indices:
            return self._add_account_payloads_impl(
                payloads,
                return_items=return_items,
                preserve_lifecycle=preserve_lifecycle,
                return_item_results=return_item_results,
            )

        references = [
            (
                payloads[index].get("proxy"),
                payloads[index].get("proxy_group_id"),
            )
            for index in proxy_payload_indices
        ]

        def persist(normalized_references: list[str]) -> dict:
            normalized_payloads = list(payloads)
            for index, proxy in zip(proxy_payload_indices, normalized_references):
                normalized_payloads[index] = {
                    **payloads[index],
                    "proxy": proxy,
                }
            return self._add_account_payloads_impl(
                normalized_payloads,
                return_items=return_items,
                preserve_lifecycle=preserve_lifecycle,
                return_item_results=return_item_results,
            )

        return self._run_proxy_assignment_mutation(references, persist)

    def _add_account_payloads_impl(
        self,
        payloads: list[dict],
        return_items: bool = True,
        *,
        preserve_lifecycle: bool = False,
        return_item_results: bool = False,
    ) -> dict:
        deduped: dict[str, dict] = {}
        input_tokens: list[str] = []
        for payload in payloads:
            if not isinstance(payload, dict):
                continue
            access_token = self._account_payload_token(payload)
            if not access_token:
                continue
            input_tokens.append(access_token)
            current = deduped.get(access_token, {})
            deduped[access_token] = {**current, **payload, "access_token": access_token}

        if not deduped:
            result = {
                "added": 0,
                "skipped": 0,
                "items": self.list_accounts() if return_items else [],
            }
            if return_item_results:
                result["item_results"] = []
            return result

        with self._lock:
            added = 0
            skipped = len(input_tokens) - len(deduped)
            outcomes_by_token: dict[str, str] = {}
            management_id_owners = {
                str(account.get("management_id") or "").strip().lower(): token
                for token, account in self._accounts.items()
                if str(account.get("management_id") or "").strip()
            }
            # One shared set for the whole batch.  Rebuilding it per incoming
            # account made bulk import O(n^2) and was the reason a 200k import
            # never finished.
            used_management_ids = set(management_id_owners)
            token_fingerprint_owners = {
                fingerprint: token
                for token, account in self._accounts.items()
                for fingerprint in self._normalize_access_token_fingerprints(
                    account.get("access_token_fingerprints"),
                    token,
                )
            }
            for access_token, payload in deduped.items():
                resolved_token = self._resolve_access_token_locked(access_token)
                if resolved_token != access_token and resolved_token in self._accounts:
                    # Re-importing a rotated token must not recreate its former account.
                    skipped += 1
                    outcomes_by_token[access_token] = "skipped"
                    continue
                current = self._accounts.get(access_token)
                if current is None:
                    fingerprint = self._access_token_fingerprint(access_token)
                    if fingerprint in token_fingerprint_owners:
                        # Persisted fingerprints preserve rotation lineage across restarts
                        # without storing former access tokens.
                        skipped += 1
                        outcomes_by_token[access_token] = "skipped"
                        continue
                    added += 1
                    outcomes_by_token[access_token] = "added"
                    current = {"created_at": self._now()}
                else:
                    skipped += 1
                    outcomes_by_token[access_token] = "skipped"
                incoming = self._refresh_token_aware_updates(
                    current,
                    payload,
                    preserve_lifecycle=preserve_lifecycle,
                )
                if not incoming.get("created_at"):
                    incoming.pop("created_at", None)
                merged = {**current, **incoming, "access_token": access_token}
                incoming_type = self._normalize_account_type(incoming.get("type"))
                current_type = self._normalize_account_type(current.get("type"))
                merged["type"] = incoming_type or current_type
                account = self._normalize_account(merged)
                if account is not None:
                    previous_management_id = str(
                        current.get("management_id") or ""
                    ).strip().lower()
                    if (
                        previous_management_id
                        and management_id_owners.get(previous_management_id) == access_token
                    ):
                        management_id_owners.pop(previous_management_id, None)
                        used_management_ids.discard(previous_management_id)
                    account["management_id"] = self._unique_management_id(
                        access_token,
                        account.get("management_id"),
                        used_management_ids,
                    )
                    self._accounts[access_token] = account
                    management_id_owners[account["management_id"]] = access_token
                    used_management_ids.add(account["management_id"])
                    for fingerprint in account["access_token_fingerprints"]:
                        token_fingerprint_owners[fingerprint] = access_token
            conflict_existing_tokens: set[str] = set()
            self._save_accounts(
                conflict_existing_tokens=conflict_existing_tokens,
            )
            for access_token in conflict_existing_tokens:
                if outcomes_by_token.get(access_token) != "added":
                    continue
                outcomes_by_token[access_token] = "skipped"
                added = max(0, added - 1)
                skipped += 1
            if added:
                self._cumulative_total += added
                self._save_cumulative_total()
            items = [dict(item) for item in self._accounts.values()] if return_items else []
            log_service.add(LOG_TYPE_ACCOUNT, f"新增 {added} 个账号，跳过 {skipped} 个",
                            {"added": added, "skipped": skipped})
        result = {"added": added, "skipped": skipped, "items": items}
        added_tokens = [
            access_token
            for access_token, outcome in outcomes_by_token.items()
            if outcome == "added"
        ]
        if added_tokens:
            try:
                from services.account_memory_service import account_memory_service

                account_memory_service.schedule_tokens(added_tokens)
            except Exception as exc:
                logger.warning({
                    "event": "account_memory_auto_disable_schedule_failed",
                    "count": len(added_tokens),
                    "error": diagnostic_excerpt(exc, 300),
                })
        if return_item_results:
            seen_tokens: set[str] = set()
            item_results: list[str] = []
            for access_token in input_tokens:
                if access_token in seen_tokens:
                    item_results.append("skipped")
                    continue
                seen_tokens.add(access_token)
                item_results.append(outcomes_by_token.get(access_token, "skipped"))
            result["item_results"] = item_results
        return result

    @staticmethod
    def _report_account_mutation_progress(
        callback: Callable[[str, int], None] | None,
        stage: str,
        total: int,
    ) -> None:
        if callback is None:
            return
        try:
            callback(stage, max(0, int(total)))
        except Exception:
            # Progress reporting is auxiliary and must not change a committed mutation.
            pass

    @staticmethod
    def _add_account_log_best_effort(message: str, data: dict[str, Any]) -> None:
        try:
            log_service.add(LOG_TYPE_ACCOUNT, message, data)
        except Exception:
            # The account store is authoritative; diagnostics must not reverse its outcome.
            pass

    @account_processing_batch
    def delete_accounts(
        self,
        tokens: list[str],
        return_items: bool = True,
        *,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> dict:
        requested_tokens = list(dict.fromkeys(token for token in tokens if token))
        if not requested_tokens:
            return {
                "removed": 0,
                "removed_ids": [],
                "missing_tokens": [],
                "items": self.list_accounts() if return_items else [],
            }
        self._report_account_mutation_progress(
            progress_callback,
            "prepare_accounts",
            len(requested_tokens),
        )
        with self._lock:
            target_tokens: list[str] = []
            candidate_ids: list[str] = []
            missing_tokens: list[str] = []
            seen_tokens: set[str] = set()
            for requested_token in requested_tokens:
                access_token = self._resolve_access_token_locked(requested_token)
                if access_token in seen_tokens:
                    continue
                seen_tokens.add(access_token)
                account = self._accounts.get(access_token)
                if account is None:
                    missing_tokens.append(requested_token)
                    continue
                target_tokens.append(access_token)
                management_id = str(account.get("management_id") or "").strip().lower()
                if management_id:
                    candidate_ids.append(management_id)

            target_set = set(target_tokens)
            removed = sum(self._accounts.pop(token, None) is not None for token in target_tokens)
            if removed:
                self._report_account_mutation_progress(
                    progress_callback,
                    "save_accounts",
                    removed,
                )
                self._save_accounts()
                self._remove_account_runtime_state_locked(target_set)
                self._add_account_log_best_effort(
                    f"删除 {removed} 个账号",
                    {"removed": removed},
                )
            existing_ids = {
                str(account.get("management_id") or "").strip().lower()
                for account in self._accounts.values()
                if str(account.get("management_id") or "").strip()
            }
            removed_ids = [
                management_id
                for management_id in dict.fromkeys(candidate_ids)
                if management_id not in existing_ids
            ]
            items = [dict(item) for item in self._accounts.values()] if return_items else []
        self._report_account_mutation_progress(
            progress_callback,
            "publish_results",
            len(removed_ids),
        )
        return {
            "removed": len(removed_ids),
            "removed_ids": removed_ids,
            "missing_tokens": missing_tokens,
            "items": items,
        }

    def _normalized_account_update_locked(
            self,
            access_token: str,
            current: dict[str, Any],
            updates: dict[str, Any],
            *,
            preserve_disabled: bool = False,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Build one normalized account update while the account lock is held."""
        normalized_updates = self._refresh_token_aware_updates(current, updates)
        merged = {**current, **normalized_updates, "access_token": access_token}
        if (
            "proxy" in updates
            and str(current.get("proxy") or "").strip()
            != str(merged.get("proxy") or "").strip()
        ):
            self._scrub_diagnostic_secrets(merged, [current.get("proxy")])
        if preserve_disabled and current.get("status") == "禁用":
            merged["status"] = "禁用"
        if (
            current.get("status") == "限流"
            and "status" in updates
            and self._normalize_account_status(updates.get("status"), merged) == "正常"
            and "restore_at" not in updates
        ):
            merged["restore_at"] = None

        account = self._normalize_account(merged)
        if account is None:
            return None, None
        if account.get("status") == "异常" and config.auto_remove_invalid_accounts:
            return account, "自动移除异常账号"
        if account.get("status") == "限流" and config.auto_remove_rate_limited_accounts:
            return account, "自动移除额度耗尽账号"
        return account, None

    @account_processing_batch
    def update_accounts(
        self,
        access_tokens: list[str],
        updates: dict,
        *,
        quiet: bool = True,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> dict[str, Any]:
        if not updates or not any(str(token or "").strip() for token in access_tokens):
            return self._update_accounts_impl(
                access_tokens,
                updates,
                quiet=quiet,
                progress_callback=progress_callback,
            )
        return self._with_normalized_proxy_updates(
            updates,
            lambda normalized: self._update_accounts_impl(
                access_tokens,
                normalized,
                quiet=quiet,
                progress_callback=progress_callback,
            ),
        )

    def _update_accounts_impl(
        self,
        access_tokens: list[str],
        updates: dict,
        *,
        quiet: bool = True,
        progress_callback: Callable[[str, int], None] | None = None,
    ) -> dict[str, Any]:
        """Apply one update to many accounts with a single storage mutation."""
        requested_tokens = list(dict.fromkeys(
            str(token or "").strip() for token in access_tokens if str(token or "").strip()
        ))
        if not requested_tokens or not updates:
            return {
                "updated_ids": [],
                "removed_ids": [],
                "missing_tokens": requested_tokens,
            }

        self._report_account_mutation_progress(
            progress_callback,
            "prepare_accounts",
            len(requested_tokens),
        )

        with self._lock:
            requested: list[tuple[str, str]] = []
            missing_tokens: list[str] = []
            auto_remove_events: list[tuple[str, str]] = []
            seen_tokens: set[str] = set()
            changed = False

            for requested_token in requested_tokens:
                access_token = self._resolve_access_token_locked(requested_token)
                if access_token in seen_tokens:
                    continue
                seen_tokens.add(access_token)
                current = self._accounts.get(access_token)
                if current is None:
                    missing_tokens.append(requested_token)
                    continue

                management_id = str(current.get("management_id") or "").strip().lower()
                if not management_id:
                    missing_tokens.append(requested_token)
                    continue
                requested.append((access_token, management_id))

                account, auto_remove_message = self._normalized_account_update_locked(
                    access_token,
                    current,
                    updates,
                )
                if account is None:
                    missing_tokens.append(requested_token)
                    continue
                if auto_remove_message:
                    self._accounts.pop(access_token, None)
                    changed = True
                    auto_remove_events.append((access_token, auto_remove_message))
                    continue

                if account != current:
                    self._accounts[access_token] = account
                    changed = True

            if changed:
                self._report_account_mutation_progress(
                    progress_callback,
                    "save_accounts",
                    len(requested),
                )
                self._save_accounts()

            updated_ids = []
            removed_ids = []
            for token, management_id in requested:
                # Per-token membership check instead of rebuilding the whole
                # pool's management-id set (that scan dominated large batches).
                current_after = self._accounts.get(token)
                current_id = (
                    str(current_after.get("management_id") or "").strip().lower()
                    if current_after
                    else ""
                )
                if current_after is not None and current_id == management_id:
                    updated_ids.append(management_id)
                else:
                    removed_ids.append(management_id)
            removed_tokens = {
                token
                for token, management_id in requested
                if management_id in removed_ids
            }
            if removed_tokens:
                self._remove_account_runtime_state_locked(removed_tokens)

            for access_token, message in auto_remove_events:
                self._add_account_log_best_effort(
                    message,
                    {"token": anonymize_token(access_token)},
                )
            if not quiet and updated_ids:
                self._add_account_log_best_effort(
                    f"\u6279\u91cf\u66f4\u65b0 {len(updated_ids)} \u4e2a\u8d26\u53f7",
                    {"updated": len(updated_ids)},
                )

            result = {
                "updated_ids": updated_ids,
                "removed_ids": removed_ids,
                "missing_tokens": missing_tokens,
            }
        self._report_account_mutation_progress(
            progress_callback,
            "publish_results",
            len(result["updated_ids"]) + len(result["removed_ids"]),
        )
        return result

    @staticmethod
    def _remote_check_marker(
        account: dict | None,
    ) -> _RemoteCheckMarker:
        item = account or {}
        return (
            str(item.get("last_token_refresh_at") or ""),
            str(item.get("last_remote_check_result") or ""),
            str(item.get("last_remote_check_attempt_at") or ""),
            str(item.get("last_remote_check_event") or ""),
            item.get("pending_auth_remove_invalid"),
            str(item.get("pending_auth_scope") or ""),
            str(item.get("pending_auth_verification_id") or ""),
        )

    def update_account(
        self,
        access_token: str,
        updates: dict,
        quiet: bool = False,
        *,
        expected_access_token: str | None = None,
        expected_refresh_token: str | None = None,
        expected_remote_check_marker: _RemoteCheckMarker | None = None,
        preserve_disabled: bool = False,
    ) -> dict | None:
        if not access_token:
            return None
        return self._with_normalized_proxy_updates(
            updates,
            lambda normalized: self._update_account_impl(
                access_token,
                normalized,
                quiet,
                expected_access_token=expected_access_token,
                expected_refresh_token=expected_refresh_token,
                expected_remote_check_marker=expected_remote_check_marker,
                preserve_disabled=preserve_disabled,
            ),
        )

    def _update_account_impl(
        self,
        access_token: str,
        updates: dict,
        quiet: bool = False,
        *,
        expected_access_token: str | None = None,
        expected_refresh_token: str | None = None,
        expected_remote_check_marker: _RemoteCheckMarker | None = None,
        preserve_disabled: bool = False,
    ) -> dict | None:
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return None
            if expected_access_token is not None and (
                access_token != str(expected_access_token or "").strip()
            ):
                return None
            if expected_refresh_token is not None and (
                str(current.get("refresh_token") or "").strip()
                != str(expected_refresh_token or "").strip()
            ):
                return None
            if (
                expected_remote_check_marker is not None
                and self._remote_check_marker(current) != expected_remote_check_marker
            ):
                return None
            normalized_updates = dict(updates)
            replacement_access_token = str(
                normalized_updates.pop("access_token", "") or ""
            ).strip()
            next_access_token = replacement_access_token or access_token
            resolved_replacement = self._resolve_access_token_locked(next_access_token)
            if (
                next_access_token != access_token
                and resolved_replacement in self._accounts
                and resolved_replacement != access_token
            ):
                raise ValueError("access token already belongs to another account")

            refresh_token_changed = (
                "refresh_token" in normalized_updates
                and str(normalized_updates.get("refresh_token") or "").strip()
                != str(current.get("refresh_token") or "").strip()
            )
            credentials_changed = next_access_token != access_token or refresh_token_changed
            if credentials_changed:
                self._scrub_diagnostic_secrets(
                    normalized_updates,
                    [
                        current.get("access_token"),
                        current.get("refresh_token"),
                        current.get("id_token"),
                    ],
                )
                normalized_updates.update({
                    "last_token_refresh_error": None,
                    "last_token_refresh_error_at": None,
                    "last_refresh_error": None,
                    "last_refresh_error_at": None,
                    "last_invalid_at": None,
                    "invalid_count": 0,
                })
                if refresh_token_changed:
                    normalized_updates["refresh_token_invalid_at"] = None
                if current.get("last_remote_check_result") == "invalid":
                    normalized_updates.update({
                        "last_remote_check_result": None,
                        "last_remote_check_error": None,
                        "last_remote_check_error_at": None,
                        "last_remote_check_event": None,
                        "pending_auth_remove_invalid": None,
                        "pending_auth_scope": None,
                    })
                if current.get("status") == "异常":
                    normalized_updates["status"] = "正常"
            expected_generation = None
            if expected_access_token is not None and expected_refresh_token is not None:
                expected_generation = (
                    str(expected_access_token or "").strip(),
                    str(expected_refresh_token or "").strip(),
                    (
                        expected_remote_check_marker[0]
                        if expected_remote_check_marker is not None
                        else str(current.get("last_token_refresh_at") or "").strip()
                    ),
                )
            account, auto_remove_message = self._normalized_account_update_locked(
                next_access_token,
                current,
                normalized_updates,
                preserve_disabled=preserve_disabled,
            )
            if account is None:
                return None
            if auto_remove_message:
                self._accounts.pop(access_token, None)
                saved = self._save_accounts(
                    expected_credential_generation=expected_generation,
                )
                if not saved:
                    return None
                self._remove_account_runtime_state_locked({access_token})
                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    auto_remove_message,
                    {"token": anonymize_token(access_token)},
                )
                return None
            rotated = next_access_token != access_token
            alias_sources = {
                source
                for source in {access_token, *self._token_aliases}
                if self._resolve_access_token_locked(source) == access_token
            }
            if rotated:
                self._accounts.pop(access_token, None)
            self._accounts[next_access_token] = account
            saved = self._save_accounts(
                expected_credential_generation=expected_generation,
            )
            if not saved:
                return None
            if rotated:
                self._token_aliases.pop(next_access_token, None)
                self._move_account_runtime_token_locked(next_access_token, alias_sources)
                self._image_slot_condition.notify_all()
            access_token = self._resolve_access_token_locked(next_access_token)
            persisted = self._accounts.get(access_token)
            if persisted is None:
                return None
            if not quiet:
                log_service.add(LOG_TYPE_ACCOUNT, "更新账号",
                                {"token": anonymize_token(access_token), "status": persisted.get("status")})
            return dict(persisted)
        return None

    def _record_refresh_success(
        self,
        access_token: str,
        updates: dict,
        event: str = "fetch_remote_info",
        *,
        expected_access_token: str,
        expected_refresh_token: str,
        expected_remote_check_marker: _RemoteCheckMarker,
    ) -> dict | None:
        now = datetime.now(timezone.utc).isoformat()
        return self.update_account(
            access_token,
            {
                **updates,
                "invalid_count": 0,
                "last_invalid_at": None,
                "last_refresh_error": None,
                "last_refresh_error_at": None,
                "last_remote_checked_at": now,
                "last_remote_check_attempt_at": now,
                "last_remote_check_error": None,
                "last_remote_check_error_at": None,
                "last_remote_check_event": event,
                "last_remote_check_result": "ok",
                "pending_auth_remove_invalid": None,
                "pending_auth_scope": None,
            },
            quiet=True,
            expected_access_token=expected_access_token,
            expected_refresh_token=expected_refresh_token,
            expected_remote_check_marker=expected_remote_check_marker,
            preserve_disabled=True,
        )

    def _record_remote_check_error(
        self,
        access_token: str,
        event: str,
        error: str,
        *,
        expected_access_token: str | None = None,
        expected_refresh_token: str | None = None,
        expected_remote_check_marker: _RemoteCheckMarker | None = None,
    ) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return False
            if expected_access_token is not None and (
                access_token != str(expected_access_token or "").strip()
            ):
                return False
            if expected_refresh_token is not None and (
                str(current.get("refresh_token") or "").strip()
                != str(expected_refresh_token or "").strip()
            ):
                return False
            if (
                expected_remote_check_marker is not None
                and self._remote_check_marker(current) != expected_remote_check_marker
            ):
                return False
            expected_generation = None
            if expected_access_token is not None and expected_refresh_token is not None:
                expected_generation = (
                    str(expected_access_token or "").strip(),
                    str(expected_refresh_token or "").strip(),
                    (
                        expected_remote_check_marker[0]
                        if expected_remote_check_marker is not None
                        else str(current.get("last_token_refresh_at") or "").strip()
                    ),
                )
            next_item = dict(current)
            next_item["last_remote_check_attempt_at"] = now
            next_item["last_remote_check_error"] = str(error or "remote check failed")
            next_item["last_remote_check_error_at"] = now
            next_item["last_remote_check_event"] = event
            current_result = str(current.get("last_remote_check_result") or "")
            next_item["last_remote_check_result"] = (
                "invalid" if current_result == "invalid" else "error"
            )
            next_item["pending_auth_remove_invalid"] = None
            next_item["pending_auth_scope"] = None
            account = self._normalize_account(next_item)
            if account is not None:
                self._accounts[access_token] = account
                saved = self._save_accounts(
                    expected_credential_generation=expected_generation,
                )
                if not saved:
                    return False
                access_token = self._resolve_access_token_locked(access_token)
                persisted = self._accounts.get(access_token)
                return bool(
                    persisted is not None
                    and persisted.get("last_remote_check_error_at") == now
                )
        return False

    def _apply_invalid_token_state(
        self,
        access_token: str,
        event: str,
        error: str,
        *,
        remove: bool,
        expected_access_token: str | None = None,
        expected_refresh_token: str | None = None,
        expected_remote_check_marker: _RemoteCheckMarker | None = None,
        token_refresh_error: str | None = None,
        refresh_token_terminal: bool = False,
        quiet: bool = False,
    ) -> bool:
        _ = quiet
        now = datetime.now(timezone.utc)
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return False
            if expected_access_token is not None and (
                access_token != str(expected_access_token or "").strip()
            ):
                return False
            if expected_refresh_token is not None and (
                str(current.get("refresh_token") or "").strip()
                != str(expected_refresh_token or "").strip()
            ):
                return False
            if (
                expected_remote_check_marker is not None
                and self._remote_check_marker(current) != expected_remote_check_marker
            ):
                return False
            expected_generation = None
            if expected_access_token is not None and expected_refresh_token is not None:
                expected_generation = (
                    str(expected_access_token or "").strip(),
                    str(expected_refresh_token or "").strip(),
                    (
                        expected_remote_check_marker[0]
                        if expected_remote_check_marker is not None
                        else str(current.get("last_token_refresh_at") or "").strip()
                    ),
                )

            terminal_already_recorded = bool(
                refresh_token_terminal
                and token_refresh_error
                and current.get("refresh_token_invalid_at")
                and current.get("status") in {"异常", "禁用"}
                and current.get("last_remote_check_result") == "invalid"
                and str(current.get("last_token_refresh_error") or "")
                == str(token_refresh_error)
            )
            if terminal_already_recorded:
                account = dict(current)
                if not (remove and account.get("status") == "异常"):
                    return False
            else:
                next_item = dict(current)
                next_item["status"] = "禁用" if current.get("status") == "禁用" else "异常"
                next_item["quota"] = 0
                next_item["image_quota_unknown"] = True
                next_item["invalid_count"] = int(next_item.get("invalid_count") or 0) + 1
                next_item["last_invalid_at"] = now.isoformat()
                next_item["last_refresh_error"] = str(error or "invalid access token")
                next_item["last_refresh_error_at"] = now.isoformat()
                next_item["last_remote_checked_at"] = now.isoformat()
                next_item["last_remote_check_attempt_at"] = now.isoformat()
                next_item["last_remote_check_error"] = str(error or "invalid access token")
                next_item["last_remote_check_error_at"] = now.isoformat()
                next_item["last_remote_check_event"] = event
                next_item["last_remote_check_result"] = "invalid"
                next_item["pending_auth_remove_invalid"] = None
                next_item["pending_auth_scope"] = None
                if refresh_token_terminal and token_refresh_error:
                    next_item["last_token_refresh_error"] = str(token_refresh_error)
                    next_item["last_token_refresh_error_at"] = now.isoformat()
                    next_item["refresh_token_invalid_at"] = now.isoformat()
                account = self._normalize_account(next_item)
                if account is None:
                    return False

            final_status = str(account.get("status") or "异常")
            removed = bool(remove and final_status == "异常")
            if removed:
                self._accounts.pop(access_token, None)
            else:
                self._accounts[access_token] = account
            saved = self._save_accounts(
                expected_credential_generation=expected_generation,
            )
            if not saved:
                return False
            access_token = self._resolve_access_token_locked(access_token)
            persisted = self._accounts.get(access_token)
            if persisted is None:
                self._remove_account_runtime_state_locked({access_token})
                if not removed:
                    return False
            elif removed:
                return False
            else:
                account = dict(persisted)
                final_status = str(account.get("status") or "异常")

            diagnostic_error = self._credential_error_text(
                error,
                account,
                access_token=access_token,
            )
            if not terminal_already_recorded:
                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    "账号鉴权确认失效" if final_status == "异常" else "已禁用账号鉴权确认失效",
                    {
                        "source": event,
                        "token": anonymize_token(access_token),
                        "status": final_status,
                        "error": diagnostic_error,
                    },
                )
            if removed:
                log_service.add(LOG_TYPE_ACCOUNT, "删除 1 个账号", {"removed": 1})
                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    "自动移除异常账号",
                    {
                        "source": event,
                        "token": anonymize_token(access_token),
                        "error": diagnostic_error,
                    },
                )
        return removed

    @staticmethod
    def _mark_remote_check_pending(
        account: dict,
        event: str,
        now: str,
        *,
        remove_invalid: bool | None = None,
        scope: str = "account",
    ) -> None:
        existing_scope = (
            str(account.get("pending_auth_scope") or "").strip().lower()
            if account.get("last_remote_check_result") == "pending"
            else ""
        )
        account["last_remote_check_result"] = "pending"
        account["last_remote_check_event"] = event
        account["last_remote_check_attempt_at"] = now
        account["last_remote_check_error"] = None
        account["last_remote_check_error_at"] = None
        account["pending_auth_verification_id"] = uuid4().hex
        account["pending_auth_scope"] = (
            "account"
            if existing_scope == "account" or scope != "image"
            else "image"
        )
        requested_remove = (
            config.auto_remove_invalid_accounts
            if remove_invalid is None
            else bool(remove_invalid)
        )
        existing_remove = account.get("pending_auth_remove_invalid")
        account["pending_auth_remove_invalid"] = bool(existing_remove) or requested_remove

    def schedule_auth_verification(
        self,
        access_token: str,
        event: str,
        *,
        expected_access_token: str | None = None,
        expected_refresh_token: str | None = None,
        expected_last_token_refresh_at: str | None = None,
        remove_invalid: bool | None = None,
        scope: str = "account",
    ) -> bool:
        """Block a rejected account now and verify it in the background."""
        if not access_token:
            return False
        now = datetime.now(timezone.utc).isoformat()
        with self._image_slot_condition:
            access_token = self._resolve_access_token_locked(access_token)
            current = self._accounts.get(access_token)
            if current is None:
                return False
            expected_generation = None
            if expected_access_token is not None and expected_refresh_token is not None:
                expected_generation = (
                    str(expected_access_token or "").strip(),
                    str(expected_refresh_token or "").strip(),
                    str(expected_last_token_refresh_at or "").strip(),
                )
                if self._credential_generation(access_token, current) != expected_generation:
                    return False
            next_item = dict(current)
            self._mark_remote_check_pending(
                next_item,
                event,
                now,
                remove_invalid=remove_invalid,
                scope=scope,
            )
            account = self._normalize_account(next_item)
            if account is None:
                return False
            pending_verification_id = account.get("pending_auth_verification_id")
            self._accounts[access_token] = account
            saved = self._save_accounts(
                expected_credential_generation=expected_generation,
            )
            if not saved:
                return False
            access_token = self._resolve_access_token_locked(access_token)
            persisted = self._accounts.get(access_token)
            if (
                persisted is None
                or persisted.get("pending_auth_verification_id")
                != pending_verification_id
            ):
                return False
            pending_marker = self._remote_check_marker(persisted)
            pending_generation = self._credential_generation(access_token, persisted)
            self._image_slot_condition.notify_all()
        scheduled = self._schedule_account_refresh_after_image_failure(access_token, force=True)
        if not scheduled:
            self._record_remote_check_error(
                access_token,
                event,
                "Account verification could not be scheduled.",
                expected_access_token=pending_generation[0],
                expected_refresh_token=pending_generation[1],
                expected_remote_check_marker=pending_marker,
            )
        return scheduled

    def _verify_pending_auth(self, access_token: str, event: str) -> None:
        active_token, refresh_token, account = self._credential_snapshot(access_token)
        if not account or account.get("last_remote_check_result") != "pending":
            return
        pending_remove = account.get("pending_auth_remove_invalid")
        remove_invalid = (
            config.auto_remove_invalid_accounts
            if pending_remove is None
            else self._bool_value(pending_remove)
        )
        initial_token = active_token
        initial_refresh_token = refresh_token
        initial_remote_check_marker = self._remote_check_marker(account)

        from services.openai_backend_api import InvalidAccessTokenError, OpenAIBackendAPI

        def request_user_info(token: str) -> dict[str, Any]:
            with account_processing_slot():
                with OpenAIBackendAPI(token) as backend:
                    return backend.get_user_info()

        def reschedule_pending(token: str) -> None:
            current_token, _, current = self._credential_snapshot(token)
            if current and current.get("last_remote_check_result") == "pending":
                self._schedule_account_refresh_after_image_failure(
                    current_token,
                    force=True,
                )

        def snapshot_changed(token: str, refresh: str) -> bool:
            current_token, current_refresh, current = self._credential_snapshot(token)
            if current and (current_token, current_refresh) == (token, refresh):
                return False
            return True

        def request_user_info_for_snapshot(token: str, refresh: str) -> dict[str, Any]:
            try:
                result = request_user_info(token)
            except Exception as exc:
                if snapshot_changed(token, refresh):
                    raise RefreshCredentialsChangedError() from exc
                raise
            if snapshot_changed(token, refresh):
                raise RefreshCredentialsChangedError()
            return result

        try:
            result = request_user_info_for_snapshot(initial_token, initial_refresh_token)
        except InvalidAccessTokenError as initial_error:
            if not initial_refresh_token:
                self.handle_invalid_token(
                    initial_token,
                    event,
                    error=str(initial_error),
                    remove=remove_invalid,
                    expected_access_token=initial_token,
                    expected_refresh_token=initial_refresh_token,
                    expected_remote_check_marker=initial_remote_check_marker,
                )
                reschedule_pending(initial_token)
                return
            try:
                active_token = self.force_refresh_access_token(
                    initial_token,
                    event=f"{event}:auth_recovery",
                    raise_on_error=True,
                    image_scope=True,
                    expected_credentials=(
                        initial_token,
                        initial_refresh_token,
                        initial_remote_check_marker[0],
                    ),
                )
            except TerminalRefreshTokenError as refresh_error:
                self.handle_invalid_token(
                    initial_token,
                    event,
                    error=str(initial_error),
                    remove=remove_invalid,
                    expected_access_token=initial_token,
                    expected_refresh_token=initial_refresh_token,
                    expected_remote_check_marker=initial_remote_check_marker,
                    token_refresh_error=str(refresh_error),
                    refresh_token_terminal=True,
                )
                reschedule_pending(initial_token)
                return
            except Exception as exc:
                self._record_remote_check_error(
                    initial_token,
                    event,
                    str(exc),
                    expected_access_token=initial_token,
                    expected_refresh_token=initial_refresh_token,
                    expected_remote_check_marker=initial_remote_check_marker,
                )
                reschedule_pending(initial_token)
                return

            verification_token, verification_refresh_token, verification_account = (
                self._credential_snapshot(active_token)
            )
            if not verification_account:
                return
            verification_remote_check_marker = self._remote_check_marker(verification_account)
            try:
                result = request_user_info_for_snapshot(
                    verification_token,
                    verification_refresh_token,
                )
            except InvalidAccessTokenError as exc:
                self.handle_invalid_token(
                    verification_token,
                    event,
                    error=str(exc),
                    remove=remove_invalid,
                    expected_access_token=verification_token,
                    expected_refresh_token=verification_refresh_token,
                    expected_remote_check_marker=verification_remote_check_marker,
                )
                reschedule_pending(verification_token)
                return
            except Exception as exc:
                self._record_remote_check_error(
                    verification_token,
                    event,
                    str(exc),
                    expected_access_token=verification_token,
                    expected_refresh_token=verification_refresh_token,
                    expected_remote_check_marker=verification_remote_check_marker,
                )
                reschedule_pending(verification_token)
                return

            self._record_refresh_success(
                verification_token,
                result,
                event,
                expected_access_token=verification_token,
                expected_refresh_token=verification_refresh_token,
                expected_remote_check_marker=verification_remote_check_marker,
            )
            reschedule_pending(verification_token)
            return
        except Exception as exc:
            self._record_remote_check_error(
                initial_token,
                event,
                str(exc),
                expected_access_token=initial_token,
                expected_refresh_token=initial_refresh_token,
                expected_remote_check_marker=initial_remote_check_marker,
            )
            reschedule_pending(initial_token)
            return

        self._record_refresh_success(
            initial_token,
            result,
            event,
            expected_access_token=initial_token,
            expected_refresh_token=initial_refresh_token,
            expected_remote_check_marker=initial_remote_check_marker,
        )
        reschedule_pending(initial_token)

    def _refresh_account_after_image_failure(self, access_token: str) -> None:
        try:
            account = self.get_account(access_token) or {}
            event = str(account.get("last_remote_check_event") or "account_failure")
            if account.get("last_remote_check_result") == "pending":
                if account.get("pending_auth_scope") == "image":
                    self._verify_pending_auth(access_token, event)
                else:
                    self.fetch_remote_info(access_token, event)
            else:
                self.fetch_remote_info(access_token, event)
        except Exception as exc:
            account = self.get_account(access_token)
            log_service.add(
                LOG_TYPE_ACCOUNT,
                "鉴权失败后核验账号失败",
                {
                    "token": anonymize_token(access_token),
                    "error": self._credential_error_text(
                        exc,
                        account,
                        access_token=access_token,
                    ),
                },
            )

    def _schedule_account_refresh_after_image_failure(self, access_token: str, *, force: bool = False) -> bool:
        if not access_token:
            return False
        now = time.monotonic()
        with self._lock:
            access_token = self._resolve_access_token_locked(access_token)
            account = self._accounts.get(access_token) or {}
            requested_scope = (
                "image" if account.get("pending_auth_scope") == "image" else "account"
            )
            with self._image_failure_refresh_lock:
                cutoff = now - self._IMAGE_FAILURE_REFRESH_DEDUP_SECONDS
                self._image_failure_refresh_started_at = {
                    token: started_at
                    for token, started_at in self._image_failure_refresh_started_at.items()
                    if token in self._image_failure_refresh_active or started_at >= cutoff
                }
                active_refresh_token = next(
                    (
                        token for token in self._image_failure_refresh_active
                        if self._resolve_access_token_locked(token) == access_token
                    ),
                    None,
                )
                pending_refresh_token = next(
                    (
                        token for token in self._image_failure_refresh_pending_set
                        if self._resolve_access_token_locked(token) == access_token
                    ),
                    None,
                )
                last_started_at = max(
                    (
                        started_at
                        for token, started_at in self._image_failure_refresh_started_at.items()
                        if self._resolve_access_token_locked(token) == access_token
                    ),
                    default=0.0,
                )
                if active_refresh_token is not None:
                    if force:
                        self._image_failure_refresh_rerun.add(active_refresh_token)
                    return True
                if pending_refresh_token is not None:
                    existing_scope = self._image_failure_refresh_pending_scopes.get(
                        pending_refresh_token,
                        "account",
                    )
                    self._image_failure_refresh_pending_scopes[pending_refresh_token] = (
                        "account"
                        if "account" in {existing_scope, requested_scope}
                        else "image"
                    )
                    return True
                if (
                    not force
                    and now - last_started_at < self._IMAGE_FAILURE_REFRESH_DEDUP_SECONDS
                ):
                    return False
                self._image_failure_refresh_pending.append(access_token)
                self._image_failure_refresh_pending_set.add(access_token)
                self._image_failure_refresh_pending_scopes[access_token] = requested_scope
        self._start_pending_image_failure_refreshes()
        return True

    def _start_pending_image_failure_refreshes(self) -> None:
        while True:
            with self._image_failure_refresh_lock:
                if not self._image_failure_refresh_pending:
                    return

                task_count = (
                    len(self._image_failure_refresh_active)
                    + len(self._image_failure_refresh_pending)
                )
                max_workers = account_processing_worker_count(task_count)
                if len(self._image_failure_refresh_active) >= max_workers:
                    return

                access_token = self._image_failure_refresh_pending.popleft()
                refresh_scope = self._image_failure_refresh_pending_scopes.get(
                    access_token,
                    "account",
                )
                self._image_failure_refresh_pending_set.discard(access_token)
                self._image_failure_refresh_pending_scopes.pop(access_token, None)
                self._image_failure_refresh_active.add(access_token)
                self._image_failure_refresh_active_scopes[access_token] = refresh_scope
                self._image_failure_refresh_started_at[access_token] = time.monotonic()

            def refresh(token: str = access_token) -> None:
                try:
                    self._refresh_account_after_image_failure(token)
                finally:
                    with self._lock:
                        resolved_token = self._resolve_access_token_locked(token)
                        account = self._accounts.get(resolved_token)
                        with self._image_failure_refresh_lock:
                            self._image_failure_refresh_active.discard(token)
                            self._image_failure_refresh_active_scopes.pop(token, None)
                            rerun_requested = token in self._image_failure_refresh_rerun
                            self._image_failure_refresh_rerun.discard(token)
                            if (
                                rerun_requested
                                and account is not None
                                and account.get("last_remote_check_result") == "pending"
                                and resolved_token not in self._image_failure_refresh_pending_set
                            ):
                                self._image_failure_refresh_pending.append(resolved_token)
                                self._image_failure_refresh_pending_set.add(resolved_token)
                                self._image_failure_refresh_pending_scopes[resolved_token] = (
                                    "image"
                                    if account.get("pending_auth_scope") == "image"
                                    else "account"
                                )
                    self._start_pending_image_failure_refreshes()

            (
                expected_access_token,
                expected_refresh_token,
                expected_account,
            ) = self._credential_snapshot(access_token)
            expected_remote_check_marker = self._remote_check_marker(expected_account)
            try:
                Thread(
                    target=refresh,
                    name="image-account-refresh",
                    daemon=True,
                ).start()
            except Exception as exc:
                with self._image_failure_refresh_lock:
                    self._image_failure_refresh_active.discard(access_token)
                    self._image_failure_refresh_active_scopes.pop(access_token, None)
                self._record_remote_check_error(
                    access_token,
                    "image_failure",
                    str(exc),
                    expected_access_token=expected_access_token,
                    expected_refresh_token=expected_refresh_token,
                    expected_remote_check_marker=expected_remote_check_marker,
                )
                log_service.add(
                    LOG_TYPE_ACCOUNT,
                    "image failure refresh scheduling failed",
                    {
                        "token": anonymize_token(access_token),
                        "error": self._credential_error_text(
                            exc,
                            expected_account,
                            access_token=access_token,
                        ),
                    },
                )

    def mark_image_result(
        self,
        access_token: str,
        success: bool,
        *,
        failure: ImageFailure | None = None,
        quota_consumed: bool | None = None,
        capabilities: set[str] | tuple[str, ...] | None = None,
        duration_ms: float | None = None,
        expected_access_token: str | None = None,
        expected_refresh_token: str | None = None,
        expected_last_token_refresh_at: str | None = None,
    ) -> dict | None:
        # Capability successes are recorded for soft model/GIF routing.
        if not access_token:
            return None
        now = datetime.now(timezone.utc)
        should_verify_after_failure = False
        consumed_quota = success if quota_consumed is None else bool(quota_consumed)
        with self._image_slot_condition:
            access_token = self._resolve_access_token_locked(access_token)
            self._release_image_slot_locked(access_token)
            try:
                current = self._accounts.get(access_token)
                if current is None:
                    return None
                expected_generation = None
                if expected_access_token is not None and expected_refresh_token is not None:
                    expected_generation = (
                        str(expected_access_token or "").strip(),
                        str(expected_refresh_token or "").strip(),
                        str(expected_last_token_refresh_at or "").strip(),
                    )
                    if (
                        self._credential_generation(access_token, current)
                        != expected_generation
                    ):
                        return None
                next_item = dict(current)
                next_item["last_used_at"] = now.isoformat()
                image_quota_unknown = bool(next_item.get("image_quota_unknown"))
                if success:
                    next_item["success"] = int(next_item.get("success") or 0) + 1
                if consumed_quota:
                    if not image_quota_unknown:
                        current_quota = max(0, int(next_item.get("quota") or 0))
                        next_item["quota"] = max(0, current_quota - 1)
                        if current_quota <= 1:
                            # 本地扣减到 0 只能说明“展示值需要远程刷新”，不能直接证明账号已限流。
                            # 下一次调度会进入远程预检，由 get_user_info 的结果决定是否写入“限流”。
                            next_item["image_quota_unknown"] = True
                            next_item["last_quota_estimated_empty_at"] = now.isoformat()
                    if next_item.get("status") == "限流":
                        # 上游已经消耗图片额度，说明远程额度已恢复。
                        next_item["status"] = "正常"
                        next_item["image_quota_unknown"] = True
                        next_item["restore_at"] = None
                if not success and failure is not None and failure.verify_account:
                    next_item["fail"] = int(next_item.get("fail") or 0) + 1
                    self._mark_remote_check_pending(
                        next_item,
                        "image_failure",
                        now.isoformat(),
                        scope="image",
                    )
                    should_verify_after_failure = True
                failure_code = str(getattr(failure, "code", "") or "")
                record_image_health(
                    next_item,
                    success=success,
                    failure_code=failure_code,
                    capabilities=capabilities,
                    duration_ms=duration_ms,
                )
                record_egress_result(
                    next_item,
                    success=success,
                    failure_code=failure_code,
                )
                if success:
                    upstream_circuit.record(True)
                elif failure_code in UPSTREAM_FAILURE_CODES:
                    upstream_circuit.record(False)
                account = self._normalize_account(next_item)
                if account is None:
                    return None
                self._accounts[access_token] = account
                saved = self._save_accounts(
                    expected_credential_generation=expected_generation,
                )
                if not saved:
                    should_verify_after_failure = False
                    return None
                access_token = self._resolve_access_token_locked(access_token)
                persisted = self._accounts.get(access_token)
                if persisted is None:
                    should_verify_after_failure = False
                    return None
                result = dict(persisted)
            finally:
                self._image_slot_condition.notify_all()
        if should_verify_after_failure:
            scheduled = self._schedule_account_refresh_after_image_failure(
                access_token,
                force=True,
            )
            if not scheduled:
                self._record_remote_check_error(
                    access_token,
                    "image_failure",
                    "Account verification could not be scheduled.",
                )
        return result

    def fetch_remote_info(
        self,
        access_token: str,
        event: str = "fetch_remote_info",
        remove_invalid: bool | None = None,
        *,
        image_scope: bool = False,
        allow_refresh_token_exchange: bool = True,
        preflight_refresh: bool = True,
        request_timeout: float | None = None,
        light: bool = False,
    ) -> dict[str, Any] | None:
        if not access_token:
            raise ValueError("access_token is required")

        if allow_refresh_token_exchange and preflight_refresh:
            refresh_kwargs = {"event": f"{event}:preflight"}
            if image_scope:
                refresh_kwargs["image_scope"] = True
            active_token = self.ensure_access_token(access_token, **refresh_kwargs)
        else:
            active_token = self.resolve_access_token(access_token) or access_token
        from services.openai_backend_api import InvalidAccessTokenError, OpenAIBackendAPI

        def request_user_info(token: str) -> dict[str, Any]:
            slot = account_processing_priority_slot() if image_scope else account_processing_slot()
            with slot:
                with OpenAIBackendAPI(token) as backend:
                    if light:
                        return (
                            backend.get_user_info_light(timeout=request_timeout)
                            if request_timeout
                            else backend.get_user_info_light()
                        )
                    return backend.get_user_info(timeout=request_timeout) if request_timeout else backend.get_user_info()

        request_token, request_refresh_token, request_account = self._credential_snapshot(active_token)
        if not request_account:
            raise RefreshCredentialsChangedError()
        request_generation = self._credential_generation(request_token, request_account)
        successful_snapshot = (request_token, request_refresh_token)
        successful_remote_check_marker = self._remote_check_marker(request_account)
        try:
            result = request_user_info(request_token)
        except InvalidAccessTokenError as exc:
            rejected_error: InvalidAccessTokenError | None = exc
            rejected_snapshot = (request_token, request_refresh_token)
            rejected_remote_check_marker = successful_remote_check_marker
            current_token, current_refresh_token, current_account = self._credential_snapshot(request_token)
            current_snapshot = (current_token, current_refresh_token)
            current_remote_check_marker = self._remote_check_marker(current_account)
            current_generation = self._credential_generation(current_token, current_account)
            if current_account and current_generation != request_generation:
                try:
                    result = request_user_info(current_token)
                except InvalidAccessTokenError as current_exc:
                    rejected_error = current_exc
                    rejected_snapshot = current_snapshot
                    rejected_remote_check_marker = current_remote_check_marker
                except Exception as current_exc:
                    self._record_remote_check_error(
                        current_token,
                        event,
                        str(current_exc),
                        expected_access_token=current_token,
                        expected_refresh_token=current_refresh_token,
                        expected_remote_check_marker=current_remote_check_marker,
                    )
                    raise
                else:
                    successful_snapshot = current_snapshot
                    successful_remote_check_marker = current_remote_check_marker
                    active_token = current_token
                    rejected_error = None

            if rejected_error is not None:
                if image_scope:
                    self.schedule_auth_verification(
                        rejected_snapshot[0],
                        event,
                        expected_access_token=rejected_snapshot[0],
                        expected_refresh_token=rejected_snapshot[1],
                        expected_last_token_refresh_at=rejected_remote_check_marker[0],
                        remove_invalid=remove_invalid,
                        scope="image",
                    )
                    raise rejected_error

                if not allow_refresh_token_exchange:
                    refresh_token_can_recover = bool(rejected_snapshot[1]) and not bool(
                        (current_account or {}).get("refresh_token_invalid_at")
                    )
                    self.handle_invalid_token(
                        rejected_snapshot[0],
                        event,
                        error=str(rejected_error),
                        remove=False if refresh_token_can_recover else remove_invalid,
                        expected_access_token=rejected_snapshot[0],
                        expected_refresh_token=rejected_snapshot[1],
                        expected_remote_check_marker=rejected_remote_check_marker,
                    )
                    raise rejected_error

                before_refresh = self.get_account(rejected_snapshot[0]) or {}
                before_refresh_at = str(before_refresh.get("last_token_refresh_at") or "")
                before_error_at = str(before_refresh.get("last_token_refresh_error_at") or "")
                try:
                    active_token = self.force_refresh_access_token(
                        rejected_snapshot[0],
                        event=f"{event}:invalid_access_token",
                        expected_credentials=(
                            rejected_snapshot[0],
                            rejected_snapshot[1],
                            rejected_remote_check_marker[0],
                        ),
                    )
                except TerminalRefreshTokenError as refresh_error:
                    self.handle_invalid_token(
                        rejected_snapshot[0],
                        event,
                        error=str(rejected_error),
                        remove=remove_invalid,
                        expected_access_token=rejected_snapshot[0],
                        expected_refresh_token=rejected_snapshot[1],
                        expected_remote_check_marker=rejected_remote_check_marker,
                        token_refresh_error=str(refresh_error),
                        refresh_token_terminal=True,
                    )
                    raise
                except RefreshCredentialsChangedError:
                    raise
                except Exception as refresh_exc:
                    self._record_remote_check_error(
                        rejected_snapshot[0],
                        event,
                        str(refresh_exc),
                        expected_access_token=rejected_snapshot[0],
                        expected_refresh_token=rejected_snapshot[1],
                        expected_remote_check_marker=rejected_remote_check_marker,
                    )
                    raise

                after_refresh = self.get_account(active_token or rejected_snapshot[0]) or {}
                after_refresh_at = str(after_refresh.get("last_token_refresh_at") or "")
                after_error_at = str(after_refresh.get("last_token_refresh_error_at") or "")
                refresh_failed = bool(after_error_at and after_error_at != before_error_at)
                refresh_succeeded = bool(after_refresh_at and after_refresh_at != before_refresh_at)
                if refresh_failed and not refresh_succeeded:
                    self._record_remote_check_error(
                        rejected_snapshot[0],
                        event,
                        str(
                            after_refresh.get("last_token_refresh_error")
                            or "refresh token failed"
                        ),
                        expected_access_token=rejected_snapshot[0],
                        expected_refresh_token=rejected_snapshot[1],
                        expected_remote_check_marker=rejected_remote_check_marker,
                    )
                    raise rejected_error

                verification_token, verification_refresh_token, verification_account = (
                    self._credential_snapshot(active_token)
                )
                if not verification_account:
                    raise RefreshCredentialsChangedError()
                verification_remote_check_marker = self._remote_check_marker(verification_account)
                try:
                    result = request_user_info(verification_token)
                except InvalidAccessTokenError as retry_exc:
                    self.handle_invalid_token(
                        verification_token,
                        event,
                        error=str(retry_exc),
                        remove=remove_invalid,
                        expected_access_token=verification_token,
                        expected_refresh_token=verification_refresh_token,
                        expected_remote_check_marker=verification_remote_check_marker,
                    )
                    raise
                except Exception as retry_exc:
                    self._record_remote_check_error(
                        verification_token,
                        event,
                        str(retry_exc),
                        expected_access_token=verification_token,
                        expected_refresh_token=verification_refresh_token,
                        expected_remote_check_marker=verification_remote_check_marker,
                    )
                    raise
                active_token = verification_token
                successful_snapshot = (verification_token, verification_refresh_token)
                successful_remote_check_marker = verification_remote_check_marker
        except Exception as exc:
            self._record_remote_check_error(
                request_token,
                event,
                str(exc),
                expected_access_token=request_token,
                expected_refresh_token=request_refresh_token,
                expected_remote_check_marker=successful_remote_check_marker,
            )
            raise

        updated = self._record_refresh_success(
            active_token,
            result,
            event,
            expected_access_token=successful_snapshot[0],
            expected_refresh_token=successful_snapshot[1],
            expected_remote_check_marker=successful_remote_check_marker,
        )
        if updated is not None:
            return updated
        # update_account 可能因为“自动移除额度耗尽账号”删除了远程确认限流的账号。
        # 调用方仍需要知道本次预检的真实结果，不能把它混成普通预检失败。
        if str(result.get("status") or "") == "\u9650\u6d41" and config.auto_remove_rate_limited_accounts:
            return {**result, "access_token": active_token, "_removed_after_refresh": True}
        current_token, current_refresh_token, current_account = self._credential_snapshot(active_token)
        if (
            current_account is not None
            and current_token == successful_snapshot[0]
            and current_refresh_token == successful_snapshot[1]
        ):
            # A newer verification marker won the compare-and-swap. Preserve and
            # return that newer account state instead of reporting a credential race.
            return current_account
        # A deleted account must not leak its stale token back into the selection path.
        raise RefreshCredentialsChangedError()

    # ---- 刷新进度追踪 ----

    @classmethod
    def _prune_refresh_progress_locked(
        cls,
        now: float,
        *,
        target_id: str | None = None,
    ) -> None:
        full_scan_due = (
            now < cls._refresh_progress_last_pruned_at
            or now - cls._refresh_progress_last_pruned_at
            >= cls._REFRESH_PROGRESS_PRUNE_INTERVAL_SECONDS
        )
        if full_scan_due:
            candidates = list(cls._refresh_progress)
            cls._refresh_progress_last_pruned_at = now
        elif target_id and target_id in cls._refresh_progress:
            candidates = [target_id]
        else:
            return

        for progress_id in candidates:
            progress = cls._refresh_progress.get(progress_id)
            if progress is None:
                continue
            updated_at_value = progress.get("_updated_at_monotonic")
            if isinstance(updated_at_value, (int, float)):
                updated_at = float(updated_at_value)
            else:
                updated_at = now
                progress["_updated_at_monotonic"] = now
            ttl = (
                cls._REFRESH_PROGRESS_COMPLETED_TTL_SECONDS
                if progress.get("done")
                else cls._REFRESH_PROGRESS_ACTIVE_TTL_SECONDS
            )
            if now - updated_at >= ttl:
                cls._refresh_progress.pop(progress_id, None)

    def init_refresh_progress(self, progress_id: str, total: int) -> None:
        """初始化刷新进度记录。"""
        now = time.monotonic()
        with self._refresh_progress_lock:
            self._prune_refresh_progress_locked(now, target_id=progress_id)
            self._refresh_progress[progress_id] = {
                "total": total,
                "processed": 0,
                "done": False,
                "error": None,
                "job_id": "",
                "stage": "queued",
                "stage_label": "等待队列调度",
                "status_counts": {"正常": 0, "限流": 0, "异常": 0, "禁用": 0},
                "total_quota": 0,
                "events": [],
                "_event_sequence": 0,
                "_updated_at_monotonic": now,
            }

    def attach_refresh_progress_job(self, progress_id: str, job_id: str) -> None:
        """Link an account operation progress record to its queue job."""
        now = time.monotonic()
        with self._refresh_progress_lock:
            progress = self._refresh_progress.get(progress_id)
            if progress is None:
                return
            progress["job_id"] = str(job_id or "")
            progress["stage"] = "running"
            progress["stage_label"] = "队列执行中"
            progress["_updated_at_monotonic"] = now

    def update_refresh_progress_stage(
        self,
        progress_id: str,
        stage: str,
        stage_label: str,
    ) -> None:
        """Publish task-level progress without counting an account as completed."""
        now = time.monotonic()
        with self._refresh_progress_lock:
            self._prune_refresh_progress_locked(now, target_id=progress_id)
            progress = self._refresh_progress.get(progress_id)
            if progress is None or progress.get("done"):
                return
            progress["stage"] = str(stage or "").strip()
            progress["stage_label"] = str(stage_label or "").strip()
            progress["_updated_at_monotonic"] = now

    @staticmethod
    def _account_operation_identity(
        account: dict | None,
        *,
        fallback_id: str = "",
    ) -> tuple[str, str]:
        item = account or {}
        account_id = str(item.get("management_id") or fallback_id or "").strip()
        account_label = str(item.get("email") or account_id).strip()
        return account_id, account_label

    @classmethod
    def _append_refresh_progress_event_locked(
        cls,
        progress: dict[str, Any],
        *,
        account_id: str = "",
        account_label: str = "",
        action: str,
        status: str,
        message: str,
        sensitive_values: tuple[object, ...] = (),
        proxy_values: tuple[object, ...] = (),
    ) -> None:
        sequence = int(progress.get("_event_sequence") or 0) + 1
        event = normalize_account_operation_event(
            {
                "sequence": sequence,
                "account_id": account_id,
                "account_label": account_label,
                "action": action,
                "status": status,
                "message": message,
            },
            fallback_sequence=sequence,
            sensitive_values=sensitive_values,
            proxy_values=proxy_values,
        )
        if event is None:
            return
        events = progress.setdefault("events", [])
        events.append(event)
        if len(events) > cls._REFRESH_PROGRESS_EVENT_LIMIT:
            del events[:-cls._REFRESH_PROGRESS_EVENT_LIMIT]
        progress["_event_sequence"] = sequence

    def update_refresh_progress(
        self,
        progress_id: str,
        token: str,
        result_account: dict | None = None,
        *,
        account_id: str = "",
        account_label: str = "",
        action: str = "",
        event_status: str = "info",
        event_message: str = "",
    ) -> None:
        """刷新单个账号后，更新进度计数。"""
        account = result_account or self.get_account(token)
        status = str(account.get("status") or "正常").strip() if account else "异常"
        quota = max(0, int(account.get("quota") or 0)) if account else 0

        now = time.monotonic()
        with self._refresh_progress_lock:
            self._prune_refresh_progress_locked(now, target_id=progress_id)
            progress = self._refresh_progress.get(progress_id)
            if progress is None:
                return
            progress["processed"] += 1
            progress["status_counts"][status] = progress["status_counts"].get(status, 0) + 1
            progress["total_quota"] += quota
            if action and event_message:
                event_account_id, event_account_label = self._account_operation_identity(
                    account,
                    fallback_id=account_id,
                )
                sensitive_values = tuple(
                    value
                    for value in (
                        token,
                        (account or {}).get("access_token"),
                        (account or {}).get("refresh_token"),
                        (account or {}).get("id_token"),
                        (account or {}).get("password"),
                    )
                    if value
                )
                proxy_values = tuple(
                    value for value in ((account or {}).get("proxy"),) if value
                )
                self._append_refresh_progress_event_locked(
                    progress,
                    account_id=event_account_id,
                    account_label=account_label or event_account_label,
                    action=action,
                    status=event_status,
                    message=event_message,
                    sensitive_values=sensitive_values,
                    proxy_values=proxy_values,
                )
            progress["_updated_at_monotonic"] = now

    def advance_refresh_progress(
        self,
        progress_id: str,
        count: int,
        *,
        status: str = "",
        quota: int = 0,
    ) -> int:
        """Advance a progress record by ``count`` accounts in one lock.

        Large batches (thousands of accounts) cannot afford one locked event
        append per account; they publish a handful of sample events and then
        advance the counters here.
        """
        step = max(0, int(count))
        if step <= 0:
            return 0
        now = time.monotonic()
        with self._refresh_progress_lock:
            self._prune_refresh_progress_locked(now, target_id=progress_id)
            progress = self._refresh_progress.get(progress_id)
            if progress is None:
                return 0
            total = max(0, int(progress.get("total") or 0))
            processed = max(0, int(progress.get("processed") or 0))
            applied = min(step, max(0, total - processed)) if total else step
            if applied <= 0:
                return 0
            progress["processed"] = processed + applied
            if status:
                counts = progress.setdefault("status_counts", {})
                counts[status] = int(counts.get(status, 0)) + applied
            if quota:
                progress["total_quota"] = int(progress.get("total_quota") or 0) + quota * applied
            progress["_updated_at_monotonic"] = now
            return applied

    def finish_refresh_progress(
        self,
        progress_id: str,
        result: dict | None = None,
        error: str | None = None,
    ) -> None:
        """标记刷新完成。"""
        now = time.monotonic()
        with self._refresh_progress_lock:
            self._prune_refresh_progress_locked(now, target_id=progress_id)
            progress = self._refresh_progress.get(progress_id)
            if progress is None:
                return
            progress["done"] = True
            progress["result"] = result
            progress["stage"] = "completed"
            progress["stage_label"] = ""
            if error:
                progress["error"] = error
            progress["_updated_at_monotonic"] = now

    def get_refresh_progress(self, progress_id: str) -> dict | None:
        """查询刷新进度。"""
        now = time.monotonic()
        with self._refresh_progress_lock:
            self._prune_refresh_progress_locked(now, target_id=progress_id)
            progress = self._refresh_progress.get(progress_id)
            return {
                key: value
                for key, value in progress.items()
                if not key.startswith("_")
            } if progress else None

    def clean_refresh_progress(self, progress_id: str) -> None:
        """清理过期进度记录。"""
        with self._refresh_progress_lock:
            self._refresh_progress.pop(progress_id, None)

    def _exchange_access_tokens(
        self,
        access_tokens: list[str],
        progress_id: str | None = None,
        *,
        force: bool,
        event: str,
        finalize_progress: bool = True,
    ) -> dict[str, Any]:
        """Exchange RT-to-AT credentials without querying account metadata."""
        requested_tokens = list(dict.fromkeys(token for token in access_tokens if token))
        if not requested_tokens:
            result = {
                "refreshed": 0,
                "skipped": 0,
                "updated_ids": [],
                "removed_ids": [],
                "errors": [],
            }
            if progress_id and finalize_progress:
                self.finish_refresh_progress(progress_id, result)
            return result

        if progress_id and self.get_refresh_progress(progress_id) is None:
            self.init_refresh_progress(progress_id, len(requested_tokens))

        targets: list[tuple[str, str]] = []
        initial_errors: list[dict[str, Any]] = []
        for requested_token in requested_tokens:
            active_token, _refresh_token, account = self._credential_snapshot(requested_token)
            account_id = str((account or {}).get("management_id") or "").strip()
            if not account or not account_id:
                error = {
                    "id": account_id,
                    "token": anonymize_token(requested_token),
                    "code": "account_not_found",
                    "error": "account not found",
                }
                initial_errors.append(error)
                if progress_id:
                    self.update_refresh_progress(
                        progress_id,
                        requested_token,
                        account_id=account_id,
                        action="refresh_access_token",
                        event_status="failed",
                        event_message=error["error"],
                    )
                continue
            targets.append((active_token, account_id))

        refreshed = 0
        skipped = 0
        refreshed_ids: list[str] = []
        errors = list(initial_errors)

        def exchange(
            token: str,
            account_id: str,
        ) -> tuple[str, dict | None, bool, dict[str, Any] | None]:
            active_token, refresh_token, account = self._credential_snapshot(token)
            if not account:
                return token, None, False, {
                    "id": account_id,
                    "token": anonymize_token(token),
                    "code": "account_not_found",
                    "error": "account not found",
                }
            if not refresh_token:
                return active_token, account, False, {
                    "id": account_id,
                    "token": anonymize_token(active_token),
                    "code": "refresh_token_missing",
                    "error": "refresh token is missing",
                }
            if account.get("refresh_token_invalid_at"):
                return active_token, account, False, {
                    "id": account_id,
                    "token": anonymize_token(active_token),
                    "code": "refresh_token_invalid",
                    "error": "refresh token is invalid",
                }
            before_refresh_at = str(account.get("last_token_refresh_at") or "")
            try:
                operation = (
                    self.force_refresh_access_token
                    if force
                    else self.ensure_access_token
                )
                refreshed_token = operation(
                    active_token,
                    raise_on_error=True,
                    event=event,
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                failure = classify_image_exception(exc)
                current_token = self.resolve_access_token(active_token) or active_token
                current_account = self.get_account(current_token) or account
                return current_token, self.get_account(current_token), False, {
                    "id": account_id,
                    "token": anonymize_token(active_token),
                    "error": self._credential_error_text(
                        exc,
                        current_account,
                        access_token=active_token,
                    ),
                    **failure.diagnostic_fields(),
                }
            result_account = self.get_account(refreshed_token)
            after_refresh_at = str((result_account or {}).get("last_token_refresh_at") or "")
            exchanged = bool(
                refreshed_token != active_token
                or (after_refresh_at and after_refresh_at != before_refresh_at)
            )
            return refreshed_token, result_account, exchanged, None

        max_workers = account_processing_worker_count(len(targets))
        executor = ThreadPoolExecutor(max_workers=max_workers) if max_workers else None
        try:
            futures = {
                executor.submit(exchange, token, account_id): (token, account_id)
                for token, account_id in targets
            } if executor else {}
            for future in as_completed(futures):
                token, account_id = futures[future]
                active_token, result_account, exchanged, error = future.result()
                if error:
                    errors.append(error)
                    event_status = "failed"
                    event_message = str(
                        error.get("error")
                        or "\u8bbf\u95ee\u4ee4\u724c\u5237\u65b0\u5931\u8d25"
                    )
                elif exchanged:
                    refreshed += 1
                    refreshed_ids.append(account_id)
                    event_status = "success"
                    event_message = "\u8bbf\u95ee\u4ee4\u724c\u5df2\u5237\u65b0"
                else:
                    skipped += 1
                    event_status = "skipped"
                    event_message = "\u8bbf\u95ee\u4ee4\u724c\u65e0\u9700\u66f4\u65b0"
                if progress_id:
                    self.update_refresh_progress(
                        progress_id,
                        active_token or token,
                        result_account,
                        account_id=account_id,
                        action="refresh_access_token",
                        event_status=event_status,
                        event_message=event_message,
                    )
        except (KeyboardInterrupt, SystemExit):
            if progress_id:
                self.finish_refresh_progress(progress_id, error="cancelled")
            if executor:
                executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            if executor:
                executor.shutdown(wait=True, cancel_futures=True)

        existing_ids = {
            str(account.get("management_id") or "").strip()
            for account in self.list_accounts()
            if str(account.get("management_id") or "").strip()
        }
        target_ids = [account_id for _token, account_id in targets]
        result = {
            "refreshed": refreshed,
            "skipped": skipped,
            "updated_ids": [
                account_id for account_id in refreshed_ids if account_id in existing_ids
            ],
            "removed_ids": [account_id for account_id in target_ids if account_id not in existing_ids],
            "errors": errors,
        }
        if progress_id and finalize_progress:
            self.finish_refresh_progress(progress_id, result)
        return result

    def refresh_access_tokens(
        self,
        access_tokens: list[str],
        progress_id: str | None = None,
        *,
        finalize_progress: bool = True,
    ) -> dict[str, Any]:
        """Force RT-to-AT exchanges for an explicit administrator action."""
        return self._exchange_access_tokens(
            access_tokens,
            progress_id,
            force=True,
            event="refresh_access_tokens",
            finalize_progress=finalize_progress,
        )

    def renew_expiring_access_tokens(self, access_tokens: list[str]) -> dict[str, Any]:
        """Conditionally renew ATs selected by a periodic expiry scan."""
        return self._exchange_access_tokens(
            access_tokens,
            force=False,
            event="renew_expiring_access_tokens",
        )

    def sync_accounts_and_quota(
        self,
        access_tokens: list[str],
        progress_id: str | None = None,
        remove_invalid: bool | None = None,
        *,
        finalize_progress: bool = True,
    ) -> dict[str, Any]:
        """Synchronize remote account metadata and image quota."""
        access_tokens = list(dict.fromkeys(token for token in access_tokens if token))
        if not access_tokens:
            items = self.list_accounts()
            result = {"synced": 0, "errors": [], "items": items}
            if progress_id and finalize_progress:
                self.finish_refresh_progress(progress_id, result)
            return result

        synced = 0
        errors = []
        max_workers = account_processing_worker_count(len(access_tokens))

        if progress_id and self.get_refresh_progress(progress_id) is None:
            self.init_refresh_progress(progress_id, len(access_tokens))

        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            futures = {}
            for token in access_tokens:
                account_id, account_label = self._account_operation_identity(
                    self.get_account(token)
                )
                future = executor.submit(
                    self.fetch_remote_info,
                    token,
                    "sync_accounts_and_quota",
                    remove_invalid,
                    preflight_refresh=False,
                )
                futures[future] = (token, account_id, account_label)
            for future in as_completed(futures):
                token, account_id, account_label = futures[future]
                result_account = None
                try:
                    account = future.result()
                except (KeyboardInterrupt, SystemExit):
                    executor.shutdown(wait=False, cancel_futures=True)
                    raise
                except Exception as exc:
                    failure = classify_image_exception(exc)
                    account = self.get_account(token)
                    errors.append({
                        "token": anonymize_token(token),
                        "account_id": account_id,
                        "account_label": account_label,
                        "error": self._credential_error_text(
                            exc,
                            account,
                            access_token=token,
                        ),
                        **failure.diagnostic_fields(),
                    })
                    event_status = "failed"
                    event_message = self._credential_error_text(
                        exc,
                        account,
                        access_token=token,
                    )
                else:
                    if account is not None:
                        synced += 1
                        result_account = account
                        event_status = "success"
                        event_message = "\u8d26\u53f7\u4e0e\u989d\u5ea6\u5df2\u540c\u6b65"
                    else:
                        event_status = "failed"
                        event_message = "\u8d26\u53f7\u4e0e\u989d\u5ea6\u540c\u6b65\u5931\u8d25"

                if progress_id:
                    self.update_refresh_progress(
                        progress_id,
                        token,
                        result_account,
                        account_id=account_id,
                        account_label=account_label,
                        action="sync_account",
                        event_status=event_status,
                        event_message=event_message,
                    )
        except (KeyboardInterrupt, SystemExit):
            if progress_id:
                self.finish_refresh_progress(progress_id, error="cancelled")
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            executor.shutdown(wait=True, cancel_futures=True)

        result = {
            "synced": synced,
            "errors": errors,
            "items": self.list_accounts(),
        }

        if progress_id and finalize_progress:
            self.finish_refresh_progress(progress_id, result)

        return result

    def refresh_accounts(
        self,
        access_tokens: list[str],
        progress_id: str | None = None,
        remove_invalid: bool | None = None,
        *,
        finalize_progress: bool = True,
    ) -> dict[str, Any]:
        """Compatibility alias for synchronize-account-and-quota callers."""
        result = self.sync_accounts_and_quota(
            access_tokens,
            progress_id,
            remove_invalid,
            finalize_progress=finalize_progress,
        )
        return {**result, "refreshed": int(result.get("synced") or 0)}

    def build_export_items(
            self,
            access_tokens: list[str] | None = None,
            *,
            full: bool = False,
    ) -> list[dict[str, Any]]:
        self._refresh_accounts_snapshot_if_stale()
        target_tokens = set(token for token in (access_tokens or []) if token)
        with self._lock:
            accounts = [
                dict(item)
                for item in self._accounts.values()
                if not target_tokens or str(item.get("access_token") or "") in target_tokens
            ]

        items: list[dict[str, Any]] = []
        for account in accounts:
            access_token = str(account.get("access_token") or "").strip()
            refresh_token = str(account.get("refresh_token") or "").strip()
            id_token = str(account.get("id_token") or "").strip()
            if not access_token:
                continue

            if full:
                item: dict[str, Any] = {"access_token": access_token}
                for key in (
                    "management_id",
                    "access_token_fingerprints",
                    "refresh_token",
                    "id_token",
                    "password",
                    "type",
                    "plan_type",
                    "export_type",
                    "source_type",
                    "status",
                    "quota",
                    "image_quota_unknown",
                    "limits_progress",
                    "default_model_slug",
                    "email",
                    "user_id",
                    "account_id",
                    "proxy",
                    "group_id",
                    "created_at",
                    "last_token_refresh_at",
                    "last_token_refresh_error",
                    "last_token_refresh_error_at",
                    "refresh_token_invalid_at",
                    "last_remote_checked_at",
                    "last_remote_check_attempt_at",
                    "last_remote_check_error",
                    "last_remote_check_error_at",
                    "last_remote_check_event",
                    "last_remote_check_result",
                ):
                    if key in account and account.get(key) not in (None, ""):
                        item[key] = account.get(key)
                items.append(item)
                continue

            access_payload = self._decode_jwt_payload(access_token)
            id_payload = self._decode_jwt_payload(id_token)
            auth_claim = access_payload.get("https://api.openai.com/auth")
            auth_claim = auth_claim if isinstance(auth_claim, dict) else {}
            profile_claim = access_payload.get("https://api.openai.com/profile")
            profile_claim = profile_claim if isinstance(profile_claim, dict) else {}

            email = (
                str(account.get("email") or "").strip()
                or str(profile_claim.get("email") or "").strip()
                or str(id_payload.get("email") or "").strip()
            )
            account_id = (
                str(account.get("account_id") or "").strip()
                or str(auth_claim.get("chatgpt_account_id") or "").strip()
                or str(account.get("user_id") or "").strip()
            )
            item: dict[str, Any] = {
                "type": str(account.get("export_type") or "codex"),
                "email": email,
                "account_id": account_id,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "id_token": id_token,
                "expired": self._timestamp_to_iso(access_payload.get("exp")),
                "last_refresh": self._timestamp_to_iso(access_payload.get("iat")),
            }
            password = str(account.get("password") or "").strip()
            if password:
                item["password"] = password
            items.append(item)
        return items

    def get_stats(self) -> dict:
        self._refresh_accounts_snapshot_if_stale()
        with self._lock:
            items = list(self._accounts.values())
        total = len(items)
        active = sum(1 for a in items if a.get("status") == "正常")
        limited = sum(1 for a in items if a.get("status") == "限流")
        abnormal = sum(1 for a in items if a.get("status") == "异常")
        disabled = sum(1 for a in items if a.get("status") == "禁用")
        # Quota totals must only include accounts that can actually be leased.
        # Summing by status alone counted dead credentials and pending accounts,
        # so the dashboard showed 90,972 while the selectable pool only held
        # 78,202.
        quota_items = [
            item
            for item in items
            if self._is_account_selectable(item, allow_limited=False)
        ]
        total_quota = sum(max(0, int(a.get("quota") or 0)) for a in quota_items)
        quota_distribution = {"zero": 0, "1_5": 0, "6_20": 0, "21_plus": 0}
        for item in quota_items:
            if self._is_unlimited_image_quota_account(item):
                continue
            if bool(item.get("image_quota_unknown")):
                continue
            quota = max(0, int(item.get("quota") or 0))
            if quota <= 0:
                quota_distribution["zero"] += 1
            elif quota <= 5:
                quota_distribution["1_5"] += 1
            elif quota <= 20:
                quota_distribution["6_20"] += 1
            else:
                quota_distribution["21_plus"] += 1
        unlimited = sum(1 for a in quota_items if self._is_unlimited_image_quota_account(a))
        unknown_quota = sum(
            1
            for a in quota_items
            if (
                bool(a.get("image_quota_unknown"))
                or (not bool(a.get("image_quota_unknown")) and max(0, int(a.get("quota") or 0)) <= 0)
            )
            and not self._is_unlimited_image_quota_account(a)
        )
        total_success = sum(int(a.get("success") or 0) for a in items)
        total_fail = sum(int(a.get("fail") or 0) for a in items)
        by_type = {}
        for a in items:
            t = a.get("type") or "unknown"
            by_type[t] = by_type.get(t, 0) + 1
        return {
            "total": total,
            "cumulative_total": self._cumulative_total,
            "active": active,
            "limited": limited,
            "abnormal": abnormal,
            "disabled": disabled,
            "total_quota": total_quota,
            "quota_distribution": quota_distribution,
            "low_quota_count": quota_distribution["zero"] + quota_distribution["1_5"],
            "unlimited_quota_count": unlimited,
            "unknown_quota_count": unknown_quota,
            "total_success": total_success,
            "total_fail": total_fail,
            "by_type": by_type,
        }

    def account_health(self) -> dict:
        stats = self.get_stats()
        return {
            "healthy": stats["active"] > 0 or stats["unlimited_quota_count"] > 0,
            "status": "ok" if stats["active"] > 0 else "degraded",
            **stats,
        }


from services.proxy_management_service import proxy_management_service

account_service = AccountService(
    config.get_storage_backend(),
    proxy_reference_mutation=proxy_management_service.mutate_assignment_references,
)
proxy_management_service.bind_account_provider(account_service.list_accounts)
