"""OpenBox unified server: sandbox management + AI agent platform."""
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, APIRouter
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# Direct imports and the managed entrypoint share one profile-selection rule.
# An explicit OPENBOX_ENV_FILE never gets silently mixed with backend/.env.
from scripts.wuying_env import load_environment

load_environment()

from core.log import create_logger
from sandbox.protocol import REQUIRED_ACTION_SERVER_CAPABILITIES

log = create_logger("main")

_MAX_WUYING_CLOCK_SKEW_SECONDS = 5.0
_REQUIRED_WUYING_CAPABILITIES = REQUIRED_ACTION_SERVER_CAPABILITIES


async def _init_agent():
    """Initialize the agent subsystem: register tools and start background tasks."""
    from tool.registry import register_builtin_tools, reconcile_platform_plugins
    register_builtin_tools(load_custom=False)
    await reconcile_platform_plugins()

    try:
        from tool.truncation import start_cleanup_task
        start_cleanup_task()
    except Exception as e:
        log.warning(f"Could not start truncation cleanup: {e}")

    log.info("Agent subsystem initialized")


def _init_infrastructure(config):
    """Initialize storage and the cache-backed authentication subsystems."""
    if config.jwt_secret:
        from db.base import init_engine

        init_engine(config.database_url, config.db_pool_size, config.db_pool_overflow)

    # One cache instance owns the whole application lifespan.  Redis backs
    # SaaS deployments; the desktop mode only needs process-local storage for
    # short-lived preview credentials.
    from cache import set_cache

    cache = getattr(config, "_cache", None)
    if cache is None:
        if config.jwt_secret:
            from cache.redis_cache import RedisCache

            cache = RedisCache(config.redis_url)
        else:
            from cache.memory_cache import MemoryCache

            cache = MemoryCache()
    set_cache(cache)

    from auth import setup_auth

    setup_auth(config, cache)

    config._cache = cache
    if config.jwt_secret:
        log.info("Multi-user infrastructure initialized")
    else:
        log.info("Single-user infrastructure initialized with in-memory preview tokens")


async def _cleanup_infrastructure(config):
    """Close the shared database and the mode-appropriate cache once."""
    try:
        from db.base import close_engine
        await close_engine()
    except Exception as e:
        log.warning(f"Error closing database: {e}")

    cache = getattr(config, "_cache", None)
    try:
        if cache:
            await cache.close()
    except Exception as e:
        log.warning(f"Error closing cache: {e}")
    finally:
        from cache import get_cache, set_cache
        from auth.preview_token import init_preview_store

        if get_cache() is cache:
            set_cache(None)
        # Do not leave auth.preview_token pointing at a closed Redis/memory
        # client when a lifespan ends or a test application is recreated.
        init_preview_store(None)
        config._cache = None


def _validate_deployment_contract(config) -> None:
    """Reject deployment modes the current execution plane cannot isolate."""
    if config.jwt_secret and config.sandbox_provider == "wuying":
        raise RuntimeError(
            "JWT multi-user mode requires one isolated WUYING desktop per user; "
            "the current WUYING provider is a shared single-desktop provider"
        )


async def _wuying_readiness(config, sandbox_provider=None) -> dict:
    """Verify the execution protocol, clock, reachability and API key."""
    configured = bool((getattr(config, "wuying_api_key", "") or "").strip())
    result = {
        "ready": False,
        "configured": configured,
        "reachable": False,
        "authenticated": False,
    }
    if getattr(config, "sandbox_provider", None) != "wuying":
        result["reason"] = "unsupported_provider"
        return result
    if not configured:
        result["reason"] = "api_key_missing"
        return result

    if sandbox_provider is None:
        from sandbox import provider as sandbox_provider

    try:
        desktop = sandbox_provider.get_user_container("default")
        container_id = getattr(desktop, "id", None) or "wuying-desktop"
        probe_started = datetime.now(timezone.utc)
        alive_response = await sandbox_provider.forward_to_container(
            container_id,
            "GET",
            "/alive",
            user_id="default",
            timeout=3.0,
        )
        probe_finished = datetime.now(timezone.utc)
        if alive_response.status_code != 200:
            result["reachable"] = True
            result["reason"] = "action_server_unready"
            return result
        alive_payload = alive_response.json()
        capabilities = alive_payload.get("capabilities")
        if not isinstance(capabilities, list) or not all(
            isinstance(item, str) for item in capabilities
        ):
            result["reachable"] = True
            result["reason"] = "invalid_action_server_identity"
            return result
        result["receipt_capable"] = "run_lease_receipt_v2" in capabilities
        result["project_terminal_capable"] = (
            "terminal_project_cwd_v1" in capabilities
        )
        result["action_server_version"] = str(alive_payload.get("version") or "")[:96]
        if not result["receipt_capable"]:
            result["reachable"] = True
            result["reason"] = "run_lease_receipt_unsupported"
            return result
        if not result["project_terminal_capable"]:
            result["reachable"] = True
            result["reason"] = "terminal_project_cwd_unsupported"
            return result
        raw_timestamp = alive_payload.get("timestamp")
        if not isinstance(raw_timestamp, str):
            result["reachable"] = True
            result["reason"] = "action_server_clock_unverified"
            return result
        try:
            remote_time = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        except (ValueError, OverflowError):
            result["reachable"] = True
            result["reason"] = "action_server_clock_unverified"
            return result
        if remote_time.tzinfo is None:
            remote_time = remote_time.replace(tzinfo=timezone.utc)
        midpoint = probe_started + (probe_finished - probe_started) / 2
        clock_skew = abs((remote_time.astimezone(timezone.utc) - midpoint).total_seconds())
        result["clock_skew_ms"] = round(clock_skew * 1000)
        if clock_skew > _MAX_WUYING_CLOCK_SKEW_SECONDS:
            result["reachable"] = True
            result["reason"] = "action_server_clock_skew"
            return result
        missing_capabilities = sorted(
            _REQUIRED_WUYING_CAPABILITIES.difference(capabilities)
        )
        if missing_capabilities:
            result["reachable"] = True
            result["missing_capabilities"] = missing_capabilities
            result["reason"] = "action_server_capabilities_missing"
            return result
        response = await sandbox_provider.forward_to_container(
            container_id,
            "GET",
            "/system_info",
            user_id="default",
            timeout=3.0,
        )
    except Exception as exc:
        log.debug(
            "WUYING readiness probe failed error_type=%s",
            type(exc).__name__,
        )
        result["reason"] = "unreachable"
        return result

    result["reachable"] = True
    if response.status_code == 200:
        result["ready"] = True
        result["authenticated"] = True
    elif response.status_code in (401, 403):
        result["reason"] = "api_key_rejected"
    else:
        result["reason"] = "action_server_unready"
    return result


def _model_provider_readiness(config) -> dict:
    """Validate the default Agent model's real provider binding, offline."""
    from agent.llm import provider_configuration_readiness
    from agent.model_resolve import resolve

    try:
        model_id, _replaced = resolve(None, config, context="readiness")
        return provider_configuration_readiness(model_id, config)
    except Exception as exc:
        # Never log config values here: provider blocks contain credentials.
        log.debug(
            "Model provider readiness check failed error_type=%s",
            type(exc).__name__,
        )
        return {
            "configured": False,
            "ready": False,
            "reason": "configuration_check_failed",
        }


def _tool_exposure_readiness(config) -> dict:
    """Report the release posture without exposing allowlist contents.

    The exact provider payload is measured and logged for every Agent step;
    readiness reports the deployment-level mode and hard ceilings that govern
    those measurements.  ``native_auto`` is still a per-request binding gate,
    so the diagnostic only says whether both allowlists are present, never
    that native search has been enabled for a model.
    """
    from agent.tool_runtime import effective_exposure_mode

    exposure = config.tool_exposure
    configured_mode = exposure.mode
    endpoint_count = len(exposure.native_endpoint_allowlist)
    model_count = len(exposure.native_model_allowlist)
    native_allowlists_present = bool(endpoint_count and model_count)
    reasons = {
        "legacy_eager": "explicit_legacy_rollback",
        "shadow": "shadow_observation",
        "portable": "portable_active",
        "emergency_eager": "explicit_emergency_rollback",
    }
    if configured_mode == "native_auto":
        reason = (
            "native_binding_gate_pending"
            if native_allowlists_present
            else "native_auto_portable_fallback"
        )
    else:
        reason = reasons[configured_mode]

    return {
        "ready": True,
        "configured_mode": configured_mode,
        "build_effective_mode": effective_exposure_mode(
            configured_mode,
            "build",
        ),
        "non_build_without_opt_in_mode": effective_exposure_mode(
            configured_mode,
            "non-build",
        ),
        "native_allowlists_present": native_allowlists_present,
        "native_endpoint_allowlist_entries": endpoint_count,
        "native_model_allowlist_entries": model_count,
        "limits": {
            "resident_hard_chars": exposure.resident_hard_chars,
            "active_hard_chars": exposure.active_hard_chars,
            "native_wire_hard_chars": exposure.native_wire_hard_chars,
            "max_search_calls_per_step": exposure.max_search_calls_per_step,
            "max_reveals_per_step": exposure.max_reveals_per_step,
            "max_search_result_chars_per_step": (
                exposure.max_search_result_chars_per_step
            ),
        },
        "reason": reason,
    }


async def _readiness_report(config) -> dict:
    """Collect every dependency required before accepting Agent work."""
    from cron.service import cron_service
    from db.base import database_schema_ready

    database_ready, wuying = await asyncio.gather(
        database_schema_ready(),
        _wuying_readiness(config),
    )
    try:
        cron = cron_service.readiness_status()
    except Exception as exc:
        log.debug(
            "Cron readiness check failed error_type=%s",
            type(exc).__name__,
        )
        cron = {
            "ready": False,
            "started": False,
            "heartbeat_fresh": False,
            "last_tick_at": None,
        }

    checks = {
        "database": {"ready": bool(database_ready)},
        "cron": cron,
        "model_provider": _model_provider_readiness(config),
        "tool_exposure": _tool_exposure_readiness(config),
        "wuying": wuying,
    }
    ready = all(check["ready"] for check in checks.values())
    return {
        "status": "ready" if ready else "not_ready",
        "version": "0.1.0",
        "checks": checks,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    from core.config import get_config
    config = get_config()

    _validate_deployment_contract(config)
    _init_infrastructure(config)
    await _init_agent()

    # Desktop mode does not initialize SQL above, but sessions/projects still
    # need the shared store. Multi-user infrastructure already initialized it,
    # so this is a no-op there.
    from db.base import ensure_engine

    await ensure_engine(config)

    # Rebuild process-local routing from the real execution plane. Provider
    # resources can outlive one web process; deleting them on startup would
    # kill work that a restarted process still needs to recover.
    from sandbox import provider
    await provider.reconcile()
    log.info(f"OpenBox starting ({config.sandbox_provider} provider, reconciled)")

    # Initialize Redis event bus for cross-worker broadcasting (if in multi-user mode)
    if config.jwt_secret:
        from bus.bus import init_redis_bus
        await init_redis_bus(config.redis_url)

    # Run one ordered recovery pass now that the provider and event bus are
    # ready, then keep sweeping independently of Cron. A replica that starts
    # before a dead peer's lease expires will still converge the accepted work.
    from agent.recovery_service import agent_recovery_service

    await agent_recovery_service.start()
    log.info("Agent recovery service initialized")

    # Initialize Cron scheduler
    try:
        from cron.service import cron_service
        from cron.executor import execute_cron_job
        cron_service.set_executor(execute_cron_job)
        await cron_service.start()
        log.info("Cron scheduler initialized")
    except Exception as e:
        log.warning(f"Failed to start cron scheduler: {e}")

    # Re-drive direct video finalizations stranded by a previous process exit.
    # The periodic sweep piggybacks on the cron timer tick; this schedules the
    # startup pass so provider-completed jobs converge promptly after restart.
    if config.jwt_secret:
        try:
            from video.job_recovery import schedule_startup_recovery
            schedule_startup_recovery()
        except Exception as e:
            log.warning(f"Failed to schedule video job recovery: {e}")

    # Production hot replacement is an explicit low-frequency reconcile loop,
    # not import-time magic. Unchanged source digests reuse the standing
    # generations without importing plugin code.
    from tool.registry import platform_plugin_watcher
    await platform_plugin_watcher.start(
        interval_seconds=getattr(
            config,
            "platform_plugin_watch_interval_seconds",
            5.0,
        ),
    )

    log.info("OpenBox starting...")
    try:
        yield
    finally:
        # Stop the source trigger before any execution or infrastructure
        # teardown so no new generation can publish during shutdown.
        try:
            await platform_plugin_watcher.stop()
        except Exception as e:
            log.warning(f"Error stopping platform plugin watcher: {e}")
    log.info("OpenBox shutting down, cleaning up...")

    # Quiesce recovery before stopping execution infrastructure. Any in-flight
    # database pass is allowed to commit or roll back cleanly.
    try:
        await agent_recovery_service.stop()
    except Exception as e:
        log.warning(f"Error stopping Agent recovery service: {e}")

    # Stop cron scheduler
    try:
        from cron.service import cron_service
        await cron_service.stop()
    except Exception as e:
        log.warning(f"Error stopping cron scheduler: {e}")

    # Abort active agent loops
    from session.status import abort_all, active_session_ids
    aborted = abort_all()
    if aborted:
        log.info(f"Sent abort signal to {aborted} active session(s), waiting up to 30s...")
        for _ in range(30):
            remaining = active_session_ids()
            if not remaining:
                break
            await asyncio.sleep(1)
        else:
            remaining = active_session_ids()
            if remaining:
                log.warning(f"{len(remaining)} session(s) still active after 30s timeout")

    # Do not bulk-mark BUSY rows here.  In a multi-worker deployment those may
    # belong to a healthy peer.  This worker's loops release their own fenced
    # generations above; crashed generations are handled by expiry recovery.

    # Agent calls are quiescent now, so trusted plugin generations can stop
    # accepting work, drain any nested invocations, and release resources while
    # the database/cache/bus infrastructure they may depend on is still alive.
    try:
        from tool.registry import shutdown_platform_plugins
        await shutdown_platform_plugins()
    except Exception as e:
        log.warning(f"Error shutting down platform plugins: {e}")

    # Close Redis event bus
    try:
        from bus.bus import close_redis_bus
        await close_redis_bus()
    except Exception as e:
        log.warning(f"Error closing Redis bus: {e}")

    # Container state cleanup
    from sandbox import sandbox_manager
    await sandbox_manager.release_all(destroy=False)
    # Provider resources intentionally outlive the web process. Explicit owner
    # deletion and the database-guarded idle reaper own destructive cleanup;
    # a rolling web restart must not terminate recoverable provider work.
    await _cleanup_infrastructure(config)


def create_app() -> FastAPI:
    """Application factory: build and configure the FastAPI app."""
    from core.config import get_config
    config = get_config()
    preview_public_origin = getattr(config, "preview_public_origin", "")

    application = FastAPI(
        title="OpenBox API",
        version="0.1.0",
        lifespan=lifespan,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from auth.preview_origin import ControlPlaneFrameGuardMiddleware

    application.add_middleware(
        ControlPlaneFrameGuardMiddleware,
        preview_public_origin=preview_public_origin,
    )

    if preview_public_origin:
        from auth.preview_origin import PreviewOriginIsolationMiddleware

        # Added after CORS so this is the outer, fail-closed host/path gate;
        # even a preflight cannot bypass it to discover control-plane routes.
        application.add_middleware(
            PreviewOriginIsolationMiddleware,
            preview_public_origin=preview_public_origin,
        )

    # The v2 UI serves both authenticated SaaS deployments and the historical
    # single-user desktop mode.  Auth routes are intentionally absent in the
    # latter, so expose one public, non-secret bootstrap contract that lets the
    # client distinguish "no refresh cookie" from "there is no login system".
    @application.get("/api/auth/bootstrap")
    async def auth_bootstrap():
        if config.jwt_secret:
            return {"mode": "multi_user"}
        return {
            "mode": "single_user",
            "user": {"id": "default", "username": "default", "role": "admin"},
        }

    # ── Auth routes (no auth required for register/login/refresh) ──
    if config.jwt_secret:
        from auth.routes import router as auth_router
        application.include_router(auth_router)
    else:
        from auth.single_user import router as single_user_auth_router
        application.include_router(single_user_auth_router)

    # ── WebSocket endpoint (replaces SSE) ──
    from api.ws import router as ws_router
    application.include_router(ws_router)

    # ── Container management routes ──
    from api.containers import (
        preview_config_router,
        preview_router,
        router as containers_router,
    )
    from api.terminal import router as terminal_router
    from api.files import router as files_router
    from api.dev_browser import router as dev_browser_router

    application.include_router(containers_router)
    application.include_router(preview_config_router)
    # Browser subresources authenticate through a scoped preview-token cookie,
    # rather than the API JWT header used by the management routes.
    application.include_router(preview_router)
    application.include_router(terminal_router)
    application.include_router(files_router)
    application.include_router(dev_browser_router)

    from api.cron import router as cron_router
    application.include_router(cron_router)

    from api.desktop import router as desktop_router
    application.include_router(desktop_router)

    from api.browser import router as browser_router
    application.include_router(browser_router)

    from api.assets import router as assets_router
    application.include_router(assets_router)

    from api.memories import router as memories_router
    application.include_router(memories_router)

    from api.video_productions import router as video_productions_router
    application.include_router(video_productions_router)

    from api.video_materials import router as video_materials_router
    application.include_router(video_materials_router)

    # ── Agent routes ──
    agent_router = APIRouter(prefix="/api/agent", tags=["Agent"])

    from api.projects import router as project_router
    from api.sessions import router as session_router
    from api.permissions import router as perm_router
    from api.questions import router as question_router
    from api.metadata import router as metadata_router

    agent_router.include_router(project_router)
    agent_router.include_router(session_router)
    agent_router.include_router(perm_router)
    agent_router.include_router(question_router)
    agent_router.include_router(metadata_router)

    from api.prompt_history import router as prompt_history_router
    agent_router.include_router(prompt_history_router)

    application.include_router(agent_router)

    # ── Liveness and dependency readiness ──
    @application.get("/health")
    async def health():
        return {"status": "ok", "version": "0.1.0"}

    @application.get("/ready")
    async def readiness():
        report = await _readiness_report(config)
        return JSONResponse(
            status_code=200 if report["status"] == "ready" else 503,
            content=report,
        )

    return application


app = create_app()
