"""Bounded OKF 0.1 exchange parsing. No extraction to disk or runtime authority.

The format follows llm-wiki-compiler's OKF mapping (see UPSTREAM_LICENSE).
Unknown producer metadata is retained as inert JSON, including foreign gates.
"""
import io
import json
import posixpath
import re
import stat
import zipfile
from urllib.parse import unquote, urlsplit

import yaml

from wiki_compiler.hashing import canonical_hash, text_hash

MAX_UPLOAD = 8 * 1024 * 1024
MAX_TOTAL = 12 * 1024 * 1024
MAX_FILE = 256 * 1024
MAX_ENTRIES = 500


class ExchangeError(ValueError):
    pass


class BoundedLoader(yaml.SafeLoader):
    def __init__(self, stream):
        super().__init__(stream)
        self.depth, self.nodes = 0, 0

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise ExchangeError("wiki_exchange_yaml_alias")
        self.depth, self.nodes = self.depth + 1, self.nodes + 1
        if self.depth > 20 or self.nodes > 20000:
            raise ExchangeError("wiki_exchange_metadata_limit")
        try:
            return super().compose_node(parent, index)
        finally:
            self.depth -= 1

    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if any(not isinstance(key, str) for key in keys) or len(set(keys)) != len(keys):
            raise ExchangeError("wiki_exchange_duplicate_key")
        return super().construct_mapping(node, deep=deep)


# Keep dates and other standard scalar values as strings for lossless JSON storage.
BoundedLoader.yaml_implicit_resolvers = {key: [pair for pair in pairs if pair[0] != "tag:yaml.org,2002:timestamp"]
                                       for key, pairs in yaml.SafeLoader.yaml_implicit_resolvers.items()}


def safe_path(path):
    if (not isinstance(path, str) or not path or len(path) > 512 or path.startswith("/") or "\\" in path
            or any(ord(char) < 32 for char in path) or re.match(r"^[A-Za-z]:", path)
            or any(part in {"", ".", ".."} for part in path.split("/"))):
        raise ExchangeError("wiki_exchange_unsafe_path")
    return path


def frontmatter(text):
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    if not match:
        raise ExchangeError("wiki_exchange_frontmatter_required")
    try:
        meta = yaml.load(match[1], Loader=BoundedLoader)
        if not isinstance(meta, dict):
            raise ExchangeError("wiki_exchange_invalid_metadata")
        # Reject sets, binaries, NaN, cyclic/unsupported native YAML objects.
        json.dumps(meta, ensure_ascii=False, allow_nan=False)
    except (yaml.YAMLError, TypeError, ValueError) as exc:
        if isinstance(exc, ExchangeError):
            raise
        raise ExchangeError("wiki_exchange_invalid_metadata") from exc
    return meta, text[match.end():]


def canonical_body(body):
    return re.sub(r"\n+#\s+Citations\b[\s\S]*$", "", body).rstrip() + "\n"


class PortableDumper(yaml.SafeDumper):
    def ignore_aliases(self, data):
        return True


def render_markdown(meta, body):
    return "---\n" + yaml.dump(meta, Dumper=PortableDumper, allow_unicode=True, sort_keys=False).rstrip() + "\n---\n" + body


def _read_zip(data):
    if len(data) > MAX_UPLOAD:
        raise ExchangeError("wiki_exchange_upload_limit")
    files, total = {}, 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES:
                raise ExchangeError("wiki_exchange_entry_limit")
            seen = set()
            for entry in entries:
                path = safe_path(entry.filename.rstrip("/") if entry.is_dir() else entry.filename)
                if path.casefold() in seen:
                    raise ExchangeError("wiki_exchange_duplicate_path")
                seen.add(path.casefold())
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or entry.flag_bits & 1:
                    raise ExchangeError("wiki_exchange_unsafe_archive")
                if entry.is_dir():
                    continue
                total += entry.file_size
                if (entry.file_size > MAX_FILE or total > MAX_TOTAL
                        or entry.file_size > max(1, entry.compress_size) * 200):
                    raise ExchangeError("wiki_exchange_size_limit")
                if not path.lower().endswith((".md", ".txt", ".json")):
                    raise ExchangeError("wiki_exchange_unsupported_file")
                with archive.open(entry) as stream:
                    body = stream.read(MAX_FILE + 1)
                if len(body) != entry.file_size or len(body) > MAX_FILE:
                    raise ExchangeError("wiki_exchange_size_limit")
                files[path] = body.decode("utf-8-sig")
    except (zipfile.BadZipFile, UnicodeError, RuntimeError, NotImplementedError) as exc:
        raise ExchangeError("wiki_exchange_invalid_zip") from exc
    return files


def reference_issues(files):
    issues = []
    for path, body in files.items():
        # References inside code examples do not describe dependencies.
        rendered = re.sub(r"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`", "", body)
        for target in re.findall(r"(?<!!)\[[^\]\n]*\]\(([^\s)]+)(?:\s+[^)]*)?\)", rendered):
            try:
                parsed = urlsplit(target.strip("<>"))
            except ValueError as exc:
                raise ExchangeError("wiki_exchange_invalid_reference") from exc
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            decoded = unquote(parsed.path)
            candidate = decoded.lstrip("/") if decoded.startswith("/") else posixpath.normpath(posixpath.join(posixpath.dirname(path), decoded))
            safe_path(candidate)
            if candidate not in files:
                issues.append({"path": path, "target": candidate, "code": "missing_reference"})
    return issues


def parse_okf(data):
    files = _read_zip(data)
    if "index.md" not in files:
        raise ExchangeError("wiki_exchange_index_required")
    manifest, _ = frontmatter(files["index.md"])
    if str(manifest.get("okf_version")) != "0.1":
        raise ExchangeError("wiki_exchange_version_unsupported")
    documents, attachments = [], {}
    warnings = reference_issues(files)
    for path, text in sorted(files.items()):
        if path in {"index.md", "log.md"} or path.startswith("references/") or not path.endswith(".md"):
            attachments[path] = text
            continue
        meta, body = frontmatter(text)
        if not isinstance(meta.get("type"), str) or not meta["type"].strip():
            raise ExchangeError("wiki_exchange_type_required")
        title = meta.get("title") or posixpath.basename(path)[:-3]
        if not isinstance(title, str) or not 1 <= len(title.strip()) <= 160 or not body.strip():
            raise ExchangeError("wiki_exchange_invalid_document")
        producer = meta.get("x-llmwiki", {})
        if isinstance(producer, dict) and producer.get("contentHash") and producer["contentHash"] != text_hash(canonical_body(body)):
            # Upstream hashes before wikilink rewriting. Report, never silently
            # reinterpret the producer hash as locally verified provenance.
            warnings.append({"path": path, "code": "producer_hash_mismatch"})
        documents.append({"path": path, "frontmatter": meta, "body": body, "title": title.strip(),
                          "content_hash": canonical_hash({"frontmatter": meta, "body": body})})
    if not documents:
        raise ExchangeError("wiki_exchange_no_documents")
    return {"manifest": manifest, "attachments": attachments, "documents": documents,
            "warnings": warnings, "content_hash": canonical_hash(files)}


def zip_files(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(files.items()):
            safe_path(path)
            archive.writestr(path, content)
    return stream.getvalue()
