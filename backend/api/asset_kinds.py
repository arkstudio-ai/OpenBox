"""One place that decides what kind of thing an asset is.

The resource centre filters by kind and the UI picks an icon and a preview
mode from the same answer, so the classification cannot live in two places.
MIME wins when it is specific; `application/octet-stream` (what `file
--brief` hands back for plenty of real formats) falls through to the
extension.
"""

KINDS = ("image", "video", "audio", "document", "archive", "code", "other")

_EXT_KIND: dict[str, str] = {
    **{e: "image" for e in ("png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "avif", "ico", "heic", "tiff")},
    **{e: "video" for e in ("mp4", "webm", "mov", "m4v", "avi", "mkv", "ogv")},
    **{e: "audio" for e in ("mp3", "wav", "ogg", "m4a", "flac", "aac", "opus")},
    **{e: "document" for e in ("pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "rtf", "csv", "odt")},
    **{e: "archive" for e in ("zip", "tar", "gz", "tgz", "rar", "7z", "bz2", "xz", "zst")},
    **{
        e: "code"
        for e in (
            "ts", "tsx", "js", "jsx", "mjs", "cjs", "py", "go", "rs", "java", "kt", "c", "cc", "cpp",
            "h", "hpp", "css", "scss", "html", "htm", "json", "yaml", "yml", "toml", "sh", "rb",
            "php", "sql", "vue", "swift", "ipynb", "xml",
        )
    },
}

_MIME_PREFIX_KIND = (("image/", "image"), ("video/", "video"), ("audio/", "audio"))

_MIME_KIND: dict[str, str] = {
    "application/pdf": "document",
    "application/msword": "document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "document",
    "application/vnd.ms-excel": "document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "document",
    "application/vnd.ms-powerpoint": "document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "document",
    "application/zip": "archive",
    "application/x-tar": "archive",
    "application/gzip": "archive",
    "application/x-7z-compressed": "archive",
    "application/x-rar-compressed": "archive",
    "application/json": "code",
    "application/xml": "code",
    "text/html": "code",
    "text/css": "code",
    "text/javascript": "code",
    "text/csv": "document",
    "text/markdown": "document",
    "text/plain": "document",
}


def extension(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def kind_of(mime: str, name: str) -> str:
    """Classify an asset into one of KINDS."""
    m = (mime or "").split(";")[0].strip().lower()
    for prefix, kind in _MIME_PREFIX_KIND:
        if m.startswith(prefix):
            return kind
    by_ext = _EXT_KIND.get(extension(name))
    # A generic MIME says nothing; trust the name before falling back to it.
    if m in ("", "application/octet-stream", "binary/octet-stream"):
        return by_ext or "other"
    if m in _MIME_KIND:
        return _MIME_KIND[m]
    if m.startswith("text/"):
        return by_ext or "document"
    return by_ext or "other"
