from __future__ import annotations

from contextlib import asynccontextmanager
from threading import Event

from anyio.to_thread import current_default_thread_limiter
from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from api import accounts, ai, image_tasks, prompts, system
from api.errors import install_exception_handlers
from api.support import resolve_web_asset, require_admin, start_account_lifecycle_watcher
from services.account_check_queue import KIND_CHECK, job_queue
from services.account_service import account_service
from services.adaptive_governor import governor
from services.backup_service import backup_service
from services.config import config
from services.dashboard_metrics_service import dashboard_metrics_service
from services.genbox_push_service import (
    shutdown_genbox_push_service,
    start_genbox_push_service,
)
from services.hardware_profile import TIER_SEEDED_FLAG, tier_default_updates
from services.homepage_service import homepage_service
from services.admin_auth_service import admin_auth_service
from services.image_task_service import image_task_service
from services.account_governance_service import account_governance_service
from services.admission import admission_gate, resolve_base_limit
from services.debug_log_service import debug_log_service
from services.legacy_sync_service import legacy_sync_service
from services.local_backup_service import local_backup_service
from services.log_service import log_service
from services.pool_alert_service import pool_alert_service
from services.proxy_service import proxy_settings
from services.realtime_monitor_service import realtime_monitor_service
from services.retention_cleanup_service import retention_cleanup_coordinator, start_retention_cleanup_scheduler
from services.runtime_configuration import DEFAULT_THREAD_TOKENS
from utils.log import logger


RETENTION_SHUTDOWN_TIMEOUT_SECS = 1.0
THREAD_TOKEN_FLOOR = 64


def _legacy_sync_check_worker(token: str) -> None:
    """Liveness check for accounts imported from a legacy directory."""
    try:
        account_service.fetch_remote_info(
            token,
            "legacy_sync",
            image_scope=False,
            preflight_refresh=True,
        )
    except Exception as exc:  # noqa: BLE001 - 单个账号失败不影响队列
        logger.info({
            "event": "legacy_sync_check_failed",
            "error": type(exc).__name__,
        })


def _submit_legacy_sync_checks(tokens: list[str]) -> None:
    if not tokens:
        return
    job_queue().submit(
        kind=KIND_CHECK,
        tokens=tokens,
        worker=_legacy_sync_check_worker,
        label="旧库同步测活",
    )


def start_legacy_sync_loop() -> None:
    legacy_sync_service.set_check_handler(_submit_legacy_sync_checks)
    legacy_sync_service.start_loop()
    pool_alert_service.start_loop()
    account_governance_service.start_loop()
    local_backup_service.start_loop()
    debug_log_service.start()
    snapshot = legacy_sync_service.get_config()
    logger.info({
        "event": "legacy_sync_service_started",
        "enabled": snapshot["enabled"],
        "paths": len(snapshot["paths"]),
        "interval_seconds": snapshot["interval_seconds"],
    })
    logger.info({
        "event": "pool_alert_service_started",
        "enabled": pool_alert_service.get_config()["enabled"],
    })
    logger.info({
        "event": "account_governance_service_started",
        "enabled": account_governance_service.get_config()["enabled"],
    })


def _configure_threadpool() -> None:
    """Size the async thread pool for the admission budget.

    Every admitted request occupies one thread for the duration of its
    upstream turn, so the pool must be at least as large as the admission
    limit; otherwise 500 concurrent requests queue behind 128 threads and
    clients time out while the box looks idle.
    """
    base = max(THREAD_TOKEN_FLOOR, resolve_base_limit())
    tokens = max(
        THREAD_TOKEN_FLOOR,
        governor().effective_limit(base, floor=THREAD_TOKEN_FLOOR),
    )
    limiter = current_default_thread_limiter()
    previous = int(getattr(limiter, "total_tokens", 0) or 0)
    if previous != tokens:
        limiter.total_tokens = tokens
    realtime_monitor_service.set_threadpool(tokens=tokens, previous_tokens=previous)
    logger.info({
        "event": "runtime_threadpool_configured",
        "previous_tokens": previous,
        "tokens": tokens,
        "source": "admission-budget",
        "configured_worker_threads": int(config.worker_threads),
    })

    def _apply_adaptive_tokens(snapshot: dict[str, object]) -> None:
        base_tokens = max(THREAD_TOKEN_FLOOR, resolve_base_limit())
        effective = max(
            THREAD_TOKEN_FLOOR,
            governor().effective_limit(base_tokens, floor=THREAD_TOKEN_FLOOR),
        )
        current = int(getattr(limiter, "total_tokens", 0) or 0)
        if effective != current:
            limiter.total_tokens = effective
            logger.info({
                "event": "runtime_threadpool_adapted",
                "scale": snapshot.get("scale"),
                "previous_tokens": current,
                "tokens": effective,
            })

    governor().add_listener(_apply_adaptive_tokens)


def _seed_hardware_defaults() -> None:
    """Seed first-run worker budgets from the detected hardware tier.

    Stored settings always win; seeding happens once per install and is then
    marked in the settings row so later hardware changes cannot overwrite an
    operator's explicit choices.
    """
    try:
        stored = dict(getattr(config, "data", {}) or {})
        profile = governor().snapshot()["hardware"]
        tier = str(profile.get("effective_tier") or "low")
        updates = tier_default_updates(
            stored,
            tier,
            already_seeded=bool(stored.get(TIER_SEEDED_FLAG)),
        )
        payload = dict(updates)
        if not stored.get(TIER_SEEDED_FLAG):
            payload[TIER_SEEDED_FLAG] = True
        if payload:
            config.update(payload)
            logger.info({
                "event": "hardware_tier_defaults_seeded",
                "tier": tier,
                "settings": sorted(updates),
            })
    except Exception as exc:
        logger.warning({"event": "hardware_tier_seed_failed", "error": str(exc)})


def create_app() -> FastAPI:
    app_version = config.app_version

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        governor().start()
        _seed_hardware_defaults()
        job_queue().start()
        prober = proxy_settings.create_health_prober()
        prober.start()
        _configure_threadpool()
        start_genbox_push_service()
        image_task_service.start()
        try:
            projection_reset = await run_in_threadpool(
                dashboard_metrics_service.reset_projection_schema_if_needed
            )
            if projection_reset.changed:
                logger.info({
                    "event": "dashboard_metrics_projection_schema_reset",
                    "state_recreated": projection_reset.state_recreated,
                    "hourly_recreated": projection_reset.hourly_recreated,
                    "model_hourly_recreated": projection_reset.model_hourly_recreated,
                })
        except Exception as exc:
            logger.error({"event": "dashboard_metrics_projection_schema_reset_failed", "error": str(exc)})
        try:
            cleanup_result = await run_in_threadpool(
                retention_cleanup_coordinator.run_startup_automatic,
                enforce_image_free_space=False,
            )
            cleanup_errors = cleanup_result.get("errors") or {}
            if cleanup_errors.get("logs"):
                logger.error({"event": "log_startup_cleanup_failed", "error": cleanup_errors["logs"]})
            if cleanup_errors.get("images"):
                logger.error({"event": "image_startup_cleanup_failed", "error": cleanup_errors["images"]})
        except Exception as exc:
            logger.error({"event": "retention_startup_cleanup_failed", "error": str(exc)})
        try:
            await run_in_threadpool(
                dashboard_metrics_service.sync_from_log_service,
                log_service,
            )
        except Exception as exc:
            logger.error({"event": "dashboard_metrics_startup_sync_failed", "error": str(exc)})
        account_service.cleanup_auto_remove_accounts()
        start_legacy_sync_loop()
        stop_event = Event()
        thread = start_account_lifecycle_watcher(stop_event)
        cleanup_thread = start_retention_cleanup_scheduler(stop_event)
        dashboard_metrics_thread = dashboard_metrics_service.start_refresh_scheduler(
            log_service,
            stop_event,
        )
        backup_service.start()
        try:
            yield
        finally:
            stop_event.set()
            thread.join(timeout=1)
            dashboard_metrics_thread.join(timeout=1)
            prober.stop()
            governor().stop()
            job_queue().stop()
            await run_in_threadpool(cleanup_thread.join, RETENTION_SHUTDOWN_TIMEOUT_SECS)
            await run_in_threadpool(image_task_service.shutdown_cancel_pending_and_wait)
            await run_in_threadpool(shutdown_genbox_push_service)
            try:
                await run_in_threadpool(
                    dashboard_metrics_service.sync_from_log_service,
                    log_service,
                )
            except Exception as exc:
                logger.error({"event": "dashboard_metrics_shutdown_sync_failed", "error": str(exc)})
            backup_service.stop()
    app = FastAPI(title="chatgpt2api", version=app_version, lifespan=lifespan)
    install_exception_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Export-Requested", "X-Exported", "X-Skipped"],
    )
    app.include_router(ai.create_router())
    app.include_router(accounts.create_router())
    app.include_router(image_tasks.create_router())
    app.include_router(prompts.create_router())
    app.include_router(system.create_router(app_version))

    @app.middleware("http")
    async def guard_large_admin_uploads(request: Request, call_next):
        """Reject unauthenticated/oversized legacy uploads before body parsing.

        FastAPI resolves ``UploadFile`` before the handler body runs, so an
        anonymous 2GB body would be spooled to disk before require_admin().
        A middleware sees headers first and can stop it cheaply.
        """
        if request.method == "POST" and request.url.path == "/api/system/legacy-sync/upload":
            try:
                require_admin(request.headers.get("authorization"))
            except HTTPException as exc:
                detail = exc.detail
                body = detail if isinstance(detail, dict) else {"error": str(detail)}
                return JSONResponse(content=body, status_code=exc.status_code)
            raw_length = request.headers.get("content-length", "").strip()
            if raw_length:
                try:
                    declared = int(raw_length)
                except ValueError:
                    declared = 0
                if declared > 2 * 1024 * 1024 * 1024:
                    return JSONResponse(
                        content={"error": "数据库文件超过 2GB 上限"},
                        status_code=413,
                    )
        return await call_next(request)

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_root():
        html = homepage_service.public_html()
        if not html:
            raise HTTPException(status_code=404, detail="Not Found")
        return HTMLResponse(html)

    @app.api_route("/admin-recover", methods=["GET"], include_in_schema=False)
    async def serve_admin_recover():
        return HTMLResponse(ADMIN_RECOVER_HTML)

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    async def serve_web(full_path: str):
        asset = resolve_web_asset(full_path)
        if asset is None:
            raise HTTPException(status_code=404, detail="Not Found")
        # A client route is served as index.html by resolve_web_asset. Only the
        # configured admin path may receive the SPA shell; arbitrary paths such
        # as /login or /old-admin must 404 instead of revealing the console.
        if asset.name == "index.html":
            admin_path = admin_auth_service.current_admin_path().strip("/")
            requested = full_path.strip("/")
            if requested != admin_path and not requested.startswith(f"{admin_path}/"):
                raise HTTPException(status_code=404, detail="Not Found")
        return FileResponse(asset)

    return app


ADMIN_RECOVER_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>倾梦GPT · 后台路径恢复</title>
<style>body{font-family:system-ui,sans-serif;background:#f5f5f4;margin:0;padding:40px}
.card{max-width:520px;margin:0 auto;background:#fff;border:1px solid #e7e5e4;border-radius:20px;padding:28px}
h1{font-size:20px;margin:0 0 8px}p{color:#57534e;font-size:14px;line-height:1.6}
label{display:block;margin-top:16px;font-size:13px;font-weight:600}
input{width:100%;box-sizing:border-box;margin-top:6px;padding:10px 12px;border:1px solid #d6d3d1;border-radius:10px;font-size:14px}
button{margin-top:20px;width:100%;padding:11px;border:0;border-radius:10px;background:#18181b;color:#fff;font-size:14px;cursor:pointer}
pre{white-space:pre-wrap;background:#fafaf9;border:1px solid #e7e5e4;border-radius:10px;padding:12px;font-size:13px}
a{color:#18181b}</style></head>
<body><div class="card">
<h1>后台路径恢复</h1>
<p>忘记自定义后台路径时，使用恢复密钥设置一个新的后台路径。此操作不会修改登录方式或密码。</p>
<label>恢复密钥</label><input id="key" type="password" placeholder="QINGMENG_AUTH_KEY">
<label>新的后台路径</label><input id="path" type="text" placeholder="/my-admin">
<button onclick="recover()">保存新的后台路径</button>
<pre id="out">等待操作。</pre>
</div>
<script>
async function recover(){
  const key=document.getElementById('key').value.trim();
  const path=document.getElementById('path').value.trim();
  const out=document.getElementById('out');
  if(!key||!path){out.textContent='请填写恢复密钥和新的后台路径。';return;}
  out.textContent='提交中...';
  try{
    const r=await fetch('/api/system/admin-path/recover',{method:'POST',
      headers:{'Content-Type':'application/json','Authorization':'Bearer '+key},
      body:JSON.stringify({admin_path:path})});
    const data=await r.json();
    if(!r.ok){const msg=(data&&data.detail&&data.detail.error)||(data&&data.error&&(data.error.error||data.error))||('恢复失败：'+r.status);out.textContent=msg;return;}
    out.innerHTML='已更新后台路径：<a href="'+data.admin_path+'">'+data.admin_path+'</a>';
  }catch(e){out.textContent='请求失败：'+e;}
}
</script></body></html>"""
