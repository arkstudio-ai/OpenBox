"""Bounded local parsing with no network or document macro execution."""
import csv
from html.parser import HTMLParser
from io import BytesIO, StringIO
import json
from pathlib import PurePath
import re
import sys
from xml.etree import ElementTree as ET
from zipfile import ZipFile

MAX_BYTES = 10 * 1024 * 1024
MAX_CHARS = 500_000
EXTENSIONS = frozenset({".pdf", ".docx", ".txt", ".md", ".markdown", ".csv", ".html", ".htm"})
VERSION = "document-parser-v1"


class DocumentError(ValueError):
    pass


def decode(data):
    encodings = ["utf-16"] if data[:2] in {b"\xff\xfe", b"\xfe\xff"} else ["utf-8-sig", "gb18030"]
    for encoding in encodings:
        try:
            text = data.decode(encoding)
            if "\x00" in text:
                raise DocumentError("document_invalid_text")
            return text
        except UnicodeDecodeError:
            continue
    raise DocumentError("document_encoding_unsupported")


class HtmlText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template"}:
            self.hidden += 1
        elif not self.hidden:
            if re.fullmatch("h[1-6]", tag):
                self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
            elif tag in {"p", "div", "br", "li", "tr"}:
                self.parts.append("\n")
            elif tag in {"td", "th"}:
                self.parts.append("\t")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template"}:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and (tag in {"p", "div", "li", "tr"} or re.fullmatch("h[1-6]", tag)):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def docx_text(data):
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    w = "{" + namespace["w"] + "}"

    def paragraph(node):
        parts = []
        for item in node.iter():
            if item.tag == w + "t":
                parts.append(item.text or "")
            elif item.tag == w + "tab":
                parts.append("\t")
            elif item.tag in {w + "br", w + "cr"}:
                parts.append("\n")
        body = "".join(parts)
        style = node.find("w:pPr/w:pStyle", namespace)
        if style is not None:
            match = re.search(r"(?:heading|标题)\s*([1-6])", style.get(w + "val", ""), re.I)
            if match:
                body = "#" * int(match[1]) + " " + body
        return body

    with ZipFile(BytesIO(data)) as archive:
        infos = archive.infolist()
        if (len(infos) > 4096 or len({i.filename for i in infos}) != len(infos)
                or sum(i.file_size for i in infos) > 40 * 1024 * 1024
                or any(i.file_size > 20 * 1024 * 1024 for i in infos)):
            raise DocumentError("document_archive_too_large")
        if "word/document.xml" not in archive.namelist():
            raise DocumentError("document_invalid_docx")
        texts = []
        for name in ["word/document.xml", "word/footnotes.xml", "word/endnotes.xml"]:
            if name not in archive.namelist():
                continue
            xml = archive.read(name)
            if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                raise DocumentError("document_invalid_docx")
            root = ET.fromstring(xml)
            body = root.find("w:body", namespace)
            if body is None:
                texts.extend(paragraph(p) for p in root.findall(".//w:p", namespace))
                continue
            for child in body:
                if child.tag == w + "p":
                    texts.append(paragraph(child))
                elif child.tag == w + "tbl":
                    rows = [[" / ".join(paragraph(p) for p in cell.findall(".//w:p", namespace))
                             for cell in row.findall("w:tc", namespace)] for row in child.findall("w:tr", namespace)]
                    texts.append(table(rows))
        return "\n\n".join(texts)


def table(rows):
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    lines = ["| " + " | ".join(str(c).replace("|", "\\|").replace("\n", "\\n") for c in
             [*row, *([""] * (width - len(row)))]) + " |" for row in rows]
    lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
    return "\n".join(lines)


def split_text(text, maximum, *, overlap=0):
    """Keep complete character coverage and deterministic offsets, even for long lines."""
    if not 0 <= overlap < maximum // 2:
        raise ValueError("invalid overlap")
    start = 0
    while start < len(text):
        end = min(len(text), start + maximum)
        if end < len(text):
            for separator in ("\n\n", "\n", "。", ". ", " "):
                cut = text.rfind(separator, start + maximum // 2, end)
                if cut >= 0:
                    end = cut + len(separator)
                    break
        yield {"text": text[start:end], "start": start, "end": end}
        if end == len(text):
            break
        start = end - overlap


def parse(data, filename):
    suffix = PurePath(filename).suffix.lower()
    if suffix not in EXTENSIONS:
        raise DocumentError("document_format_unsupported")
    if not data or len(data) > MAX_BYTES:
        raise DocumentError("document_size_limit")
    locations = []
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            raise DocumentError("document_password_protected")
        if len(reader.pages) > 300:
            raise DocumentError("document_page_limit")
        pages, offset = [], 0
        for index, page in enumerate(reader.pages):
            stream = page.get_contents()
            if stream is not None and len(stream.get_data()) > 5 * 1024 * 1024:
                raise DocumentError("document_page_too_complex")
            body = page.extract_text(extraction_mode="layout")
            if not body.strip() and len(page.images):
                raise DocumentError("document_ocr_required")
            pages.append(body)
            locations.append({"page": index + 1, "start": offset, "end": offset + len(body)})
            offset += len(body) + 2
            if offset > MAX_CHARS:
                raise DocumentError("document_text_limit")
        text = "\n\n".join(pages)
    elif suffix == ".docx":
        text = docx_text(data)
    else:
        text = decode(data)
        if suffix in {".html", ".htm"}:
            parser = HtmlText()
            parser.feed(text)
            text = "".join(parser.parts)
        elif suffix == ".csv":
            text = table(list(csv.reader(StringIO(text))))
    if not text.strip():
        raise DocumentError("document_no_text")
    if len(text) > MAX_CHARS:
        raise DocumentError("document_text_limit")
    title = PurePath(filename).stem[:150] or "Document"
    sections = [{"title": title if not i else f"{title} · {i + 1}", "body": piece["text"],
                 "start": piece["start"], "end": piece["end"]}
                for i, piece in enumerate(split_text(text, 12000))]
    return {"sections": sections, "metadata": {"parser": VERSION, "characters": len(text), "locations": locations}}


if __name__ == "__main__":
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
        # Darwin does not support lowering RLIMIT_AS; the parent watches RSS.
        if sys.platform != "darwin":
            resource.setrlimit(resource.RLIMIT_AS, (1024 ** 3, 1024 ** 3))
        result = parse(sys.stdin.buffer.read(MAX_BYTES + 1), sys.argv[1])
        print(json.dumps(result, ensure_ascii=False))
    except DocumentError as exc:
        print(json.dumps({"error": str(exc)}))
    except Exception:
        print(json.dumps({"error": "document_parse_failed"}))
