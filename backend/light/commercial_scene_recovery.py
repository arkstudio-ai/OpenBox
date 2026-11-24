"""Recover a prior selected edit from its owned photographed scene-comparison sheets."""
from __future__ import annotations

from assets.service import owned_ready_query
from db.base import get_db_session
from db.models.file_asset import FileAsset
from sqlalchemy.exc import SQLAlchemyError

from light import commercial_plan as recipe


async def _assets(ctx, ids):
    async with get_db_session() as db:
        query = owned_ready_query(ctx.user_id, ctx.workspace_id).where(
            FileAsset.session_id == ctx.session_id, FileAsset.workspace_id == ctx.workspace_id, FileAsset.id.in_(ids)
        ).with_only_columns(FileAsset.id, FileAsset.created_at, FileAsset.mime)
        return {key: (when, mime) for key, when, mime in (await db.execute(query)).all()}


async def restore(ctx, run, plan, cuts):
    """Return additional shots only. All final edit/continuity decisions run normally."""
    from light.commercial_film import (
        DEPENDENCIES,
        MATCH_IN,
        SCENE_JOINS,
        _fingerprint,
        _obsolete_selection,
    )

    repairs = run.get("commercial_repairs") or []
    if (getattr(plan, "format", None) != recipe.FORMAT or getattr(plan, "score_revision", 1) != 2
            or run.get("wan_review_version") != recipe.REVIEW or run.get("wan_final_key") or run.get("output")
            or not repairs or (repairs[-1].get("report") or {}).get("kind") != "commercial"):
        return {}
    target = repairs[-1].get("target")
    if not isinstance(target, int) or not 1 <= target <= len(recipe.BEATS):
        return {}
    skipped = {target}
    for i in range(1, len(recipe.BEATS) + 1):
        if skipped.intersection(DEPENDENCIES.get(i, ())):
            skipped.add(i)
    decisions = run.get("director_decisions") or {}
    replaced = {row["replaces_review"] for row in decisions.values() if row.get("replaces_review")}
    takes, scenes, ids = {}, {}, set()
    for key, row in decisions.items():
        cut = row.get("cut") or {}
        i = cut.get("shot")
        if (key not in replaced and isinstance(i, int) and 1 <= i <= len(recipe.BEATS)
                and key.startswith(f"{recipe.REVIEW}:take:{i}:") and row.get("ok") is True
                and cut.get("review_version") == recipe.REVIEW and cut.get("output_frames") == recipe.BEATS[i - 1].frames
                and all(cut.get(field) for field in ("source", "review_board", "review_head", "review_tail"))):
            if (i == 26 and not cut.get("motion_performance")) or _obsolete_selection(plan, i, row) == "departure-position":
                continue
            takes.setdefault((i, cut["review_board"]), []).append((key, row))
            ids.update(cut[field] for field in ("source", "review_board", "review_head", "review_tail"))
        if not key.startswith(f"{recipe.REVIEW}:scene:"):
            continue
        scene = key.split(":")[2]
        pairs = SCENE_JOINS.get(scene)
        if (not pairs or row.get("pairs") != [[i, i + 1] for i in pairs] or not isinstance(row.get("ok"), bool)
                or len(row.get("cut_assets") or []) != len(pairs) + 1 or not row.get("sheets")):
            continue
        scenes.setdefault(scene, []).append(row)
        ids.update(row["sheets"])
    if not scenes or not takes:
        return {}
    try:
        available = await _assets(ctx, ids)
    except (OSError, SQLAlchemyError):
        return {}

    def owned(key, kind):
        return key in available and available[key][1].startswith(kind + "/")

    chosen = {}
    for scene, snapshots in scenes.items():
        # An unreadable newer sheet must not silently select one of its predecessors.
        if any(not all(owned(a, "image") for a in s["sheets"]) for s in snapshots):
            continue
        dated = [(max(available[a][0] for a in s["sheets"]), s) for s in snapshots]
        newest = max(t for t, _ in dated)
        matches = {tuple(s["cut_assets"]) for t, s in dated if t == newest}
        if len(matches) != 1:
            continue
        boards, = matches
        start = SCENE_JOINS[scene][0]
        group = dict(enumerate(boards, start))
        chosen.update({i: (board, group) for i, board in group.items()})

    frozen = {c["shot"]: c for c in run.get("wan_cuts") or []}
    frozen.update(cuts)
    recovered = {}
    for i, (board, group) in sorted(chosen.items()):
        if i in skipped or i in frozen or i == 28:
            continue
        rows = takes.get((i, board), [])
        if not rows:
            continue
        # The same board must identify one source window even if it was reviewed again.
        windows = {tuple(r["cut"].get(field) for field in ("source", "start", "frames", "speed")) for _, r in rows}
        if len(windows) != 1 or None in next(iter(windows)):
            continue
        known = {**frozen, **recovered}
        parents = DEPENDENCIES.get(i, ())
        if any(parent not in known or not known[parent].get("review_tail") for parent in parents):
            continue
        needed = [known[parent]["review_tail"] for parent in parents]
        for key, row in rows:
            cut = row["cut"]
            if not owned(cut["source"], "video") or not all(owned(cut[field], "image") for field in ("review_board", "review_head", "review_tail")):
                continue
            recorded = row.get("continuity_anchors")
            if needed and recorded is None and i in MATCH_IN:
                evidence = row.get("evidence") or []
                if len(row.get("setup") or []) == 2:
                    edge = 2 + bool(row["setup"][1])
                    recorded = evidence[edge:edge + 1]
                else:
                    # Early receipts did not repeat their inputs. The original
                    # paid take key itself hashes its parent tails and attempt.
                    digest = _fingerprint(needed, "", cut.get("take", 1))
                    if key.split(":")[3] == digest and needed[0] in evidence[2:4]:
                        recorded = list(needed)
            if needed:
                if recorded is None:
                    matches = all(group.get(parent) == known[parent].get("review_board") for parent in parents)
                else:
                    matches = recorded == needed
                if not matches:
                    continue
            recovered[i] = cut
            break
    return recovered
