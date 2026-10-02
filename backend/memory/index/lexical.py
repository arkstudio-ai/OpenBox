"""Application BM25 with explicit Unicode/CJK tokenization, independent of Qdrant.

    CJK unigrams and bigrams handle unspaced Chinese; Latin identifiers,
    negations, numbers and units survive normalization. No global corpus cache
    can mix access domains: callers pass only freshly authorized documents.
"""
import math
import re
import unicodedata
from collections import Counter

TOKENIZER_VERSION = "unicode-cjk-bigrams-v1"


def tokenize(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).casefold()
    tokens = re.findall(r"[a-z0-9_]+(?:[.\-/][a-z0-9_]+)*", text)
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        tokens.extend(run)
        tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
    tokens.extend(re.findall(r"\d+(?:\.\d+)?\s*[\u3400-\u9fff%]+", text))
    return tokens


def bm25(query: str, documents: list[str]) -> list[tuple[int, float]]:
    terms = set(tokenize(query))
    counters = [Counter(tokenize(text)) for text in documents]
    n = len(counters)
    if not n or not terms:
        return []
    lengths = [sum(counter.values()) for counter in counters]
    average = sum(lengths) / n or 1
    frequency = Counter(term for counter in counters for term in counter if term in terms)
    result = []
    for index, counter in enumerate(counters):
        score = 0.0
        for term in terms:
            count = counter.get(term, 0)
            if not count:
                continue
            idf = math.log(1 + (n - frequency[term] + 0.5) / (frequency[term] + 0.5))
            score += idf * count * 2.2 / (count + 1.2 * (0.25 + 0.75 * lengths[index] / average))
        if score > 0:
            result.append((index, score))
    return sorted(result, key=lambda hit: (-hit[1], hit[0]))
