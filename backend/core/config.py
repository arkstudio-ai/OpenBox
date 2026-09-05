"""OpenBox unified configuration loading and validation."""
import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from core.log import create_logger

log = create_logger("config")


class ProvisioningConfigError(Exception):
    """A desktop provisioning setting would create an invalid or unpaid order."""


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class ProviderConfig(BaseModel):
    api_key: str | None = None
    base_url: str | None = None
    options: dict[str, Any] = {}


class CompactionConfig(BaseModel):
    auto: bool = True
    prune: bool = True
    reserved: int | None = None  # Override default buffer
    # How much recent history survives a compaction verbatim instead of being
    # replaced by the summary. None = 25% of usable context, clamped to
    # [8k, 60k] tokens. See agent/compaction_select.
    preserve_recent_tokens: int | None = None
    # Cap on how many recent turns are eligible for that tail. 0 disables the
    # tail entirely (summary-only, the pre-0.2 behaviour).
    tail_turns: int | None = None


class McpServerConfig(BaseModel):
    type: str = "local"  # "local" or "remote"
    command: list[str] = []
    url: str | None = None
    env: dict[str, str] = {}


class ModelConfig(BaseModel):
    id: str
    name: str | None = None       # Display name; defaults to last segment of id
    provider: str | None = None    # Defaults to first segment of id
    max_tokens: int = 200000
    context_limit: int | None = None  # Override context window size (tokens)
    # Whether the model accepts image input. None = fall back to the family
    # heuristic in agent.vision. Set it explicitly when a gateway exposes a
    # text-only variant of an otherwise multimodal family.
    vision: bool | None = None


class AgentOverride(BaseModel):
    """Per-agent config. Also defines an agent when the name is unknown.

    Mirrors opencode's `agent` config block: the same entry can retune a
    built-in or introduce one of your own, and `disable` takes one away.
    """
    model: str | None = None
    temperature: float | None = None
    prompt: str | None = None
    max_steps: int | None = None
    permission: list[dict[str, str]] | None = None
    # -- registry-level: what the agent *is*, not just how it runs --
    description: str | None = None
    #: "primary" (chat only) | "subagent" (task-spawned only) | "all" (both).
    #: A config-defined agent defaults to "all", as in opencode.
    mode: str | None = None
    #: Keep it out of the picker without deleting it.
    hidden: bool | None = None
    #: Replaces the toolset outright — an agent's tools are a whitelist, and
    #: accumulating them would quietly widen what a narrowed agent can do.
    tools: list[str] | None = None
    #: Accent colour for the UI, as in opencode.
    color: str | None = None
    #: Remove a built-in agent entirely.
    disable: bool = False


class SkillsConfig(BaseModel):
    paths: list[str] = []
    urls: list[str] = []


class ToolExposureConfig(BaseModel):
    """Tool schema exposure and discovery budgets.

    ``legacy_eager`` remains the safe migration default.  The other modes are
    explicit so rollout never silently changes when a provider/model name is
    edited elsewhere in the config.
    """

    mode: Literal[
        "legacy_eager",
        "shadow",
        "portable",
        "native_auto",
        "emergency_eager",
    ] = "legacy_eager"
    resident_soft_chars: int = Field(default=20_000, ge=1_000, le=128_000)
    resident_hard_chars: int = Field(default=24_000, ge=1_000, le=128_000)
    active_soft_chars: int = Field(default=28_000, ge=1_000, le=128_000)
    active_hard_chars: int = Field(default=32_000, ge=1_000, le=128_000)
    native_wire_soft_chars: int = Field(default=96_000, ge=1_000, le=128_000)
    # 128K is a platform safety ceiling, not a tuneable performance target.
    # Deployments may lower it but cannot configure their way around it.
    native_wire_hard_chars: int = Field(default=128_000, ge=1_000, le=128_000)
    single_tool_soft_chars: int = Field(default=2_500, ge=100, le=128_000)
    single_tool_hard_chars: int = Field(default=5_000, ge=100, le=128_000)
    intent_pack_soft_chars: int = Field(default=10_000, ge=500, le=128_000)
    intent_pack_hard_chars: int = Field(default=12_000, ge=500, le=128_000)
    skill_listing_soft_chars: int = Field(default=6_000, ge=500, le=128_000)
    skill_listing_hard_chars: int = Field(default=8_000, ge=500, le=128_000)
    reveal_ttl_seconds: int = Field(default=1_800, ge=60, le=86_400)
    max_persisted_reveals: int = Field(default=8, ge=1, le=64)
    max_search_calls_per_step: int = Field(default=2, ge=1, le=10)
    max_reveals_per_step: int = Field(default=5, ge=1, le=50)
    max_search_result_chars_per_step: int = Field(default=2_000, ge=100, le=32_000)
    native_endpoint_allowlist: list[str] = Field(default_factory=list)
    native_model_allowlist: list[str] = Field(default_factory=list)
    allow_emergency_eager: bool = False

    @model_validator(mode="after")
    def validate_budget_order(self) -> "ToolExposureConfig":
        for prefix in (
            "resident",
            "active",
            "native_wire",
            "single_tool",
            "intent_pack",
            "skill_listing",
        ):
            if getattr(self, f"{prefix}_soft_chars") > getattr(self, f"{prefix}_hard_chars"):
                raise ValueError(f"{prefix}_soft_chars must not exceed {prefix}_hard_chars")
        if self.mode == "emergency_eager" and not self.allow_emergency_eager:
            raise ValueError(
                "tool_exposure.mode=emergency_eager requires allow_emergency_eager=true"
            )
        return self


class ImageGenerationConfig(BaseModel):
    """OpenAI-compatible Image API used by the built-in ``image_gen`` tool.

    Credentials stay in the existing provider block.  This section only
    selects which configured provider/model to use and supplies conservative
    output defaults, so chat and image traffic can share one gateway without
    duplicating secrets.
    """

    provider: str = ""  # empty = provider prefix of the default chat model
    model: str = "gpt-image-2"
    default_size: str = "auto"
    default_quality: str = "medium"
    output_format: str = "png"
    timeout_seconds: int = Field(default=600, ge=30, le=1800)
    # Content-addressed reuse of identical completed generations (n==1 only).
    dedupe: bool = True


class VideoModelConfig(BaseModel):
    """One selectable video model, declared rather than hard-coded.

    The split is by *protocol*, not by model: this entry says which wire
    channel a model speaks and whose credential pays for it, while the three
    adapters (ark/sd2/task) stay in ``tool/video_providers.py``. Adding a model
    that speaks an existing protocol is therefore config-only; a genuinely new
    protocol still needs code, because no config schema can express "poll
    ``metadata.url`` and unwrap a ``{code,message,data}`` envelope".

    The capability fields are not decoration, and they are the *only* record
    of what each model accepts: the relay exposes no model or pricing endpoint,
    so nothing can be discovered at runtime. Gateway-side behaviour differs per
    channel — the wan3 adapter rejects unknown switches with a 400, while the
    sd2 720p tier still discards video references silently and bills for the
    substitute. Declaring limits here lets the backend refuse before paying.
    """

    #: Wire model name sent to the provider, and the id the UI selects by.
    id: str
    #: Display name; defaults to the id.
    name: str | None = None
    channel: Literal["ark", "sd2", "task"] = "ark"
    #: Which ``provider`` credential entry pays for it. Empty means the
    #: channel's entry in ``channel_providers``, then ``provider`` above.
    provider: str = ""
    #: None = no declared ceiling (the provider's own limit applies).
    max_duration_seconds: int | None = Field(default=None, ge=1, le=300)
    #: Empty = accept whatever the production asks for.
    resolutions: list[str] = []
    supports_reference_image: bool = True
    supports_reference_video: bool = True
    #: Fast tiers render silent video. Declaring it lets the picker and the
    #: `models` listing say so up front, instead of the caller discovering it
    #: from a refused submit.
    supports_generated_audio: bool = True
    #: Accepted aspect ratios. Empty = whatever the channel validator allows.
    #: Declare them per model: wan3 rejects 21:9 outright rather than
    #: substituting a default, so a global list would mis-describe it.
    ratios: list[str] = []
    #: Inclusive (min, max) explicit duration in seconds. None = channel default.
    duration_range: tuple[int, int] | None = None
    #: Whether ``duration=-1`` (let the model choose) is accepted.
    supports_smart_duration: bool = True
    #: A reproducible-noise seed. Useful for holding one presenter's look
    #: across separately generated shots without re-describing them.
    supports_seed: bool = False
    #: first_frame / last_frame reference roles (continuity between shots).
    supports_first_last_frame: bool = False
    #: reference_audio role (drive the performance from an audio track).
    supports_reference_audio: bool = False
    #: Which body shape the gateway channel behind this model actually reads.
    #: Measured 2026-09-01 against the gateway source, because the three
    #: differ and a wrong one is accepted and then ignored:
    #:   "flat"     — resolution/ratio at the top level, passed through as-is
    #:                (Sora adaptor, type 55: the body is relayed verbatim)
    #:   "metadata" — resolution/ratio/seed/content[] under `metadata`; the
    #:                top-level DTO has no field for them (DoubaoVideo, 54)
    #:   "size"     — a `WxH` string at the top level, which the adaptor
    #:                parses into its own resolution tiers (MiniMax, 58)
    wire_shape: Literal["flat", "metadata", "size"] = "flat"
    #: Shown next to the name in the picker so an expensive switch is visible.
    tier: str = ""

    @model_validator(mode="after")
    def _check_duration_range(self):
        if self.duration_range is not None:
            low, high = self.duration_range
            if low < 1 or high < low:
                raise ValueError("duration_range must be (min, max) with 1 <= min <= max")
        return self


class VideoGenerationConfig(BaseModel):
    """Async video generation plus sandbox-render orchestration.

    Provider credentials are selected from the existing provider map.  The
    renderer itself never receives them; it only sees object-scoped OSS URLs.
    """

    provider: str = "bossip"
    # Wan 3.0 is the product default. Deployments still declare its concrete
    # channel below because the BossIP relay serves it through ``/v1/videos``
    # (``sd2``), while another gateway may expose the native ``task`` protocol.
    model: str = "wan3.0-video"
    # 720p, not 1080p: it is the tier every model offers, and it is what a
    # vertical short actually needs. Defaulting to the top tier quietly made
    # every generation the most expensive one available.
    default_resolution: str = "720p"
    default_ratio: str = "9:16"
    default_duration: int = Field(default=-1, ge=-1, le=30)
    default_generate_audio: bool = True
    default_watermark: bool = False
    submit_timeout_seconds: int = Field(default=180, ge=30, le=600)
    status_timeout_seconds: int = Field(default=60, ge=10, le=180)
    poll_interval_seconds: float = Field(default=5.0, ge=1.0, le=30.0)
    wait_timeout_seconds: float = Field(default=25.0, ge=0.0, le=25.0)
    provider_input_url_ttl_seconds: int = Field(default=3600, ge=600, le=86400)
    render_url_ttl_seconds: int = Field(default=86400, ge=3600, le=604800)
    max_provider_output_bytes: int = Field(
        default=1024 * 1024 * 1024, ge=1024 * 1024, le=4 * 1024 * 1024 * 1024
    )
    # Model → channel routing lives in code (tool/video_providers.py); this map
    # only says which provider-credential entry serves each gateway channel,
    # e.g. {"sd2": "newapi", "task": "newapi"}. A missing key disables the
    # channel. The default "provider" above stays the ark-channel fallback.
    channel_providers: dict[str, str] = {}
    #: Declared, selectable video models. Empty keeps the historical behaviour
    #: where routing is inferred from the model name alone; any entry here wins
    #: over that inference, so a deployment can add a model without a release.
    models: list[VideoModelConfig] = []
    # Optional whitelist for per-segment model overrides; empty = any model the
    # routing predicates accept.
    allowed_models: list[str] = []
    # Cross-user prompt-hash reuse of identical completed segments.
    dedupe: bool = True
    #: Per-user daily ceiling on paid generation submits. 0 disables the check.
    #: A stand-in for the shared credits ledger: it is back-pressure, not an
    #: approval gate — the tool refuses and tells the agent to relay that to
    #: the user, rather than asking anyone to confirm a charge.
    daily_job_limit: int = Field(default=50, ge=0, le=10_000)
    #: Refuse a paid submit when an identical request (same prompt_hash) is
    #: still in flight for this user. Callers that genuinely want a second take
    #: pass allow_duplicate=true.
    refuse_duplicate_in_flight: bool = True


class VideoTranscriptionConfig(BaseModel):
    """Segment-level speech QA through a backend-owned provider.

    WUYING extracts a stable audio object first.  The backend then gives its
    short-lived URL to DashScope (default) or an explicitly configured
    OpenAI-compatible gateway.  Credentials never enter the sandbox or prompt.
    """

    engine: str = "dashscope"
    base_url: str = "https://dashscope.aliyuncs.com"
    api_key: str = ""
    model: str = "fun-asr"
    timeout_seconds: int = Field(default=180, ge=30, le=600)
    poll_interval_seconds: float = Field(default=1.0, ge=0.25, le=10.0)
    similarity_threshold: float = Field(default=0.90, ge=0.5, le=1.0)


# ---------------------------------------------------------------------------
# Unified root config
# ---------------------------------------------------------------------------

class OpenBoxConfig(BaseModel):
    """Unified configuration merging server settings and agent config."""

    # -- Server --
    host: str = "0.0.0.0"
    port: int = 8080
    debug: bool = False
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:3000"]
    )

    # -- Sandbox --
    sandbox_provider: str = "docker"               # "docker" | "kubernetes" | "wuying"
    sandbox_image: str = "openbox-sandbox:latest"
    container_name_prefix: str = "openbox-sandbox-"
    container_port_range: tuple[int, int] = (10000, 19999)
    action_server_port: int = 8000
    container_ready_timeout: int = 30
    container_ready_interval: float = 0.5

    # -- Kubernetes (GKE) --
    k8s_namespace: str = "openbox-sandbox"
    k8s_storage_class: str = "standard-rwo"
    k8s_storage_size: str = "10Gi"
    k8s_sandbox_cpu_request: str = "250m"
    k8s_sandbox_cpu_limit: str = "1000m"
    k8s_sandbox_memory_request: str = "256Mi"
    k8s_sandbox_memory_limit: str = "1Gi"
    k8s_sandbox_service_account: str = ""           # SA for sandbox pods (Workload Identity)
    sandbox_idle_timeout: int = 1800                # idle reclaim seconds (30 min)

    # -- WUYING cloud desktop (sandbox_provider="wuying") --
    # A long-lived Alibaba Cloud desktop running the action server, reached over
    # a tunnel. OpenBox never creates or destroys it.
    wuying_endpoint: str = "http://127.0.0.1:18000"
    wuying_api_key: str = ""                        # must match SESSION_API_KEY on the desktop
    wuying_desktop_id: str = ""                     # ecd-... , used by the desktop-view ticket API
    wuying_region_id: str = "cn-hangzhou"           # region the desktop lives in
    wuying_end_user_id: str = ""                    # Wuying end user the web view logs in as

    # -- WUYING per-user desktop provisioning (wuying_mode="per_user") --
    # "shared" keeps the single pre-provisioned desktop above; "per_user" makes
    # OpenBox create one ECD desktop + one convenience EndUser per user via the
    # ECD OpenAPI (ported from the bossip wuying-bridge integration).
    wuying_mode: Literal["shared", "per_user"] = "shared"
    # Routing is deliberately independent from provisioning mode so a release
    # can retain the proven shared execution path while per-desktop routing is
    # rolled out (or rolled back) without changing the desktop-view contract.
    wuying_routing: Literal["shared", "per_desktop"] = "shared"
    wuying_channel: Literal["direct", "ssh"] = "ssh"
    wuying_relay_host: str = ""
    wuying_relay_port: int = Field(default=2222, ge=1, le=65535)
    wuying_relay_user: str = "obxtunnel"
    wuying_relay_hostkey: str = ""                 # complete known_hosts line
    wuying_tunnel_bind: str = "172.17.0.1"
    wuying_tunnel_port_range: str = "18100-18999"
    wuying_channel_key: str = ""                   # 32-byte base64/hex encryption key
    internal_api_token: str = ""
    wuying_health_interval_sec: int = Field(default=30, ge=5, le=3600)
    wuying_image_id: str = ""                       # golden image; required in per_user mode —
                                                    # never fall back to a community image silently
    wuying_office_site_id: str = ""                 # ECD office site (workspace directory) id
    wuying_desktop_type: str = "eds.enterprise_office.6c12g"
    wuying_system_disk_size: int = 50               # GiB; v2 golden image target
    # No default on purpose: the policy group pins the session resolution (the
    # deployment standard is "OpenBox Personal 1080p"), and Alibaba's default
    # policy is resolution-adaptive — a desktop created with it drifts away
    # from what the agent screenshots. per_user provisioning refuses to create
    # desktops until this is set explicitly, like wuying_image_id.
    wuying_policy_group_id: str = ""
    wuying_charge_type: Literal["PostPaid", "PrePaid"] = "PostPaid"
    wuying_period: int = 1
    wuying_period_unit: Literal["Month", "Year"] = "Month"
    wuying_auto_pay: bool = True
    wuying_auto_renew: bool = False
    pool_max_unit_price_cny: float = Field(default=300.0, gt=0)
    pool_enabled: bool = False
    pool_auto_purchase: bool = False
    pool_target_prewarm: int = Field(default=5, ge=0, le=100)
    pool_max_purchases_per_tick: int = Field(default=1, ge=1, le=10)
    pool_max_purchases_per_day: int = Field(default=2, ge=1, le=100)
    pool_min_account_balance_multiple: float = Field(default=2.0, ge=1, le=100)
    pool_renew_before_days: int = Field(default=3, ge=1, le=30)
    pool_assign_on_provision: bool = True
    pool_adopt_allowlist: str = ""
    fleet_snapshot_interval_sec: int = Field(default=600, ge=60, le=86400)
    fleet_channel_down_alert_sec: int = Field(default=600, ge=60, le=86400)
    wuying_password_salt: str = ""                  # per-deployment secret for derived EndUser passwords
    wuying_env_tag: str = "default"                 # openbox-env tag; isolates prod/dev sharing one account

    # -- Browser automation on the cloud desktop --
    # local=drive a headed Chrome on the cloud desktop over CDP; extension=drive
    # the user's own browser through the dev-browser relay; auto=prefer the
    # user's browser but fall back to the cloud one when it disconnects.
    browser_mode: str = "auto"
    browser_chrome_port: int = 9333                 # remote-debugging port of the desktop-local Chrome

    @model_validator(mode="after")
    def validate_wuying_billing(self) -> "OpenBoxConfig":
        if self.wuying_charge_type == "PrePaid" and not self.wuying_auto_pay:
            raise ProvisioningConfigError(
                "WUYING_AUTO_PAY must be true when WUYING_CHARGE_TYPE=PrePaid; "
                "otherwise Alibaba Cloud creates an unpaid order instead of a desktop"
            )
        allowed = {"Month": {1, 2, 3, 6}, "Year": {1, 2, 3, 4, 5}}
        if self.wuying_period not in allowed[self.wuying_period_unit]:
            values = ", ".join(str(v) for v in sorted(allowed[self.wuying_period_unit]))
            raise ProvisioningConfigError(
                f"WUYING_PERIOD must be one of {values} for {self.wuying_period_unit}"
            )
        return self

    # -- OSS asset transfer (browser -> OSS -> cloud desktop) --
    oss_bucket: str = ""                            # empty = OSS transfer disabled
    oss_region: str = "cn-hangzhou"
    oss_endpoint: str = ""                          # default oss-{region}.aliyuncs.com
    #: Per-user ceiling the resource centre reports; 0 = unlimited.
    oss_user_quota_bytes: int = 5 * 1024 * 1024 * 1024

    # -- Multi-user infrastructure --
    database_url: str = "postgresql+asyncpg://openbox:openbox@localhost:5432/openbox"
    db_pool_size: int = 10
    db_pool_overflow: int = 20
    redis_url: str = "redis://localhost:6379/0"
    blob_provider: str = "azure"
    blob_azure_connection_string: str = ""
    blob_azure_container: str = "ads-staging"
    blob_local_path: str = "/opt/openbox/blobs"
    gcs_bucket: str = ""

    # -- Authentication / quotas --
    jwt_secret: str = ""
    jwt_access_expire_minutes: int = 15
    jwt_refresh_expire_days: int = 7
    max_containers_per_user: int = 5
    max_sessions_per_user: int = 200
    # Concurrent agent runs per user. One meant a second conversation could not
    # start while the first was still thinking — the common case of parking a
    # long task and working on something else was simply blocked.
    max_concurrent_agents: int = 5
    monthly_cost_limit: float = 50.0
    rate_limit_login: str = "5/minute"
    rate_limit_api: str = "60/minute"

    # -- Logto OIDC (optional; local username/password still works when unset) --
    # The browser is a public PKCE client, so no client secret is stored here.
    logto_endpoint: str = ""                 # e.g. https://account.example.com
    logto_app_id: str = ""
    #: The Logto "Native" application the mobile app signs in with. A native
    #: client is public — it holds no secret — so it must be its own
    #: application, and its id is a second valid ID-token audience.
    logto_native_app_id: str = ""
    # Required only for a confidential app (Logto "Traditional Web"); a SPA /
    # Native app is a public client and leaves this empty.
    logto_app_secret: str = ""
    logto_issuer: str = ""                   # defaults to {endpoint}/oidc
    logto_jwks_uri: str = ""                 # defaults to {endpoint}/oidc/jwks
    logto_redirect_uri: str = "http://localhost:3000/callback"
    logto_post_logout_redirect_uri: str = "http://localhost:3000"

    # -- Cron (scheduled tasks) --
    # The Wuying deployment shares one cloud desktop across users, so the
    # global concurrency stays small; raise it per-deployment when the sandbox
    # provider can absorb more parallel agent loops.
    cron_max_concurrent_jobs: int = 2
    cron_max_concurrent_per_user: int = 2
    cron_max_jobs_per_user: int = 20
    cron_max_jobs_per_project: int = 10
    cron_min_interval_seconds: int = 300           # recurring jobs may not fire more often
    cron_max_task_prompt_length: int = 5000
    cron_timeout_seconds_min: int = 60
    cron_timeout_seconds_max: int = 7200
    cron_auto_disable_after: int = 10              # consecutive failures before auto-disable
    cron_missed_run_max_age_seconds: int = 6 * 3600  # older missed runs reschedule instead of replay
    cron_summary_model: str = ""                   # "" = job model > session model > default model
    cron_transcript_keep_per_job: int = 20         # newest run transcripts kept per job (30d cap on top)

    cron_default_locale: str = "zh-CN"             # injected-text language when the user never chose one

    # -- Agent --
    model: str = "anthropic/claude-sonnet-4-20250514"
    mcp_filter_model: str = ""
    models: list[ModelConfig] = []
    provider: dict[str, ProviderConfig] = {}
    agent: dict[str, AgentOverride] = {}
    #: Which agent a new conversation starts in. Must be one a person can
    #: pick — see agent.default_agent_name, which falls back rather than
    #: stranding every new chat in an agent that cannot hold a conversation.
    default_agent: str | None = None
    permission: dict[str, Any] = {}
    mcp: dict[str, McpServerConfig] = {}
    skills: SkillsConfig = SkillsConfig()
    tool_exposure: ToolExposureConfig = ToolExposureConfig()
    image_generation: ImageGenerationConfig = ImageGenerationConfig()
    video_generation: VideoGenerationConfig = VideoGenerationConfig()
    video_transcription: VideoTranscriptionConfig = VideoTranscriptionConfig()
    compaction: CompactionConfig = CompactionConfig()
    instructions: list[str] = []


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_config: OpenBoxConfig | None = None


# ---------------------------------------------------------------------------
# Config file discovery
# ---------------------------------------------------------------------------

def _find_config_files() -> list[Path]:
    """Find config files in priority order (later overrides earlier)."""
    candidates: list[Path] = []

    # 1. Global config
    config_home = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))

    # Prefer openbox, fall back to openagent
    global_path_new = Path(config_home) / "openbox" / "openbox.json"
    global_path_old = Path(config_home) / "openagent" / "openagent.json"
    if global_path_new.exists():
        candidates.append(global_path_new)
    elif global_path_old.exists():
        candidates.append(global_path_old)

    # 2. Project-level config (current directory)
    cwd = Path.cwd()
    for name in ["openbox.json", "openbox.jsonc", "openagent.json", "openagent.jsonc"]:
        project_path = cwd / name
        if project_path.exists():
            candidates.append(project_path)
            break

    # 3. Custom via environment variable (prefer OPENBOX_CONFIG, fall back to OPENAGENT_CONFIG)
    custom = os.environ.get("OPENBOX_CONFIG") or os.environ.get("OPENAGENT_CONFIG")
    if custom and Path(custom).exists():
        candidates.append(Path(custom))

    return candidates


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _strip_json_comments(text: str) -> str:
    """Strip // and /* */ comments from JSON, respecting quoted strings."""
    result = []
    i = 0
    n = len(text)
    while i < n:
        # String literal: copy verbatim
        if text[i] == '"':
            result.append('"')
            i += 1
            while i < n:
                ch = text[i]
                result.append(ch)
                i += 1
                if ch == '\\' and i < n:
                    result.append(text[i])
                    i += 1
                elif ch == '"':
                    break
        # Line comment
        elif text[i:i+2] == '//':
            while i < n and text[i] != '\n':
                i += 1
        # Block comment
        elif text[i:i+2] == '/*':
            i += 2
            while i < n and text[i-1:i+1] != '*/':
                i += 1
            i += 1
        else:
            result.append(text[i])
            i += 1
    return ''.join(result)


def _load_json(path: Path) -> dict:
    """Load JSON with comment stripping (supports // and /* */ in any .json/.jsonc)."""
    content = path.read_text(encoding="utf-8")
    content = _strip_json_comments(content)
    return json.loads(content)


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dicts, override wins for conflicts."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _resolve_env_vars(data: Any) -> Any:
    """Resolve {env:VAR_NAME} placeholders in config values."""
    if isinstance(data, str):
        import re

        def replacer(m):
            var_name = m.group(1)
            return os.environ.get(var_name, "")

        return re.sub(r"\{env:(\w+)\}", replacer, data)
    elif isinstance(data, dict):
        return {k: _resolve_env_vars(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [_resolve_env_vars(v) for v in data]
    return data


# ---------------------------------------------------------------------------
# Environment variable overrides for server fields
# ---------------------------------------------------------------------------

def _apply_env_overrides(data: dict) -> dict:
    """Apply environment variable overrides for server/sandbox/agent fields."""
    # Server & sandbox
    env_map = {
        "host": "HOST",
        "port": "PORT",
        "debug": "DEBUG",
        "sandbox_provider": "SANDBOX_PROVIDER",
        "sandbox_image": "SANDBOX_IMAGE",
        "container_name_prefix": "CONTAINER_NAME_PREFIX",
        "k8s_namespace": "K8S_NAMESPACE",
        "k8s_storage_class": "K8S_STORAGE_CLASS",
        "k8s_storage_size": "K8S_STORAGE_SIZE",
        "k8s_sandbox_cpu_request": "K8S_SANDBOX_CPU_REQUEST",
        "k8s_sandbox_cpu_limit": "K8S_SANDBOX_CPU_LIMIT",
        "k8s_sandbox_memory_request": "K8S_SANDBOX_MEMORY_REQUEST",
        "k8s_sandbox_memory_limit": "K8S_SANDBOX_MEMORY_LIMIT",
        "k8s_sandbox_service_account": "K8S_SANDBOX_SERVICE_ACCOUNT",
        "sandbox_idle_timeout": "SANDBOX_IDLE_TIMEOUT",
        "wuying_endpoint": "WUYING_ENDPOINT",
        "wuying_api_key": "WUYING_API_KEY",
        "wuying_desktop_id": "WUYING_DESKTOP_ID",
        "wuying_region_id": "WUYING_REGION_ID",
        "wuying_end_user_id": "WUYING_END_USER_ID",
        "wuying_mode": "WUYING_MODE",
        "wuying_routing": "WUYING_ROUTING",
        "wuying_channel": "WUYING_CHANNEL",
        "wuying_relay_host": "WUYING_RELAY_HOST",
        "wuying_relay_port": "WUYING_RELAY_PORT",
        "wuying_relay_user": "WUYING_RELAY_USER",
        "wuying_relay_hostkey": "WUYING_RELAY_HOSTKEY",
        "wuying_tunnel_bind": "WUYING_TUNNEL_BIND",
        "wuying_tunnel_port_range": "WUYING_TUNNEL_PORT_RANGE",
        "wuying_channel_key": "WUYING_CHANNEL_KEY",
        "internal_api_token": "INTERNAL_API_TOKEN",
        "wuying_health_interval_sec": "WUYING_HEALTH_INTERVAL_SEC",
        "wuying_image_id": "WUYING_IMAGE_ID",
        "wuying_office_site_id": "WUYING_OFFICE_SITE_ID",
        "wuying_desktop_type": "WUYING_DESKTOP_TYPE",
        "wuying_system_disk_size": "WUYING_SYSTEM_DISK_SIZE",
        "wuying_policy_group_id": "WUYING_POLICY_GROUP_ID",
        "wuying_charge_type": "WUYING_CHARGE_TYPE",
        "wuying_period": "WUYING_PERIOD",
        "wuying_period_unit": "WUYING_PERIOD_UNIT",
        "wuying_auto_pay": "WUYING_AUTO_PAY",
        "wuying_auto_renew": "WUYING_AUTO_RENEW",
        "pool_max_unit_price_cny": "POOL_MAX_UNIT_PRICE_CNY",
        "pool_enabled": "POOL_ENABLED",
        "pool_auto_purchase": "POOL_AUTO_PURCHASE",
        "pool_target_prewarm": "POOL_TARGET_PREWARM",
        "pool_max_purchases_per_tick": "POOL_MAX_PURCHASES_PER_TICK",
        "pool_max_purchases_per_day": "POOL_MAX_PURCHASES_PER_DAY",
        "pool_min_account_balance_multiple": "POOL_MIN_ACCOUNT_BALANCE_MULTIPLE",
        "pool_renew_before_days": "POOL_RENEW_BEFORE_DAYS",
        "pool_assign_on_provision": "POOL_ASSIGN_ON_PROVISION",
        "pool_adopt_allowlist": "POOL_ADOPT_ALLOWLIST",
        "fleet_snapshot_interval_sec": "FLEET_SNAPSHOT_INTERVAL_SEC",
        "fleet_channel_down_alert_sec": "FLEET_CHANNEL_DOWN_ALERT_SEC",
        "wuying_password_salt": "WUYING_PASSWORD_SALT",
        "wuying_env_tag": "WUYING_ENV_TAG",
        "browser_mode": "BROWSER_MODE",
        "browser_chrome_port": "BROWSER_CHROME_PORT",
        "oss_bucket": "OSS_BUCKET",
        "oss_region": "OSS_REGION",
        "oss_endpoint": "OSS_ENDPOINT",
        "oss_user_quota_bytes": "OSS_USER_QUOTA_BYTES",
        "database_url": "DATABASE_URL",
        "db_pool_size": "DB_POOL_SIZE",
        "db_pool_overflow": "DB_POOL_OVERFLOW",
        "redis_url": "REDIS_URL",
        "blob_provider": "BLOB_PROVIDER",
        "blob_azure_connection_string": "BLOB_AZURE_CONNECTION_STRING",
        "blob_azure_container": "BLOB_AZURE_CONTAINER",
        "blob_local_path": "BLOB_LOCAL_PATH",
        "gcs_bucket": "GCS_BUCKET",
        "jwt_secret": "JWT_SECRET",
        "jwt_access_expire_minutes": "JWT_ACCESS_EXPIRE_MINUTES",
        "jwt_refresh_expire_days": "JWT_REFRESH_EXPIRE_DAYS",
        "max_containers_per_user": "MAX_CONTAINERS_PER_USER",
        "max_sessions_per_user": "MAX_SESSIONS_PER_USER",
        "max_concurrent_agents": "MAX_CONCURRENT_AGENTS",
        "monthly_cost_limit": "MONTHLY_COST_LIMIT",
        "rate_limit_login": "RATE_LIMIT_LOGIN",
        "rate_limit_api": "RATE_LIMIT_API",
        "logto_endpoint": "LOGTO_ENDPOINT",
        "logto_app_id": "LOGTO_APP_ID",
        "logto_native_app_id": "LOGTO_NATIVE_APP_ID",
        "logto_app_secret": "LOGTO_APP_SECRET",
        "logto_issuer": "LOGTO_ISSUER",
        "logto_jwks_uri": "LOGTO_JWKS_URI",
        "logto_redirect_uri": "LOGTO_REDIRECT_URI",
        "logto_post_logout_redirect_uri": "LOGTO_POST_LOGOUT_REDIRECT_URI",
    }
    for field_name, env_var in env_map.items():
        value = os.environ.get(env_var)
        if value is not None:
            if field_name == "port":
                data[field_name] = int(value)
            elif field_name == "sandbox_idle_timeout":
                data[field_name] = int(value)
            elif field_name in {"db_pool_size", "db_pool_overflow", "jwt_access_expire_minutes",
                                "jwt_refresh_expire_days", "max_containers_per_user", "max_sessions_per_user",
                                "max_concurrent_agents", "browser_chrome_port",
                                "oss_user_quota_bytes", "wuying_system_disk_size",
                                "wuying_period", "pool_target_prewarm",
                                "pool_max_purchases_per_tick", "pool_max_purchases_per_day",
                                "pool_renew_before_days", "fleet_snapshot_interval_sec",
                                "fleet_channel_down_alert_sec"}:
                data[field_name] = int(value)
            elif field_name in {"monthly_cost_limit", "pool_max_unit_price_cny",
                                "pool_min_account_balance_multiple"}:
                data[field_name] = float(value)
            elif field_name in {"debug", "wuying_auto_pay", "wuying_auto_renew",
                                "pool_enabled", "pool_auto_purchase",
                                "pool_assign_on_provision"}:
                data[field_name] = value.lower() == "true"
            else:
                data[field_name] = value

    # Agent model
    model_env = os.environ.get("OPENBOX_MODEL")
    if model_env:
        data["model"] = model_env

    # Provider: OPENBOX_API_KEY / OPENBOX_BASE_URL apply to the active model's provider
    api_key = os.environ.get("OPENBOX_API_KEY")
    base_url = os.environ.get("OPENBOX_BASE_URL")
    if api_key or base_url:
        model_id = data.get("model", "anthropic/claude-sonnet-4-20250514")
        provider_name = model_id.split("/")[0] if "/" in model_id else model_id
        providers = data.setdefault("provider", {})
        provider_cfg = providers.setdefault(provider_name, {})
        if api_key:
            provider_cfg["api_key"] = api_key
        if base_url:
            provider_cfg["base_url"] = base_url

    # Tool exposure has its own nested config. Keep environment overrides
    # explicit rather than inventing a generic nested-key parser that could
    # accidentally accept misspelled security settings.
    exposure_mode = os.environ.get("OPENBOX_TOOL_EXPOSURE_MODE")
    allow_emergency = os.environ.get("OPENBOX_ALLOW_EMERGENCY_EAGER")
    if exposure_mode or allow_emergency is not None:
        exposure = data.setdefault("tool_exposure", {})
        if exposure_mode:
            exposure["mode"] = exposure_mode
        if allow_emergency is not None:
            exposure["allow_emergency_eager"] = allow_emergency.lower() == "true"

    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config() -> OpenBoxConfig:
    """Load and merge all config files."""
    merged: dict = {}

    for path in _find_config_files():
        try:
            data = _load_json(path)
            merged = _deep_merge(merged, data)
            log.info(f"Loaded config from {path}")
        except Exception as e:
            log.warning(f"Failed to load config {path}: {e}")

    # Apply inline config from environment (prefer OPENBOX_, fall back to OPENAGENT_)
    inline = os.environ.get("OPENBOX_CONFIG_CONTENT") or os.environ.get("OPENAGENT_CONFIG_CONTENT")
    if inline:
        try:
            data = json.loads(inline)
            merged = _deep_merge(merged, data)
        except Exception as e:
            log.warning(f"Failed to parse inline config content: {e}")

    # Resolve environment variables
    merged = _resolve_env_vars(merged)

    # Apply env var overrides for server fields
    merged = _apply_env_overrides(merged)

    return OpenBoxConfig(**merged)


def get_config() -> OpenBoxConfig:
    """Get the current config (loads on first access). Works both sync and async."""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reload_config() -> OpenBoxConfig:
    """Force reload config."""
    global _config
    _config = None
    _config = load_config()
    return _config
