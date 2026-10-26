"""Load the pinned proxy vocabulary from disk, with no download fallback.

The build prepares tiktoken's cache. Runtime must not call get_encoding:
its loader downloads on a missing/corrupt cache, even after a local probe.
The pattern, special tokens and checksum below are the o200k_base contract
in the locked tiktoken 0.12 package; this remains a proxy for other models.
"""
from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from pathlib import Path

VOCABULARY_URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
VOCABULARY_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
VOCABULARY_CACHE_KEY = hashlib.sha1(VOCABULARY_URL.encode()).hexdigest()
_PATTERN = "|".join([
    r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
    r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
    r"\p{N}{1,3}", r" ?[^\s\p{L}\p{N}]+[\r\n/]*", r"\s*[\r\n]+", r"\s+(?!\S)", r"\s+",
])


def load_local_encoding():
    """Own and verify the bytes once; never call a network-capable loader."""
    if "TIKTOKEN_CACHE_DIR" in os.environ:
        directory = os.environ["TIKTOKEN_CACHE_DIR"]
    elif "DATA_GYM_CACHE_DIR" in os.environ:
        directory = os.environ["DATA_GYM_CACHE_DIR"]
    else:
        directory = os.path.join(tempfile.gettempdir(), "data-gym-cache")
    if not directory:
        return None
    try:
        data = (Path(directory) / VOCABULARY_CACHE_KEY).read_bytes()
        if hashlib.sha256(data).hexdigest() != VOCABULARY_SHA256:
            return None
        ranks = {}
        for line in data.splitlines():
            token, rank = line.split()
            ranks[base64.b64decode(token, validate=True)] = int(rank)
        import tiktoken

        return tiktoken.Encoding(
            name="o200k_base", pat_str=_PATTERN, mergeable_ranks=ranks,
            special_tokens={"<|endoftext|>": 199999, "<|endofprompt|>": 200018},
        )
    except (OSError, ValueError, ImportError):
        # Preserve a bad cache for diagnostics. The existing proxy estimate
        # keeps the service available without disk mutation or network IO.
        return None
