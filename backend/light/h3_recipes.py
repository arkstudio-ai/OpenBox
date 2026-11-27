"""Source timing and creative direction for new fast-tier App template runs."""
from pathlib import Path

from light.silk_road_world import CAMERA
from light import commercial_plan, product_story_plan, remake_case, shortcut_plan, silk_road, wan_story

TEMPLATES = frozenset({
    product_story_plan.TEMPLATE, shortcut_plan.TEMPLATE, silk_road.TEMPLATE,
    wan_story.SODA_TEMPLATE, remake_case.TEMPLATE,
})


def recipe(template: str) -> dict:
    if template == product_story_plan.TEMPLATE:
        return {
            "title": "商品节奏广告", "canvas": "16:9", "fps": commercial_plan.FPS,
            "cuts": [{"frames": b.frames, "beat": b.labels[0], "camera": b.camera,
                      "product_visible": b.product} for b in commercial_plan.BEATS],
            "match_frames": list(commercial_plan.MATCH_FRAMES),
            "direction": "五场以同一接触动作三次匹配转场。围绕当前商品设计接触、实际使用、交接与街角反转。"
                         "短插镜按帧剪；相邻动作可在一个生成片段内拍摄。记录每个切点对应的素材和时间。",
        }
    if template == shortcut_plan.TEMPLATE:
        return {
            "title": "早就吃上了", "canvas": "9:16",
            "cuts": [{"seconds": s, "beat": b} for s, b in zip(shortcut_plan.DURATIONS, (
                "开场交代共同目标", "费劲的一方出发", "拍到行动受阻", "两人停下看向画外",
                "切反打揭晓另一方早已达成目标", "商品特写解释捷径", "两条行动线汇合并回扣开场",
            ), strict=True)],
            "direction": "三人在各自拍摄位置。第4、5镜视线相接。按商品真实用途设计捷径和反转。",
        }
    if template == silk_road.TEMPLATE:
        case = silk_road.case()
        return {
            "title": "丝路留香", "canvas": silk_road.CANVAS, "seconds": silk_road.DURATION,
            "shots": [{"take": s["name"], "beat": s["label"], "camera": camera}
                      for s, camera in zip(case["shots"], CAMERA, strict=True)],
            "cuts": case["cuts"],
            "direction": "保留错选、两次试错、商品揭晓与同行者回应的因果。随商品调整拿取、交换和使用动作。"
                         "H3 以16:9拍，人物、商品与文字放在中央超宽裁切安全区。IMS按模板画布裁切。"
                         "参考切表中的帧数决定剪辑节奏，素材内入点根据新表演调整。",
        }
    if template == wan_story.SODA_TEMPLATE:
        return {
            "title": "苏打水广告", "canvas": "9:16", "duration_range": [25, 30],
            "direction": "约6至8镜。贴桌低机位开场，切人物反应，正反打解围，暖光下亮商品，再切冷色场景反转。"
                         "在最后的商品原画中排入用户确认的题字。按当前商品重新设定人物、道具和台词。",
        }
    if template == remake_case.TEMPLATE:
        source = remake_case.manifest()
        return {"title": source["title"], "canvas": "9:16",
                "cuts": [{k: s[k] for k in ("beat", "from_sec", "to_sec", "product_visible")}
                         for s in source["shots"]],
                "direction": "让当次角色演出买单误会与解围的完整因果，按当前商品和已确认文案改写对答。"}
    raise ValueError(f"Unknown H3 video template: {template}")


def reference_files(template: str) -> list[Path]:
    if template == silk_road.TEMPLATE:
        return [silk_road.ASSETS / "motion" / f"reference-{shot['name']}.mp4" for shot in silk_road.case()["shots"]]
    if template == product_story_plan.TEMPLATE:
        directory = Path(__file__).with_name("remake_assets") / "commercial-reference" / "visual"
        return [directory / f"{i:02}.jpg" for i in range(1, len(commercial_plan.BEATS) + 1)]
    if template == remake_case.TEMPLATE:
        return [remake_case.ASSETS / f"{s['shot']:02}.jpg" for s in remake_case.manifest()["shots"]]
    return []
