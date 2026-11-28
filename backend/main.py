"""OpenBox unified server: sandbox management + AI agent platform."""
import asyncio
import importlib
import logging
import os
import time
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

    Multi-user mode initializes shared database, Redis and authentication.
    Desktop mode only needs a local one-time subscription ticket store.
    """
    if not config.jwt_secret:
        from cache.memory_cache import MemoryCache
        from auth.ticket import init_ticket_store
        config._cache = MemoryCache()
        init_ticket_store(config._cache)
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
    """Close the shared database and the initialized ticket/cache resources."""
    try:
        from db.base import close_engine
        await close_engine()
    except Exception as e:
        log.warning(f"Error closing database: {e}")

    try:
        cache = getattr(config, '_cache', None)
        if cache:
            await cache.close()
    except Exception as e:
        log.warning(f"Error closing cache: {e}")


def _build_kernel_extensions(config):
    """The kernel's plugins for this process (docs/kernel-refactor/plugin-interfaces.md).

    Built once per app and installed by the lifespan; the kernel reaches it
    through ``agent.extensions.extensions()``.
    """
    from agent.extensions import KernelExtensions
    from memory.plugin import MemoryCleanup, MemoryPlugin
    from trajectory.plugin import TrajectoryPlugin
    # MemoryCleanup carries deletion and retention work that must keep running
    # even when the MemoryPlugin feature is left out (memory/plugin.py).
    return KernelExtensions().add(TrajectoryPlugin()).add(MemoryCleanup()).add(MemoryPlugin())


async def _stop_background_services(**services) -> None:
    """Request every consumer to stop before waiting for any one to drain.

    A slow warm-up or client shutdown must not leave this retired process
    claiming new work while its replacement is already serving requests.
    Each service retains its own lease-safe shutdown policy.
    """
    async def stop(name, operation):
        log.info("Stopping background service %s", name)
        try:
            await operation
        except Exception:
            log.exception("Error stopping background service %s", name)
        else:
            log.info("Stopped background service %s", name)

    await asyncio.gather(*(stop(name, operation) for name, operation in services.items()))


@asynccontextmanager
async def lifespan(app: FastAPI):
    from core.config import get_config
    config = get_config()
    from agent.extensions import install as install_kernel_extensions
    kernel_extensions = getattr(app.state, "kernel_extensions", None) or _build_kernel_extensions(config)
    previous_kernel_extensions = install_kernel_extensions(kernel_extensions)

    # LiteLLM's first import is expensive and may fetch its price map. Do it
    # before accepting requests, off the event loop, using the same local-map
    # default as the production image. No model request or billing occurs.
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    warmup_started = time.monotonic()
    await asyncio.to_thread(importlib.import_module, "litellm")
    log.info("Model runtime ready warmup_ms=%.1f", (time.monotonic() - warmup_started) * 1000)

    from agent.tool_payload import _proxy_encoding
    tokenizer_started = time.monotonic()
    encoding = await asyncio.to_thread(_proxy_encoding)
    log.info("Context tokenizer ready mode=%s warmup_ms=%.1f",
             "local_vocabulary" if encoding is not None else "estimate",
             (time.monotonic() - tokenizer_started) * 1000)

    from core.runtime_gc import freeze_startup_heap
    freeze_startup_heap()

    _init_infrastructure(config)
    _init_agent()

    # Desktop mode has no auth bootstrap, but sessions/projects still need the
    # shared SQL store. Multi-user infrastructure already initialized it, so
    # this is a no-op there.
    from db.base import ensure_engine

    await ensure_engine(config)
    await kernel_extensions.startup(app)

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
    from agent.recovery_service import agent_recovery_service
    await agent_recovery_service.start()
    # The first assistant turn after a restart reads a cached fold instead of
    # replaying the whole conversation.
    from assistant.fold_warmup import schedule_fold_warmup, stop_fold_warmup
    schedule_fold_warmup()
    from tool.registry import platform_plugin_watcher
    await platform_plugin_watcher.start(interval_seconds=5.0)

    # Plugins' background workers start after the kernel's own services.
    await kernel_extensions.start_background(app)
    from assets.descriptions import DescriptionWorker
    asset_description_worker = DescriptionWorker()
    asset_description_worker.start()

    from notifications.providers import PushProviders
    from notifications.runtime import PushWorker
    app.state.push_providers = PushProviders.from_env()
    push_worker = PushWorker(app.state.push_providers)
    push_worker.start()
    from notifications.announcements import InboxJanitor
    inbox_janitor = InboxJanitor()
    inbox_janitor.start()

    log.info("OpenBox starting...")
    # Operator pricing rules: load once so the first quote already sees them,
    # then keep the process-local cache within REFRESH_SECONDS of the database.
    from billing import rules as pricing_rules
    try:
        await pricing_rules.start()
    except Exception as e:
        log.warning(f"Pricing rules unavailable at startup (base rates.json applies): {e}")
    from video import shot_review
    shot_review.start()
    try:
        yield
    finally:
        await shot_review.stop()
    log.info("OpenBox shutting down, cleaning up...")
    if config.sandbox_provider == "wuying" and config.wuying_mode == "per_user":
        from sandbox.wuying_desktop_service import wuying_desktop_service
        wuying_desktop_service.stop_patrol()

    from cron.service import cron_service
    await _stop_background_services(
        recovery=agent_recovery_service.stop(),
        questions=question_worker.stop(),
        cron=cron_service.stop(),
        desktop_activation=desktop_activation_service.stop(),
        plugins=platform_plugin_watcher.stop(),
        pricing=pricing_rules.stop(),
        fold_warmup=stop_fold_warmup(),
        asset_descriptions=asset_description_worker.stop(),
        inbox_janitor=inbox_janitor.stop(),
        push=push_worker.stop(),
        **kernel_extensions.stop_background(app),
    )

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

    from agent.gateway import close_clients
    await close_clients()

    # Close Redis event bus
    from tool.registry import shutdown_platform_plugins
    await shutdown_platform_plugins()
    try:
        from bus.bus import close_redis_bus
        await close_redis_bus()
    except Exception as e:
        log.warning(f"Error closing Redis bus: {e}")

    # Container state cleanup
    from sandbox import sandbox_manager
    try:
        await sandbox_manager.release_all(destroy=False)
        # Provider resources intentionally outlive the web process. Explicit
        # owner deletion and the database-guarded idle reaper own cleanup.
    finally:
        try:
            await kernel_extensions.shutdown(app)
        finally:
            try:
                await _cleanup_infrastructure(config)
            finally:
                install_kernel_extensions(previous_kernel_extensions)


def create_app() -> FastAPI:
    """Application factory: build and configure the FastAPI app."""
    from core.config import get_config
    config = get_config()

    application = FastAPI(
        title="OpenBox API",
        version="0.1.0",
        lifespan=lifespan,
    )
    application.state.kernel_extensions = _build_kernel_extensions(config)

    from sandbox.entitlement import SandboxSubscriptionRequired
    from fastapi.responses import JSONResponse

    @application.exception_handler(SandboxSubscriptionRequired)
    async def sandbox_subscription_required(_request, exc):
        return JSONResponse(exc.payload, status_code=403)

    from api.v1.app import CORSMiddlewareExemptingV1

    from assistant.scheduling import TaskSchedulingHeld

    @application.exception_handler(TaskSchedulingHeld)
    async def assistant_task_held(_request, exc):
        return JSONResponse({"detail": {"code": exc.code, "message": str(exc)}}, status_code=exc.status)

    # Accept-Language → request.state.ui_language (docs/LIGHT_MODE_I18N_PLAN.md §2.1).
    from core.ui_language import UILanguageMiddleware
    application.add_middleware(UILanguageMiddleware)
    application.add_middleware(
        CORSMiddlewareExemptingV1,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Auth routes (no auth required for register/login/refresh) ──
    if config.jwt_secret:
        from auth.routes import router as auth_router
        application.include_router(auth_router)
        from auth.legal import router as legal_router
        application.include_router(legal_router)

    # ── WebSocket endpoint (replaces SSE) ──
    from api.ws import router as ws_router
    application.include_router(ws_router)
    from api.voice import router as voice_router
    application.include_router(voice_router)

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

    from api.publish_route import router as publish_route_router
    application.include_router(publish_route_router)

    from api.assets import router as assets_router
    application.include_router(assets_router)

    from api.assistant import router as assistant_router
    application.include_router(assistant_router)
    from api.assistant_appearance import router as assistant_appearance_router
    application.include_router(assistant_appearance_router)

    from api.video_productions import router as video_productions_router
    application.include_router(video_productions_router)

    from api.first_video import router as first_video_router
    application.include_router(first_video_router)

    from api.light import router as light_router
    application.include_router(light_router)
    from api.video_templates import router as video_templates_router
    application.include_router(video_templates_router)
    from api.media_templates import router as media_templates_router
    application.include_router(media_templates_router)
    from api.stores import router as stores_router
    application.include_router(stores_router)

    from api.workspaces import router as workspaces_router
    from api.admin import router as admin_router
    from api.admin_billing import router as admin_billing_router
    from api.admin_pricing import router as admin_pricing_router
    from api.admin_partners import router as admin_partners_router
    from api.admin_fleet import router as admin_fleet_router
    from api.admin_skills import router as admin_skills_router
    application.include_router(workspaces_router)
    application.include_router(admin_router)
    application.include_router(admin_fleet_router)
    application.include_router(admin_skills_router)
    application.include_router(admin_billing_router)
    application.include_router(admin_pricing_router)
    application.include_router(admin_partners_router)
    # Plugin routes mount here, in registration order (the embedded admin
    # trajectory API first); none of them overlaps a route included above.
    application.state.kernel_extensions.mount(application)

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
    from api.admin_examples import router as admin_examples_router
    application.include_router(admin_examples_router)  # Light 参考样例库
    application.include_router(douyin_webhook_router)  # No auth — signed by the platform

    # ── Agent routes ──
    agent_router = APIRouter(prefix="/api/agent", tags=["Agent"])

    from api.projects import brief_router as project_brief_router, router as project_router
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
    application.include_router(project_brief_router)

    # ── Public harness API (API-key auth, its own error contract) ──
    from api.v1.app import create_v1_app
    application.mount("/v1", create_v1_app())

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
