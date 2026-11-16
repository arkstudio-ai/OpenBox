"""Volcengine Ark Seedream behind image_gen: what the endpoint takes, and its price tier.

Ark's ``/api/v3/images/generations`` speaks the OpenAI Images dialect closely
enough that image_gen's synchronous "openai" channel carries it; this module
holds only the differences. Everything below was measured against
``doubao-seedream-5-0-pro-260628`` on 2026-11-16 (docs/VOLCENGINE_IMAGE_CHANNEL.md):

* editing is the same endpoint with ``image: [url, ...]``; ``/images/edits`` is 404;
* ``size`` is exact pixels, area 921,600 to 4,624,220; ``auto`` is refused;
* one image per call (``n=2`` returned one), ``output_format`` jpeg or png only;
* ``background=transparent`` needs exactly one input image;
* without ``watermark: false`` every image carries an "AI生成" corner mark.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

MODEL_PREFIX = "doubao-seedream-"
MIN_PIXELS = 921_600
MAX_PIXELS = 4_624_220
#: Ark's price list splits output images at 2.61 megapixels (0.30 / 0.60 CNY).
TIER_SPLIT_PIXELS = 2_610_000
MAX_INPUTS = 10
MAX_INPUT_MB = 10
#: Ark downloads the inputs itself while the request is open.
INPUT_URL_TTL_SECONDS = 900
_DEFAULT_LONG_EDGE = 2048


def is_model(model: str) -> bool:
    return model.lower().startswith(MODEL_PREFIX)


class ImageRequestRefused(ValueError):
    """Refused here before any paid call; the message names only the request."""


@dataclass(frozen=True)
class PlannedRequest:
    #: WIDTHxHEIGHT sent to Ark; never "auto".
    size: str
    #: Billing tier in billing/rates.json: "1k" up to 2.61 MP, "2k" above.
    tier: str
    note: str | None = None


def unsupported(*, n: int, mask: bool, background: str | None, input_count: int,
                output_format: str) -> str | None:
    """Why Seedream on Ark cannot take this request, or None."""
    if n != 1:
        return "Seedream makes one image per call; call image_gen again for each variant"
    if mask:
        return "Seedream has no mask input; describe the region to change in the prompt instead"
    if output_format not in {"png", "jpeg"}:
        return "Seedream outputs png or jpeg only"
    if background == "transparent" and input_count != 1:
        return ("Seedream makes a transparent background only when editing exactly one input image; "
                "generate the picture first, then edit it with background=transparent")
    if input_count > MAX_INPUTS:
        return f"Seedream accepts at most {MAX_INPUTS} input images"
    return None


def _fit(width: int, height: int) -> tuple[int, int]:
    """The same shape scaled into Ark's allowed area, on an 8px grid."""
    area = width * height
    if area > MAX_PIXELS:
        scale = (MAX_PIXELS / area) ** 0.5
        return tuple(max(8, int(side * scale) // 8 * 8) for side in (width, height))
    if area < MIN_PIXELS:
        scale = (MIN_PIXELS / area) ** 0.5
        return tuple(-(-int(side * scale + 0.999) // 8) * 8 for side in (width, height))
    return width, height


def plan_request(*, size: str, input_size: tuple[int, int] | None = None) -> PlannedRequest:
    """Resolve the tool's size into the exact pixels Ark is asked for."""
    match = re.fullmatch(r"(\d+)x(\d+)", size)
    if size != "auto" and not match:
        raise ImageRequestRefused("size must be 'auto' or WIDTHxHEIGHT (for example 2048x1152)")
    note = None
    if match:
        width, height = int(match.group(1)), int(match.group(2))
        if min(width, height) <= 0:
            raise ImageRequestRefused("size needs a positive width and height")
        fitted = _fit(width, height)
        if fitted != (width, height):
            limit = (f"above Seedream's limit of {MAX_PIXELS:,} pixels" if width * height > MAX_PIXELS
                     else f"below Seedream's minimum of {MIN_PIXELS:,} pixels")
            note = f"size {width}x{height} is {limit}; it was made at {fitted[0]}x{fitted[1]}, the same shape"
        width, height = fitted
    elif input_size and min(input_size) > 0:
        # "auto" while editing: keep the source's shape at the default long edge.
        scale = _DEFAULT_LONG_EDGE / max(input_size)
        width, height = _fit(*(max(8, round(side * scale / 8) * 8) for side in input_size))
    else:
        width = height = _DEFAULT_LONG_EDGE
    tier = "1k" if width * height <= TIER_SPLIT_PIXELS else "2k"
    return PlannedRequest(f"{width}x{height}", tier, note)
