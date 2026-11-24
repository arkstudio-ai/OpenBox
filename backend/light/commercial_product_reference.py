"""Give the photographer only the visible pixels of the user's chosen product."""

import asyncio
import hashlib
import io
import math
from typing import Annotated, Literal

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat
from pydantic import BaseModel, Field, model_validator

from assets.service import owned_ready_query
from core.oss import OssError, OssNotConfigured, get_oss
from db.base import get_db_session
from db.models.file_asset import FileAsset
from light import reference_quality
from light.remake import RemakeStopped, _reference_asset, _reference_id
from light.video_language import for_text

VERSION = "commercial-product-pixels-v12"
ERRORS = {
    "zh": "无法从所选照片或视频帧中分离并核对真实商品，请选用能看清商品的素材。",
    "vi": "Chưa thể tách và xác minh sản phẩm trong ảnh hoặc khung hình đã chọn. Vui lòng chọn tư liệu thấy rõ sản phẩm.",
    "en": "The product could not be isolated and verified in the selected photo or footage. Choose a clearer view.",
}
READ_ERRORS = {
    "zh": "暂时无法读取或核对商品素材，请稍后重试。",
    "vi": "Tạm thời chưa đọc hoặc kiểm tra được tư liệu sản phẩm. Vui lòng thử lại.",
    "en": "The product reference could not be read or reviewed. Please try again.",
}
Coord = Annotated[int, Field(ge=0, le=1000)]
Point = Annotated[list[Coord], Field(min_length=2, max_length=2)]
Polygon = Annotated[list[Point], Field(min_length=3, max_length=72)]
NEUTRAL = (239, 239, 236)


class Location(BaseModel):
    usable: bool
    studio: bool = Field(description="Only this item on an empty seamless neutral studio background.")
    box: list[Coord] = Field(
        min_length=4, max_length=4, description="Visible item bounds, x_min,y_min,x_max,y_max in 0..1000."
    )
    evidence: str = Field(min_length=1, max_length=650)

    @model_validator(mode="after")
    def ordered(self):
        if self.usable and (self.box[2] <= self.box[0] or self.box[3] <= self.box[1]):
            raise ValueError("Visible bounds must have positive area")
        return self


class AlternateView(BaseModel):
    same_product: bool
    identifying_surfaces_visible: bool
    evidence: str = Field(min_length=1, max_length=450)


SAME_PRODUCT = (
    "The first images are the director's selected product frames. The last image is a different moment "
    "of that same uploaded file. Check whether the last image really shows the named product, comparing visible "
    "physical identifiers with the selected frames and the given appearance. Different poses, occlusion and "
    "camera angles are fine. A montage may show other items: sharing a file, colour, room or actor is not evidence "
    "of identity. Reject a lookalike, a generic cropped surface, or an image with insufficient identifiers. "
    "Describe concrete features visible in the input images supporting the result."
)


class Outline(BaseModel):
    surfaces: list[Polygon] = Field(
        min_length=1,
        max_length=15,
        description="Ordered clock-wise visible product contours. Use separate contours for disconnected parts.",
    )
    remove: list[Polygon] = Field(
        max_length=16,
        description="Actual holes, exposed background, overlapping fingers or other visible non-product pixels.",
    )
    keep_points: list[Point] = Field(
        max_length=18,
        description="Certain product pixels centered on thin pieces, edges of holes, and distinct materials.",
    )
    remove_points: list[Point] = Field(
        max_length=18,
        description="Certain background and occluder pixels next to the silhouette, including inside openings.",
    )


class Detail(BaseModel):
    box: list[Coord] = Field(
        min_length=4,
        max_length=4,
        description="x0,y0,x1,y1 in the unmarked original crop (image 2), coordinates 0..1000.",
    )
    defect: str = Field(min_length=1, max_length=300)


class EdgeLine(BaseModel):
    inside: Literal["right", "left", "above", "below"] = Field(
        description="Side containing the real photographed product, away from the scene."
    )
    points: list[Point] = Field(
        min_length=2,
        max_length=32,
        description="Ordered points on ONE real optical boundary in the local crop coordinates.",
    )


class MaskEdit(BaseModel):
    edges: list[EdgeLine] = Field(
        default_factory=list,
        max_length=6,
        description="On a disputed continuous lip, wall or rim give its open boundary and product side rather than erasing through opaque material.",
    )
    erase: list[Polygon] = Field(
        max_length=12, description="Only the unwanted pixels still present in image 3."
    )
    restore: list[Polygon] = Field(
        max_length=12, description="Only photographed product pixels still missing from image 3."
    )


class CardReview(BaseModel):
    same_product: bool
    visible_features_kept: bool
    visible_holes_kept: bool
    free_of_people_and_scene: bool
    no_invented_surfaces: bool
    reason: str = Field(min_length=1, max_length=500)
    correction: str = Field(default="", max_length=650)
    details: list[Detail] = Field(
        default_factory=list,
        max_length=3,
        description="On rejection, tight image-2 rectangles enclosing the defective contour, with enough of both sides to trace it; group adjacent issues. Empty on acceptance.",
    )

    @property
    def accepted(self):
        return all(
            (
                self.same_product,
                self.visible_features_kept,
                self.visible_holes_kept,
                self.free_of_people_and_scene,
                self.no_invented_surfaces,
            )
        )


LOCATE = (
    "Identify only the named physical main product in this real image. Give its visible enclosing box, including any "
    "thin handle, opening, foot, lid or visible label. Exclude people, supporting surfaces, other things, cast shadows "
    "and reflections on those surfaces. Mark unusable if there are too few identifying surfaces or its identity is ambiguous. "
    "studio is true only for a clean, isolated packshot: no set, counter, furniture, graphics, hands or people anywhere."
)
TRACE = (
    "Trace a digital cutout in the first image's 0..1000 XY coordinates, x increases right and y down. The second "
    "image has the same crop with ruler labels and 100-unit grid. Return vertices in order around the actual visible "
    "object; give enough vertices on a curved outline and at changes of direction. Do not trace the bounding box. "
    "Mark every visible enclosed hole and any finger/hand or supporting surface occluding the product in remove. "
    "An aperture shows the studio matte, never the skin or room visible through it. Keep dark outlines, small actual "
    "features, thin edges and opaque labels. Use separate surfaces or cut out overlaps where a part is hidden. "
    "Do not complete an obscured shape from imagination. Supply reliable point seeds in each distinct product surface "
    "and on each definite non-product area near the outline. If the hand covers an edge, leave the covered portion absent. "
    "Use compact polygons: usually 12–24 points for an outline, 6–16 for a hole and under 70 vertices total. "
    "Local pixel-edge refinement follows your approximate contours. Spend points on real changes of direction."
)
REVIEW = (
    "Independently compare the complete original photograph (1), its unmarked enlarged crop (2), and the candidate card (3). "
    "For an unchanged studio photo there is one image. Inspect the product at the card's normal viewing size, then use any "
    "registered diptychs (original left, masked right) to resolve doubtful pixels. The numeric crop size is in actual source-camera "
    "pixels, independent of display magnification. Check the visible black or light rim, open holes, contours, label, colour, and "
    "proportions against the original. A genuine opaque rim that is visibly continuous in the original must remain continuous. "
    "A portion actually covered by a finger is correctly absent. Reject an attached piece of finger or skin, an occluder shape in an "
    "opening, a strip of background extending beyond the optical edge, identifiable scenery, missing visible rim, or invented surface. "
    "Hairline colour transition, antialiasing, or reflected colour confined to about ONE original camera pixel along the actual "
    "boundary is normal photographic coverage. It passes when the opening is clear and no contiguous foreign patch, hand shape or "
    "scene detail remains. A long/high-contrast nick that visibly breaks the real rim fails, even when thin. For a foreign patch, "
    "name the actual shape that remains, location, and estimated extent in original pixels. On rejection, locate each defect in the "
    "unmarked crop's normalized 0..1000 coordinates, include surrounding good edges for repair, group related issues into at most "
    "three boxes. A one-product seamless studio packshot with actual visible features intact is acceptable."
)
DETAIL_TRACE = (
    "Refine a binary product matte in this tightly magnified real photograph. Image 1 is the literal original pixel detail; "
    "image 2 is the same detail with a 0..1000 grid. Image 3 shows the current cutout at exactly the same scale and origin. "
    "Trace every visible product surface within the full rectangular window, including areas already correct. "
    "Extend product surfaces all the way to the border wherever the product continues outside this window. "
    "Vertices are (x,y) within image 1 only: x=0 left, x=1000 right, y=0 top, y=1000 bottom. Follow visible pixel boundaries "
    "accurately, taking special care with holes, thin edges, occluding fingers, labels and reflections. Leave missing pieces "
    "missing and keep every visible opaque rim. Background and skin must be completely outside the matte. Use separate "
    "polygons for disconnected visible surfaces; mark enclosed openings in remove. Place reliable interior/exterior seeds near "
    "the defect. Keep under 65 vertices total and concentrate them on changes of direction."
)
EDIT = (
    "Make a precise correction to an existing digital cutout. Image 1 is the actual magnified original, image 2 is "
    "its coordinate grid, and image 3 is the current matte at the exact same pixel registration. "
    "Return narrow edit polygons only where the current matte differs from the actual product boundary. "
    "In erase, outline only retained scene/skin fragments. In restore, outline only visible material missing from "
    "the matte. Never remove real reflections on an opaque surface. Never bridge an occluded area. "
    "Coordinates x,y range from 0..1000 within this local image. Thin slivers often need a tight ribbon of ordered "
    "points marking both their edges. You may use separate ribbons for disconnected errors. "
    "The preferred edit for a wall, rim, notch or lip is a polyline in edges: trace the single actual boundary through "
    "the disputed section and name on which side the product lies. Split a bend when that side changes. A vertical left "
    "exterior has product right; the horizontal lower edge of a handle opening has product below. Put end points just "
    "beyond the issue. Use polygons for disconnected fragments only. Do not punch a hole through continuous material or "
    "erase genuine highlights. Use the real original source and correct just the disputed section."
)


def _image(data):
    with Image.open(io.BytesIO(data)) as original:
        rgba = ImageOps.exif_transpose(original).convert("RGBA")
        rgba.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
        backing = Image.new("RGB", rgba.size, NEUTRAL)
        backing.paste(rgba, mask=rgba.getchannel("A"))
        return backing


def _png(image):
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


def _crop(source, box):
    w, h = source.size
    x, y, r, b = box
    padx = max(3, (r - x) * w / 1000 * 0.10)
    pady = max(3, (b - y) * h / 1000 * 0.07)
    bounds = (
        max(0, round(x * w / 1000 - padx)),
        max(0, round(y * h / 1000 - pady)),
        min(w, round(r * w / 1000 + padx)),
        min(h, round(b * h / 1000 + pady)),
    )
    if bounds[2] - bounds[0] < 10 or bounds[3] - bounds[1] < 10:
        return None
    crop = source.crop(bounds)
    crop.thumbnail((700, 700), Image.Resampling.LANCZOS)
    return crop


def _grid(crop):
    scaled = ImageOps.contain(crop, (730, 730), Image.Resampling.LANCZOS)
    w, h = scaled.size
    pad = 33
    grid = Image.new("RGB", (w + 2 * pad, h + 2 * pad), "white")
    grid.paste(scaled, (pad, pad))
    draw = ImageDraw.Draw(grid)
    font = ImageFont.load_default(size=12)
    for i in range(11):
        x = pad + round((w - 1) * i / 10)
        y = pad + round((h - 1) * i / 10)
        draw.text((x, pad - 4), str(i * 100), font=font, anchor="mb", fill="black")
        draw.text((pad - 3, y), str(i * 100), font=font, anchor="rm", fill="black")
        for py in range(pad, pad + h, 8):
            draw.point((x, py), fill="#ddffff")
        for px in range(pad, pad + w, 8):
            draw.point((px, y), fill="#ddffff")
    return grid


def _magnify(crop):
    return ImageOps.contain(crop, (730, 730), Image.Resampling.LANCZOS)


def _detail_bounds(crop, box):
    w, h = crop.size
    x, y, r, b = box
    if r <= x or b <= y:
        return None
    # A few genuine surrounding pixels register the repaired edge to the existing matte.
    bounds = (
        max(0, round(x * w / 1000) - 3),
        max(0, round(y * h / 1000) - 3),
        min(w, round(r * w / 1000) + 3),
        min(h, round(b * h / 1000) + 3),
    )
    return bounds if bounds[2] - bounds[0] >= 4 and bounds[3] - bounds[1] >= 4 else None


def _studio_border(image):
    sample = image.resize((50, 50), Image.Resampling.BOX)
    border = Image.new("RGB", (50, 4))
    for i, edge in enumerate(
        (
            sample.crop((0, 0, 50, 1)),
            sample.crop((0, 49, 50, 50)),
            sample.crop((0, 0, 1, 50)).transpose(Image.Transpose.TRANSPOSE),
            sample.crop((49, 0, 50, 50)).transpose(Image.Transpose.TRANSPOSE),
        )
    ):
        border.paste(edge, (0, i))
    return max(ImageStat.Stat(border).stddev) < 17


def _snap_contour(photo, polygon, foreground, background, radius):
    """Fit a continuous, closed path to photographed edges near the annotated polygon."""
    pixels = photo.load()
    width, height = photo.size

    def colour(x, y):
        return pixels[max(0, min(width - 1, round(x))), max(0, min(height - 1, round(y)))]

    # Points roughly one native pixel apart keep the curvature/continuity cost independent of the supplied vertex spacing.
    points = []
    for (x, y), (r, b) in zip(polygon, (*polygon[1:], polygon[0])):
        count = max(1, math.ceil(math.hypot(r - x, b - y)))
        points.extend((x + (r - x) * i / count, y + (b - y) * i / count) for i in range(count))
    winding = sum(x * b - r * y for (x, y), (r, b) in zip(polygon, (*polygon[1:], polygon[0])))
    direction = 1 if winding >= 0 else -1
    shifts = [s * 0.5 for s in range(-radius * 2, radius * 2 + 1)]
    paths, scores = [], []

    def nearest(x, y, samples):
        return (
            min(samples, key=lambda sample: (sample[0] - x) ** 2 + (sample[1] - y) ** 2)[2] if samples else None
        )

    for i, (x, y) in enumerate(points):
        before, after = points[i - 2], points[(i + 2) % len(points)]
        tx, ty = after[0] - before[0], after[1] - before[1]
        length = max(0.01, math.hypot(tx, ty))
        nx, ny = -ty * direction / length, tx * direction / length
        # The correct inner and outer edges of a narrow rim have opposite colour polarity.
        inner, outer = nearest(x, y, foreground), nearest(x, y, background)
        expected = [a - b for a, b in zip(inner, outer)] if inner and outer else (0, 0, 0)
        strength = max(1, math.sqrt(sum(c * c for c in expected)))
        values = []
        positions = []
        for distance in shifts:
            px, py = x + nx * distance, y + ny * distance
            positions.append((px, py))
            inside = colour(px + nx * 1.2, py + ny * 1.2)
            outside = colour(px - nx * 1.2, py - ny * 1.2)
            delta = [a - b for a, b in zip(inside, outside)]
            signed = sum(a * b for a, b in zip(expected, delta)) / strength
            # Reward genuine local contrast, attenuate the wrong material direction, and keep weak edges near the annotation.
            values.append(max(abs(c) for c in delta) + 0.35 * signed - 2.4 * abs(distance))
        paths.append(positions)
        scores.append(values)
    # Penalize rapidly varying displacements so a table reflection cannot turn a straight product wall into a staircase.
    m = len(shifts)
    start = max(range(m), key=scores[0].__getitem__)
    previous = [-float("inf")] * m
    previous[start] = scores[0][start]
    backtrack = []
    for values in scores[1:]:
        back = []
        costs = []
        for j, local in enumerate(values):
            prior = max(range(max(0, j - 2), min(m, j + 3)), key=lambda k: previous[k] - 16 * abs(k - j))
            costs.append(local + previous[prior] - 16 * abs(prior - j))
            back.append(prior)
        backtrack.append(back)
        previous = costs
    end = max(range(m), key=lambda k: previous[k] - 24 * abs(k - start))
    chosen = [end]
    for back in reversed(backtrack):
        chosen.append(back[chosen[-1]])
    fitted = [locations[k] for locations, k in zip(paths, reversed(chosen))]
    fair = []
    count = len(fitted)
    for i, (x, y) in enumerate(fitted):
        before, after = fitted[(i - 4) % count], fitted[(i + 4) % count]
        ax, ay = x - before[0], y - before[1]
        bx, by = after[0] - x, after[1] - y
        if ax * bx + ay * by < 0.60 * math.hypot(ax, ay) * math.hypot(bx, by):
            fair.append((x, y))
            continue
        adjacent = [fitted[(i + step) % count] for step in (-2, -1, 0, 1, 2)]
        fair.append(
            tuple(
                sum(point[axis] * weight for point, weight in zip(adjacent, (0.10, 0.22, 0.36, 0.22, 0.10)))
                for axis in (0, 1)
            )
        )
    return fair


def _mask(crop, outline, *, detail=False):
    width, height = crop.size

    def point(p):
        return (width - 1) * p[0] / 1000, (height - 1) * p[1] / 1000

    def swatches(points):
        return [(x, y, crop.getpixel((round(x), round(y)))) for x, y in map(point, points)]

    material, scene = swatches(outline.keep_points), swatches(outline.remove_points)
    radius = (
        max(1, min(3, round(max(width, height) / 75)))
        if detail
        else max(2, min(6, round(max(width, height) / 80)))
    )
    scale = 4
    raster = Image.new("L", (width * scale, height * scale))
    pen = ImageDraw.Draw(raster)
    for polygons, inside, outside, fill in (
        (outline.surfaces, material, scene, 255),
        (outline.remove, scene, material, 0),
    ):
        for polygon in polygons:
            contour = _snap_contour(crop, [point(p) for p in polygon], inside, outside, radius)
            pen.polygon([(round((x + 0.5) * scale), round((y + 0.5) * scale)) for x, y in contour], fill=fill)
    # Pixel coverage is an area integral. A ringing interpolation kernel would
    # punch tiny transparent holes in otherwise fully opaque material.
    mask = raster.resize(crop.size, Image.Resampling.BOX)
    if detail and material and scene:
        mask = _local_colours(crop, mask, material, scene)
    return mask


def _local_colours(crop, mask, material, scene):
    """Use observed colours only in the narrow border of a disputed, enlarged detail."""
    rgb = crop.load()

    def features(colour):
        r, g, b = colour
        mean = (r + g + b) / 3
        # Keep both luminance and normalized chroma: a shadowed finger and a dark rim may have similar intensity.
        scale = 82 / (mean + 20)
        return (0.48 * mean, (r - g) * scale, (g - b) * scale, 0.56 * (r - b))

    def swatches(samples):
        return [(x, y, features(value)) for x, y, value in samples]

    foreground = swatches(material)
    outside = swatches(scene)

    def distance(x, y, colour, samples):
        best = float("inf")
        for sx, sy, swatch in samples:
            difference = sum((a - b) ** 2 for a, b in zip(colour, swatch))
            spatial = (x - sx) ** 2 + (y - sy) ** 2
            best = min(best, math.sqrt(difference + 0.06 * spatial))
        return best

    radius = max(2, min(5, round(max(crop.size) / 43)))
    boundary = ImageChops.subtract(
        mask.filter(ImageFilter.MaxFilter(2 * radius + 1)), mask.filter(ImageFilter.MinFilter(2 * radius + 1))
    )
    out = mask.copy()
    output = out.load()
    old = mask.load()
    for pos, border in enumerate(boundary.tobytes()):
        if border < 120:
            continue
        x, y = pos % crop.width, pos // crop.width
        value = features(rgb[x, y])
        fg, bg = distance(x, y, value, foreground), distance(x, y, value, outside)
        if min(fg, bg) > 43 or abs(fg - bg) < 8:
            continue
        # A feature sample specifies a real material, not a replacement colour.
        # Reassign its coverage only where the opposing class is measurably less likely.
        weight = min(1.0, (abs(fg - bg) - 8) / 18)
        new = 255 if fg < bg else 0
        output[x, y] = round(old[x, y] * (1 - weight) + new * weight)
    # Remove isolated single-pixel classifications at the edge without filling a genuine aperture.
    smooth = out.filter(ImageFilter.MedianFilter(3)).filter(ImageFilter.GaussianBlur(0.55))
    smooth = smooth.point(lambda value: max(0, min(255, round((value - 64) * 255 / 128))))
    return Image.composite(smooth, out, boundary)


def _repair_optical_edge(photo, matte, edge):
    """Re-establish a continuous camera boundary, including its highlights, along a measured line."""
    width, height = photo.size
    vertical = edge.inside in ("left", "right")
    direction = 1 if edge.inside in ("right", "below") else -1

    def xy(p):
        x, y = (width - 1) * p[0] / 1000, (height - 1) * p[1] / 1000
        return (y, x) if vertical else (x, y)

    knots = sorted(xy(p) for p in edge.points)
    scans = list(
        range(max(0, math.ceil(knots[0][0])), min(height if vertical else width, math.floor(knots[-1][0]) + 1))
    )
    if len(scans) < 2:
        return

    def coord(t, s):
        return (s, t) if vertical else (t, s)

    pixels = photo.load()
    selected = matte.load()
    extent = (width if vertical else height) - 1

    def colour(t, s):
        return pixels[coord(t, min(extent, max(0, round(s))))]

    def dist(a, b):
        return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))

    radius = max(2, min(4, round(max(width, height) / 35)))
    offsets = [v * 0.5 for v in range(-2 * radius, 2 * radius + 1)]
    prior = []
    scores = []
    j = 0
    for t in scans:
        while j < len(knots) - 2 and t > knots[j + 1][0]:
            j += 1
        first, last = knots[j], knots[j + 1]
        centre = first[1] + (last[1] - first[1]) * (t - first[0]) / max(0.01, last[0] - first[0])
        prior.append(centre)
        # Use the scene immediately beyond the boundary, independently on each scanline.
        far = colour(t, centre - direction * (radius + 3))
        values = []
        for offset in offsets:
            at = centre + direction * offset
            outside = colour(t, at - direction * 1.4)
            inside = colour(t, at + direction * 1.4)
            contrast = dist(inside, outside)
            # At a reflective rim there may be a stronger *internal* edge; the outside of the true edge resembles the adjacent scene.
            values.append(contrast + 0.36 * dist(inside, far) - 0.78 * dist(outside, far) - 4 * abs(offset))
        scores.append(values)
    costs = scores[0]
    back = []
    for values in scores[1:]:
        parents = []
        current = []
        for n, score in enumerate(values):
            k = max(range(max(0, n - 2), min(len(offsets), n + 3)), key=lambda i: costs[i] - 22 * abs(n - i))
            parents.append(k)
            current.append(costs[k] + score - 22 * abs(n - k))
        costs = current
        back.append(parents)
    path = [max(range(len(costs)), key=costs.__getitem__)]
    for parents in reversed(back):
        path.append(parents[path[-1]])
    line = [p + direction * offsets[k] for p, k in zip(prior, reversed(path))]
    for i, t in enumerate(scans):
        lo = max(0, i - 2)
        hi = min(len(line), i + 3)
        border = sum(line[lo:hi]) / (hi - lo)
        for s in range(
            max(0, math.floor(min(prior[i], border) - radius - 2)),
            min(extent, math.ceil(max(prior[i], border) + radius + 2)) + 1,
        ):
            at = coord(t, s)
            # A single monotone coverage value prevents scratches on the opaque side.
            opacity = max(0.0, min(1.0, 0.5 + direction * (s - border)))
            weight = min(1.0, (i + 1) / 2, (len(scans) - i) / 2)
            selected[at] = round((1 - weight) * selected[at] + weight * 255 * opacity)


def _apply_edits(photo, current, edit):
    width, height = current.size

    def coverage(polygons):
        result = Image.new("L", (width * 4, height * 4))
        pen = ImageDraw.Draw(result)
        for polygon in polygons:
            pen.polygon(
                [
                    (round(((width - 1) * x / 1000 + 0.5) * 4), round(((height - 1) * y / 1000 + 0.5) * 4))
                    for x, y in polygon
                ],
                fill=255,
            )
        return result.resize(current.size, Image.Resampling.BOX)

    updated = ImageChops.lighter(ImageChops.subtract(current, coverage(edit.erase)), coverage(edit.restore))
    for edge in edit.edges:
        _repair_optical_edge(photo, updated, edge)
    return updated


def _isolated_pixels(crop, mask):
    """Composite the photographed product while unmixing its former background from subpixel edges."""
    w, h = crop.size
    matte = mask.copy()
    straight = crop.copy()
    clean = straight.load()
    # Optical background removal can alter only subpixel coverage that is
    # already present in the matte. A colour similar to the room may also be a
    # real opaque reflection; treating it as coverage punches holes in the rim.
    edge = mask.point(lambda alpha: 255 if 0 < alpha < 252 else 0)
    smooth = mask.filter(ImageFilter.GaussianBlur(1.4)).load()
    src = crop.load()
    selected = mask.load()
    result = matte.load()
    for n, active in enumerate(edge.tobytes()):
        if not active:
            continue
        x, y = n % w, n // w
        dx = smooth[min(w - 1, x + 1), y] - smooth[max(0, x - 1), y]
        dy = smooth[x, min(h - 1, y + 1)] - smooth[x, max(0, y - 1)]
        length = math.hypot(dx, dy)
        if length < 8:
            continue
        dx, dy = dx / length, dy / length

        def at(distance):
            return max(0, min(w - 1, round(x + dx * distance))), max(0, min(h - 1, round(y + dy * distance)))

        inside = max((at(d) for d in (1, 2, 3, 4)), key=lambda pt: (selected[pt], smooth[pt]))
        outside = next((pt for distance in (3, 4, 5, 2, 1) if selected[pt := at(-distance)] < 65), None)
        if outside is None or selected[inside] < 180 or smooth[inside] < 90:
            continue
        foreground, background, pixel = src[inside], src[outside], src[x, y]
        vector = [f - b for f, b in zip(foreground, background)]
        energy = sum(d * d for d in vector)
        if energy < 420:
            continue
        alpha = sum((c - b) * d for c, b, d in zip(pixel, background, vector)) / energy
        residual = max(abs(c - b - alpha * d) for c, b, d in zip(pixel, background, vector))
        # Undo the original background's contribution to partially covered camera
        # pixels using C=aF+(1-a)B. Keep distinct observed highlights unmodified.
        if -0.15 <= alpha < 0.97 and residual <= max(7, min(20, math.sqrt(energy) * 0.24)):
            if alpha <= 0.16:
                result[x, y] = 0
            else:
                clean[x, y] = tuple(
                    max(0, min(255, round((c - (1 - alpha) * b) / alpha))) for c, b in zip(pixel, background)
                )
                result[x, y] = min(result[x, y], round(255 * alpha))
    return Image.composite(straight, Image.new("RGB", crop.size, NEUTRAL), matte)


def _card(crop, mask):
    bounds = mask.getbbox()
    if bounds is None or mask.histogram()[255] < max(18, crop.width * crop.height * 0.025):
        return None
    real = _isolated_pixels(crop, mask).crop(bounds)
    size = tuple(max(1, round(v * 650 / max(real.size))) for v in real.size)
    real = real.resize(size, Image.Resampling.BILINEAR if max(real.size) < 650 else Image.Resampling.LANCZOS)
    card = Image.new("RGB", (736, 736), NEUTRAL)
    card.paste(real, ((736 - size[0]) // 2, (736 - size[1]) // 2))
    return card


def _paired_detail(crop, isolated, bounds):
    original = crop.crop(bounds)
    after = isolated.crop(bounds)
    left = ImageOps.contain(original, (398, 408), Image.Resampling.NEAREST)
    right = after.resize(left.size, Image.Resampling.NEAREST)
    board = Image.new("RGB", (left.width * 2 + 8, left.height), (96, 96, 96))
    board.paste(left, (0, 0))
    board.paste(right, (left.width + 8, 0))
    return board


async def _cached(ctx, key):
    async with get_db_session() as db:
        return await db.scalar(
            owned_ready_query(ctx.user_id, ctx.workspace_id)
            .where(FileAsset.id == _reference_id(ctx, key))
            .with_only_columns(FileAsset.id)
        )


async def _identical_prior_card(ctx, verified, earlier):
    """Keep the same camera input when a newer reference review produced byte-identical pixels."""
    if not earlier:
        return verified
    ids = list(dict.fromkeys([verified, *earlier]))
    async with get_db_session() as db:
        owned = owned_ready_query(ctx.user_id, ctx.workspace_id).where(
            FileAsset.session_id == ctx.session_id, FileAsset.id.in_(ids), FileAsset.mime == "image/png")
        rows = {row.id: row for row in (await db.scalars(owned)).all()}
    current = rows.get(verified)
    if not current:
        return verified
    possible = [rows[asset] for asset in ids[1:] if asset in rows and rows[asset].size == current.size]
    if possible:
        try:
            storage = get_oss()
            data = await storage.get_object(current.oss_key)
            for candidate in possible:
                if await storage.get_object(candidate.oss_key) == data:
                    return candidate.id
        except (OssError, OssNotConfigured, OSError):
            pass  # The newly approved card is independently usable.
    return verified


async def _read(ctx, ref):
    from light.product_story import _asset

    row = await _asset(ctx, ref)
    if not row.mime.startswith("image/"):
        raise RemakeStopped("商品参考需要一张照片或可读取的视频帧。")
    return _image(await get_oss().get_object(row.oss_key))


def _card_key(ref, appearance):
    tag = hashlib.sha256(f"{VERSION}\0{ref}\0{appearance}".encode()).hexdigest()[:26]
    return f"commercial-product-card-{tag}"


async def _isolate_one(ctx, ref, appearance, *, evidence=None, language=None):
    key = _card_key(ref, appearance)
    if prior := await _cached(ctx, key):
        return prior
    source = await _read(ctx, ref)
    language = language or for_text(appearance, "en")

    async def ask(name, schema, guide, images, **data):
        response = await reference_quality.ask(
            ctx,
            name,
            schema,
            guide + " Write explanations and corrections in the requested language.",
            {"appearance": appearance, "language": language, **data},
            images,
            max_tokens=4800 if schema in (Outline, MaskEdit) else 2000,
            timeout=90,
            reasoning_effort="medium" if schema is CardReview else "low",
        )
        return schema.model_validate(response["report"])

    location = await ask("commercial_locate_product", Location, LOCATE, [ref])
    if not location.usable:
        return None
    if location.studio and _studio_border(source):
        report = await ask(
            "commercial_verify_studio", CardReview, REVIEW, [ref], candidate="Unmodified source image"
        )
        if report.accepted:
            if evidence is not None:
                evidence["unmodified_studio_packshot"] = True
            return await _reference_asset(ctx, key, _png(source), mime="image/png")
    crop = _crop(source, location.box)
    if crop is None:
        return None

    async def view(image, label):
        data = _png(image)
        digest = hashlib.sha256(data).hexdigest()[:16]
        return await _reference_asset(ctx, f"{key}-{label}-{digest}", data, mime="image/png")

    crop_ref, grid_ref = await asyncio.gather(view(_magnify(crop), "crop"), view(_grid(crop), "grid"))

    async def trace_full(previous=None, correction=""):
        outline = await ask(
            "commercial_trace_product",
            Outline,
            TRACE,
            [crop_ref, grid_ref, *([previous] if previous else [])],
            visible=location.evidence,
            previous_mask_correction=correction,
        )
        return await asyncio.to_thread(_mask, crop, outline)

    async def repair(detail, current, visible, surgical):
        bounds = _detail_bounds(crop, detail.box)
        if bounds is None:
            return None
        original = crop.crop(bounds)
        prior = visible.crop(bounds)
        source_detail, grid_detail, prior_detail = await asyncio.gather(
            view(_magnify(original), "detail"), view(_grid(original), "grid"), view(_magnify(prior), "prior")
        )
        images = [source_detail, grid_detail, prior_detail]
        if surgical:
            edits = await ask("commercial_correct_matte", MaskEdit, EDIT, images, problem=detail.defect)
            fixed = _apply_edits(original, current.crop(bounds), edits)
        else:
            outline = await ask(
                "commercial_refine_product_edge", Outline, DETAIL_TRACE, images, problem=detail.defect
            )
            fixed = await asyncio.to_thread(_mask, original, outline, detail=True)
        # The defect is inside the zoom rectangle. Retain the unmodified pixels at
        # its margin so a partial polygon at the crop border never creates a seam.
        inset = 2 if min(original.size) > 8 else 1
        x, y, r, b = bounds
        keep = (
            inset if x else 0,
            inset if y else 0,
            fixed.width - inset if r < crop.width else fixed.width,
            fixed.height - inset if b < crop.height else fixed.height,
        )
        return (x + keep[0], y + keep[1]), fixed.crop(keep)

    mask = await trace_full()
    registration = []
    if evidence is not None:
        evidence.update(camera_crop_pixels=list(crop.size), matte_checks=[])
    for attempt in range(3):
        card = await asyncio.to_thread(_card, crop, mask)
        if card is None:
            return None
        candidate = await view(card, "candidate")
        review = await ask(
            "commercial_verify_product_card",
            CardReview,
            REVIEW,
            [ref, crop_ref, candidate, *registration],
            visible=location.evidence,
            camera_crop_pixels=[crop.width, crop.height],
        )
        if evidence is not None:
            evidence["matte_checks"].append({"candidate": candidate, "registered_pairs": list(registration),
                                             "review": review.model_dump()})
        if review.accepted:
            return await _reference_asset(ctx, key, _png(card), mime="image/png")
        if attempt == 2:
            break
        if review.details:
            previous = await asyncio.to_thread(_isolated_pixels, crop, mask)
            pieces = await asyncio.gather(
                *(repair(detail, mask, previous, attempt > 0) for detail in review.details)
            )
            for piece in pieces:
                if piece:
                    position, changed = piece
                    mask.paste(changed, position)
            isolated = await asyncio.to_thread(_isolated_pixels, crop, mask)
            bounds = [_detail_bounds(crop, detail.box) for detail in review.details]
            registration = await asyncio.gather(
                *(view(_paired_detail(crop, isolated, box), "comparison") for box in bounds if box)
            )
        elif attempt == 0:
            mask = await trace_full(candidate, review.correction or review.reason)
        else:
            break
    return None


async def isolate(ctx, refs, appearance, *, language=None):
    """Return owned, verified product-only reference IDs; live-action review keeps its original refs."""
    from question.runtime import RunRevoked

    original = list(dict.fromkeys(refs))[:2]
    results = await asyncio.gather(
        *(_isolate_one(ctx, ref, appearance, language=language) for ref in original), return_exceptions=True
    )
    for result in results:
        if isinstance(result, (RunRevoked, asyncio.CancelledError)):
            raise result
        if isinstance(result, BaseException) and not isinstance(
            result, (RemakeStopped, OSError, OssError, OssNotConfigured)
        ):
            raise result
    verified = [result for result in results if isinstance(result, str)]
    if not verified:
        labels = (
            READ_ERRORS if results and all(isinstance(result, BaseException) for result in results) else ERRORS
        )
        locale = language if language in labels else for_text(appearance, "zh")
        raise RemakeStopped(labels[locale or "zh"])
    return verified


async def filming_inputs(ctx, plan, *, prefix=None, run=None):
    """Bind every verified card to its camera source, recovering only within that same selected upload."""
    from light import commercial_plan, commercial_product_photograph
    from light.remake import _material_refs
    from question.runtime import RunRevoked

    read_errors = (OSError, OssError, OssNotConfigured, RemakeStopped)
    try:
        roles = await commercial_plan.filming_reference_roles(ctx, plan)
    except read_errors as exc:
        raise RemakeStopped(READ_ERRORS[plan.language]) from exc
    groups = {}
    for role, (upload, point, frames) in roles.items():
        if role.startswith("product_"):
            selected = groups.setdefault(upload, {})
            # Keep an empty selected view distinct from a frame explicitly declined by visual review.
            selected[point] = next(iter(frames), None)
    checkpoints = run.setdefault("director_decisions", {}) if run is not None else {}
    stage_reference = bool(prefix and run is not None and getattr(plan, "score_revision", 1) == 2)

    async def remember(operation, identifiers, inspect):
        payload = "\0".join((VERSION, plan.product, plan.appearance, *identifiers))
        key = f"commercial-product:{operation}:{hashlib.sha256(payload.encode()).hexdigest()[:26]}"
        if key in checkpoints:
            previous = checkpoints[key]
            if previous.get("card") and not await _cached(ctx, _card_key(previous["source"], plan.appearance)):
                raise RemakeStopped(READ_ERRORS[plan.language])
            if previous.get("approved") and previous.get("photograph"):
                from light.product_story import _asset

                row = await _asset(ctx, previous["photograph"])
                if row.session_id != ctx.session_id or not row.mime.startswith("image/"):
                    raise RemakeStopped(READ_ERRORS[plan.language])
            return previous
        verdict = await inspect()
        if run is not None and prefix is not None:
            from light.production import save_director_decision

            verdict = await save_director_decision(ctx, prefix, 1, key, verdict)
        checkpoints[key] = verdict
        return verdict

    async def inspect_view(upload, source):
        async def measure():
            evidence = {}
            card = await _isolate_one(ctx, source, plan.appearance, evidence=evidence, language=plan.language)
            earlier = [v.get("camera_card") or v["card"] for k, v in checkpoints.items()
                       if k.startswith("commercial-product:isolation:") and v.get("approved") and v.get("card")
                       and (v.get("upload"), v.get("source")) == (upload, source)]
            camera = await _identical_prior_card(ctx, card, earlier) if card and earlier else card
            return {"upload": upload, "source": source, "card": card, "approved": bool(card),
                    **({"camera_card": camera} if camera != card else {}), **evidence}

        return await remember("isolation", (upload, source), measure)

    async def same_product(upload, primary, alternative):
        async def compare():
            answer = await reference_quality.ask(
                ctx,
                "commercial_verify_alternate_product",
                AlternateView,
                SAME_PRODUCT,
                {"product": plan.product, "appearance": plan.appearance, "selected_frames": len(primary),
                 "language": plan.language},
                [*primary, alternative],
                max_tokens=1300,
                timeout=90,
                reasoning_effort="low",
            )
            result = AlternateView.model_validate(answer["report"])
            return {
                "upload": upload,
                "source": alternative,
                "compared_with": primary,
                "approved": result.same_product and result.identifying_surfaces_visible,
                "evidence": result.evidence,
            }

        verdict = await remember("source-identity", (upload, *primary, alternative), compare)
        return verdict["approved"]

    def collect(results):
        verified = []
        failures = []
        for result in results:
            if isinstance(result, (RunRevoked, asyncio.CancelledError)):
                raise result
            if isinstance(result, BaseException):
                if not isinstance(result, read_errors):
                    raise result
                failures.append(result)
            else:
                verified.extend(result)
        if not verified and failures:
            raise failures[0]
        return verified

    async def group(upload, selected):
        primary = list(dict.fromkeys(source for source in selected.values() if source))
        if not primary:
            raise RemakeStopped(READ_ERRORS[plan.language])

        async def neutral(sources):
            async def take_photograph():
                return await commercial_product_photograph.photograph(ctx, prefix, plan, sources, remember=remember)

            receipt = await remember("neutral-photograph", (upload, *sources), take_photograph)
            return [(source, receipt["photograph"]) for source in sources] if receipt["approved"] else []

        width = commercial_plan.REFERENCE_WIDTH if plan.video_reference_version == 2 else None
        other_points = [p for p in ("opening", "middle", "late") if p not in selected and upload not in primary]
        if stage_reference and upload in getattr(plan, "obscured_product_videos", []):
            # The storyboard saw an external obstruction in every sampled view. A matte cannot reveal covered material.
            # Establish the product across all three real angles before commissioning and independently checking a neutral view.
            try:
                additional = await asyncio.gather(*(_material_refs(ctx, [upload], video_sample=p, detail_width=width)
                                                    for p in other_points))
            except read_errors:
                additional = []
            sources = list(dict.fromkeys([*primary, *(source for frames in additional for source in frames)]))
            if len(sources) == 3 and upload not in sources and (complete := await neutral(sources)):
                return complete

        async def pair(source):
            verdict = await inspect_view(upload, source)
            if not verdict["approved"]:
                return []
            card = verdict["card"]
            if old := verdict.get("camera_card"):
                async with get_db_session() as db:
                    still_available = await db.scalar(owned_ready_query(ctx.user_id, ctx.workspace_id).where(
                        FileAsset.id == old, FileAsset.session_id == ctx.session_id).with_only_columns(FileAsset.id))
                card = still_available or card
            if stage_reference and not verdict.get("unmodified_studio_packshot"):
                async def check_geometry():
                    return await commercial_product_photograph.assess_camera_view(ctx, plan, source, card)

                usable = await remember("camera-view-v1", (upload, source, card, plan.carry, plan.contact), check_geometry)
                if not usable["approved"]:
                    return []
            return [(verdict["source"], card)]

        initial = await asyncio.gather(*(pair(source) for source in primary), return_exceptions=True)
        verified = collect(initial)
        if verified:
            return verified
        # Only a definitive visual rejection of all available selections reaches this point.
        # Missing media or a failed vision service must never cause a different source to be used.
        seen = set(primary)
        matched = list(primary)
        for point in other_points:
            images = await _material_refs(ctx, [upload], video_sample=point, detail_width=width)
            for alternative in images:
                if alternative in seen or alternative == upload:
                    continue
                seen.add(alternative)
                if await same_product(upload, primary, alternative):
                    matched.append(alternative)
                    if recovered := await pair(alternative):
                        return recovered
        if stage_reference:
            return await neutral(matched)
        return []

    attempts = await asyncio.gather(
        *(group(upload, selected) for upload, selected in groups.items()), return_exceptions=True
    )
    try:
        verified = collect(attempts)
    except read_errors as exc:
        raise RemakeStopped(READ_ERRORS[plan.language]) from exc
    if not verified:
        raise RemakeStopped((ERRORS if groups else READ_ERRORS)[plan.language])
    originals = [raw for raw, _ in verified]
    cards = list(dict.fromkeys(card for _, card in verified))
    return (
        originals,
        cards,
        roles["protagonist"][2] if "protagonist" in roles else [],
        roles["setting"][2] if "setting" in roles else [],
    )
