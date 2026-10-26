"""Decode Git's quoted path bytes without treating ordinary paths as escapes."""
import re


def unquote_git_path(path: str) -> str:
    if not (path.startswith('"') and path.endswith('"')):
        return path
    escapes = dict(zip('abtnvfr"\\', [7, 8, 9, 10, 11, 12, 13, 34, 92]))
    body = path[1:-1]
    value = bytearray()
    pos = 0
    while pos < len(body):
        if body[pos] != '\\':
            value.extend(body[pos].encode('utf-8'))
            pos += 1
            continue
        match = re.match(r'\\([0-7]{1,3}|[abtnvfr"\\])', body[pos:])
        if match is None:
            return path
        token = match.group(1)
        byte = int(token, 8) if token[0].isdigit() else escapes[token]
        if byte > 255:
            return path
        value.append(byte)
        pos += len(match.group(0))
    try:
        return value.decode('utf-8')
    except UnicodeDecodeError:
        return path
