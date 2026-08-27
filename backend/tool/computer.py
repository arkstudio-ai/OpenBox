"""computer: drive the sandbox's graphical desktop — see it, click it, type.

The model runs in the backend and the desktop lives on a cloud VM, so every
action is a round trip: xdotool injects input over the desktop's X session,
`scrot` captures the result, and the screenshot travels desktop → OSS →
model (never through the tunnel — see sandbox/assets.attach_sandbox_image).

Coordinates are the subtle part. The real screen is 4K; the model is shown a
downscaled screenshot and aims at THAT, so every coordinate it sends is
mapped back through the same scale factor the screenshot was taken with.
Geometry is re-read on each capture rather than assumed.
"""
import asyncio
import shlex
import time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from core.log import create_logger
from sandbox.desktop import (
    SHOT_PATH,
    NoDesktopError,
    ensure_desktop_tools,
    invalidate,
    take_screenshot,
    take_stable_screenshot,
    to_native,
    x,
)
from tool.tool import ToolResult, ToolContext, define_tool

log = create_logger("tool.computer")

#: Actions that change the screen, so the result is worth showing the model.
_VISUAL_ACTIONS = {
    "left_click", "right_click", "middle_click", "double_click", "triple_click",
    "left_click_drag", "type", "key", "hold_key", "scroll", "mouse_move",
}

_CLICK_BUTTON = {
    "left_click": "1",
    "middle_click": "2",
    "right_click": "3",
    "double_click": "1",
    "triple_click": "1",
}

_SCROLL_BUTTON = {"up": "4", "down": "5", "left": "6", "right": "7"}

#: Cached per container: the geometry of the last capture, so a click that
#: follows a screenshot needs no extra round trip.
_geometry_cache: dict[str, dict] = {}
# Re-probe occasionally, not before every click. A missing helper still heals
# on the next probe, while a normal desktop turn saves one tunnel round trip
# per computer call.
_probe_valid_until: dict[str, float] = {}
_PROBE_TTL_SECONDS = 60.0

AtomicAction = Literal[
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "triple_click",
    "left_click_drag",
    "mouse_move",
    "left_mouse_down",
    "left_mouse_up",
    "type",
    "key",
    "hold_key",
    "scroll",
    "wait",
]

SingleAction = Literal[
    "screenshot",
    "left_click",
    "right_click",
    "middle_click",
    "double_click",
    "triple_click",
    "left_click_drag",
    "mouse_move",
    "left_mouse_down",
    "left_mouse_up",
    "type",
    "key",
    "hold_key",
    "scroll",
    "cursor_position",
    "wait",
    "open_browser",
]


def _sandbox_key(ctx: ToolContext) -> str:
    """Cache identity of the machine, not the conversation.

    The desktop tooling and the screen resolution belong to the container;
    keying them by session made every new chat re-verify the install and
    re-measure a screen that had not changed.
    """
    return getattr(ctx.sandbox, "base_url", "") or f"{ctx.user_id}:{ctx.session_id}"


class _ActionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    coordinate: list[int] | None = Field(
        default=None,
        description="[x, y] in the coordinate space of the screenshot you were shown. "
        "Required for clicks, mouse_move, scroll and the start of a drag.",
    )
    to_coordinate: list[int] | None = Field(
        default=None, description="[x, y] the drag releases at (left_click_drag only)"
    )
    text: str | None = Field(
        default=None,
        description="Text to type (action=type), or the key combo to press "
        "(action=key), e.g. 'Return', 'ctrl+c', 'alt+Tab', 'super'.",
    )
    scroll_direction: Literal["up", "down", "left", "right"] | None = Field(
        default=None, description="Scroll direction (action=scroll)"
    )
    scroll_amount: int = Field(default=3, description="Number of scroll clicks (action=scroll)")
    duration: float = Field(default=1.0, description="Seconds to wait (action=wait), max 10")


class ComputerAction(_ActionPayload):
    """One atomic action inside a local desktop batch."""

    action: AtomicAction = Field(description="Atomic action to execute in the batch")


class ComputerArgs(_ActionPayload):
    """Runtime parser with the same invariants as the advertised union."""

    action: SingleAction | Literal["batch"] = Field(description="What to do on the desktop")
    actions: list[ComputerAction] | None = Field(
        default=None,
        min_length=1,
        max_length=12,
        description="Ordered atomic actions for action=batch. They run locally in one "
        "desktop round trip and produce one screenshot after the whole batch.",
    )

    @model_validator(mode="after")
    def validate_action_shape(self):
        if self.action == "batch" and not self.actions:
            raise ValueError("action='batch' requires a non-empty actions list")
        if self.action == "batch":
            batch_extras = self.model_fields_set & {
                "coordinate", "to_coordinate", "text", "scroll_direction",
                "scroll_amount", "duration",
            }
            if batch_extras:
                fields = ", ".join(sorted(batch_extras))
                raise ValueError(
                    f"action='batch' accepts only action and actions; remove: {fields}"
                )
        if self.action != "batch" and self.actions is not None:
            raise ValueError("actions is only valid when action='batch'")
        return self

    @classmethod
    def model_json_schema(cls, **kwargs) -> dict:
        """Advertise two disjoint shapes instead of one ambiguous object.

        The execution model stays convenient for internal callers, while the
        LLM sees a discriminator: normal actions cannot emit `actions`, and a
        batch cannot emit top-level coordinate/text/default fields.
        """
        schema = _computer_schema_adapter().json_schema(**kwargs)
        # The action const/enum values already discriminate the two branches.
        # Pydantic's optional `discriminator.mapping` contains $defs references
        # which become stale after our provider-compatibility inliner removes
        # $defs, so do not advertise that redundant keyword.
        schema.pop("discriminator", None)
        schema["type"] = "object"
        return schema


class _SingleComputerArgs(_ActionPayload):
    action: SingleAction = Field(description="One desktop action to execute")


class _BatchComputerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["batch"] = Field(description="Execute ordered desktop actions locally")
    actions: list[ComputerAction] = Field(
        min_length=1,
        max_length=12,
        description="Ordered atomic actions. Exactly one final OSS screenshot is returned.",
    )


def _computer_schema_adapter() -> TypeAdapter:
    return TypeAdapter(Annotated[
        _SingleComputerArgs | _BatchComputerArgs,
        Field(discriminator="action"),
    ])


def _bad(message: str) -> ToolResult:
    return ToolResult(title="computer: invalid arguments", output=message)


def _finalize_result(
    result: ToolResult,
    action: str,
    started: float,
    lease: dict | None = None,
) -> ToolResult:
    """Attach honest end-to-end timing and lease evidence to every result."""
    timings = result.metadata.setdefault("timings", {})
    if lease is not None:
        lease_metadata = {
            "wait_ms": int(lease.get("wait_ms", 0)),
            "ttl_seconds": int(lease.get("ttl_seconds", 0)),
        }
        result.metadata["lease"] = lease_metadata
        timings["lease_wait_ms"] = lease_metadata["wait_ms"]
    timings["total_ms"] = round((time.monotonic() - started) * 1000)

    # Tool metadata is persisted for the UI, but the model only receives the
    # textual tool result. Give it the same breakdown so it does not mistake
    # frame-settle time for the whole desktop transaction.
    visible = (
        "prepare_ms", "geometry_ms", "execute_ms", "capture_ms",
        "settle_capture_ms", "oss_ms", "lease_wait_ms", "total_ms",
    )
    has_work_timing = any(key in timings for key in visible[:-2])
    if has_work_timing:
        breakdown = ", ".join(
            f"{key.removesuffix('_ms')}={timings[key]}ms"
            for key in visible if key in timings
        )
        result.output = f"{result.output} Timings: {breakdown}."

    log.info(
        f"computer {action} batch={result.metadata.get('batch_size', 1)} timings={timings}"
    )
    return result


def _validate_keys(text: str) -> str | None:
    """Shell-safe, space-separated xdotool keysyms, or None if unrecognisable.

    Keysyms are alphanumeric with `+` for combos and `_` inside names
    (Page_Down, Control_L). Anything else is not a key the model meant, and
    quoting alone should not be the only thing standing between a model
    hallucination and a shell.
    """
    keys = text.split()
    if not keys or not all(k.replace("+", "").replace("_", "").isalnum() for k in keys):
        return None
    return " ".join(shlex.quote(k) for k in keys)


def _point(value: list[int] | None, field: str) -> tuple[int, int] | None:
    if not value or len(value) != 2:
        return None
    try:
        return int(value[0]), int(value[1])
    except (TypeError, ValueError):
        log.debug(f"non-numeric {field}: {value!r}")
        return None


async def _geometry(ctx: ToolContext, key: str) -> dict:
    """Screen geometry for coordinate mapping, capturing once if unknown.

    Deliberately the geometry of the LAST capture, not a fresh reading: the
    model aimed at that screenshot, so its scale is the one that maps those
    coordinates correctly. Every action re-captures afterwards, so the cache
    tracks the screen within one action.
    """
    cached = _geometry_cache.get(key)
    if cached:
        return cached
    geometry = await take_screenshot(ctx.sandbox)
    _geometry_cache[key] = geometry
    return geometry


async def _prepare(ctx: ToolContext, key: str) -> None:
    """Ensure the desktop tooling is present, healing a stale cache.

    `ensure_desktop_tools` remembers what it installed, but a sandbox can be
    recreated under us (desktop reboot, container replaced) — then the helper
    scripts are gone while the cache still says "ready". A probe for the
    helper is one cheap round trip and turns a confusing mid-task failure
    into a silent reinstall.
    """
    await ensure_desktop_tools(ctx.sandbox, key)
    now = time.monotonic()
    if _probe_valid_until.get(key, 0.0) > now:
        return
    probe = await ctx.sandbox.execute(
        'PATH="$HOME/.local/bin:$PATH" command -v obx-shot >/dev/null && command -v xdotool >/dev/null'
        " && echo ok || echo gone",
        timeout=20,
    )
    if probe.stdout.strip().endswith("gone"):
        log.info(f"desktop tooling vanished for {key}; reinstalling")
        invalidate(key)
        _geometry_cache.pop(key, None)
        _probe_valid_until.pop(key, None)
        await ensure_desktop_tools(ctx.sandbox, key)
    _probe_valid_until[key] = time.monotonic() + _PROBE_TTL_SECONDS


def _build_command(action: str, args: _ActionPayload, geometry: dict) -> str | ToolResult:
    """The xdotool invocation for an action, or a ToolResult explaining why not."""
    point = _point(args.coordinate, "coordinate")

    if action in _CLICK_BUTTON:
        if not point:
            return _bad(f"action '{action}' needs coordinate: [x, y]")
        nx, ny = to_native(*point, geometry)
        button = _CLICK_BUTTON[action]
        repeat = {"double_click": " --repeat 2 --delay 80", "triple_click": " --repeat 3 --delay 80"}.get(action, "")
        return f"xdotool mousemove {nx} {ny} click{repeat} --clearmodifiers {button}"

    if action == "mouse_move":
        if not point:
            return _bad("action 'mouse_move' needs coordinate: [x, y]")
        nx, ny = to_native(*point, geometry)
        return f"xdotool mousemove {nx} {ny}"

    if action == "left_click_drag":
        end = _point(args.to_coordinate, "to_coordinate")
        if not point or not end:
            return _bad("action 'left_click_drag' needs both coordinate and to_coordinate")
        sx, sy = to_native(*point, geometry)
        ex, ey = to_native(*end, geometry)
        return (
            f"xdotool mousemove {sx} {sy} sleep 0.1 mousedown 1 "
            f"mousemove {ex} {ey} sleep 0.2 mouseup 1"
        )

    if action == "type":
        if not args.text:
            return _bad("action 'type' needs text")
        # --delay keeps fast typing from outrunning the target app's input
        # handling; -- stops a leading dash being read as a flag.
        return f"xdotool type --clearmodifiers --delay 12 -- {shlex.quote(args.text)}"

    if action == "key":
        if not args.text:
            return _bad("action 'key' needs text, e.g. 'Return' or 'ctrl+s'")
        keys = _validate_keys(args.text)
        if keys is None:
            return _bad(f"unsupported key syntax: {args.text!r}")
        return f"xdotool key --clearmodifiers -- {keys}"

    if action == "scroll":
        if not args.scroll_direction:
            return _bad("action 'scroll' needs scroll_direction")
        button = _SCROLL_BUTTON[args.scroll_direction]
        amount = max(1, min(25, args.scroll_amount))
        move = ""
        if point:
            nx, ny = to_native(*point, geometry)
            move = f"mousemove {nx} {ny} "
        return f"xdotool {move}click --repeat {amount} --delay 60 --clearmodifiers {button}"

    if action in ("left_mouse_down", "left_mouse_up"):
        # Held-button gestures the standard action set exposes for drawing,
        # text selection and drag-and-drop that a single drag cannot express.
        verb = "mousedown" if action == "left_mouse_down" else "mouseup"
        prefix = ""
        if point:
            nx, ny = to_native(*point, geometry)
            prefix = f"mousemove {nx} {ny} "
        return f"xdotool {prefix}{verb} 1"

    if action == "hold_key":
        if not args.text:
            return _bad("action 'hold_key' needs text, e.g. 'shift'")
        key = _validate_keys(args.text)
        if key is None:
            return _bad(f"unsupported key syntax: {args.text!r}")
        seconds = max(0.1, min(10.0, args.duration))
        return f"xdotool keydown -- {key} sleep {seconds:g} keyup -- {key}"

    if action == "cursor_position":
        return "xdotool getmouselocation --shell"

    if action == "wait":
        seconds = max(0.0, min(10.0, args.duration))
        return f"sleep {seconds:g}"

    return _bad(f"unknown action: {action}")


def _build_batch(args: ComputerArgs, geometry: dict) -> tuple[str, list[ComputerAction]] | ToolResult:
    """Build one shell program for an ordered batch of desktop actions."""
    actions = args.actions or []
    if not actions:
        return _bad("action 'batch' needs a non-empty actions list")
    total_wait = sum(
        max(0.0, min(10.0, item.duration))
        for item in actions
        if item.action in {"wait", "hold_key"}
    )
    if total_wait > 30:
        return _bad("a desktop batch may wait or hold keys for at most 30 seconds total")

    commands: list[str] = []
    for index, item in enumerate(actions, start=1):
        command = _build_command(item.action, item, geometry)
        if isinstance(command, ToolResult):
            return ToolResult(
                title=f"computer: batch action {index} invalid",
                output=command.output,
            )
        commands.append(command)

    # obx-x discovers DISPLAY/XAUTHORITY once, while sh performs every action
    # locally. shlex.quote preserves the validation/quoting done above.
    cleanup = ""
    if any(item.action == "left_mouse_down" for item in actions):
        cleanup = "trap 'xdotool mouseup 1 >/dev/null 2>&1 || true' EXIT; "
    program = f"set -e; {cleanup}" + "; ".join(commands)
    return x(f"sh -c {shlex.quote(program)}"), actions


async def _attach_screenshot(ctx: ToolContext, geometry: dict) -> str:
    """Push the current /tmp screenshot to OSS and pin it on the message."""
    from sandbox.assets import attach_sandbox_image

    width, height = geometry["scaled"]
    asset_id, size = await attach_sandbox_image(
        ctx,
        SHOT_PATH,
        "image/png",
        int(geometry.get("bytes", 0)),
        name=f"screen-{ctx.part_id or 'shot'}.png",
        transient=True,
        relation_kind="computer_screenshot",
        relation_role="evidence",
        relation_label="Computer checkpoint",
    )
    log.debug(f"screenshot asset={asset_id} {width}x{height} {size}B")
    return f"{width}x{height}"


async def _open_browser(ctx: ToolContext, key: str) -> ToolResult:
    """Start (or re-attach to) the desktop's managed browser.

    This exists because the obvious alternative does not work. A closed browser
    looks to a computer-use agent like a missing window, so it goes hunting for
    an icon — and a Chrome started by clicking its icon comes up with **no
    remote-debugging port**, which is precisely the browser `dev-browser`
    cannot drive. Succeeding at the icon hunt produces a broken state that
    then fails later and further away.

    `ensure_browser` is the only launcher that gets the profile, the policy,
    the CDP port and the extension right, and it re-probes the live port rather
    than trusting a cache, so a browser the user closed by hand comes back.
    """
    if not ctx.sandbox:
        return ToolResult(title="no sandbox", output="There is no sandbox to open a browser on.")

    from sandbox.browser import ensure_browser
    from session.browser_pref import get_browser_mode, relay_mode

    preference = await get_browser_mode(ctx.user_id)
    try:
        state = await ensure_browser(ctx.sandbox, ctx.session_id, relay_mode(preference))
    except Exception as e:
        return ToolResult(
            title="could not open the browser",
            output=(
                f"The managed browser failed to start: {str(e)[:300]}\n"
                "Do NOT fall back to clicking a browser icon on the desktop — that "
                "starts a Chrome with no debug port, which dev-browser cannot drive. "
                "Report this instead."
            ),
            metadata={"error": True},
        )

    effective = state.get("mode", "unknown")
    lines = [f"Browser ready (running as: {effective}, preference: {preference})."]
    if effective == "local":
        lines.append(
            "This is the cloud desktop's Chrome, so it is on the screen you can "
            "screenshot — but drive pages with the dev-browser skill, not by clicking "
            "pixels. Use `computer` only for what the page cannot reach."
        )
    elif effective == "extension":
        lines.append(
            "This is the user's OWN browser, on their machine. It is NOT on this "
            "desktop, so screenshots here will not show it."
        )

    # Show the model the browser it just opened — but only for the desktop's
    # own Chrome, since the user's browser is not on this screen. Screenshots
    # need the desktop toolchain and OSS, neither of which `open_browser`
    # otherwise requires: opening a browser is still worth doing on a sandbox
    # that cannot produce images, so a failure here degrades to a note.
    note = ""
    if effective == "local":
        try:
            await _prepare(ctx, key)
            geometry = await take_screenshot(ctx.sandbox)
            _geometry_cache[key] = geometry
            dims = await _attach_screenshot(ctx, geometry)
            note = f" Screenshot attached ({dims}); you will see it next turn."
        except Exception as e:
            log.warning(f"post-open screenshot failed: {e}")
            note = " (could not capture the screen just now; take a `screenshot` explicitly)"

    return ToolResult(
        title=f"browser ready ({effective})",
        output=" ".join(lines) + note,
        metadata={"mode": effective, "preference": preference},
    )


async def _execute_locked(args: ComputerArgs, ctx: ToolContext) -> ToolResult:
    from core.oss import OssNotConfigured, get_oss

    action = args.action
    key = _sandbox_key(ctx)

    if action == "wait":
        await asyncio.sleep(max(0.0, min(10.0, args.duration)))
        return ToolResult(title=f"waited {args.duration:g}s", output="Waited.")

    if action == "open_browser":
        return await _open_browser(ctx, key)

    # A screenshot must reach the model as an image, which needs OSS. Fail
    # loudly here rather than silently acting blind.
    try:
        get_oss()
    except OssNotConfigured as e:
        return ToolResult(
            title="computer unavailable",
            output=f"Screenshots need OSS transfer, which is not configured: {e}",
        )

    timings: dict[str, int] = {}
    try:
        prepare_started = time.monotonic()
        await _prepare(ctx, key)
        timings["prepare_ms"] = round((time.monotonic() - prepare_started) * 1000)
    except NoDesktopError as e:
        return ToolResult(title="no graphical desktop", output=str(e))
    except Exception as e:
        return ToolResult(title="computer unavailable", output=str(e)[:400])

    try:
        if action == "screenshot":
            capture_started = time.monotonic()
            geometry = await take_screenshot(ctx.sandbox)
            timings["capture_ms"] = round((time.monotonic() - capture_started) * 1000)
            _geometry_cache[key] = geometry
            oss_started = time.monotonic()
            dims = await _attach_screenshot(ctx, geometry)
            timings["oss_ms"] = round((time.monotonic() - oss_started) * 1000)
            native = "x".join(str(v) for v in geometry["native"])
            return ToolResult(
                title=f"screenshot {dims}",
                output=(
                    f"Screenshot attached ({dims}, downscaled from {native}). "
                    "It becomes visible to you on your next turn. Use these "
                    f"{dims} coordinates when clicking."
                ),
                metadata={"geometry": geometry, "timings": timings},
            )

        geometry_started = time.monotonic()
        geometry = await _geometry(ctx, key)
        timings["geometry_ms"] = round((time.monotonic() - geometry_started) * 1000)
        batch_actions: list[ComputerAction] = []
        if action == "batch":
            batch = _build_batch(args, geometry)
            if isinstance(batch, ToolResult):
                return batch
            command, batch_actions = batch
        else:
            command = _build_command(action, args, geometry)
        if isinstance(command, ToolResult):
            return command

        execute_started = time.monotonic()
        wrapped = command if action == "batch" else x(command)
        result = await ctx.sandbox.execute(wrapped, timeout=120 if action == "batch" else 60)
        timings["execute_ms"] = round((time.monotonic() - execute_started) * 1000)
        if result.exit_code != 0:
            return ToolResult(
                title=f"computer: {action} failed",
                output=(result.stderr or result.stdout).strip()[:300] or "xdotool failed",
            )

        if action == "cursor_position":
            values = dict(
                line.split("=", 1) for line in result.stdout.split() if "=" in line
            )
            try:
                nx, ny = int(values.get("X", 0)), int(values.get("Y", 0))
                native_w, native_h = geometry["native"]
                scaled_w, scaled_h = geometry["scaled"]
                mx = round(nx * scaled_w / max(1, native_w))
                my = round(ny * scaled_h / max(1, native_h))
                return ToolResult(title="cursor position", output=f"Cursor is at ({mx}, {my}).")
            except (TypeError, ValueError):
                return ToolResult(title="cursor position", output=result.stdout.strip()[:200])

        # Show the model what the action or action batch did. Stability is
        # measured inside the desktop, then the final frame follows the same
        # OSS attachment path as before.
        note = ""
        capture = action == "batch" or action in _VISUAL_ACTIONS
        if capture:
            try:
                capture_started = time.monotonic()
                geometry = await take_stable_screenshot(ctx.sandbox)
                timings["settle_capture_ms"] = round(
                    (time.monotonic() - capture_started) * 1000
                )
                _geometry_cache[key] = geometry
                oss_started = time.monotonic()
                dims = await _attach_screenshot(ctx, geometry)
                timings["oss_ms"] = round((time.monotonic() - oss_started) * 1000)
                settle_ms = int(geometry.get("settle_ms", 0))
                stable = bool(geometry.get("stable", False))
                state = "stable" if stable else "settle timeout"
                note = (
                    f" Screenshot attached via OSS ({dims}, {state} after {settle_ms}ms); "
                    "you will see it next turn."
                )
            except Exception as e:
                log.warning(f"post-action screenshot failed: {e}")
                note = " (screenshot after the action failed; take one explicitly)"

        if action == "batch":
            described = f"ran {len(batch_actions)} desktop actions"
            summary = ", ".join(_describe(item.action, item) for item in batch_actions[:4])
            if len(batch_actions) > 4:
                summary += f", +{len(batch_actions) - 4} more"
            output = f"Done in one local batch: {summary}.{note}"
        else:
            described = _describe(action, args)
            output = f"Done: {described}.{note}"
        return ToolResult(
            title=described,
            output=output,
            metadata={
                "geometry": geometry,
                "batch_size": len(batch_actions) or 1,
                "timings": timings,
            },
        )

    except Exception as e:
        log.warning(f"computer {action} failed: {e}")
        return ToolResult(title=f"computer: {action} failed", output=str(e)[:400])


async def execute(args: ComputerArgs, ctx: ToolContext) -> ToolResult:
    """Run one complete computer transaction under the remote desktop lease."""
    started = time.monotonic()
    if args.action == "wait":
        result = await _execute_locked(args, ctx)
        return _finalize_result(result, args.action, started)

    lease_factory = getattr(ctx.sandbox, "desktop_lease", None)
    if lease_factory is None:
        result = await _execute_locked(args, ctx)
        return _finalize_result(result, args.action, started)

    try:
        async with lease_factory(
            session_id=ctx.session_id,
            tool_call_id=ctx.part_id,
            operation="computer",
        ) as lease:
            result = await _execute_locked(args, ctx)
        return _finalize_result(result, args.action, started, lease)
    except Exception as e:
        log.warning(f"computer desktop lease failed: {e}")
        result = ToolResult(
            title="computer unavailable",
            output=f"Could not acquire the shared desktop lease: {str(e)[:300]}",
            metadata={"error": True},
        )
        return _finalize_result(result, args.action, started)


def _describe(action: str, args: _ActionPayload) -> str:
    if action == "type":
        preview = (args.text or "")[:40]
        return f"typed {preview!r}"
    if action == "key":
        return f"pressed {args.text}"
    if action == "scroll":
        return f"scrolled {args.scroll_direction} x{args.scroll_amount}"
    if args.coordinate:
        return f"{action} at {tuple(args.coordinate)}"
    return action


COMPUTER_DESCRIPTION = """\
Control the sandbox's graphical desktop: take a screenshot, move and click the \
mouse, type text, press keys, scroll. Use this for anything that only exists \
in a GUI — a browser page, a desktop app, a dialog — when no CLI can do the job.

Workflow: take a `screenshot` first, read it on your next turn, then act on \
what you see. Actions return a fresh screenshot automatically, so you can \
chain steps without asking for one each time.

Use this `computer` tool with `action: "batch"` when several actions do not \
need an intermediate visual decision, \
for example click a known field → type text → press Return. Put the ordered \
atomic actions in `actions`; they execute locally and only one final screenshot \
is uploaded through OSS. Never put `computer` calls inside the generic parallel \
`batch` tool; one desktop is stateful and cannot be driven concurrently. Do not \
batch a later coordinate click when its target \
only appears after an earlier action — inspect the intermediate screenshot first.

Coordinates: the screenshot you are shown is downscaled from the real screen. \
Always give coordinates in the screenshot's own pixel space (its size is \
reported with every capture) — they are mapped back to the real display for \
you. Never guess coordinates you have not seen in a screenshot.

Typing goes to whatever currently has keyboard focus, which is often NOT the \
window you just opened. Click the window (or the field) first, then type, and \
check the next screenshot to confirm the text landed where you meant. If a \
launcher or overview overlay is covering the screen, press Escape — twice if \
a search box still holds text, since the first Escape only clears it.

Keys use xdotool names: Return, Escape, Tab, BackSpace, Delete, Up/Down/Left/\
Right, super, and combos like ctrl+c, ctrl+shift+t, alt+Tab.

**Need a browser and there is none on screen? Use `open_browser`.** Never hunt \
for a browser icon in a dock, launcher or menu. A Chrome started by clicking \
its icon comes up with no remote-debugging port, so the dev-browser skill \
cannot drive it — the icon hunt fails, and succeeding at it would be worse. \
`open_browser` starts the managed browser (correct profile, policy, debug \
port, extension) and re-attaches to one that is already running, including one \
the user closed by hand. Once it reports ready, drive pages with the \
dev-browser skill; keep `computer` for what the page itself cannot reach."""

computer_tool = define_tool(
    "computer",
    description=COMPUTER_DESCRIPTION,
    parameters=ComputerArgs,
    execute=execute,
    parallel_safe=False,
)
