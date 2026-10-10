"""Portable projections from the same permission-checked snapshot (no I/O)."""
import json
import re
from xml.etree import ElementTree as ET

from wiki_compiler.exchange import ExchangeError, canonical_body, parse_okf, render_markdown, safe_path, zip_files
from wiki_compiler.hashing import text_hash


def native_links(body, page, pages, paths):
    """Resolve same-project wikilinks and leave code/literal escapes untouched."""
    def replace(match):
        target, _, alias = match[1].partition("|")
        candidates = [item for item in pages if item.get("project_id") == page.get("project_id")
                      and target in {item["slug"], item["title"]}]
        if len(candidates) != 1:
            return match[0]
        linked = candidates[0]
        label = (alias or linked["title"]).replace("[", "\\[").replace("]", "\\]")
        return "[" + label + "](/" + paths[linked["id"]] + ")"
    parts = re.split(r"(```[\s\S]*?```|~~~[\s\S]*?~~~|`+[^`\n]*`+)", body)
    return "".join(part if index % 2 else re.sub(r"(?<![\\!])\[\[([^\]\n]+)\]\]", replace, part)
                   for index, part in enumerate(parts))


def okf_files(snapshot):
    files, paths = {}, {}
    foreign = snapshot["foreign_bundles"]
    prefixes = {key: "" if len(foreign) == 1 else "imports/" + key + "/" for key in foreign}
    for page in snapshot["pages"]:
        original = page.get("foreign")
        path = (prefixes[original["bundle_id"]] + original["path"]) if original else "concepts/" + page["slug"] + ".md"
        if path in paths.values():
            path = "concepts/" + page["id"] + ".md"
        paths[page["id"]] = safe_path(path)
    for page in snapshot["pages"]:
        original = page.get("foreign")
        meta = dict(original["frontmatter"]) if original else {"type": "concept"}
        body = page["body"] if original else native_links(page["body"], page, snapshot["pages"], paths)
        producer = dict(meta.get("x-llmwiki", {})) if isinstance(meta.get("x-llmwiki"), dict) else {}
        producer.update({"schemaVersion": "0.1", "contentHash": text_hash(canonical_body(body)),
                         "pageDirectory": producer.get("pageDirectory", "concepts")})
        if original:
            bundle_id = original["bundle_id"]
            prefix = prefixes[bundle_id]
            for ref_path, content in foreign[bundle_id]["attachments"].items():
                if ref_path in {"index.md", "log.md"}:
                    continue
                path = safe_path(prefix + ref_path)
                if path in files and files[path] != content:
                    raise ExchangeError("wiki_exchange_path_conflict")
                files[path] = content
            if prefix:
                body = re.sub(r"(\]\()/", lambda match: match[1] + "/" + prefix, body)
                producer["contentHash"] = text_hash(canonical_body(body))
        else:
            citations = []
            source_versions = []
            for source in page["sources"]:
                raw = snapshot["sources"][source["id"]]
                path = "references/" + raw["id"] + ".md"
                # Upstream reads every typed Markdown file as a knowledge page.
                # References follow its exporter: original bytes, no invented type.
                files[path] = raw["body"]
                citations.append({"file": path})
                source_versions.append({"id": raw["id"], "file": path, "revision": raw["revision"], "contentHash": raw["content_hash"]})
            meta["x-openbox"] = {"sourceVersions": source_versions}
            producer.update({"sources": [item["file"] for item in citations], "citations": citations})
            if citations:
                body = canonical_body(body) + "\n# Citations\n\n" + "\n".join(
                    "- [" + item["file"] + "](/" + item["file"] + ")" for item in citations) + "\n"
        meta.update({"title": page["title"], "timestamp": page["updated_at"], "x-llmwiki": producer,
                     "x-openbox": {**(meta.get("x-openbox", {}) if isinstance(meta.get("x-openbox"), dict) else {}),
                                   "slug": page["slug"], "revision": page["revision"], "pageId": page["id"]}})
        if paths[page["id"]] in files:
            raise ExchangeError("wiki_exchange_path_conflict")
        files[paths[page["id"]]] = render_markdown(meta, body)
    manifest = dict(next(iter(foreign.values()))["manifest"]) if len(foreign) == 1 else {}
    imported_projection = manifest.get("x-openbox", {})
    manifest.update({"okf_version": "0.1", "title": "OpenBox Wiki", "x-openbox": {
        **(imported_projection if isinstance(imported_projection, dict) else {}),
        "schemaVersion": 1, "snapshotHash": snapshot["snapshot_hash"], "concepts": snapshot["concepts"],
        "relations": snapshot["relations"], "profiles": snapshot["profiles"], "workflows": snapshot["workflows"],
        "records": snapshot.get("records", []), "artifacts": snapshot.get("artifacts", [])}})
    if foreign:
        manifest["x-openbox-foreign-bundles"] = {key: {"prefix": prefixes[key], "manifest": value["manifest"]}
                                                for key, value in foreign.items()}
    files["index.md"] = render_markdown(manifest, "# OpenBox Wiki\n\n" + "\n".join(
        "- [" + page["title"].replace("]", "\\]") + "](/" + paths[page["id"]] + ")" for page in snapshot["pages"]) + "\n")
    logs = [value["attachments"].get("log.md", "") for value in foreign.values()]
    files["log.md"] = "\n".join(logs) + "\n# Export\n\nCurrent reviewed OpenBox knowledge. Imported history is inert.\n"
    return files


def _graphml(snapshot):
    namespace = "http://graphml.graphdrawing.org/xmlns"
    ET.register_namespace("", namespace)
    root = ET.Element("{" + namespace + "}graphml")
    for key in ("title", "type", "body"):
        ET.SubElement(root, "key", {"id": key, "for": "all", "attr.name": key, "attr.type": "string"})
    graph = ET.SubElement(root, "graph", {"id": "wiki", "edgedefault": "directed"})
    nodes = {page["id"]: page for page in snapshot["pages"]}
    nodes.update({concept["id"]: concept for concept in snapshot["concepts"]})
    nodes.update({record["id"]: record for record in snapshot.get("records", [])})
    for key, item in nodes.items():
        node = ET.SubElement(graph, "node", {"id": key})
        ET.SubElement(node, "data", {"key": "title"}).text = item["title"]
        ET.SubElement(node, "data", {"key": "body"}).text = item.get("body", item.get("description", ""))
    for relation in snapshot["relations"]:
        if relation["from"] in nodes and relation["to"] in nodes:
            edge = ET.SubElement(graph, "edge", {"id": relation["id"], "source": relation["from"], "target": relation["to"]})
            ET.SubElement(edge, "data", {"key": "type"}).text = relation["type"]
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def render_export(snapshot, format):
    if format == "okf":
        archive = zip_files(okf_files(snapshot))
        # Never emit an archive that our own documented safety caps reject.
        # An oversized scope can be exported by project or as JSON instead.
        try:
            parse_okf(archive)
        except ExchangeError as exc:
            raise ExchangeError("wiki_exchange_export_limit") from exc
        return archive, "application/zip", "openbox-wiki.okf.zip"
    if format == "json":
        return json.dumps(snapshot, ensure_ascii=False, indent=2).encode(), "application/json", "openbox-wiki.json"
    if format == "jsonld":
        graph = [{"@id": "urn:openbox:wiki:" + page["id"], "@type": "CreativeWork", "name": page["title"],
                  "text": page["body"], "version": page["revision"]} for page in snapshot["pages"]]
        graph += [{"@id": "urn:openbox:wiki:" + concept["id"], "@type": "DefinedTerm", "name": concept["title"],
                   "alternateName": concept["aliases"]} for concept in snapshot["concepts"]]
        graph += [{"@id": "urn:openbox:wiki:" + record["id"], "@type": "CreativeWork", "name": record["title"],
                   "additionalType": record["entity_type"], "version": record["revision"], "keywords": list(record["fields"])}
                  for record in snapshot.get("records", [])]
        for relation in snapshot["relations"]:
            graph.append({"@id": "urn:openbox:wiki:" + relation["id"], "@type": "PropertyValue",
                "name": relation["type"], "value": {"@id": "urn:openbox:wiki:" + relation["to"]},
                "subjectOf": {"@id": "urn:openbox:wiki:" + relation["from"]}})
        return json.dumps({"@context": "https://schema.org", "@graph": graph}, ensure_ascii=False, indent=2).encode(), "application/ld+json", "openbox-wiki.jsonld"
    if format == "graphml":
        return _graphml(snapshot), "application/graphml+xml", "openbox-wiki.graphml"
    if format == "marp":
        text = render_markdown({"marp": True, "title": "OpenBox Wiki"}, "\n\n---\n\n".join(
            "# " + page["title"] + "\n\n" + page["body"] for page in snapshot["pages"]))
        return text.encode(), "text/markdown; charset=utf-8", "openbox-wiki-slides.md"
    if format == "llms":
        text = "# OpenBox Wiki\n\n> Reviewed knowledge with versioned provenance.\n\n" + "\n\n".join(
            "## " + page["title"] + "\n\n" + page["body"] for page in snapshot["pages"])
        return text.encode(), "text/plain; charset=utf-8", "llms.txt"
    raise ExchangeError("wiki_exchange_format_unsupported")
