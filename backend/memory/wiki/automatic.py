"""Grounded publication and lossless fallback; no user review queue required."""
import asyncio
from dataclasses import replace

from memory.grounding import GroundingVerifier
from memory.providers.common import MemoryProviderError
from wiki_compiler import WikiContractError, compile_candidate
from wiki_compiler.compiler import validate_request

POLICY = "automatic-grounded-v1"
COMPLETE_TOPIC_POLICY = "source-grounded-wiki-full-coverage-v1"


class VerbatimModel:
    """Preserve entire source statements, including negations and qualifications.

    Never fall back to cherry-picked model quotes or silently truncate evidence.
    Oversized sources fail the normal compiler limits and remain raw memories.
    """
    async def generate(self, request):
        paragraphs = []
        for source in request.sources:
            # Splitting is only a presentation boundary. Every character stays
            # in the same order, including the context around short quotations.
            for offset in range(0, len(source.text), 1500):
                piece = source.text[offset:offset + 1500]
                if len(piece.strip()) < 2:
                    if paragraphs:
                        paragraphs[-1]["text"] += piece
                    continue
                paragraphs.append({"text": piece,
                    "citations": [{"source_id": source.id, "quote": piece}]})
        return {"paragraphs": paragraphs}, {"model_calls": 0}


class AdmittedMemoryModel:
    def __init__(self, paragraphs):
        self.paragraphs = paragraphs

    async def generate(self, request):
        return {"paragraphs": self.paragraphs}, {"model_calls": 0}


def proof(result, mode, usage):
    return replace(result, usage={**result.usage, **usage, "automatic_grounding": {
        "policy": POLICY, "candidate_hash": result.candidate.candidate_hash, "mode": mode}})


async def compile_automatic(request, *, model, config, cache=None, verifier=None, reserve=None, admitted=None):
    validate_request(request)
    usage = {}
    try:
        result = await compile_candidate(request, model=model, cache=cache)
        usage = dict(result.usage)
        if reserve:
            await reserve()
        check = verifier or GroundingVerifier(config)
        sources = {source.id: source.text for source in request.sources}
        items = [{"claim": paragraph.text, "sources": list(dict.fromkeys(
            sources[citation.source_id] for citation in paragraph.citations))}
            for paragraph in result.candidate.paragraphs]
        if request.policy.version == COMPLETE_TOPIC_POLICY and admitted:
            facts = [paragraph["text"] for paragraph in admitted]
            items.append({"claim": "\n\n".join(p.text for p in result.candidate.paragraphs),
                          "sources": facts, "required_facts": facts})
        supported, check_usage = await check.verify(items, purpose="wiki", model=request.policy.model)
        usage["verification"] = check_usage
        if len(supported) == len(items) and all(value is True for value in supported):
            return proof(result, "verified_synthesis", usage)
        usage["fallback_reason"] = "unsupported_synthesis"
    except (MemoryProviderError, WikiContractError, asyncio.TimeoutError) as exc:
        usage["fallback_reason"] = exc.code if isinstance(exc, MemoryProviderError) else type(exc).__name__
        usage["generation"] = getattr(model, "last_usage", None)
    result = await compile_candidate(request, model=AdmittedMemoryModel(admitted) if admitted else VerbatimModel())
    return proof(result, "admitted_memory_copy" if admitted else "verbatim_sources", usage)


def is_verified(result):
    check = result.usage.get("automatic_grounding", {})
    return (check.get("policy") == POLICY and check.get("candidate_hash") == result.candidate.candidate_hash
            and check.get("mode") in {"verified_synthesis", "verbatim_sources", "admitted_memory_copy"})
