"""Optional UI copy, kept separate from Skill identity and model instructions."""
from collections.abc import Mapping

DISPLAY_FILE = "openbox-display.json"
DISPLAY_MAX_BYTES = 16_384
DISPLAY_LIMITS = {"display_name": 120, "display_description": 1000}
DISPLAY_LOCALES = ("zh-CN", "en-US")


def display_fields(value, *, strict: bool = False, package: bool = False) -> dict:
    """Bound untrusted display metadata; strict mode validates author input."""
    result = {}
    if not isinstance(value, Mapping):
        if strict:
            raise ValueError("Skill display metadata must be an object")
        return result
    prefixes = ("", "package_") if package else ("",)
    for prefix in prefixes:
        for field, limit in DISPLAY_LIMITS.items():
            key = prefix + field
            if key not in value or value[key] is None:
                continue
            translations = value[key]
            if not isinstance(translations, Mapping):
                if strict:
                    raise ValueError(f"{key} must contain zh-CN / en-US text")
                continue
            localized = {}
            for language, text in translations.items():
                valid = (language in DISPLAY_LOCALES and isinstance(text, str)
                         and len(text.strip()) <= limit)
                if not valid:
                    if strict:
                        raise ValueError(f"{key} supports zh-CN / en-US, at most {limit} characters each")
                    continue
                if text.strip():
                    localized[language] = text.strip()
            if localized:
                result[key] = localized
    return result


def with_package_display(row: dict, package: dict, *, single: bool) -> dict:
    fields = display_fields(package)
    return {**row, **{f"package_{key}": value for key, value in fields.items()},
            **({key: {**row.get(key, {}), **value} for key, value in fields.items()} if single else {})}
