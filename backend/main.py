"""OpenBox unified server: sandbox management + AI agent platform."""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, APIRouter
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# Load .env from project root before anything reads os.environ
load_dotenv(Path(__file__).parent / ".env")

from core.log import create_logger

log = create_logger("main")


def _init_agent():
    """Initialize the agent subsystem: register tools and start background tasks."""
    from tool.registry import register_builtin_tools
    register_builtin_tools()

    try:
        from tool.truncation import start_cleanup_task
        start_cleanup_task()
    except Exception as e:
        log.warning(f"Could not start truncation cleanup: {e}")

    log.info("Agent subsystem initialized")


def _init_infrastructure(config):
    """Initialize multi-user infrastructure (DB, Redis, Blob, Auth).

    Only initializes if jwt_secret is configured (multi-user mode).
    Single-user mode (no jwt_secret) skips infrastructure init.
    """
    if not config.jwt_secret:
        log.info("No JWT_SECRET configured — running in single-user mode (no auth)")
        return

    from db.base import init_engine
    init_engine(config.database_url, config.db_pool_size, config.db_pool_overflow)

    from cache.redis_cache import RedisCache
    from cache import set_cache
    cache = RedisCache(config.redis_url)
    set_cache(cache)

    from auth import setup_auth
    setup_auth(config, cache)

    config._cache = cache
    log.info("Multi-user infrastructure initialized")


async def _cleanup_infrastructure(config):
    """Close the shared database plus multi-user-only cache resources."""
    try:
        from db.base import close_engine
        await close_engine()
    except Exception as e:
        log.warning(f"Error closing database: {e}")

    if not config.jwt_secret:
        return

    try:
        cache = getattr(config, '_cache', None)
        if cache:
            await cache.close()
    except Exception as e:
        log.warning(f"Error closing Redis: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from core.config import get_config
    config = get_config()

    _init_infrastructure(config)
    _init_agent()

    # Desktop mode has no auth bootstrap, but sessions/projects still need the
    # shared SQL store. Multi-user infrastructure already initialized it, so
    # this is a no-op there.
    from db.base import ensure_engine

    await ensure_engine(config)

    # Rebuild process-local routing from the real execution plane. Provider
    # resources can outlive one web process; deleting them on startup would
    # kill work that a restarted process still needs to recover.
    from sandbox import provider
    await provider.reconcile()
    log.info(f"OpenBox starting ({config.sandbox_provider} provider, reconciled)")

    # Say, in one place, which desktop each plane means. They drifted apart
    # once already — the view streamed a per-user desktop while the agent ran
    # on the shared one — and nothing in the logs put the two side by side, so
    # it read as "the agent is lying" rather than "these are two machines".
    # The region belongs here too: a desktop id that is real in another region
    # fails as NotFindDesktopId, which reads like a missing desktop.
    if config.sandbox_provider == "wuying":
        from api.desktop import _per_user
        from sandbox import get_provider

        view = "the caller's own desktop" if _per_user() else (
            f"{config.wuying_desktop_id or '(unset)'} in {config.wuying_region_id}")
        if config.wuying_routing == "per_desktop":
            log.info(
                "Cloud desktop — agent and view route to each caller's assigned "
                "desktop in %s",
                config.wuying_region_id,
            )
        else:
            log.info(f"Cloud desktop — agent runs on: {get_provider().desktop_id}; view streams: {view}")

    # Per-user ECD fleet patrol (log-only; desktops are subscription-resident)
    if config.sandbox_provider == "wuying" and config.wuying_mode == "per_user":
        from sandbox.wuying_desktop_service import wuying_desktop_service
        wuying_desktop_service.start_patrol(config.wuying_health_interval_sec)

    # Initialize Redis event bus for cross-worker broadcasting (if in multi-user mode)
    if config.jwt_secret:
        from bus.bus import init_redis_bus
        await init_redis_bus(config.redis_url)

    # Initialize Cron scheduler
    try:
        from cron.internal_tasks import register_builtin_tasks
        from cron.service import cron_service
        from cron.executor import execute_cron_job
        register_builtin_tasks()
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

    from sandbox.desktop_activation import desktop_activation_service
    desktop_activation_service.start()
    from question.continuation import question_worker
    from question.legacy import reconcile_legacy_questions
    legacy_questions = await reconcile_legacy_questions()
    if legacy_questions:
        log.info("Closed %s legacy questions requiring fresh confirmation", legacy_questions)
    question_worker.start()

    from notifications.providers import PushProviders
    from notifications.runtime import PushWorker
    app.state.push_providers = PushProviders.from_env()
    push_worker = PushWorker(app.state.push_providers)
    push_worker.start()
    from notifications.announcements import InboxJanitor
    inbox_janitor = InboxJanitor()
    inbox_janitor.start()

    log.info("OpenBox starting...")
    yield
    log.info("OpenBox shutting down, cleaning up...")
    await inbox_janitor.stop()
    await push_worker.stop()
    await question_worker.stop()
    await desktop_activation_service.stop()

    if config.sandbox_provider == "wuying" and config.wuying_mode == "per_user":
        from sandbox.wuying_desktop_service import wuying_desktop_service
        wuying_desktop_service.stop_patrol()

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

    # Execution leases recover interrupted runs. Never mark every busy
    # session as failed here: other workers may still be executing them.

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

    application = FastAPI(
        title="OpenBox API",
        version="0.1.0",
        lifespan=lifespan,
    )

    from sandbox.entitlement import SandboxSubscriptionRequired
    from fastapi.responses import JSONResponse

    @application.exception_handler(SandboxSubscriptionRequired)
    async def sandbox_subscription_required(_request, exc):
        return JSONResponse(exc.payload, status_code=403)

    application.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Auth routes (no auth required for register/login/refresh) ──
    if config.jwt_secret:
        from auth.routes import router as auth_router
        application.include_router(auth_router)

    # ── WebSocket endpoint (replaces SSE) ──
    from api.ws import router as ws_router
    application.include_router(ws_router)

    # ── Container management routes ──
    from api.containers import router as containers_router, preview_router
    from api.terminal import router as terminal_router
    from api.files import router as files_router
    from api.dev_browser import router as dev_browser_router

    application.include_router(containers_router)
    application.include_router(preview_router)  # No auth — browser accesses directly
    application.include_router(terminal_router)
    application.include_router(files_router)
    application.include_router(dev_browser_router)

    from api.cron import router as cron_router
    application.include_router(cron_router)

    from api.desktop import router as desktop_router
    application.include_router(desktop_router)

    from api.internal import router as internal_router
    application.include_router(internal_router)

    from api.browser import router as browser_router
    application.include_router(browser_router)

    from api.assets import router as assets_router
    application.include_router(assets_router)

    from api.memories import router as memories_router
    application.include_router(memories_router)

    from api.video_productions import router as video_productions_router
    application.include_router(video_productions_router)

    from api.workspaces import router as workspaces_router
    from api.admin import router as admin_router
    from api.admin_billing import router as admin_billing_router
    from api.admin_fleet import router as admin_fleet_router
    from api.admin_skills import router as admin_skills_router
    application.include_router(workspaces_router)
    application.include_router(admin_router)
    application.include_router(admin_fleet_router)
    application.include_router(admin_skills_router)
    application.include_router(admin_billing_router)

    from api.billing import router as billing_router
    application.include_router(billing_router)

    # ── 授权中心: platform accounts, publish jobs, notifications, webhooks ──
    from api.platform_accounts import (
        jobs_router as publish_jobs_router,
        platforms_router,
        public_router as platform_callback_router,
        router as platform_accounts_router,
    )
    from api.notifications import router as notifications_router
    from api.webhooks_douyin import router as douyin_webhook_router
    application.include_router(platform_accounts_router)
    application.include_router(platform_callback_router)  # No auth — OAuth redirect lands here
    application.include_router(platforms_router)
    application.include_router(publish_jobs_router)
    application.include_router(notifications_router)
    from api.push import router as push_router
    application.include_router(push_router)
    from api.admin_push import router as admin_push_router
    application.include_router(admin_push_router)
    # ── 消息中心: inbox, public topic pages, admin announcements ──
    from api.inbox import router as inbox_router, topics_router
    from api.admin_messages import router as admin_messages_router
    application.include_router(inbox_router)
    application.include_router(topics_router)  # No auth — published topics are shareable
    application.include_router(admin_messages_router)
    application.include_router(douyin_webhook_router)  # No auth — signed by the platform

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

    # ── Deployment environment (public; feeds the UI badge) ──
    @application.get("/api/environment")
    async def environment():
        return {"name": get_config().app_env}

    # ── Health check ──
    @application.get("/health")
    async def health():
        from db.base import database_schema_ready

        if not await database_schema_ready():
            return JSONResponse(
                status_code=503,
                content={"status": "not_ready", "version": "0.1.0"},
            )
        return {"status": "ok", "version": "0.1.0"}

    return application


app = create_app()
