"""The generation tool as a standalone primitive.

These cover what atomization actually changed: a caller can describe a shot
and pay for it without a production, capability limits are enforced from the
declared registry, and two guards stand in for the credits ledger that will
eventually price this.
"""
import inspect

import pytest
from pydantic import ValidationError

from core.config import VideoGenerationConfig, VideoModelConfig
from tool import video_providers
from tool.video_production import VideoGenerateArgs, VideoInputRef, _model_capability_lines


WAN3 = VideoModelConfig(
    id="wan3.0-video",
    channel="sd2",
    ratios=["adaptive", "16:9", "9:16"],
    duration_range=(2, 30),
    supports_seed=True,
    supports_first_last_frame=True,
    supports_reference_audio=True,
    resolutions=["1080p"],
)
SEEDANCE = VideoModelConfig(
    id="video-sd-720p-proⅠ",
    channel="sd2",
    resolutions=["720p"],
    duration_range=(4, 15),
    supports_reference_video=False,
)


def _route(model: str, channel: str = "sd2"):
    from types import SimpleNamespace

    return SimpleNamespace(channel=channel, model=model, model_type="sd2_video")


def _validate(entry, route, **kw):
    params = dict(
        resolution="", ratio="", duration=-1, generate_audio=True, input_mimes=[]
    )
    params.update(kw)
    video_providers.validate_request(route, declared=entry, **params)


def test_ratio_outside_the_declared_set_is_refused():
    """wan3 rejects 21:9 upstream rather than substituting, so catch it free."""
    with pytest.raises(RuntimeError, match="supports ratios"):
        _validate(WAN3, _route("wan3.0-video"), resolution="1080p", ratio="21:9")


def test_declared_duration_range_replaces_the_old_seedance_clamp():
    """A 24s wan3 request is legal; the old shared 4-15 clamp dropped it."""
    _validate(WAN3, _route("wan3.0-video"), resolution="1080p", ratio="9:16", duration=24)

    with pytest.raises(RuntimeError, match="accepts 4-15s"):
        _validate(SEEDANCE, _route("video-sd-720p-proⅠ"), resolution="720p", duration=24)


def test_frame_roles_are_refused_on_models_that_lack_them():
    with pytest.raises(RuntimeError, match="first_frame/last_frame"):
        _validate(
            SEEDANCE,
            _route("video-sd-720p-proⅠ"),
            resolution="720p",
            roles=("last_frame",),
        )


def test_only_a_name_encoded_tier_refuses_frame_roles():
    """The flat body has no field for a role; the metadata shape does.

    wan3 reaches the gateway's task adaptor, whose content[] carries an
    explicit role, so frame continuity is expressible there. The name-encoded
    sd2 tiers still take a flat image list and must refuse.
    """
    _validate(WAN3, _route("wan3.0-video"), resolution="1080p",
              ratio="9:16", roles=("last_frame",))

    # A flat tier that nonetheless claims the capability is caught by the
    # channel rule rather than the declaration, which is the case that matters:
    # the body simply has nowhere to put the role.
    flat = VideoModelConfig(id="video-sd-1080p-pro", channel="sd2",
                            resolutions=["1080p"], supports_first_last_frame=True)
    with pytest.raises(RuntimeError, match="cannot express"):
        _validate(flat, _route("video-sd-1080p-pro"), resolution="1080p",
                  roles=("last_frame",))


def test_a_task_adaptor_model_carries_its_parameters_in_metadata():
    """Top-level resolution/ratio are dropped by the gateway's video DTO.

    Measured 2026-09-01: 720p/9:16 sent at the top level came back
    1920x1080 — the upstream default — while the same values under
    `metadata` came back 720x1280 exactly.
    """
    _path, body = video_providers.build_payload(
        _route("wan3.0-video"),
        prompt="一只猫跳上窗台",
        refs=[],
        resolution="720p",
        ratio="9:16",
        duration=24,
        generate_audio=True,
        watermark=False,
        seed=42,
    )

    assert "resolution" not in body, "the DTO has no top-level field for it"
    assert body["metadata"]["resolution"] == "720p"
    assert body["metadata"]["ratio"] == "9:16"
    assert body["metadata"]["duration"] == 24
    assert body["metadata"]["seed"] == 42


def test_a_name_encoded_tier_keeps_the_flat_body():
    _path, body = video_providers.build_payload(
        _route("video-sd-1080p-pro"), prompt="一只猫", refs=[],
        resolution="1080p", ratio="9:16", duration=6,
        generate_audio=True, watermark=False, seed=None,
    )

    assert body["resolution"] == "1080p"
    assert "metadata" not in body


def test_smart_duration_sends_no_duration_field():
    _path, body = video_providers.build_payload(
        _route("wan3.0-video"),
        prompt="x",
        refs=[],
        resolution="1080p",
        ratio="9:16",
        duration=-1,
        generate_audio=True,
        watermark=False,
    )

    assert "duration" not in body


def test_audio_input_must_name_its_role():
    """Guessing that an audio file is a reference track would change the take."""
    assert VideoInputRef(asset_id="a", role="reference_audio").role == "reference_audio"
    assert VideoInputRef(asset_id="a").role is None


def test_capability_lines_describe_each_model():
    config = type(
        "C", (), {"video_generation": VideoGenerationConfig(model="wan3.0-video", models=[WAN3, SEEDANCE])}
    )()
    text = "\n".join(_model_capability_lines(config))

    assert "default_model=wan3.0-video" in text
    assert "duration=2-30s" in text
    assert "seed" in text
    assert "ratios=adaptive/16:9/9:16" in text


def test_estimate_needs_no_idempotency_key():
    """Validating a request costs nothing, so it must not demand a paid key."""
    args = VideoGenerateArgs(action="estimate", prompt="一只猫")

    assert args.idempotency_key is None


def test_duplicate_override_is_explicit():
    assert VideoGenerateArgs(action="models").allow_duplicate is False
    with pytest.raises(ValidationError):
        VideoGenerateArgs(action="submit", prompt="x")


def test_a_zero_valued_optional_is_read_as_absent():
    """Some callers populate every schema field, including ones they never set.

    Such a caller sends seed=0 and duration=0 for parameters it has no opinion
    about. Reading those as real requests made every seedless model refuse work
    nobody had asked for, and left the caller no way to express "no seed" —
    it cannot omit a field its own serializer always writes.
    """
    args = VideoGenerateArgs(
        action="submit", prompt="一只猫", idempotency_key="k:1", seed=0, duration=0
    )

    assert (args.seed or None) is None
    assert (args.duration or None) is None


def test_a_real_seed_still_travels():
    args = VideoGenerateArgs(
        action="submit", prompt="一只猫", idempotency_key="k:1", seed=42
    )

    assert (args.seed or None) == 42


def test_an_unusable_seed_is_dropped_rather_than_refusing_the_shot():
    """A seed the model cannot use is worth less than the generation itself.

    Missing it costs reproducibility; the video is still the one that was
    asked for. Refusing costs the whole request — and a caller whose
    serializer always writes every field cannot express "no seed" at all,
    so the refusal made every seedless model unreachable from it.
    """
    _validate(SEEDANCE, _route("video-sd-720p-proⅠ"), resolution="720p")

    import inspect
    source = inspect.getsource(video_providers._validate_declared)
    assert "does not accept a seed" not in source


def test_content_changing_roles_are_still_refused():
    """first/last frame and reference audio change what the video IS."""
    with pytest.raises(RuntimeError, match="first_frame/last_frame"):
        _validate(SEEDANCE, _route("video-sd-720p-proⅠ"), resolution="720p",
                  roles=("first_frame",))
    with pytest.raises(RuntimeError, match="audio reference"):
        _validate(SEEDANCE, _route("video-sd-720p-proⅠ"), resolution="720p",
                  roles=("reference_audio",))


def _sd2_body(model, refs, prompt="她自然看向镜头说话。"):
    _path, body = video_providers.build_payload(
        _route(model), prompt=prompt, refs=refs, resolution="1080p",
        ratio="9:16", duration=5, generate_audio=True, watermark=False,
    )
    return body


IMG = [{"kind": "image", "url": "https://oss.test/a.png", "role": "reference_image"}]


def test_wan3_sends_references_through_the_multi_material_path():
    """Measured 2026-09-01: wan3 behind this relay ignores image_url.

    image_url, first_frame_url and a doubao content[] each came back as a
    different person; `images` plus an @image_file_N mention in the prompt is
    the one shape that actually holds the face. Called adapter-to-adapter,
    wan3 locks identity perfectly — so this was never the model's limit, only
    how the request reached it.
    """
    body = _sd2_body("wan3.0-video-prime", IMG)

    assert body["images"] == ["https://oss.test/a.png"]
    assert "image_url" not in body
    assert "@image_file_1" in body["prompt"]


def test_seedance_keeps_the_image_url_path_that_already_works():
    body = _sd2_body("video-sd-1080p-pro", IMG)

    assert body["image_url"] == "https://oss.test/a.png"
    assert "images" not in body
    assert "@image_file" not in body["prompt"]


def test_a_prompt_that_already_names_its_material_is_left_alone():
    body = _sd2_body(
        "wan3.0-video-prime", IMG, prompt="@image_file_1 是主播，她开口说话。"
    )

    assert body["prompt"] == "@image_file_1 是主播，她开口说话。"


def test_every_supplied_image_gets_named():
    """An image the prompt never mentions is simply not used by the relay."""
    two = IMG + [{"kind": "image", "url": "https://oss.test/b.png", "role": "reference_image"}]
    body = _sd2_body("wan3.0-video-prime", two, prompt="@image_file_1 是主播。")

    assert "@image_file_2" in body["prompt"]


def test_the_shot_number_survives_into_the_attachment_ordinal():
    """Concurrent shots finish out of order; attach order is completion order.

    Without an explicit ordinal the renderer falls back to array position, so
    whichever shot finished second is labelled "第 2 段" — observed on a run
    where the closing shot was displayed as the second one.
    """
    args = VideoGenerateArgs(
        action="submit", prompt="一只猫", idempotency_key="k:1", shot=4
    )

    assert args.shot == 4


def test_shot_is_optional_for_a_one_off_generation():
    args = VideoGenerateArgs(action="submit", prompt="一只猫", idempotency_key="k:1")

    assert args.shot is None


def test_every_declared_model_and_resolution_is_reachable():
    """The picker offers what the registry declares, so all of it must work.

    Before this, three of nine models were unusable at the resolution they
    advertised: the sd2 native-resolution rule (a Seedance naming convention)
    was applied to wan3 and MiniMax, which pick resolution as a parameter, and
    a hardcoded "1080p only on doubao-seedance-2-0" predated the registry.
    """
    from dotenv import load_dotenv

    import core.config

    load_dotenv(".env")
    core.config._config = None
    config = core.config.get_config()

    failures = []
    for model in config.video_generation.models:
        # Only Wan 3.0 accepts -1 ("you pick"); the rest need a real number, so
        # ask each for something inside its own measured range.
        duration = -1 if model.supports_smart_duration else (model.duration_range or (5, 5))[0]
        for resolution in model.resolutions or ["720p"]:
            try:
                video_providers.validate_request(
                    video_providers.resolve_route(model.id, config),
                    resolution=resolution, ratio="9:16", duration=duration,
                    generate_audio=model.supports_generated_audio,
                    input_mimes=[], declared=model,
                )
            except Exception as exc:
                failures.append(f"{model.id} @ {resolution}: {exc}")

    assert not failures, "\n".join(failures)


def test_a_name_encoded_tier_still_pins_its_resolution():
    """video-sd-720p-proⅠ returns 720p whatever you ask for — keep refusing."""
    entry = VideoModelConfig(id="video-sd-720p-proⅠ", channel="sd2",
                             resolutions=["720p", "1080p"])

    with pytest.raises(RuntimeError, match="natively"):
        _validate(entry, _route("video-sd-720p-proⅠ"), resolution="1080p")


def test_a_silent_tier_refuses_audio_but_stays_selectable():
    entry = VideoModelConfig(id="fast-tier", channel="sd2",
                             supports_generated_audio=False)

    _validate(entry, _route("fast-tier"), generate_audio=False)
    with pytest.raises(RuntimeError, match="silent video"):
        _validate(entry, _route("fast-tier"), generate_audio=True)


def test_each_wire_shape_puts_the_resolution_where_its_adaptor_reads_it():
    """Three gateway adaptors, three mutually unreadable body shapes.

    Measured 2026-09-01 by generating one video per model and probing the
    pixels that came back:

      metadata — wan3 and Seedance behind the DoubaoVideo adaptor. Top-level
                 resolution/ratio are dropped by the video DTO, which has no
                 field for them: 720p/9:16 flat returned 1920x1080, the same
                 values under `metadata` returned 720x1280.
      flat     — the name-encoded sd2 tiers behind the Sora adaptor, whose
                 body is relayed verbatim.
      size     — MiniMax, which parses its own tiers out of a WxH string and
                 rejects the request without one ("ratio 不能为空").
    """
    shapes = {
        "metadata": ("wan3.0-video", "metadata"),
        "flat": ("video-sd-1080p-pro", "resolution"),
        "size": ("MiniMax-H3", "size"),
    }
    for shape, (model_id, expected_key) in shapes.items():
        entry = VideoModelConfig(id=model_id, channel="sd2", wire_shape=shape,
                                 resolutions=["1080p"])
        _path, body = video_providers.build_payload(
            _route(model_id), prompt="一只猫", refs=[], resolution="1080p",
            ratio="9:16", duration=5, generate_audio=True, watermark=False,
            seed=None, declared=entry,
        )
        assert expected_key in body, f"{shape}: {sorted(body)}"


def test_the_size_shape_is_portrait_for_a_vertical_ratio():
    entry = VideoModelConfig(id="MiniMax-H3", channel="sd2", wire_shape="size")

    _path, portrait = video_providers.build_payload(
        _route("MiniMax-H3"), prompt="x", refs=[], resolution="720p",
        ratio="9:16", duration=5, generate_audio=True, watermark=False,
        seed=None, declared=entry,
    )
    _path, landscape = video_providers.build_payload(
        _route("MiniMax-H3"), prompt="x", refs=[], resolution="720p",
        ratio="16:9", duration=5, generate_audio=True, watermark=False,
        seed=None, declared=entry,
    )

    assert portrait["size"] == "720x1280"
    assert landscape["size"] == "1280x720"


def test_the_budget_line_says_what_is_left_not_what_is_spent():
    """"used=50/50" was reported to a person as "50 remaining"."""
    import re

    from tool import video_production

    source = inspect.getsource(video_production._execute_estimate)
    assert "daily_submits_remaining" in source
    # The old "used=N/limit" form is what invited the misreading.
    assert not re.search(r"daily_submits_used=\{used\}\"?\s*\+", source)


def test_every_model_declares_a_duration_range_it_was_measured_at():
    """Vendor docs and this deployment disagree, so both were checked.

    通义万相 2.7's public docs say 2-15s, but the endpoint behind this relay
    honoured 30s exactly (requested 30 → 30.024s), and its adaptor refuses 31.
    Seedance's docs say -1 picks a length for you, and both its paths here
    accept it — an earlier guess had that flag off. MiniMax H3's 4-15s matches
    its docs and the gateway constants.
    """
    from dotenv import load_dotenv

    import core.config

    load_dotenv(".env")
    core.config._config = None
    models = core.config.get_config().video_generation.models

    for model in models:
        low, high = model.duration_range or (0, 0)
        assert low >= 2 and high >= low, model.id
        assert high <= 30, f"{model.id} claims more than any vendor here allows"


def test_minimax_keeps_its_own_resolution_vocabulary():
    """Its adaptor parses tiers back out of the WxH string it is sent.

    Declaring 720p/1080p put another vendor's names on it; asking for 720p
    returned 768x1344, the tier it actually rounded to.
    """
    from dotenv import load_dotenv

    import core.config

    load_dotenv(".env")
    core.config._config = None
    config = core.config.get_config()
    entry = next(m for m in config.video_generation.models if m.id == "MiniMax-H3")

    assert entry.resolutions == ["480p", "512p", "768p", "2k"]
    for tier in entry.resolutions:
        _path, body = video_providers.build_payload(
            video_providers.resolve_route("MiniMax-H3", config), prompt="猫",
            refs=[], resolution=tier, ratio="9:16", duration=6,
            generate_audio=True, watermark=False, seed=None, declared=entry,
        )
        # An unmapped tier would collapse to the default and bill for a
        # picture nobody chose.
        assert body["size"] != "720x1280" or tier == "720p", (tier, body["size"])


def test_duration_is_checked_for_every_declared_model_at_both_bounds():
    """The check has to run before the channel branches return early.

    validate_request's sd2 branch returns as soon as its own rules pass, so a
    duration check placed after it would silently cover none of the six models
    on that channel.
    """
    from dotenv import load_dotenv

    import core.config

    load_dotenv(".env")
    core.config._config = None
    config = core.config.get_config()

    for model in config.video_generation.models:
        low, high = model.duration_range
        route = video_providers.resolve_route(model.id, config)
        resolution = (model.resolutions or ["720p"])[0]
        ratio = (model.ratios or ["9:16"])[0]

        def check(duration):
            video_providers.validate_request(
                route, resolution=resolution, ratio=ratio, duration=duration,
                generate_audio=model.supports_generated_audio,
                input_mimes=[], declared=model,
            )

        with pytest.raises(RuntimeError):
            check(low - 1)
        with pytest.raises(RuntimeError):
            check(high + 1)
        check(low)
        check(high)

        if model.supports_smart_duration:
            check(-1)
        else:
            with pytest.raises(RuntimeError):
                check(-1)


def test_an_undeclared_model_still_gets_a_channel_wide_duration_guard():
    """Otherwise duration=3600 goes straight to the provider and burns a submit."""
    from dotenv import load_dotenv

    import core.config

    load_dotenv(".env")
    core.config._config = None
    route = video_providers.resolve_route("wan3.0-video", core.config.get_config())

    def check(duration):
        video_providers.validate_request(
            route, resolution="720p", ratio="9:16", duration=duration,
            generate_audio=True, input_mimes=[], declared=None,
        )

    check(5)
    check(-1)
    for absurd in (1, 99, 3600):
        with pytest.raises(RuntimeError, match="not in video_generation.models"):
            check(absurd)


def test_a_capability_refusal_points_at_the_table_not_at_another_guess():
    """"20s rejected" alone reads as an invitation to try 18, then 16.

    Each of those attempts is a paid submit spent discovering something that
    `action="models"` publishes for free, so every capability refusal carries
    the pointer — the skill may not be loaded, and this is then the only
    guidance the caller gets.
    """
    entry = VideoModelConfig(id="video-sd-1080p-pro", channel="sd2",
                             resolutions=["1080p"], duration_range=(4, 15),
                             supports_smart_duration=False)
    route = _route("video-sd-1080p-pro")

    for kwargs in ({"duration": 20}, {"duration": -1}, {"resolution": "480p"}):
        args = {"resolution": "1080p", "ratio": "9:16", "duration": 6, **kwargs}
        with pytest.raises(RuntimeError) as caught:
            video_providers.validate_request(
                route, generate_audio=True, input_mimes=[], declared=entry, **args
            )
        assert 'action="models"' in str(caught.value)
        assert 'action="estimate"' in str(caught.value)
