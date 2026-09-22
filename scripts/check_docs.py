#!/usr/bin/env python3
"""Check public repository Markdown links and keep the documentation catalog complete.

Uses Git's tracked/nonignored inventory, never a filesystem-wide crawl. It does not
read ignored credentials, run examples, fetch URLs, or modify frozen evidence.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
import html
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
CATALOG = Path('docs/CATALOG.md')
DOC_SUFFIXES = {'.md', '.mdx', '.rst', '.adoc'}


@dataclass(frozen=True)
class Link:
    target: str
    offset: int


def public_files(root: Path) -> set[Path]:
    result = subprocess.run(
        ['git', 'ls-files', '-co', '--exclude-standard', '-z'], cwd=root,
        check=True, capture_output=True,
    )
    return {Path(name) for name in result.stdout.decode().split('\0')
            if name and (root / name).is_file()}


def prose_only(text: str) -> str:
    """Mask fenced and indented code without changing source positions."""
    lines = []
    fence = None
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
        if fence:
            if re.match(r'^ {0,3}' + re.escape(fence[0]) + '{' + str(fence[1]) + r',}\s*$', line):
                fence = None
            lines.append(re.sub(r'[^\n]', ' ', line))
        elif marker:
            fence = (marker[1][0], len(marker[1]))
            lines.append(re.sub(r'[^\n]', ' ', line))
        elif line.startswith(('    ', '\t')):
            lines.append(re.sub(r'[^\n]', ' ', line))
        else:
            lines.append(line)
    return ''.join(lines)


def markdown_links(text: str) -> list[Link]:
    prose = prose_only(text)
    # Inline code may contain documentation syntax as an example. Equal-length
    # backtick runs delimit code; leave positions intact for diagnostics.
    prose = re.sub(r'(?<!`)(`+)(?!`)(.*?)(?<!`)\1(?!`)',
                   lambda m: re.sub(r'[^\n]', ' ', m[0]), prose, flags=re.S)
    links = []
    for match in re.finditer(r'!?\[(?:\\.|[^\]\\\n])*\]\(', prose):
        start = match.end()
        cursor = start
        depth = 1
        angle = False
        while cursor < len(prose):
            char = prose[cursor]
            if char == '\\':
                cursor += 2
                continue
            if char == '<':
                angle = True
            elif char == '>':
                angle = False
            elif not angle:
                if char == '(':
                    depth += 1
                elif char == ')':
                    depth -= 1
                    if not depth:
                        break
            cursor += 1
        if depth:
            continue
        body = text[start:cursor].strip()
        if body.startswith('<') and '>' in body:
            target = body[1:body.index('>')]
        else:
            target = re.split(r'\s+[\"\']', body, maxsplit=1)[0]
        links.append(Link(re.sub(r'\\([() ])', r'\1', target), start))
    # Reference-style destinations and HTML images/anchors are also local links.
    for match in re.finditer(r'^ {0,3}\[[^\]\n]+\]:\s*(<[^>]+>|\S+)', prose, re.M):
        links.append(Link(match[1].strip('<>'), match.start(1)))
    for match in re.finditer(r'<(?:a|img)\b[^>]*?\b(?:href|src)=[\"\']([^\"\']+)', prose, re.I):
        links.append(Link(match[1], match.start(1)))
    return links


def heading_anchors(text: str) -> set[str]:
    prose = prose_only(text)
    result = set(re.findall(r'<a\b[^>]*\b(?:id|name)=[\"\']([^\"\']+)', prose, re.I))
    counts: Counter[str] = Counter()
    headings = re.findall(r'^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$', prose, re.M)
    headings += re.findall(r'^([^\n]+)\n {0,3}(?:={3,}|-{3,})\s*$', prose, re.M)
    for heading in headings:
        heading = re.sub(r'!?\[([^\]]+)\]\([^)]*\)', r'\1', heading)
        heading = html.unescape(re.sub(r'<[^>]*>', '', heading))
        slug = re.sub(r'[^\w\s-]', '', heading.lower())
        slug = re.sub(r'\s', '-', slug.strip())
        number = counts[slug]
        result.add(slug + (f'-{number}' if number else ''))
        counts[slug] += 1
    return result


def catalog_text(root: Path, docs: list[Path]) -> str:
    lines = ['# 全仓文档目录', '',
             '由 `python3 scripts/check_docs.py --write-index` 生成。覆盖 Git 管理及未忽略的新文档；不收录私有本地文件。', '',
             '按阅读任务进入[文档首页](README.md)。本表的记录类型由目录表示，历史结果不代表当前线上状态。', '']
    buckets: dict[str, list[Path]] = {}
    for path in docs:
        if path == CATALOG:
            continue
        if path.parts[0] == 'docs' and len(path.parts) > 2:
            group = '/'.join(path.parts[:2])
        elif len(path.parts) == 1:
            group = '项目入口'
        else:
            group = path.parts[0]
        buckets.setdefault(group, []).append(path)
    for group, paths in sorted(buckets.items()):
        lines.extend([f'## {group}', '', '| 文档 | 路径 |', '|---|---|'])
        for path in paths:
            text = (root / path).read_text()
            title = re.search(r'^#\s+(.+)', prose_only(text), re.M)
            label = (title[1] if title else path.stem).replace('|', '\\|').replace('[', '\\[').replace(']', '\\]')
            dest = os.path.relpath(path, CATALOG.parent)
            if ' ' in dest:
                dest = '<' + dest + '>'
            lines.append(f'| [{label}]({dest}) | `{path}` |')
        lines.append('')
    return '\n'.join(lines)


def check_links(root: Path, docs: list[Path], files: set[Path]) -> tuple[list[str], int]:
    errors = []
    count = 0
    anchors: dict[Path, set[str]] = {}
    for doc in docs:
        if doc.suffix not in {'.md', '.mdx'}:
            continue
        text = (root / doc).read_text()
        for link in markdown_links(text):
            target = html.unescape(link.target)
            url = urlsplit(target)
            if url.scheme or url.netloc:
                continue
            count += 1
            line = text.count('\n', 0, link.offset) + 1
            location = f'{doc}:{line}'
            path = unquote(url.path)
            if path.startswith('/'):
                errors.append(f'{location}: nonportable absolute link: {target}')
                continue
            resolved = (root / doc.parent / path).resolve() if path else (root / doc).resolve()
            if not resolved.is_relative_to(root):
                errors.append(f'{location}: link leaves repository: {target}')
                continue
            relative = resolved.relative_to(root)
            if relative not in files and not (resolved.is_dir() and any(p.is_relative_to(relative) for p in files)):
                errors.append(f'{location}: missing or unversioned target: {target}')
                continue
            fragment = unquote(url.fragment)
            if fragment and resolved.suffix in {'.md', '.mdx'}:
                if relative not in anchors:
                    anchors[relative] = heading_anchors(resolved.read_text())
                if fragment not in anchors[relative]:
                    errors.append(f'{location}: missing heading #{fragment}: {target}')
    return errors, count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write-index', action='store_true', help='Regenerate docs/CATALOG.md before validation')
    args = parser.parse_args()
    files = public_files(ROOT)
    docs = sorted(p for p in files if p.suffix in DOC_SUFFIXES)
    expected = catalog_text(ROOT, docs)
    if args.write_index:
        (ROOT / CATALOG).write_text(expected)
        files.add(CATALOG)
        docs = sorted(set(docs) | {CATALOG})
    errors, count = check_links(ROOT, docs, files)
    if not (ROOT / CATALOG).is_file() or (ROOT / CATALOG).read_text() != expected:
        errors.append('Documentation catalog is stale: run python3 scripts/check_docs.py --write-index')
    mapping_file = ROOT / 'docs/moved-documents.json'
    if mapping_file.is_file():
        for old, new in json.loads(mapping_file.read_text())['paths'].items():
            if Path(new) not in files:
                errors.append(f'Document migration target is missing: {old} -> {new}')
            if Path(old) in files:
                errors.append(f'Obsolete document still exists: {old}')
    for error in errors:
        print(error)
    print(f'{len(docs)} public documents, {count} local links, {len(errors)} errors. External URLs were not fetched.')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
