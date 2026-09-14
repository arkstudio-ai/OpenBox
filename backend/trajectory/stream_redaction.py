"""Stateful, pre-commit redaction for one logical output stream.

An incomplete credential prefix must never be published and removed later.
Each scanner owns its lexical state; there is no process-global content cache.
Raw provider text is a reference to sanitized blocks, not a second delta stream.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from trajectory.redaction import _PRIVATE, _key, _schema_node, sanitize

_MARKER = "[REDACTED]"
_PREFIXES = ("sk-", "eyj", "http://", "https://")
_MAX_WORD = max(map(len, _PRIVATE)) + 1
_URL_LIMIT = 16 * 1024
_TOKEN_CHARACTERS = frozenset("_-=.")
_VALUE_END = frozenset(",;}]&\"'")
_STREAM_METADATA = frozenset({"id", "item_id", "call_id", "name", "model", "object", "type", "role", "finish_reason", "status"})


def _safe_url(value: str) -> str:
    """A URL is buffered until its query and authority are known."""
    try:
        parsed = urlsplit(value)
        query = []
        for key, item in parse_qsl(parsed.query, keep_blank_values=True):
            normalized = _key(key)
            private = (normalized in _PRIVATE or normalized.startswith(("xamz", "xoss", "xgoog"))
                       or normalized in {"sig", "se", "sp", "sv"})
            query.append((key, _MARKER if private else sanitize(item)))
        visible = urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path,
                              urlencode(query), parsed.fragment))
        # Short token prefixes are withheld/redacted too; a minimum token length
        # would already expose the first pieces of a subsequently complete token.
        return re.sub(r"(?i)\b(?:sk-[A-Za-z0-9_.=-]*|eyj[A-Za-z0-9_.=-]*)", _MARKER, visible)
    except ValueError:
        return "[REDACTED URL]"


@dataclass
class _Scanner:
    state: str = "normal"
    word: str = ""
    candidate: str = ""
    candidate_raw: str = ""
    escaped: str = ""
    quote: str = ""
    quote_raw: str = ""
    depth: int = 0
    separator: str = ""
    sensitive_key: str = ""
    key_quote: str = ""
    key_quote_raw: str = ""
    quoted_key: str = ""
    url: str = ""
    redacted: bool = False
    output: list[str] = field(default_factory=list)

    def emit(self, value: str) -> None:
        self.output.append(value)

    def mark(self, *, quoted=False) -> None:
        self.redacted = True
        self.emit('"' + _MARKER + '"' if quoted else _MARKER)

    def normal(self, char: str, raw: str) -> None:
        if char.isalnum() or char == "_":
            self.word = _key(char)
            if any(prefix.startswith(char.lower()) for prefix in _PREFIXES):
                self.state, self.candidate, self.candidate_raw = "candidate", char.lower(), raw
            else:
                self.state = "word"
                self.emit(raw)
        else:
            self.emit(raw)

    def character(self, char: str, raw: str) -> None:
        # Quoted JSON keys may spell api_key as "api key", "api.key", or
        # Unicode escapes. Keep only their bounded normalized name, not their
        # value, while the ordinary lexer preserves their original spelling.
        private = None
        if self.state in {"normal", "word", "candidate", "after_key"}:
            if self.key_quote_raw:
                if char == self.key_quote and raw == self.key_quote_raw:
                    private = self.quoted_key if self.quoted_key in _PRIVATE else None
                    self.key_quote_raw = self.quoted_key = ""
                elif len(self.quoted_key) <= _MAX_WORD:
                    self.quoted_key += _key(char)
            elif char in "\"'":
                self.key_quote, self.key_quote_raw, self.quoted_key = char, raw, ""
        else:
            self.key_quote_raw = self.quoted_key = ""
        self._consume(char, raw)
        if private:
            self.state, self.sensitive_key = "after_key", private
        if self.state in {"quoted_secret", "structured_secret", "unquoted_secret", "header_secret", "token"}:
            self.key_quote_raw = self.quoted_key = ""

    def _consume(self, char: str, raw: str) -> None:
        if self.state == "candidate":
            candidate = self.candidate + char.lower()
            if candidate in {"sk-", "eyj"}:
                self.mark()
                self.state, self.candidate, self.candidate_raw = "token", "", ""
            elif candidate in {"http://", "https://"}:
                self.url = candidate
                self.state, self.candidate, self.candidate_raw = "url", "", ""
            elif any(prefix.startswith(candidate) for prefix in _PREFIXES):
                self.candidate, self.candidate_raw = candidate, self.candidate_raw + raw
            else:
                prior = self.candidate
                self.emit(self.candidate_raw)
                self.candidate = self.candidate_raw = ""
                self.word = _key(prior)
                self.state = "word" if all(c.isalnum() or c in "_-" for c in prior) else "normal"
                self._consume(char, raw)
            return
        if self.state == "word":
            if char.isalnum() or char in "_-":
                if len(self.word) <= _MAX_WORD:
                    self.word += _key(char)
                self.emit(raw)
                return
            word, self.word = self.word, ""
            if word in _PRIVATE:
                self.state, self.sensitive_key = "after_key", word
            elif word in {"bearer", "basic"} and char.isspace():
                self.state, self.separator, self.sensitive_key = "value_start", " ", ""
                self.emit(raw)
                return
            else:
                self.state = "normal"
            self._consume(char, raw)
            return
        if self.state == "after_key":
            if char.isspace() or char in "\"'\\":
                self.emit(raw)
            elif char in "=:":
                self.emit(raw)
                self.state, self.separator = "value_start", char
            else:
                self.state = "normal"
                self._consume(char, raw)
            return
        if self.state == "value_start":
            if char.isspace():
                self.emit(raw)
                return
            if char in "\"'":
                self.emit(raw)
                self.mark()
                self.state, self.quote, self.quote_raw = "quoted_secret", char, raw
            elif char in "[{":
                self.mark(quoted=self.separator == ":")
                self.state, self.depth, self.quote = "structured_secret", 1, ""
            else:
                self.mark(quoted=self.separator == ":")
                self.state = "header_secret" if self.sensitive_key in {
                    "authorization", "proxyauthorization", "cookie", "setcookie",
                } else "unquoted_secret"
                self._consume(char, raw)
            return
        if self.state == "quoted_secret":
            if char == self.quote and raw == self.quote_raw:
                self.emit(raw)
                self.state = "normal"
            return
        if self.state == "structured_secret":
            if self.quote:
                if char == self.quote and raw == self.quote_raw:
                    self.quote = ""
            elif char in "\"'":
                self.quote, self.quote_raw = char, raw
            elif char in "[{":
                self.depth += 1
            elif char in "]}":
                self.depth -= 1
                if not self.depth:
                    self.state = "normal"
            return
        if self.state == "unquoted_secret":
            if (char.isspace() or char in _VALUE_END) and not raw.startswith("\\"):
                self.state = "normal"
                self._consume(char, raw)
            return
        if self.state == "header_secret":
            if char in "\r\n":
                self.state = "normal"
                self._consume(char, raw)
            return
        if self.state == "token":
            if not (char.isalnum() or char in _TOKEN_CHARACTERS):
                self.state = "normal"
                self._consume(char, raw)
            return
        if self.state in {"url", "discard_url"}:
            if char.isspace() or char in "<>\"'`":
                if self.state == "url":
                    safe = _safe_url(self.url)
                    self.redacted |= _MARKER in safe or "%5BREDACTED%5D" in safe or "[REDACTED URL]" in safe
                    self.emit(safe)
                self.url, self.state = "", "normal"
                self._consume(char, raw)
            elif self.state == "url":
                self.url += char
                if len(self.url) > _URL_LIMIT:
                    self.emit("[REDACTED URL]")
                    self.redacted, self.url, self.state = True, "", "discard_url"
            return
        self.normal(char, raw)

    def feed(self, text: str, *, final: bool) -> str:
        """Decode only lexical escapes, retaining each original spelling."""
        self.output = []
        value, self.escaped = self.escaped + text, ""
        index = 0
        while index < len(value):
            if value[index] != "\\":
                self.character(value[index], value[index])
                index += 1
                continue
            end = index + 1
            while end < len(value) and value[end] == "\\":
                end += 1
            if end == len(value) and not final:
                # Backslashes themselves are harmless. Keep only a bounded
                # escape suffix; excessive malformed escaping stays opaque.
                if end - index > 16:
                    self.character("\\", value[index:end - 16])
                    index = end - 16
                self.escaped = value[index:]
                break
            if end < len(value) and value[end] == "u":
                if end + 5 > len(value) and not final:
                    self.escaped = value[index:]
                    break
                code = value[end + 1:end + 5]
                if len(code) == 4 and all(char in "0123456789abcdefABCDEF" for char in code):
                    self.character(chr(int(code, 16)), value[index:end + 5])
                    index = end + 5
                    continue
            if end < len(value) and (value[end] in "\"'/nrtbf" or value[end].isspace()):
                char = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(value[end], value[end])
                self.character(char, value[index:end + 1])
                index = end + 1
            else:
                self.character("\\", value[index:end])
                index = end
        if final:
            if self.state == "candidate":
                self.emit(self.candidate_raw)
                self.candidate = self.candidate_raw = ""
            elif self.state == "url":
                safe = _safe_url(self.url)
                self.redacted |= _MARKER in safe or "%5BREDACTED%5D" in safe or "[REDACTED URL]" in safe
                self.emit(safe)
                self.url = ""
            self.state = "normal"
        return "".join(self.output)

    @property
    def pending(self) -> bool:
        return bool(self.escaped) or self.state in {"candidate", "url"}


class StreamTextRedactor:
    """One instance per tool/output block; never feed an unrelated stream.

    Delta mode emits only newly safe characters. Replace mode scans a complete
    cumulative snapshot and returns a sanitized replacement. ``final=True`` is
    required at a known EOF to release a harmless ambiguous trailing prefix.
    """
    def __init__(self):
        self._scanner = _Scanner()
        self._finished = False

    def redact(self, text: str, *, mode: str = "delta", final: bool = False) -> dict:
        if not isinstance(text, str) or mode not in {"delta", "replace"}:
            raise ValueError("Stream redaction requires text and delta/replace mode")
        if mode == "replace":
            self._scanner, self._finished = _Scanner(), False
        elif self._finished:
            raise ValueError("Cannot append to a finalized redaction stream")
        output = self._scanner.feed(text, final=final)
        self._finished = final
        return {"output": output, "mode": mode, "redaction": {
            "version": 1, "sanitized": self._scanner.redacted, "pending_prefix": self._scanner.pending,
        }}

    def finalize(self) -> dict:
        return self.redact("", final=True)


def _block_text(block: dict) -> str:
    value = block.get("delta", block.get("text", block.get("arguments", "")))
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("text", value.get("arguments", json.dumps(value, ensure_ascii=False)))
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _complete_value(value: Any) -> Any:
    """Retain complete non-streaming bodies, including tool JSON Schema."""
    def walk(item):
        if isinstance(item, dict):
            return {key: walk(child) for key, child in item.items()}
        if isinstance(item, list):
            return [walk(child) for child in item]
        if isinstance(item, str):
            if item.lstrip().startswith(("{", "[", '"')):
                try:
                    decoded = json.loads(item)
                except (ValueError, RecursionError):
                    pass
                else:
                    # sanitize already applied schema-aware structural rules.
                    # Do not reinterpret schema property names as credentials.
                    return json.dumps(walk(decoded), ensure_ascii=False)
            return StreamTextRedactor().redact(item, final=True)["output"]
        return deepcopy(item)
    return walk(sanitize(value))


def _raw_references(value: Any, blocks: list[dict], scanners: dict[str, StreamTextRedactor]) -> Any:
    """Map only observed stream content; complete response fields stay intact.

    A recognized delta without an adapter block gets its own path-scoped
    scanner. This is explicit inline sanitized content, never an empty block
    reference and never a complete-value guess about an unfinished fragment.
    """
    value = deepcopy(value)
    if not isinstance(value, dict):
        return _complete_value(value)
    identifiers = {str(block['block_id']) for block in blocks}

    def reference(text, candidates, path, *, incremental=True):
        if not isinstance(text, str):
            return text
        mapped = [str(identity) for identity in candidates if str(identity) in identifiers]
        if mapped:
            return {"$stream_blocks": mapped, "availability": "sanitized_reference"}
        if not incremental:
            return text
        scanner = scanners.setdefault(path, StreamTextRedactor())
        return {**scanner.redact(text), "availability": "sanitized_inline_delta", "stream_path": path}

    def unknown_delta(item, path, key=''):
        if isinstance(item, dict):
            if item.get('availability') in {'sanitized_reference', 'sanitized_inline_delta'}:
                return item
            if key in {'schema', 'parameters', 'input_schema', 'output_schema'} and _schema_node(item):
                return item
            return {name: _MARKER if _key(str(name)) in _PRIVATE else
                    unknown_delta(child, f'{path}/{name}', str(name)) for name, child in item.items()}
        if isinstance(item, list):
            return [unknown_delta(child, f'{path}/{child.get("index", child.get("id", index)) if isinstance(child, dict) else index}', key)
                    for index, child in enumerate(item)]
        if isinstance(item, str) and key not in _STREAM_METADATA:
            return reference(item, [], path)
        return item

    for index, choice in enumerate(value.get('choices') or []):
        if not isinstance(choice, dict):
            continue
        choice_id = choice.get('index', index)
        for container in ('delta', 'message'):
            body = choice.get(container)
            if not isinstance(body, dict):
                continue
            for field_name, kind in (('content', 'text'), ('reasoning_content', 'reasoning')):
                if field_name in body:
                    body[field_name] = reference(body[field_name], [f'{choice_id}:{kind}'],
                        f'choice:{choice_id}:{kind}', incremental=container == 'delta')
            for call_index, call in enumerate(body.get('tool_calls') or []):
                if not isinstance(call, dict) or not isinstance(call.get('function'), dict):
                    continue
                tool_id = call.get('index', call_index)
                function = call['function']
                if 'arguments' in function:
                    function['arguments'] = reference(function['arguments'],
                        [f'{choice_id}:tool:{tool_id}', f'tool:{tool_id}'],
                        f'choice:{choice_id}:tool:{tool_id}:arguments', incremental=container == 'delta')
            if container == 'delta':
                choice[container] = unknown_delta(body, f'choice:{choice_id}:delta')
    kind = str(value.get('type', ''))
    if isinstance(value.get('delta'), str):
        identity = str(value.get('item_id') or value.get('output_index', 0))
        candidates = [identity] if identity in identifiers else list(identifiers) if len(identifiers) == 1 else []
        value['delta'] = reference(value['delta'], candidates,
            f'event:{kind}:{identity}:delta', incremental=kind.endswith('.delta') or bool(blocks))
    elif isinstance(value.get('delta'), (dict, list)) and (kind.endswith('.delta') or bool(blocks)):
        value['delta'] = unknown_delta(value['delta'], f'event:{kind}:{value.get("item_id", value.get("output_index", 0))}:delta')
    response = value.get('response')
    if isinstance(response, dict):
        for index, item in enumerate(response.get('output') or []):
            if not isinstance(item, dict):
                continue
            identity = str(item.get('id') or index)
            if item.get('type') == 'function_call' and 'arguments' in item:
                item['arguments'] = reference(item['arguments'], [identity], identity, incremental=False)
            if item.get('type') in {'message', 'reasoning'}:
                for section in ('content', 'summary'):
                    for part in item.get(section) or []:
                        if isinstance(part, dict) and 'text' in part:
                            part['text'] = reference(part['text'], [identity], identity, incremental=False)
    return _complete_value(value)


class CaptureStreamRedactor:
    """Sanitize RequestCapture.chunk_data before the recorder sees any copy."""
    def __init__(self):
        self._blocks: dict[str, tuple[dict, StreamTextRedactor]] = {}
        self._raw_streams: dict[str, StreamTextRedactor] = {}
        self._last: dict | None = None
        self._finished = False

    def redact(self, data: dict) -> dict:
        if self._finished:
            raise ValueError("Cannot append to a finalized capture")
        result = sanitize({key: value for key, value in data.items() if key not in {"blocks", "raw"}})
        result["blocks"] = []
        for index, block in enumerate(data.get("blocks") or []):
            if not isinstance(block, dict):
                continue
            identity = str(block.get("block_id", f"{block.get('type', 'text')}:{index}"))
            metadata = sanitize({key: value for key, value in block.items() if key not in {"delta", "text", "arguments"}})
            metadata.setdefault("block_id", identity)
            if identity not in self._blocks:
                self._blocks[identity] = (metadata, StreamTextRedactor())
            _, scanner = self._blocks[identity]
            mode = block.get("mode", data.get("mode", "delta"))
            safe = scanner.redact(_block_text(block), mode=mode)
            visible = {**metadata, "delta": safe["output"], "redaction": safe["redaction"]}
            if mode == "replace":
                visible["mode"] = "replace"
            self._blocks[identity] = (metadata, scanner)
            result["blocks"].append(visible)
        result["raw"] = _raw_references(data.get("raw"), result["blocks"], self._raw_streams)
        result["raw_content_mode"] = "sanitized_stream_references_and_complete_fields"
        self._last = {key: deepcopy(value) for key, value in result.items() if key not in {"blocks", "raw"}}
        return result

    def finalize(self) -> dict | None:
        if self._finished:
            return None
        self._finished = True
        blocks = []
        for metadata, scanner in self._blocks.values():
            safe = scanner.finalize()
            if safe["output"]:
                # EOF contains only a previously withheld safe tail. The
                # original provider chunk count is kept separately below.
                blocks.append({**metadata, "mode": "delta", "delta": safe["output"], "redaction": safe["redaction"]})
        raw_tails = []
        for path, scanner in self._raw_streams.items():
            safe = scanner.finalize()
            if safe['output']:
                raw_tails.append({'stream_path': path, **safe})
        if not blocks and not raw_tails:
            return None
        last = self._last or {}
        observed = last.get("chunk_index", 0)
        return {**last, "chunk_index": observed + 1, "observed_chunk_index": observed,
                "redaction_control": "finalize", "mode": "delta", "blocks": blocks,
                "raw": {"availability": "not_recorded", "reason": "redaction_control_not_provider_chunk",
                        "sanitized_stream_tails": raw_tails}}
