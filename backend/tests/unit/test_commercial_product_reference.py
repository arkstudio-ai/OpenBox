from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image, ImageDraw
import pytest

from light import commercial_product_reference as ref
from light.remake import RemakeStopped


async def test_resume_keeps_only_byte_identical_camera_cards_owned_by_the_same_production(monkeypatch):
    from session.session import create_session
    from tests.unit.test_assistant_assets import asset_for
    from tests.unit.test_assistant_sessions_v2 import assistant
    from tool.tool import ToolContext

    user, _, workspace, active = await assistant()
    other = await create_session(user_id=user, workspace_id=workspace, project_id=active.project_id)
    ctx = ToolContext(user_id=user, workspace_id=workspace, session_id=active.id)
    images = {}
    async def owned(session, pixels):
        row = await asset_for(user, workspace, session_id=session.id, mime="image/png", size=len(pixels))
        images[row.oss_key] = pixels
        return row.id
    current = await owned(active, b"current")
    different = await owned(active, b"another")
    same = await owned(active, b"current")
    other_session = await owned(other, b"current")
    store = AsyncMock()
    store.get_object.side_effect = lambda key: images[key]
    monkeypatch.setattr(ref, "get_oss", lambda: store)
    assert await ref._identical_prior_card(ctx, current, [other_session, different, same]) == same
    assert await ref._identical_prior_card(ctx, current, [other_session, different]) == current
    store.get_object.side_effect = FileNotFoundError()
    assert await ref._identical_prior_card(ctx, current, [same]) == current


def source():
    im = Image.new("RGB", (101, 101), "#43938a")
    p = ImageDraw.Draw(im)
    p.rectangle((0, 75, 100, 100), fill="#886647")
    p.ellipse((4, 8, 28, 36), fill="#eda98a")  # another person in the source scene
    p.rectangle((21, 45, 73, 88), fill="#e65920")  # selected bag
    p.rectangle((29, 19, 65, 48), fill="#272421")
    p.rectangle((34, 25, 60, 42), fill="#43938a")  # the view through the handle
    p.rectangle((43, 15, 50, 31), fill="#eda98a")  # a hand overlaps the middle of the rim
    p.rectangle((38, 60, 54, 65), fill="white")
    return im


def polygon(x, y, r, b):
    return [[x, y], [r, y], [r, b], [x, b]]


def tracing():
    return ref.Outline(
        surfaces=[polygon(207, 448, 735, 883), polygon(289, 188, 649, 485)],
        remove=[polygon(340, 250, 600, 420), polygon(430, 140, 500, 315)],
        keep_points=[[300, 350], [625, 360], [550, 200], [470, 620]],
        remove_points=[[460, 225], [450, 300], [400, 340]],
    )


def review(ok):
    return dict(
        same_product=True,
        visible_features_kept=True,
        visible_holes_kept=True,
        free_of_people_and_scene=ok,
        no_invented_surfaces=True,
        reason="visible mask",
        correction="" if ok else "Exclude the hand inside the open rim.",
    )


@pytest.mark.parametrize("dense", [False, True])
def test_real_pixels_of_the_label_rim_and_hole_survive_without_person_or_room(dense):
    im = source()
    outline = tracing()
    if dense:
        def densify(corners, count):
            points = []
            for i in range(count):
                edge, step = divmod(i * len(corners), count)
                start, end = corners[edge], corners[(edge + 1) % len(corners)]
                points.append([round(a + (b - a) * step / count) for a, b in zip(start, end)])
            return points

        payload = outline.model_dump()
        payload["surfaces"][0] = densify(payload["surfaces"][0], 100)
        payload["remove"][1] = densify(payload["remove"][1], 134)
        outline = ref.Outline.model_validate(payload)
    mask = ref._mask(im, outline)
    assert all(mask.getpixel(p) == 255 for p in [(46, 62), (52, 79), (30, 36), (62, 36), (58, 20)])
    assert all(mask.getpixel(p) == 0 for p in [(45, 33), (46, 23), (41, 35), (16, 21), (4, 80), (79, 86)])
    composite = Image.composite(im, Image.new("RGB", im.size, ref.NEUTRAL), mask)
    assert composite.getpixel((46, 62)) == (255, 255, 255)
    assert composite.getpixel((45, 33)) == composite.getpixel((46, 23)) == ref.NEUTRAL
    assert composite.getpixel((30, 36)) == im.getpixel((30, 36))
    card = ref._card(im, mask)
    assert card.size == (736, 736) and card.getpixel((0, 0)) == ref.NEUTRAL
    assert ref._studio_border(im) is False
    clean = Image.new("RGB", (200, 250), "#f6f6f4")
    ImageDraw.Draw(clean).ellipse((60, 40, 135, 210), fill="#bc2944")
    assert ref._studio_border(clean)


@pytest.mark.asyncio
async def test_rejected_card_is_corrected_then_cached_and_does_not_mutate_source(monkeypatch):
    original = source()
    before = original.tobytes()
    reads = AsyncMock(return_value=original)
    monkeypatch.setattr(ref, "_read", reads)
    saved = {}
    approved = {}
    trace_requests = []
    reviews = []

    async def cached(_ctx, key):
        return approved.get(key)

    async def asset(_ctx, key, data, *, mime):
        saved[key] = data
        if not any(token in key for token in ("-crop", "-grid", "-candidate")):
            approved[key] = key
        return key

    async def ask(_ctx, name, _schema, _guide, info, images, **opts):
        if name == "commercial_locate_product":
            report = dict(usable=True, studio=False, box=[0, 0, 1000, 1000], evidence="bag with a handle")
        elif name == "commercial_trace_product":
            trace_requests.append((info, images))
            report = tracing().model_dump()
        else:
            reviews.append(images)
            report = review(len(reviews) > 1)
        return {"report": report}

    monkeypatch.setattr(ref, "_cached", cached)
    monkeypatch.setattr(ref, "_reference_asset", asset)
    monkeypatch.setattr(ref.reference_quality, "ask", ask)
    ctx = SimpleNamespace()
    first = await ref.isolate(ctx, ["camera-source", "camera-source"], "A canvas bag")
    assert len(first) == 1 and first[0] in saved and all(pair[0] == "camera-source" for pair in reviews)
    assert (
        len(reviews) == 2
        and len(trace_requests[1][1]) == 3
        and "hand" in trace_requests[1][0]["previous_mask_correction"]
    )
    assert await ref.isolate(ctx, ["camera-source"], "A canvas bag") == first
    assert reads.await_count == 1 and original.tobytes() == before


@pytest.mark.asyncio
async def test_uses_other_real_view_or_stops_if_neither_can_be_verified(monkeypatch):
    monkeypatch.setattr(ref, "_isolate_one", AsyncMock(side_effect=[None, "other-verified"]))
    assert await ref.isolate(None, ["obscured", "clear"], "Bình cam") == ["other-verified"]
    monkeypatch.setattr(ref, "_isolate_one", AsyncMock(return_value=None))
    with pytest.raises(RemakeStopped, match="Chưa thể tách"):
        await ref.isolate(None, ["obscured", "set-only"], "Product details in English", language="vi")


@pytest.mark.asyncio
async def test_localizes_unreadable_material_and_keeps_cancellation(monkeypatch):
    import asyncio

    monkeypatch.setattr(ref, "_isolate_one", AsyncMock(side_effect=OSError("image temporarily unavailable")))
    with pytest.raises(RemakeStopped, match="Tạm thời chưa đọc"):
        await ref.isolate(None, ["unreadable"], "商品外观资料", language="vi")
    monkeypatch.setattr(ref, "_isolate_one", AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        await ref.isolate(None, ["cancelled"], "产品")


def test_retains_continuous_visible_rim_and_real_aperture_when_no_finger_covers_it():
    item = source()
    paint = ImageDraw.Draw(item)
    paint.rectangle((43, 15, 50, 18), fill="#43938a")
    paint.rectangle((43, 19, 50, 24), fill="#272421")
    paint.rectangle((43, 25, 50, 31), fill="#43938a")
    outline = tracing().model_copy(
        update={
            "remove": [polygon(340, 250, 600, 420)],
            "remove_points": [[460, 325], [450, 300]],
            "keep_points": [[300, 350], [625, 360], [550, 200], [450, 210]],
        }
    )
    mask = ref._mask(item, outline)
    assert all(mask.getpixel((x, 21)) == 255 for x in range(31, 64))
    assert all(mask.getpixel((x, 33)) == 0 for x in range(35, 58))


def test_replaces_background_tint_in_mixed_camera_edge_pixels_without_changing_opaque_product():
    scene, product = (190, 105, 75), (16, 16, 16)
    pixels = Image.new("RGB", (50, 50), scene)
    matte = Image.new("L", pixels.size)
    ImageDraw.Draw(pixels).rectangle((15, 5, 33, 44), fill=product)
    ImageDraw.Draw(matte).rectangle((15, 5, 33, 44), fill=255)
    mixed = tuple(round(0.66 * f + 0.34 * b) for f, b in zip(product, scene))
    ImageDraw.Draw(pixels).line((15, 5, 15, 44), fill=mixed)
    ImageDraw.Draw(matte).line((15, 5, 15, 44), fill=170)
    # A reflected highlight in fully opaque material can look like the room.
    ImageDraw.Draw(pixels).line((17, 16, 17, 35), fill=mixed)
    result = ref._isolated_pixels(pixels, matte)
    edge = result.getpixel((15, 25))
    assert 65 < edge[0] < 112 and max(edge) - min(edge) < 5
    assert result.getpixel((18, 25)) == product
    assert result.getpixel((17, 25)) == mixed
    assert result.getpixel((10, 25)) == ref.NEUTRAL


def test_repairs_both_an_attached_scene_sliver_and_scratches_in_a_continuous_reflective_rim():
    photo = Image.new("RGB", (45, 55), (111, 109, 64))
    paint = ImageDraw.Draw(photo)
    paint.rectangle((18, 0, 34, 54), fill=(18, 20, 23))
    paint.line((18, 0, 18, 54), fill=(155, 154, 137))
    current = Image.new("L", photo.size)
    pen = ImageDraw.Draw(current)
    pen.rectangle((16, 0, 34, 54), fill=255)
    pen.rectangle((19, 17, 20, 25), fill=0)
    edit = ref.MaskEdit(erase=[], restore=[], edges=[{"inside": "right", "points": [[405, 0], [405, 1000]]}])
    repaired = ref._apply_edits(photo, current, edit)
    assert all(repaired.getpixel((16, y)) == 0 for y in range(4, 50))
    assert all(repaired.getpixel((20, y)) >= 250 for y in range(4, 50))
    assert all(repaired.getpixel((23, y)) == 255 for y in range(4, 50))


@pytest.mark.asyncio
async def test_only_the_disputed_source_detail_is_retraced_before_an_independent_recheck(monkeypatch):
    original = source()
    monkeypatch.setattr(ref, "_read", AsyncMock(return_value=original))
    monkeypatch.setattr(ref, "_cached", AsyncMock(return_value=None))
    saved, operations = {}, []

    async def asset(ctx, key, data, *, mime):
        saved[key] = data
        return key

    async def ask(ctx, operation, schema, guide, data, images, **kwargs):
        operations.append((operation, list(images)))
        if operation == "commercial_locate_product":
            report = dict(usable=True, studio=False, box=[0, 0, 1000, 1000], evidence="bag with a handle")
        elif operation == "commercial_trace_product":
            report = tracing().model_dump()
        elif operation == "commercial_refine_product_edge":
            assert len(images) == 3 and all(view in saved for view in images)
            report = ref.Outline(
                surfaces=[polygon(0, 650, 1000, 1000)], remove=[], keep_points=[], remove_points=[]
            ).model_dump()
        else:
            accepted = any(op == "commercial_refine_product_edge" for op, _ in operations)
            report = review(accepted)
            if not accepted:
                report["details"] = [dict(box=[335, 235, 660, 495], defect="Trim the visible aperture")]
            else:
                assert images[0] == "original" and len(images) == 4 and "-comparison-" in images[-1]
        return {"report": report}

    monkeypatch.setattr(ref, "_reference_asset", asset)
    monkeypatch.setattr(ref.reference_quality, "ask", ask)
    result = await ref.isolate(None, ["original"], "Photo of the bag", language="en")
    assert result[0] in saved
    assert [name for name, _ in operations] == [
        "commercial_locate_product",
        "commercial_trace_product",
        "commercial_verify_product_card",
        "commercial_refine_product_edge",
        "commercial_verify_product_card",
    ]


def film_plan():
    return SimpleNamespace(
        product="Bình cam", appearance="Bình cam nắp đen", language="vi", video_reference_version=2,
        carry="Tay trái giữ thân bình.", contact="Tay phải chạm quai."
    )


@pytest.mark.parametrize("route", ["verified", "different-product", "missing-angle"])
async def test_visibly_obstructed_video_goes_directly_to_verified_photography(monkeypatch, route):
    from light import commercial_plan, commercial_product_photograph as studio, product_story, production, remake

    plan = film_plan()
    plan.score_revision = 2
    plan.obscured_product_videos = ["clip"]
    roles = {"product_1": ("clip", "middle", ["center"]), "setting": ("shop", "opening", ["room"])}
    ctx = SimpleNamespace(session_id="session")
    monkeypatch.setattr(commercial_plan, "filming_reference_roles", AsyncMock(return_value=roles))

    async def sample(_ctx, ids, *, video_sample, detail_width):
        assert ids == ["clip"] and detail_width == 1440
        if route == "missing-angle" and video_sample == "late":
            raise OSError("optional angle unavailable")
        return [video_sample]

    monkeypatch.setattr(remake, "_material_refs", sample)
    camera = AsyncMock(return_value={"approved": route == "verified", "photograph": "neutral"})
    monkeypatch.setattr(studio, "photograph", camera)
    matte = AsyncMock(return_value="matte")
    monkeypatch.setattr(ref, "_isolate_one", matte)
    monkeypatch.setattr(studio, "assess_camera_view", AsyncMock(return_value={"approved": True, "source": "center", "card": "matte"}))
    monkeypatch.setattr(production, "save_director_decision", AsyncMock(side_effect=lambda *args: args[-1]))
    monkeypatch.setattr(product_story, "_asset", AsyncMock(return_value=SimpleNamespace(session_id=ctx.session_id, mime="image/png")))
    monkeypatch.setattr(ref, "_cached", AsyncMock(return_value="matte"))
    run = {}
    for _ in range(2):
        originals, cards, _, locations = await ref.filming_inputs(ctx, plan, prefix="film", run=run)
        assert locations == ["room"]
        assert originals == (["center", "opening", "late"] if route == "verified" else ["center"])
        assert cards == (["neutral"] if route == "verified" else ["matte"])
    assert camera.await_count == (0 if route == "missing-angle" else 1)
    if camera.await_count:
        assert camera.await_args.args[3] == ["center", "opening", "late"]
    assert matte.await_count == (0 if route == "verified" else 1)


@pytest.mark.asyncio
async def test_failed_selected_video_frames_recover_opening_from_that_upload_and_replay_each_verdict(
    monkeypatch,
):
    from light import commercial_plan, production, remake

    roles = {
        "product_1": ("video", "middle", ["f62"]),
        "product_2": ("video", "late", ["f105"]),
        "protagonist": ("actor-file", "opening", ["face"]),
        "setting": ("room-file", "late", ["room"]),
    }
    monkeypatch.setattr(commercial_plan, "filming_reference_roles", AsyncMock(return_value=roles))
    frames = AsyncMock(return_value=["f0"])
    monkeypatch.setattr(remake, "_material_refs", frames)
    seen = []

    async def isolate(ctx, source, appearance, **kwargs):
        seen.append(source)
        return "isolated-f0" if source == "f0" else None

    monkeypatch.setattr(ref, "_isolate_one", isolate)
    monkeypatch.setattr(ref, "_cached", AsyncMock(return_value="isolated-f0"))
    vision = AsyncMock(
        return_value={
            "report": {
                "same_product": True,
                "identifying_surfaces_visible": True,
                "evidence": "Same orange cylinder, black loop and silver foot.",
            }
        }
    )
    monkeypatch.setattr(ref.reference_quality, "ask", vision)

    async def persist(ctx, prefix, index, key, decision):
        assert prefix == "film" and index == 1
        return decision

    commit = AsyncMock(side_effect=persist)
    monkeypatch.setattr(production, "save_director_decision", commit)
    run = {}
    first = await ref.filming_inputs(None, film_plan(), prefix="film", run=run)
    assert first == (["f0"], ["isolated-f0"], ["face"], ["room"])
    assert sorted(seen) == ["f0", "f105", "f62"]
    assert frames.await_args.args[1] == ["video"] and frames.await_args.kwargs == {
        "video_sample": "opening",
        "detail_width": 1440,
    }
    assert vision.await_args.args[5] == ["f62", "f105", "f0"]
    assert [row["source"] for row in run["director_decisions"].values() if row.get("card")] == ["f0"]
    assert await ref.filming_inputs(None, film_plan(), prefix="film", run=run) == first
    assert len(seen) == 3 and vision.await_count == 1 and commit.await_count == 4


@pytest.mark.asyncio
async def test_alternate_must_show_the_named_product_and_cannot_borrow_another_material(monkeypatch):
    from light import commercial_plan, remake

    roles = {
        "product_1": ("montage", "middle", ["selected"]),
        "setting": ("another-camera", "opening", ["other-frame"]),
    }
    monkeypatch.setattr(commercial_plan, "filming_reference_roles", AsyncMock(return_value=roles))

    async def frames(ctx, ids, *, video_sample, detail_width):
        assert ids == ["montage"]
        return [video_sample]

    monkeypatch.setattr(remake, "_material_refs", frames)
    tested = []

    async def measure(ctx, frame, appearance, **kwargs):
        tested.append(frame)
        return "clean-" + frame if frame != "selected" else None

    monkeypatch.setattr(ref, "_isolate_one", measure)

    def classify(ctx, name, schema, prompt, data, images, **kwargs):
        assert images[0] == "selected"
        return {
            "report": {
                "same_product": images[-1] == "late",
                "identifying_surfaces_visible": True,
                "evidence": "Inspect the physical label and shape.",
            }
        }

    judge = AsyncMock(side_effect=classify)
    monkeypatch.setattr(ref.reference_quality, "ask", judge)
    assert await ref.filming_inputs(None, film_plan()) == (["late"], ["clean-late"], [], ["other-frame"])
    assert tested == ["selected", "late"] and judge.await_count == 2


@pytest.mark.asyncio
async def test_accepted_selected_view_photo_or_provider_error_never_uses_another_timestamp(monkeypatch):
    from light import commercial_plan, remake

    pick = AsyncMock(
        return_value={
            "product_1": ("video", "middle", ["primary-v"]),
            "product_2": ("video", "opening", ["good"]),
        }
    )
    monkeypatch.setattr(commercial_plan, "filming_reference_roles", pick)
    frames = AsyncMock()
    monkeypatch.setattr(remake, "_material_refs", frames)
    monkeypatch.setattr(ref, "_isolate_one", AsyncMock(side_effect=[None, "actual-card"]))
    assert await ref.filming_inputs(None, film_plan()) == (["good"], ["actual-card"], [], [])
    pick.return_value = {"product_1": ("photo", "opening", ["photo"])}
    monkeypatch.setattr(ref, "_isolate_one", AsyncMock(return_value=None))
    with pytest.raises(RemakeStopped, match="Chưa thể tách"):
        await ref.filming_inputs(None, film_plan())
    pick.return_value = {"product_1": ("video", "late", ["primary-v"])}
    monkeypatch.setattr(ref, "_isolate_one", AsyncMock(side_effect=OSError("source unavailable")))
    with pytest.raises(RemakeStopped, match="Tạm thời chưa đọc"):
        await ref.filming_inputs(None, film_plan())
    pick.side_effect = OSError("reference storage unavailable")
    with pytest.raises(RemakeStopped, match="Tạm thời chưa đọc"):
        await ref.filming_inputs(None, film_plan())
    frames.assert_not_awaited()


@pytest.mark.parametrize("outcome", ["complete-alternate", "all-partial", "vision-unavailable", "studio-original"])
async def test_faithful_cutout_of_an_occluded_part_needs_an_intact_camera_reference(monkeypatch, outcome):
    from light import commercial_plan, commercial_product_photograph as studio, product_story, production, remake

    p = film_plan()
    p.score_revision = 2
    ctx = SimpleNamespace(session_id="production")
    roles = {"product_1": ("video", "middle", ["middle"]), "product_2": ("video", "late", ["late"])}
    monkeypatch.setattr(commercial_plan, "filming_reference_roles", AsyncMock(return_value=roles))
    async def isolated(_ctx, source, _appearance, *, evidence, **_):
        if outcome == "studio-original":
            evidence["unmodified_studio_packshot"] = True
        return "card-" + source
    async def validate(_ctx, supplied, source, card):
        assert supplied is p and card == "card-" + source
        if outcome == "vision-unavailable":
            raise OSError("vision unavailable")
        return {"source": source, "card": card, "approved": outcome == "complete-alternate" and source == "opening"}
    async def capture(_ctx, _prefix, _plan, sources, *, remember):
        assert sources == ["middle", "late", "opening"]
        return {"approved": True, "photograph": "intact", "source_ids": sources}
    camera = AsyncMock(side_effect=capture)
    check = AsyncMock(side_effect=validate)
    monkeypatch.setattr(studio, "photograph", camera)
    monkeypatch.setattr(studio, "assess_camera_view", check)
    monkeypatch.setattr(ref, "_isolate_one", isolated)
    monkeypatch.setattr(ref, "_cached", AsyncMock(return_value="exists"))
    monkeypatch.setattr(remake, "_material_refs", AsyncMock(return_value=["opening"]))
    monkeypatch.setattr(ref.reference_quality, "ask", AsyncMock(return_value={"report": {
        "same_product": True, "identifying_surfaces_visible": True, "evidence": "The same bottle."}}))
    monkeypatch.setattr(production, "save_director_decision", AsyncMock(side_effect=lambda *args: args[-1]))
    monkeypatch.setattr(product_story, "_asset", AsyncMock(return_value=SimpleNamespace(session_id="production", mime="image/png")))
    run = {}
    if outcome == "vision-unavailable":
        with pytest.raises(RemakeStopped, match="Tạm thời chưa đọc"):
            await ref.filming_inputs(ctx, p, prefix="film", run=run)
        camera.assert_not_awaited()
        return
    raw, cards, _, _ = await ref.filming_inputs(ctx, p, prefix="film", run=run)
    if outcome == "studio-original":
        assert (raw, cards) == (["middle", "late"], ["card-middle", "card-late"])
        check.assert_not_awaited()
    elif outcome == "complete-alternate":
        assert (raw, cards) == (["opening"], ["card-opening"])
    else:
        assert (raw, cards) == (["middle", "late", "opening"], ["intact"])
        assert all(key not in raw for key in cards)
    reads = check.await_count
    assert (await ref.filming_inputs(ctx, p, prefix="film", run=run))[:2] == (raw, cards)
    assert camera.await_count == (1 if outcome == "all-partial" else 0) and check.await_count == reads


@pytest.mark.parametrize("outcome", ["match", "new-feature", "unreadable", "different-upload"])
async def test_neutral_product_photography_requires_actual_source_evidence_and_saves_one_receipt(monkeypatch, outcome):
    from light import commercial_plan, commercial_product_photograph as studio, product_story, production, remake

    plan = film_plan()
    plan.score_revision = 2
    ctx = SimpleNamespace(session_id="one-render")
    roles = {"product_1": ("movie", "opening", ["real-opening"])}
    monkeypatch.setattr(commercial_plan, "filming_reference_roles", AsyncMock(return_value=roles))
    monkeypatch.setattr(ref, "_isolate_one", AsyncMock(return_value=None))
    monkeypatch.setattr(remake, "_material_refs", AsyncMock(return_value=["other-real-view"]))
    monkeypatch.setattr(product_story, "_asset", AsyncMock(return_value=SimpleNamespace(session_id=ctx.session_id, mime="image/png")))
    still = AsyncMock(return_value="neutral-studio")
    monkeypatch.setattr(studio, "_still", still)

    async def check(_ctx, name, schema, guide, data, images, **kwargs):
        if name == "commercial_verify_alternate_product":
            return {"report": dict(same_product=outcome != "different-upload", identifying_surfaces_visible=True,
                                   evidence="Shape and markings of the merchandise.")}
        if name == "read_occluded_product_views":
            assert images == (["real-opening"] if outcome == "different-upload" else ["real-opening", "other-real-view"])
            return {"report": dict(one_identifiable_product=True, geometry_supported_by_views=outcome != "unreadable",
                                   shooting_view=len(images), visible_identifiers="Thân bình và quai đen.",
                                   occlusion="Ngón tay che một phần quai.")}
        assert name == "verify_rephotographed_product" and images[-1] == "neutral-studio"
        return {"report": dict(same_product=True, silhouette_and_proportions=True, same_parts_and_openings=True,
                               materials_colours_and_marks=True, no_unsupported_visible_detail=outcome != "new-feature",
                               no_source_hand_or_setting=True, evidence="Viền đáy và quai khớp ảnh gốc.")}

    ask = AsyncMock(side_effect=check)
    monkeypatch.setattr(ref.reference_quality, "ask", ask)
    async def persist(ctx, prefix, index, key, item):
        return item
    monkeypatch.setattr(production, "save_director_decision", persist)
    run = {}
    if outcome in ("unreadable", "new-feature"):
        for _ in range(2):
            with pytest.raises(RemakeStopped, match="Chưa thể tách"):
                await ref.filming_inputs(ctx, plan, prefix="job", run=run)
        assert still.await_count == (0 if outcome == "unreadable" else 1)
        return
    result = await ref.filming_inputs(ctx, plan, prefix="job", run=run)
    expected_sources = ["real-opening"] if outcome == "different-upload" else ["real-opening", "other-real-view"]
    assert result == (expected_sources, ["neutral-studio"], [], [])
    assert set(still.await_args.args[4]) == set(expected_sources)
    calls = ask.await_count
    assert await ref.filming_inputs(ctx, plan, prefix="job", run=run) == result
    assert ask.await_count == calls and still.await_count == 1
