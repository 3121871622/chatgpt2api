from __future__ import annotations

from typing import Any, Literal
from fastapi import APIRouter, Body, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, Field

from api.call_contract import CallDetail, CallLogStatus, CallSummaryPage
from api.dashboard_contract import DashboardResponseView
from api.gallery_contract import (
    GalleryCleanupResult,
    GalleryCleanupTargetResult,
    GalleryCompressResult,
    GalleryGenBoxPushRequest,
    GalleryGenBoxPushResult,
    GalleryMediaFilter,
    GalleryPage,
)
from api.monitor_contract import MonitorRecordDetailView, RealtimeMonitorView
from contracts.auth import AuthView
from contracts.debug_logs import DebugLogConfigRequest
from contracts.governance import GovernanceConfigRequest, GovernanceRunRequest
from contracts.legacy_import import DebugLogClearRequest, LegacyImportRequest
from contracts.legacy_sync import LegacySyncConfigRequest, LegacySyncRunRequest
from contracts.local_backup import LocalBackupConfigRequest
from contracts.models import ModelCatalogView
from contracts.pool_alerts import PoolAlertConfigRequest, PoolAlertRunRequest
from contracts.proxy import (
    ProxyDefaultsMutation,
    ProxyDefaultsRequest,
    ProxyGroupDeleteMutation,
    ProxyGroupMutation,
    ProxyGroupPatch,
    ProxyGroupTestResponse,
    ProxyNodeImportRequest,
    ProxyNodeImportResult,
    ProxyView,
)
from contracts.settings import (
    AdaptiveSchedulerPatch,
    ImageStorageTargetCreateRequest,
    ImageStorageTargetDeleteResult,
    ImageStorageTargetMutation,
    ImageStorageTargetPatchRequest,
    ImageStorageTargetTestResult,
    ImageStorageTargetsView,
    PublicThirdPartyAppsView,
    SettingsMutationResult,
    SettingsPatch,
    SettingsView,
)
from api.support import require_admin, require_identity, resolve_image_base_url
from services.account_check_queue import job_queue
from services.task_scheduler import task_scheduler
from services.account_scoring import egress_snapshot
from services.account_service import account_service
from services.upstream_circuit import upstream_circuit
from services.adaptive_governor import governor
from services.auth_view import build_auth_view
from services.admin_auth_service import AdminAuthError, admin_auth_service
from services.homepage_service import HomepageError, homepage_service
from services.backup_service import BackupError, backup_service
from services.changelog_service import changelog_service
from services.config import config
from services.legacy_sync_service import legacy_sync_service
from services.account_governance_service import account_governance_service
from services.admission import admission_gate
from services.audit_service import audit_service
from services.debug_log_service import debug_log_service
from services.local_backup_service import local_backup_service
from services.pool_alert_service import pool_alert_service
from services.upstream_diagnostics import diagnose as diagnose_upstream
from services.database_maintenance import (
    DatabaseMaintenanceError,
    database_maintenance_service,
)
from services.gallery_view import (
    gallery_cleanup_result,
    gallery_cleanup_target_result,
    gallery_compress_result,
)
from services.genbox_push_service import GenBoxPushError, push_gallery_image
from services.image_service import (
    cleanup_expired_images,
    compress_images,
    delete_images,
    download_images_zip,
    get_image_download_response,
    get_image_response,
    get_thumbnail_response,
    list_images,
    storage_stats,
)
from services.dashboard_view import build_dashboard_view
from services.image_storage_service import ImageStorageError, image_storage_service
from services.image_tags_service import set_tags
from services.log_service import log_service
from services.model_catalog_service import get_model_catalog
from services.monitor_view import build_monitor_record_view, build_monitor_view
from services.proxy_management_service import (
    ProxyGroupInUseError,
    normalize_proxy_node_url,
    proxy_management_service,
)
from services.proxy_service import proxy_settings, test_clearance, test_proxy
from services.realtime_monitor_service import realtime_monitor_service
from services.retention_cleanup_service import retention_cleanup_coordinator
from services.settings_management_service import (
    SettingsRevisionConflictError,
    settings_management_service,
)


class ProxyTestRequest(BaseModel):
    url: str = ""


class PasswordLoginRequest(BaseModel):
    username: str = ""
    password: str = ""


class PasswordResetRequest(BaseModel):
    new_password: str = ""


class LoginSettingsPatch(BaseModel):
    mode: Literal["key", "password"]
    username: str = ""
    password: str | None = None
    admin_path: str | None = None


class AdminPathReset(BaseModel):
    admin_path: str = ""


class HomepagePatch(BaseModel):
    enabled: bool = False
    html: str = ""


class ClearanceTestRequest(BaseModel):
    target_url: str = "https://chatgpt.com"


class ProxyGroupTestRequest(BaseModel):
    id: str = ""
    node_id: str = ""
    url: str = ""


class ImageDeleteRequest(BaseModel):
    paths: list[str] = []
    start_date: str = ""
    end_date: str = ""
    all_matching: bool = False

class ImageDownloadRequest(BaseModel):
    paths: list[str]

class ImageTagsRequest(BaseModel):
    path: str
    tags: list[str]

class LogDeleteRequest(BaseModel):
    ids: list[str] = []
class BackupDeleteRequest(BaseModel):
    key: str = ""


class RetentionCleanupRequest(BaseModel):
    log_retention_hours: int | None = None
    image_retention_hours: int | None = None


class AccountCleanupRequest(BaseModel):
    auto_remove_invalid_accounts: bool | None = None
    auto_remove_rate_limited_accounts: bool | None = None


class SchedulerModeRequest(BaseModel):
    """Smart auto scheduling (default) or a manual lock on the saved values."""

    mode: Literal["auto", "manual"] = "auto"
    enabled: bool | None = None


class AccountGovernanceRunApiRequest(GovernanceRunRequest):
    """Governance run body plus the two-phase confirmation fingerprint.

    ``expected_fingerprint`` comes from a previous preview report; when it no
    longer matches the live candidate set the service refuses to execute.
    """

    expected_fingerprint: str = ""


class DatabaseMaintenanceRequest(BaseModel):
    operations: list[str] = Field(default_factory=list)


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _settings_write_error_message(exc: OSError) -> str:
    return (
        "保存设置失败：后端无法写入 Application Database。"
        "请检查数据库连接、权限和磁盘空间。"
        f"原始错误：{exc}"
    )


def _retention_cleanup_payload(body: RetentionCleanupRequest | None = None, *, dry_run: bool) -> dict[str, Any]:
    body = body or RetentionCleanupRequest()
    log_hours = body.log_retention_hours or config.log_retention_hours
    image_hours = body.image_retention_hours or config.image_retention_hours
    return retention_cleanup_coordinator.run_retention(
        log_retention_hours=log_hours,
        image_retention_hours=image_hours,
        dry_run=dry_run,
    )


def _notify_retention_cleanup_if_changed(changed_fields: list[str]) -> None:
    retention_fields = {"log_retention_hours", "image_retention_hours"}
    if retention_fields.intersection(changed_fields):
        retention_cleanup_coordinator.notify_retention_change()


def _account_cleanup_payload(body: AccountCleanupRequest | None = None, *, dry_run: bool) -> dict[str, Any]:
    body = body or AccountCleanupRequest()
    kwargs = {
        "remove_invalid": body.auto_remove_invalid_accounts,
        "remove_rate_limited": body.auto_remove_rate_limited_accounts,
    }
    if dry_run:
        return account_service.preview_auto_remove_accounts(**kwargs)
    return account_service.cleanup_auto_remove_accounts(**kwargs)


def _proxy_group_id(value: object) -> str:
    raw = _clean_text(value)
    if raw.lower().startswith("group:"):
        raw = raw.split(":", 1)[1]
    return raw.strip()


def create_router(app_version: str) -> APIRouter:
    router = APIRouter()

    @router.get("/auth/config")
    async def auth_config():
        """Public login-method projection; never exposes password material."""
        return admin_auth_service.view()

    @router.get("/api/system/homepage")
    async def get_homepage(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return homepage_service.view()

    @router.put("/api/system/homepage")
    async def save_homepage(
        body: HomepagePatch,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            return homepage_service.update(enabled=body.enabled, html=body.html)
        except HomepageError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.post("/auth/login", response_model=AuthView)
    async def login(
        authorization: str | None = Header(default=None),
        body: PasswordLoginRequest | None = Body(default=None),
    ):
        settings = admin_auth_service.view()
        if settings["mode"] == "password":
            if body is None:
                raise HTTPException(
                    status_code=401,
                    detail={"error": "当前仅允许账号密码登录，请使用用户名和密码"},
                )
            identity = admin_auth_service.verify(body.username, body.password)
            if identity is None:
                raise HTTPException(status_code=401, detail={"error": "用户名或密码错误"})
            token = admin_auth_service.issue_session(identity)
            return build_auth_view(app_version, identity, session_token=token)
        identity = require_identity(authorization)
        return build_auth_view(app_version, identity)

    @router.post("/auth/password/reset")
    async def reset_admin_password(
        body: PasswordResetRequest,
        authorization: str | None = Header(default=None),
    ):
        # The recovery key remains usable even when key login is disabled.
        require_admin(authorization)
        try:
            return admin_auth_service.reset_password(body.new_password)
        except AdminAuthError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.get("/api/system/login-settings")
    async def get_login_settings(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return admin_auth_service.view(include_admin_path=True)

    @router.put("/api/system/login-settings")
    async def save_login_settings(
        body: LoginSettingsPatch,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            return admin_auth_service.update(
                mode=body.mode,
                username=body.username,
                password=body.password,
                admin_path=body.admin_path,
            )
        except AdminAuthError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.post("/api/system/admin-path/recover")
    async def recover_admin_path(
        body: AdminPathReset,
        authorization: str | None = Header(default=None),
    ):
        """Recovery entrance for a forgotten custom admin path.

        Requires the existing admin/recovery key; survives a lost custom path
        because this endpoint is not behind the SPA route.
        """
        require_admin(authorization)
        try:
            return admin_auth_service.reset_admin_path(body.admin_path)
        except AdminAuthError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.get("/auth/status", response_model=AuthView)
    async def auth_status(authorization: str | None = Header(default=None)):
        try:
            identity = require_identity(authorization)
        except HTTPException:
            return build_auth_view(app_version)
        return build_auth_view(app_version, identity)

    @router.get("/version")
    async def get_version():
        return {"version": app_version}

    @router.get("/api/system/changelog")
    async def get_changelog(
        limit: int = Query(default=10, ge=1, le=50),
        authorization: str | None = Header(default=None),
    ):
        """Local changelog projection (reads CHANGELOG.md, never the network)."""
        require_admin(authorization)
        return await run_in_threadpool(changelog_service.view, limit)

    @router.get("/api/system/legacy-sync")
    async def get_legacy_sync(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        status = legacy_sync_service.state()
        probes = await run_in_threadpool(legacy_sync_service.probe)
        return {**status, "probes": probes}

    @router.post("/api/system/legacy-sync")
    async def save_legacy_sync(
        body: LegacySyncConfigRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        patch = body.model_dump(exclude_none=True)
        config = await run_in_threadpool(legacy_sync_service.save_config, patch)
        legacy_sync_service.start_loop()
        probes = await run_in_threadpool(legacy_sync_service.probe)
        return {**legacy_sync_service.state(), "config": config, "probes": probes}

    @router.post("/api/system/legacy-sync/run")
    async def run_legacy_sync(
        body: LegacySyncRunRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        if legacy_sync_service.state()["running"]:
            raise HTTPException(status_code=409, detail={"error": "同步任务正在执行"})
        result = await run_in_threadpool(
            legacy_sync_service.sync,
            trigger="manual",
            dry_run=body.dry_run,
        )
        return {"result": result, **legacy_sync_service.state()}

    @router.post("/api/system/legacy-sync/upload")
    async def upload_legacy_database(
        file: UploadFile = File(...),
        authorization: str | None = Header(default=None),
    ):
        """上传旧版 SQLite 数据库文件，用于按需导入账号/配置。"""
        require_admin(authorization)
        try:
            summary = await run_in_threadpool(
                legacy_sync_service.save_upload,
                file.filename,
                file.file,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        audit_service.record(
            "legacy_sync.upload",
            target=str(summary.get("import_id") or ""),
            detail={
                "filename": summary.get("filename"),
                "accounts": summary.get("accounts"),
                "size_bytes": summary.get("size_bytes"),
            },
        )
        return summary

    @router.get("/api/system/legacy-sync/imports")
    async def list_legacy_imports(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"items": await run_in_threadpool(legacy_sync_service.list_imports)}

    @router.delete("/api/system/legacy-sync/imports")
    async def delete_all_legacy_imports(
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        removed = await run_in_threadpool(legacy_sync_service.delete_all_imports)
        audit_service.record("legacy_sync.import_delete_all", target="all", detail={"removed": removed})
        return {"removed": removed}

    @router.delete("/api/system/legacy-sync/imports/{import_id}")
    async def delete_legacy_import(
        import_id: str,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        deleted = await run_in_threadpool(legacy_sync_service.delete_import, import_id)
        if not deleted:
            raise HTTPException(status_code=404, detail={"error": "import not found"})
        audit_service.record("legacy_sync.import_delete", target=import_id)
        return {"deleted": import_id}

    @router.get("/api/system/legacy-sync/imports/{import_id}/accounts")
    async def preview_legacy_import_accounts(
        import_id: str,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=200),
        keyword: str = "",
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            return await run_in_threadpool(
                legacy_sync_service.preview_accounts,
                import_id,
                page=page,
                page_size=page_size,
                keyword=keyword,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail={"error": str(exc)}) from exc

    @router.post("/api/system/legacy-sync/imports/{import_id}/import")
    async def import_legacy_selection(
        import_id: str,
        body: LegacyImportRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        if not body.dry_run and not body.confirm:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": "importing requires confirm=true",
                        "type": "invalid_request_error",
                        "code": "confirmation_required",
                    }
                },
            )
        try:
            result = await run_in_threadpool(
                legacy_sync_service.import_selection,
                import_id,
                body.options.model_dump(),
                dry_run=body.dry_run,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail={"error": str(exc)}) from exc
        if not body.dry_run:
            audit_service.record(
                "legacy_sync.import",
                target=import_id,
                detail={
                    "accounts_added": result.get("accounts_added"),
                    "settings_updated": len(result.get("settings_updated") or []),
                    "groups_added": result.get("groups_added"),
                    "call_records_imported": result.get("call_records_imported"),
                },
            )
        return {"result": result}

    @router.get("/api/system/load")
    async def get_load_status(authorization: str | None = Header(default=None)):
        """当前准入闸门与线程池状态（500 并发的第一手观测数据）。"""
        require_admin(authorization)
        gate = admission_gate()
        try:
            from anyio.to_thread import current_default_thread_limiter

            thread_tokens = int(
                getattr(current_default_thread_limiter(), "total_tokens", 0) or 0
            )
        except Exception:
            thread_tokens = 0
        return {
            "admission": gate.stats(),
            "thread_tokens": thread_tokens,
            "governor": governor().snapshot(),
            "scheduler": task_scheduler().snapshot(),
            "account_index": account_service.selection_index_stats(),
            "account_lock": account_service.lock_diagnostics(),
            "queue": job_queue().stats(),
            "upstream_circuit": upstream_circuit.snapshot(),
            "egress": egress_snapshot(),
        }

    @router.get("/api/system/pool-alerts")
    async def get_pool_alerts(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(pool_alert_service.status)

    @router.post("/api/system/pool-alerts")
    async def save_pool_alerts(
        body: PoolAlertConfigRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        config = await run_in_threadpool(
            pool_alert_service.save_config,
            body.model_dump(exclude_none=True),
        )
        if config["enabled"]:
            pool_alert_service.start_loop()
        return {**pool_alert_service.status(), "config": config}

    @router.post("/api/system/pool-alerts/run")
    async def run_pool_alerts(
        body: PoolAlertRunRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        report = await run_in_threadpool(
            pool_alert_service.run_once,
            force=True,
            dry_run=body.dry_run,
        )
        return {"report": report, **pool_alert_service.status()}

    @router.get("/api/system/account-governance")
    async def get_account_governance(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(account_governance_service.status)

    @router.post("/api/system/account-governance")
    async def save_account_governance(
        body: GovernanceConfigRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        config = await run_in_threadpool(
            account_governance_service.save_config,
            body.model_dump(exclude_none=True),
        )
        if config["enabled"]:
            account_governance_service.start_loop()
        return {**account_governance_service.status(), "config": config}

    @router.post("/api/system/account-governance/run")
    async def run_account_governance(
        body: AccountGovernanceRunApiRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        current_config = await run_in_threadpool(account_governance_service.get_config)
        mode = str(body.mode or current_config.get("mode") or "report").strip().lower()
        if mode not in {"off", "report", "disable", "purge"}:
            mode = "report"
        destructive = mode in {"disable", "purge"}
        expected_fingerprint = body.expected_fingerprint.strip() or None

        # Confirmation is validated *before* run_once: an unconfirmed purge or
        # disable must never reach the mutating path (the previous version
        # executed first and only then answered 400, so a caller that ignored
        # the error had already deleted/deactivated accounts).
        if destructive and not body.dry_run and not body.confirm:
            preview = await run_in_threadpool(
                account_governance_service.preview_once,
                mode_override=mode,
            )
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": "destructive governance mode requires confirm=true",
                        "type": "invalid_request_error",
                        "code": "confirmation_required",
                    },
                    "preview": preview,
                },
            )

        report = await run_in_threadpool(
            account_governance_service.run_once,
            mode_override=mode,
            dry_run=body.dry_run,
            # confirm=None keeps the legacy non-destructive path; True is the
            # explicit confirmation for disable/purge (and is also accepted for
            # report/dry-run calls).
            confirm=True if body.confirm else None,
            expected_fingerprint=expected_fingerprint,
        )
        audit_service.record(
            "governance.run",
            target=str(report.get("effective_mode") or ""),
            detail={
                "dry_run": report.get("dry_run"),
                "dead_accounts": report.get("dead_accounts"),
                "removed": report.get("removed"),
                "disabled": report.get("disabled"),
            },
        )
        return {"report": report, **account_governance_service.status()}

    @router.get("/api/system/upstream-diagnostics")
    async def upstream_diagnostics(authorization: str | None = Header(default=None)):
        """上游连通性自检：DNS / 直连 TCP+TLS / 当前默认代理。"""
        require_admin(authorization)
        return await run_in_threadpool(diagnose_upstream)

    @router.get("/api/system/debug-logs")
    async def get_debug_logs(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(debug_log_service.status)

    @router.post("/api/system/debug-logs")
    async def save_debug_logs(
        body: DebugLogConfigRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        config = await run_in_threadpool(
            debug_log_service.save_config,
            body.model_dump(exclude_none=True),
        )
        audit_service.record("debug_logs.config", detail=dict(config))
        return {**await run_in_threadpool(debug_log_service.status), "config": config}

    @router.post("/api/system/debug-logs/export")
    async def export_debug_logs(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        content, name = await run_in_threadpool(debug_log_service.export_zip)
        audit_service.record("debug_logs.export", target=name, detail={"bytes": len(content)})
        return Response(
            content,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @router.post("/api/system/debug-logs/clear")
    async def clear_debug_logs(
        body: DebugLogClearRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        if not body.confirm:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": "clearing debug logs requires confirm=true",
                        "type": "invalid_request_error",
                        "code": "confirmation_required",
                    }
                },
            )
        result = await run_in_threadpool(debug_log_service.clear)
        audit_service.record("debug_logs.clear", detail=dict(result))
        return {**result, **await run_in_threadpool(debug_log_service.status)}

    @router.get("/api/system/local-backup")
    async def get_local_backup(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(local_backup_service.status)

    @router.post("/api/system/local-backup")
    async def save_local_backup(
        body: LocalBackupConfigRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        config = await run_in_threadpool(
            local_backup_service.save_config,
            body.model_dump(exclude_none=True),
        )
        if config["enabled"]:
            local_backup_service.start_loop()
        audit_service.record("backup.config", detail=dict(config))
        return {**local_backup_service.status(), "config": config}

    @router.post("/api/system/local-backup/run")
    async def run_local_backup(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        report = await run_in_threadpool(local_backup_service.run_once)
        audit_service.record(
            "backup.run",
            target=str(report.get("file") or ""),
            detail={"size_bytes": report.get("size_bytes"), "pruned": report.get("pruned")},
        )
        return {"report": report, **local_backup_service.status()}

    @router.get("/api/system/audit")
    async def list_audit_entries(
        limit: int = Query(default=100, ge=1, le=500),
        action: str = "",
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return await run_in_threadpool(
            audit_service.list,
            limit=limit,
            action=action,
        )

    @router.get("/metrics")
    async def prometheus_metrics(authorization: str | None = Header(default=None)):
        """Prometheus 文本指标（管理员凭证鉴权，不对外匿名暴露）。"""
        require_admin(authorization)
        pool = await run_in_threadpool(pool_alert_service.measure)
        queue_stats = job_queue().stats()
        database_status = await run_in_threadpool(database_maintenance_service.status)
        breaker = upstream_circuit.snapshot()
        lines = [
            "# HELP qingmeng_pool_accounts 账号总数（按凭据状态分桶）",
            "# TYPE qingmeng_pool_accounts gauge",
            f'qingmeng_pool_accounts{{state="total"}} {pool["total"]}',
            f'qingmeng_pool_accounts{{state="usable"}} {pool["usable"]}',
            f'qingmeng_pool_accounts{{state="recoverable"}} {pool["recoverable"]}',
            f'qingmeng_pool_accounts{{state="unavailable"}} {pool["unavailable"]}',
            "# HELP qingmeng_pool_refresh_valid 可续期账号数",
            "# TYPE qingmeng_pool_refresh_valid gauge",
            f"qingmeng_pool_refresh_valid {pool['refresh_valid']}",
            "# HELP qingmeng_pool_refresh_ratio_percent 可续期占比",
            "# TYPE qingmeng_pool_refresh_ratio_percent gauge",
            f"qingmeng_pool_refresh_ratio_percent {pool['refresh_ratio_percent']}",
            "# HELP qingmeng_queue_active_jobs 运行中的任务数",
            "# TYPE qingmeng_queue_active_jobs gauge",
            f'qingmeng_queue_active_jobs {queue_stats.get("active_jobs", 0)}',
            "# HELP qingmeng_queue_queued_tokens 排队中的令牌数",
            "# TYPE qingmeng_queue_queued_tokens gauge",
            f'qingmeng_queue_queued_tokens {queue_stats.get("queued_tokens", 0)}',
            "# HELP qingmeng_queue_processed_total 累计处理数",
            "# TYPE qingmeng_queue_processed_total counter",
            f'qingmeng_queue_processed_total {queue_stats.get("processed_total", 0)}',
            "# HELP qingmeng_database_size_bytes 数据库体积",
            "# TYPE qingmeng_database_size_bytes gauge",
            f'qingmeng_database_size_bytes {database_status.get("size_bytes", 0)}',
            "# HELP qingmeng_inflight_requests 在途请求数（准入闸门）",
            "# TYPE qingmeng_inflight_requests gauge",
            f'qingmeng_inflight_requests {admission_gate().stats()["inflight"]}',
            "# HELP qingmeng_queued_requests 排队请求数（准入闸门）",
            "# TYPE qingmeng_queued_requests gauge",
            f'qingmeng_queued_requests {admission_gate().stats()["queued"]}',
            "# HELP qingmeng_request_limit 准入上限",
            "# TYPE qingmeng_request_limit gauge",
            f'qingmeng_request_limit {admission_gate().stats()["limit"]}',
            "# HELP qingmeng_requests_rejected_total 准入拒绝累计",
            "# TYPE qingmeng_requests_rejected_total counter",
            f'qingmeng_requests_rejected_total {admission_gate().stats()["rejected_total"]}',
            "# HELP qingmeng_upstream_circuit_open 上游图片链路熔断是否打开",
            "# TYPE qingmeng_upstream_circuit_open gauge",
            f'qingmeng_upstream_circuit_open {1 if breaker.get("open") else 0}',
            "# HELP qingmeng_upstream_failure_rate 上游滚动失败率",
            "# TYPE qingmeng_upstream_failure_rate gauge",
            f'qingmeng_upstream_failure_rate {breaker.get("error_rate", 0)}',
            "# HELP qingmeng_upstream_breaker_samples 熔断窗口样本数",
            "# TYPE qingmeng_upstream_breaker_samples gauge",
            f'qingmeng_upstream_breaker_samples {breaker.get("samples", 0)}',
        ]
        return Response(
            "\n".join(lines) + "\n",
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @router.get("/api/system/scheduler")
    async def get_scheduler(authorization: str | None = Header(default=None)):
        """Hardware profile, live load and the effective adaptive budget."""
        require_admin(authorization)
        return await run_in_threadpool(governor().snapshot)

    @router.post("/api/system/scheduler/mode")
    async def set_scheduler_mode(
        body: SchedulerModeRequest,
        authorization: str | None = Header(default=None),
    ):
        """Switch between smart auto scheduling and a manual lock."""
        require_admin(authorization)
        current = config.adaptive_settings
        updates = dict(current)
        updates["mode"] = body.mode
        if body.enabled is not None:
            updates["enabled"] = bool(body.enabled)
        config.update({"adaptive": updates})
        try:
            await run_in_threadpool(governor().tick)
        except Exception:
            pass
        return await run_in_threadpool(governor().snapshot)

    @router.post("/api/system/scheduler/policy")
    async def save_scheduler_policy(
        body: AdaptiveSchedulerPatch,
        authorization: str | None = Header(default=None),
    ):
        """Persist the adaptive policy without touching any user preset.

        Thresholds, recovery hold time, sample interval, scale floor and the
        hardware tier override are all owned here; the governor only derives a
        temporary effective budget from them.
        """
        require_admin(authorization)
        updates = dict(config.adaptive_settings)
        patch = body.model_dump(mode="python", exclude_none=True)
        updates.update(patch)
        cpu_high = float(updates.get("cpu_high") or 0)
        cpu_low = float(updates.get("cpu_low") or 0)
        if cpu_low >= cpu_high:
            raise HTTPException(
                status_code=400,
                detail={"error": "CPU 恢复阈值必须小于过载阈值"},
            )
        mem_high = float(updates.get("mem_high") or 0)
        mem_low = float(updates.get("mem_low") or 0)
        if mem_low >= mem_high:
            raise HTTPException(
                status_code=400,
                detail={"error": "内存恢复阈值必须小于过载阈值"},
            )
        config.update({"adaptive": updates})
        try:
            await run_in_threadpool(governor().tick)
        except Exception:
            pass
        return await run_in_threadpool(governor().snapshot)

    @router.get("/api/system/queue")
    async def get_account_queue(authorization: str | None = Header(default=None)):
        """Detection / AT-refresh job queue: lanes, workers, throughput."""
        require_admin(authorization)
        queue = job_queue()
        return {
            "stats": queue.stats(),
            "jobs": queue.jobs()[:50],
        }

    @router.post("/api/system/queue/{job_id}/cancel")
    async def cancel_account_job(
        job_id: str,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        if not job_queue().cancel(job_id):
            raise HTTPException(status_code=404, detail={"error": "job not found or already finished"})
        return {"cancelled": True, "job_id": job_id}

    @router.get("/api/system/database")
    async def get_database_maintenance(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(database_maintenance_service.status)

    @router.post("/api/system/database/maintenance")
    async def run_database_maintenance(
        body: DatabaseMaintenanceRequest,
        authorization: str | None = Header(default=None),
    ):
        """One-click repair / optimise / compact for the application database."""
        require_admin(authorization)
        try:
            return await run_in_threadpool(database_maintenance_service.run, body.operations or None)
        except DatabaseMaintenanceError as exc:
            raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc

    @router.get("/api/system/proxy-health")
    async def get_proxy_health(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {
            "health": proxy_settings.proxy_health_snapshot(),
            "fallback_reference": proxy_settings.get_fallback_proxy_reference(),
        }

    @router.post("/api/system/proxy-health/probe")
    async def probe_proxy_health(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(proxy_settings.probe_all_egress)

    @router.get("/api/settings", response_model=SettingsView)
    async def get_settings(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(settings_management_service.view)

    @router.get("/api/third-party-apps", response_model=PublicThirdPartyAppsView)
    async def get_third_party_apps(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        return await run_in_threadpool(settings_management_service.public_third_party_apps_view)

    @router.patch("/api/settings", response_model=SettingsMutationResult)
    async def save_settings(body: SettingsPatch, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            result = await run_in_threadpool(settings_management_service.update, body)
            _notify_retention_cleanup_if_changed(result.changed_fields)
            return result
        except SettingsRevisionConflictError as exc:
            raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail={"error": _settings_write_error_message(exc)}) from exc

    @router.post("/api/settings/retention-cleanup/preview")
    async def preview_retention_cleanup(body: RetentionCleanupRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(_retention_cleanup_payload, body, dry_run=True)

    @router.post("/api/settings/retention-cleanup/run")
    async def run_retention_cleanup(body: RetentionCleanupRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(_retention_cleanup_payload, body, dry_run=False)

    @router.post("/api/settings/account-cleanup/preview")
    async def preview_account_cleanup(body: AccountCleanupRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(_account_cleanup_payload, body, dry_run=True)

    @router.post("/api/settings/account-cleanup/run")
    async def run_account_cleanup(body: AccountCleanupRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(_account_cleanup_payload, body, dry_run=False)

    @router.get("/api/model-catalog", response_model=ModelCatalogView)
    async def model_catalog(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        return await run_in_threadpool(get_model_catalog)

    @router.get("/api/images", response_model=GalleryPage)
    async def get_images(
        request: Request,
        start_date: str = "",
        end_date: str = "",
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=24, ge=1, le=500),
        media_type: GalleryMediaFilter = Query(default="all"),
        tag: str = Query(default=""),
        search: str = Query(default=""),
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return await run_in_threadpool(
            list_images,
            resolve_image_base_url(request),
            start_date=start_date.strip(),
            end_date=end_date.strip(),
            limit=page_size,
            offset=(page - 1) * page_size,
            media_type=media_type,
            tag=tag.strip(),
            search=search.strip(),
        )

    @router.post("/api/images/retention-cleanup", response_model=GalleryCleanupResult)
    async def cleanup_expired_images_endpoint(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        result = await run_in_threadpool(cleanup_expired_images)
        return gallery_cleanup_result(result)

    @router.get("/images/{image_path:path}", include_in_schema=False)
    async def get_image(image_path: str):
        return get_image_response(image_path)

    @router.get("/image-thumbnails/{image_path:path}", include_in_schema=False)
    async def get_image_thumbnail(image_path: str):
        return get_thumbnail_response(image_path)

    @router.post("/api/images/delete")
    async def delete_images_endpoint(body: ImageDeleteRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return delete_images(body.paths, start_date=body.start_date.strip(), end_date=body.end_date.strip(), all_matching=body.all_matching)

    @router.post("/api/images/download")
    async def download_images_endpoint(body: ImageDownloadRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        buf = download_images_zip(body.paths)
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="images.zip"'},
        )

    @router.get("/api/images/download/{image_path:path}")
    async def download_single_image_endpoint(image_path: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return get_image_download_response(image_path)

    @router.post("/api/images/genbox-push", response_model=GalleryGenBoxPushResult)
    async def push_image_to_genbox(
        body: GalleryGenBoxPushRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            return await run_in_threadpool(push_gallery_image, body.path)
        except GenBoxPushError as exc:
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": exc.message},
            ) from exc

    @router.get("/api/logs", response_model=CallSummaryPage)
    async def get_logs(
        type: str = "",
        start_date: str = "",
        end_date: str = "",
        status: CallLogStatus = "",
        endpoint: str = "",
        model: str = "",
        account: str = "",
        conversation_id: str = "",
        search: str = "",
        limit: int = Query(default=200, ge=1, le=20000),
        offset: int = Query(default=0, ge=0),
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return await run_in_threadpool(
            log_service.list_page,
            type=type.strip(),
            start_date=start_date.strip(),
            end_date=end_date.strip(),
            status=status.strip(),
            endpoint=endpoint.strip(),
            model=model.strip(),
            account=account.strip(),
            conversation_id=conversation_id.strip(),
            search=search.strip(),
            limit=limit,
            offset=offset,
        )

    @router.get("/api/logs/{log_id}", response_model=CallDetail)
    async def get_log_detail(log_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        detail = await run_in_threadpool(log_service.get_detail, log_id)
        if detail is None:
            raise HTTPException(status_code=404, detail={"error": "log not found"})
        return detail

    @router.post("/api/logs/delete")
    async def delete_logs(body: LogDeleteRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(log_service.delete, body.ids)

    @router.get(
        "/api/monitor/realtime",
        response_model=RealtimeMonitorView,
        response_model_exclude_none=True,
    )
    async def get_realtime_monitor(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return build_monitor_view(realtime_monitor_service.snapshot())

    @router.get(
        "/api/monitor/realtime/{call_id}",
        response_model=MonitorRecordDetailView,
        response_model_exclude_none=True,
    )
    async def get_realtime_monitor_call(
        call_id: str,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        record = realtime_monitor_service.call_detail(call_id)
        if record is None:
            raise HTTPException(status_code=404, detail={"error": "monitor call not found"})
        return build_monitor_record_view(record)

    @router.post("/api/proxy/test")
    async def test_proxy_endpoint(body: ProxyTestRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        explicit_url = (body.url or "").strip()
        if explicit_url:
            try:
                candidate = normalize_proxy_node_url(explicit_url)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        else:
            candidate = proxy_settings.get_profile(upstream=True).proxy_url
        if not candidate:
            raise HTTPException(status_code=400, detail={"error": "proxy url is required"})
        return {"result": await run_in_threadpool(test_proxy, candidate)}

    @router.get("/api/proxy/view", response_model=ProxyView)
    async def get_proxy_view(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return await run_in_threadpool(proxy_management_service.view)

    @router.post("/api/proxy/defaults", response_model=ProxyDefaultsMutation)
    async def save_proxy_defaults(
        body: ProxyDefaultsRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            return await run_in_threadpool(
                proxy_management_service.save_defaults,
                body.default_reference,
                body.fallback_reference,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail={"error": _settings_write_error_message(exc)}) from exc

    @router.post("/api/proxy/groups", response_model=ProxyGroupMutation)
    async def save_proxy_group(body: ProxyGroupPatch, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return await run_in_threadpool(proxy_management_service.save_group, body)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail={"error": _settings_write_error_message(exc)}) from exc

    @router.post("/api/proxy/nodes/import", response_model=ProxyNodeImportResult)
    async def import_proxy_nodes(
        body: ProxyNodeImportRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            return await run_in_threadpool(
                proxy_management_service.import_nodes,
                body.text,
                body.existing_urls,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.delete("/api/proxy/groups/{group_id}", response_model=ProxyGroupDeleteMutation)
    async def delete_proxy_group(group_id: str, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return await run_in_threadpool(proxy_management_service.delete_group, group_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail={"error": str(exc.args[0])}) from exc
        except ProxyGroupInUseError as exc:
            raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail={"error": _settings_write_error_message(exc)}) from exc

    @router.post("/api/proxy/groups/test", response_model=ProxyGroupTestResponse)
    async def test_proxy_group_endpoint(body: ProxyGroupTestRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        explicit_url = (body.url or "").strip()
        if explicit_url:
            try:
                candidate = normalize_proxy_node_url(explicit_url)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
            result = await run_in_threadpool(test_proxy, candidate)
            return proxy_management_service.group_test_response([("", result)])

        group_id = _proxy_group_id(body.id)
        if not group_id:
            raise HTTPException(status_code=400, detail={"error": "proxy group id or url is required"})
        group_list = await run_in_threadpool(proxy_management_service.list_groups)
        group = next((item for item in group_list.groups if item.id == group_id), None)
        if group is None:
            raise HTTPException(status_code=404, detail={"error": "proxy group not found"})
        node_id = _clean_text(body.node_id)
        nodes = [
            node for node in group.nodes
            if node.enabled
            and node.url
            and (not node_id or node.id == node_id)
        ]
        if not nodes:
            raise HTTPException(status_code=400, detail={"error": "proxy group node url is required"})
        results = [
            (node.id, await run_in_threadpool(test_proxy, node.url))
            for node in nodes
        ]
        return proxy_management_service.group_test_response(results)

    @router.get("/api/proxy/runtime")
    async def get_proxy_runtime_endpoint(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {
            "runtime": config.get_public_proxy_runtime_settings(),
            "status": proxy_settings.get_runtime_status(),
        }

    @router.post("/api/proxy/clearance/test")
    async def test_proxy_clearance_endpoint(body: ClearanceTestRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"result": await run_in_threadpool(test_clearance, body.target_url)}

    @router.get("/api/dashboard", response_model=DashboardResponseView)
    async def get_dashboard(
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        return await run_in_threadpool(
            build_dashboard_view,
            app_version=app_version,
        )

    @router.post("/api/backup/test")
    async def test_backup_connection(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"result": await run_in_threadpool(backup_service.test_connection)}
        except BackupError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.post("/api/image-storage/test")
    async def test_image_storage_endpoint(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"result": await run_in_threadpool(image_storage_service.test_webdav)}

    @router.post("/api/image-storage/sync")
    async def sync_image_storage_endpoint(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"result": await run_in_threadpool(image_storage_service.sync_all)}
        except ImageStorageError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.get("/api/image-storage/r2/targets", response_model=ImageStorageTargetsView)
    async def list_image_storage_r2_targets(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return {"targets": await run_in_threadpool(image_storage_service.r2_target_views)}

    @router.post(
        "/api/image-storage/r2/targets",
        response_model=ImageStorageTargetMutation,
        status_code=201,
    )
    async def create_image_storage_r2_target(
        body: ImageStorageTargetCreateRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            target = await run_in_threadpool(
                image_storage_service.save_r2_target,
                body.model_dump(),
            )
            return {"target": target}
        except ImageStorageError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.patch(
        "/api/image-storage/r2/targets/{target_id}",
        response_model=ImageStorageTargetMutation,
    )
    async def update_image_storage_r2_target(
        target_id: str,
        body: ImageStorageTargetPatchRequest,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        current = await run_in_threadpool(image_storage_service.get_r2_target, target_id)
        if current is None:
            raise HTTPException(status_code=404, detail={"error": "R2 图片存储目标不存在"})
        payload = body.model_dump(exclude_unset=True)
        payload["id"] = target_id
        try:
            target = await run_in_threadpool(image_storage_service.save_r2_target, payload)
            return {"target": target}
        except ImageStorageError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.delete(
        "/api/image-storage/r2/targets/{target_id}",
        response_model=ImageStorageTargetDeleteResult,
    )
    async def delete_image_storage_r2_target(
        target_id: str,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            deleted = await run_in_threadpool(image_storage_service.delete_r2_target, target_id)
        except ImageStorageError as exc:
            raise HTTPException(status_code=409, detail={"error": str(exc)}) from exc
        if not deleted:
            raise HTTPException(status_code=404, detail={"error": "R2 图片存储目标不存在"})
        return {"deleted": True, "id": target_id}

    @router.post(
        "/api/image-storage/r2/targets/{target_id}/test",
        response_model=ImageStorageTargetTestResult,
    )
    async def test_image_storage_r2_target(
        target_id: str,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        try:
            result = await run_in_threadpool(image_storage_service.test_r2_target, target_id)
        except ImageStorageError as exc:
            raise HTTPException(status_code=404, detail={"error": str(exc)}) from exc
        return {"result": {**result, "target_id": target_id}}

    @router.get("/api/backups")
    async def get_backups(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {
                "items": await run_in_threadpool(backup_service.list_backups),
                "state": backup_service.get_status(),
            }
        except BackupError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.post("/api/backups/run")
    async def run_backup_endpoint(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            return {"result": await run_in_threadpool(backup_service.run_backup)}
        except BackupError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.post("/api/backups/delete")
    async def delete_backup_endpoint(body: BackupDeleteRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        try:
            await run_in_threadpool(backup_service.delete_backup, body.key)
            return {"ok": True}
        except BackupError as exc:
            raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc

    @router.post("/api/images/tags")
    async def update_image_tags(body: ImageTagsRequest, authorization: str | None = Header(default=None)):
        require_admin(authorization)
        rel = body.path.strip().lstrip("/")
        if not rel:
            raise HTTPException(status_code=400, detail={"error": "path is required"})
        tags = set_tags(rel, body.tags)
        return {"ok": True, "tags": tags}

    @router.get("/api/images/storage")
    async def get_image_storage(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        return storage_stats()

    @router.post("/api/images/storage/compress", response_model=GalleryCompressResult)
    async def compress_all_images(authorization: str | None = Header(default=None)):
        require_admin(authorization)
        result = await run_in_threadpool(compress_images)
        return gallery_compress_result(result)

    @router.post(
        "/api/images/storage/cleanup-to-target",
        response_model=GalleryCleanupTargetResult,
    )
    async def cleanup_to_target(
        target_free_mb: int = Query(default=500, ge=1),
        dry_run: bool = False,
        authorization: str | None = Header(default=None),
    ):
        require_admin(authorization)
        result = await run_in_threadpool(
            retention_cleanup_coordinator.cleanup_images_to_target,
            target_free_mb,
            dry_run=dry_run,
        )
        return gallery_cleanup_target_result(
            result,
            target_free_mb=target_free_mb,
            dry_run=dry_run,
        )

    return router
