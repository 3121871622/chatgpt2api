"""Bulk memory actions for pool accounts.

``m3m=false`` disables ChatGPT web memory; ``m3m=true`` enables it again.
"clear" calls the web memories endpoint and verifies the list is empty when
that endpoint is available.  The worker pool is intentionally small so a
full-pool sweep leaves foreground image requests alone.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Iterable

from services.account_credentials import decode_access_token_payload
from services.account_processing import account_processing_slot
from services.account_service import account_service
from services.openai_backend_api import OpenAIBackendAPI
from utils.diagnostics import diagnostic_excerpt
from utils.log import logger

MEMORY_WORKERS = 10
MEMORY_ACTIONS = {"disable", "enable", "clear"}
_SCHEDULED_LOCK = threading.Lock()
_SCHEDULED: set[str] = set()


def _account_id_from_token(token: str, account: dict) -> str:
    try:
        claims = decode_access_token_payload(token)
    except Exception:
        claims = {}
    auth = claims.get("https://api.openai.com/auth") or {}
    return str(
        auth.get("chatgpt_account_id")
        or account.get("account_id")
        or account.get("user_id")
        or ""
    ).strip()


def _memory_flags(settings: dict) -> dict[str, object]:
    return {
        "m3m": settings.get("m3m"),
        "sunshine": settings.get("sunshine"),
        "moonshine": settings.get("moonshine"),
        "memory_in_search": settings.get("memory_in_search"),
        "m3m_opted_out": settings.get("m3m_opted_out"),
    }


def _is_disabled(flags: dict) -> bool:
    if flags.get("m3m") is not False:
        return False
    return not bool(flags.get("sunshine")) and not bool(flags.get("moonshine"))


def _is_enabled(flags: dict) -> bool:
    return flags.get("m3m") is True


def _memory_count(payload: object) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in ("memories", "items", "data"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def _memory_ids(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    values = payload.get("memories") or payload.get("items") or payload.get("data")
    if not isinstance(values, list):
        return []
    ids: list[str] = []
    for item in values:
        if isinstance(item, dict):
            memory_id = str(item.get("id") or item.get("memory_id") or "").strip()
            if memory_id:
                ids.append(memory_id)
    return ids


def _invalid_token_error(error: str) -> bool:
    return any(
        marker in str(error or "").lower()
        for marker in (
            "401",
            "token_revoked",
            "invalidated oauth",
            "invalid access token",
        )
    )


class AccountMemoryService:
    def _set_enabled_one(
        self,
        token: str,
        *,
        enabled: bool,
        persist: bool = True,
        event: str,
    ) -> dict:
        try:
            token = account_service.ensure_access_token(token, event=event) or token
        except Exception:
            pass
        account = account_service.get_account(token) or {}
        desired_state = "enabled" if enabled else "disabled"
        already_done = str(account.get("memory_state") or "") == desired_state
        if not enabled and not already_done:
            already_done = bool(account.get("memory_disabled_at")) and (
                str(account.get("memory_disable_result") or "") == "disabled"
            )
        if already_done:
            return {
                "status": "skipped",
                "settings": account.get("memory_settings") or {},
                "error": "",
            }
        account_id = _account_id_from_token(token, account)
        if not account_id:
            return {"status": "failed", "settings": {}, "error": "missing chatgpt_account_id"}

        settings: dict = {}
        error = ""
        try:
            with account_processing_slot():
                with OpenAIBackendAPI(token) as backend:
                    payload = backend.get_account_user_settings(account_id)
                    settings = payload.get("settings") or {}
                    flags = _memory_flags(settings)
                    done = _is_enabled(flags) if enabled else _is_disabled(flags)
                    if not done:
                        backend.set_account_user_setting("m3m", enabled, account_id)
                        payload = backend.get_account_user_settings(account_id)
                        settings = payload.get("settings") or {}
                        flags = _memory_flags(settings)
                    if not enabled and bool(flags.get("memory_in_search")):
                        try:
                            backend.set_account_user_setting(
                                "memory_in_search",
                                False,
                                account_id,
                            )
                            payload = backend.get_account_user_settings(account_id)
                            settings = payload.get("settings") or {}
                            flags = _memory_flags(settings)
                        except Exception:
                            pass
            success = _is_enabled(flags) if enabled else _is_disabled(flags)
            if not success:
                error = f"memory flags still not {desired_state}: {_memory_flags(settings)}"
            status = desired_state if success else "failed"
        except Exception as exc:
            status = "failed"
            error = diagnostic_excerpt(exc, 500)
            settings = {}
            if _invalid_token_error(error):
                try:
                    account_service.handle_invalid_token(token, event, error=error)
                except Exception:
                    pass

        if persist:
            account_service.mark_memory_state(
                token,
                settings=settings,
                success=status == desired_state,
                error=error,
                state=desired_state if status == desired_state else "failed",
            )
        return {"status": status, "settings": settings, "error": error}

    def disable_one(self, token: str, *, persist: bool = True) -> dict:
        return self._set_enabled_one(
            token,
            enabled=False,
            persist=persist,
            event="disable_memory",
        )

    def enable_one(self, token: str, *, persist: bool = True) -> dict:
        return self._set_enabled_one(
            token,
            enabled=True,
            persist=persist,
            event="enable_memory",
        )

    def clear_one(self, token: str, *, persist: bool = True) -> dict:
        try:
            token = account_service.ensure_access_token(token, event="clear_memory") or token
        except Exception:
            pass
        account = account_service.get_account(token) or {}
        account_id = _account_id_from_token(token, account)
        if not account_id:
            return {"status": "failed", "settings": {}, "error": "missing chatgpt_account_id"}

        settings: dict = {}
        error = ""
        cleared = False
        try:
            with account_processing_slot():
                with OpenAIBackendAPI(token) as backend:
                    before = backend.get_account_memories(account_id)
                    before_count = _memory_count(before)
                    if before_count == 0:
                        cleared = True
                    else:
                        memory_ids = _memory_ids(before)
                        if not memory_ids:
                            raise RuntimeError(
                                "upstream memories endpoint does not expose memory ids"
                            )
                        for memory_id in memory_ids:
                            backend.delete_account_memory(memory_id, account_id)
                        after = backend.get_account_memories(account_id)
                        after_count = _memory_count(after)
                        cleared = after_count == 0 if after_count is not None else True
                        if not cleared:
                            error = f"memories still present: {after_count}"
            status = "cleared" if cleared else "failed"
        except Exception as exc:
            status = "failed"
            error = diagnostic_excerpt(exc, 500)
            settings = {}
            if _invalid_token_error(error):
                try:
                    account_service.handle_invalid_token(token, "clear_memory", error=error)
                except Exception:
                    pass

        if persist:
            account_service.mark_memory_state(
                token,
                settings=settings,
                success=status == "cleared",
                error=error,
                state="cleared" if status == "cleared" else "failed",
            )
        return {"status": status, "settings": settings, "error": error}

    def _run_tokens(
        self,
        tokens: list[str],
        *,
        action: str = "disable",
        progress_id: str = "",
        update_progress: bool = True,
    ) -> dict:
        action = action if action in MEMORY_ACTIONS else "disable"
        worker = {
            "disable": self.disable_one,
            "enable": self.enable_one,
            "clear": self.clear_one,
        }[action]
        if progress_id and update_progress:
            if account_service.get_refresh_progress(progress_id) is None:
                account_service.init_refresh_progress(progress_id, len(tokens))
        counts = {"disabled": 0, "enabled": 0, "cleared": 0, "failed": 0, "skipped": 0}
        errors: list[str] = []
        event_action = f"{action}_memory"
        done_message = {
            "disable": "已关闭记忆",
            "enable": "已开启记忆",
            "clear": "已清空记忆",
        }[action]
        try:
            from services.task_scheduler import task_scheduler

            workers = task_scheduler().memory_workers(MEMORY_WORKERS)
        except Exception:
            workers = MEMORY_WORKERS
        try:
            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix=f"memory-{action}",
            ) as pool:
                futures = {pool.submit(worker, token): token for token in tokens}
                for future in as_completed(futures):
                    token = futures[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {
                            "status": "failed",
                            "settings": {},
                            "error": diagnostic_excerpt(exc, 300),
                        }
                    status = str(result.get("status") or "failed")
                    counts[status] = counts.get(status, 0) + 1
                    if status == "failed" and result.get("error"):
                        errors.append(str(result["error"]))
                    if progress_id and update_progress:
                        account = account_service.get_account(token)
                        account_service.update_refresh_progress(
                            progress_id,
                            token,
                            account,
                            account_id=_account_id_from_token(token, account or {}),
                            action=event_action,
                            event_status=(
                                "success"
                                if status in {"disabled", "enabled", "cleared"}
                                else "failed"
                            ),
                            event_message=(
                                f"{done_message}失败"
                                if status == "failed"
                                else f"{done_message}（跳过）"
                                if status == "skipped"
                                else done_message
                            ),
                        )
        finally:
            with _SCHEDULED_LOCK:
                _SCHEDULED.difference_update(tokens)
        if progress_id and update_progress:
            account_service.finish_refresh_progress(
                progress_id,
                {
                    **counts,
                    "errors": errors[:50],
                },
            )
        logger.info({
            "event": "account_memory_action_done",
            "action": action,
            "total": len(tokens),
            **counts,
            "errors": len(errors),
        })
        return {**counts, "errors": errors}

    def start_batch(
        self,
        tokens: Iterable[str],
        *,
        progress_id: str,
        action: str = "disable",
    ) -> int:
        action = action if action in MEMORY_ACTIONS else "disable"
        unique = [str(token) for token in dict.fromkeys(tokens) if token]
        account_service.init_refresh_progress(progress_id, len(unique))
        with _SCHEDULED_LOCK:
            _SCHEDULED.update(unique)
        thread = threading.Thread(
            target=self._run_tokens,
            args=(unique,),
            kwargs={
                "action": action,
                "progress_id": progress_id,
                "update_progress": True,
            },
            name=f"memory-{action}-batch",
            daemon=True,
        )
        thread.start()
        return len(unique)

    def start_retry_failed(self, *, progress_id: str) -> int:
        """Re-run memory disable only for accounts whose last attempt failed."""
        failed = [
            str(account.get("access_token") or "")
            for account in account_service.list_accounts()
            if str(account.get("memory_disable_result") or "") == "failed"
            and str(account.get("access_token") or "")
        ]
        return self.start_batch(failed, progress_id=progress_id, action="disable")

    def schedule_tokens(self, tokens: Iterable[str]) -> int:
        """Auto-disable memory for newly imported accounts (background, 10 workers)."""
        eligible: list[str] = []
        with _SCHEDULED_LOCK:
            for token in tokens:
                normalized = str(token or "")
                if not normalized or normalized.count(".") != 2:
                    continue
                if normalized in _SCHEDULED:
                    continue
                account = account_service.get_account(normalized) or {}
                if str(account.get("memory_state") or "") == "disabled":
                    continue
                _SCHEDULED.add(normalized)
                eligible.append(normalized)
        if not eligible:
            return 0
        thread = threading.Thread(
            target=self._run_tokens,
            args=(eligible,),
            kwargs={"action": "disable", "update_progress": False},
            name="memory-disable-import",
            daemon=True,
        )
        thread.start()
        return len(eligible)


account_memory_service = AccountMemoryService()
