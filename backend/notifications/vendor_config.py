"""Deployment-only JPush vendor options, never supplied by a push recipient."""
import json

VENDORS = ("xiaomi", "huawei", "honor", "oppo", "vivo", "meizu", "fcm")
FIELDS = {
    "xiaomi": {"channel_id"},
    "huawei": {"channel_id", "category", "importance", "receipt_id"},
    "honor": {"importance"},
    "oppo": {"channel_id", "category", "notify_level"},
    "vivo": {"category", "callback_id"},
    "meizu": set(),
    "fcm": set(),
}


def vendor_options(raw=""):
    # Prefer the ordinary connection while available, then fall back to the
    # device's vendor. This policy needs no paid forced-vendor capability and
    # keeps native foreground suppression active while the app is connected.
    result = {vendor: {"distribution": "secondary_push"} for vendor in VENDORS}
    for vendor in VENDORS:
        if vendor != "fcm":
            result[vendor]["distribution_fcm"] = "secondary_pns_push"
    if not raw.strip():
        return result
    try:
        if len(raw) > 4096:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data, dict) or data.keys() - FIELDS.keys():
            raise ValueError
        for vendor, values in data.items():
            if not isinstance(values, dict) or values.keys() - FIELDS[vendor]:
                raise ValueError
            for field, value in values.items():
                if field == "notify_level":
                    if type(value) is not int or value not in {1, 2, 16} or not values.get("category"):
                        raise ValueError
                elif not isinstance(value, str) or not value.strip() or len(value) > 256:
                    raise ValueError
                if field == "importance" and value not in {"LOW", "NORMAL", "HIGH"}:
                    raise ValueError
                if vendor == "honor" and value == "HIGH" and field == "importance":
                    raise ValueError
            result[vendor].update(values)
        return result
    except (ValueError, TypeError):
        # Do not include the raw configuration in startup logs.
        raise ValueError("Invalid BOSSIP_JPUSH_VENDOR_OPTIONS; see docs/ANDROID_PUSH_VENDORS.md") from None
