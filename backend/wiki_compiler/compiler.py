"""Source-only candidate generation, with paragraph citations and immutable hash."""
from dataclasses import asdict, replace
import re

from wiki_compiler.adapters import CompilationCache, WikiModel
from wiki_compiler.contracts import (
    CONTRACT_VERSION, CandidateDraft, Citation, CompileRequest, CompileResult, Dependency, Paragraph,
)
from wiki_compiler.hashing import cache_key, canonical_hash, text_hash


class WikiContractError(ValueError):
    pass


def validate_request(request: CompileRequest) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", request.slug):
        raise WikiContractError("invalid_slug")
    if not request.title.strip() or len(request.title) > 160 or not request.domain:
        raise WikiContractError("invalid_title_or_domain")
    if not 1 <= len(request.sources) <= request.policy.max_sources:
        raise WikiContractError("source_budget_exceeded")
    if len({source.id for source in request.sources}) != len(request.sources):
        raise WikiContractError("duplicate_source")
    if sum(len(source.text) for source in request.sources) > request.policy.max_source_chars:
        raise WikiContractError("source_budget_exceeded")
    for source in request.sources:
        if (source.domain != request.domain or source.kind != "source" or not source.text
                or source.revision < 1 or source.content_hash != text_hash(source.text)):
            raise WikiContractError("invalid_source_snapshot")
    if request.target.revision < 0 or (request.target.revision == 0) != (request.target.content_hash is None):
        raise WikiContractError("invalid_target_snapshot")


def validate_output(value: dict, request: CompileRequest) -> tuple[Paragraph, ...]:
    if not isinstance(value, dict) or set(value) != {"paragraphs"}:
        raise WikiContractError("invalid_output_envelope")
    raw = value["paragraphs"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= request.policy.max_paragraphs:
        raise WikiContractError("invalid_paragraph_count")
    by_id = {source.id: source for source in request.sources}
    paragraphs = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"text", "citations"}:
            raise WikiContractError("invalid_paragraph")
        text, citations = item["text"], item["citations"]
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise WikiContractError("invalid_paragraph_text")
        if not isinstance(citations, list) or not 1 <= len(citations) <= request.policy.max_sources:
            raise WikiContractError("uncited_paragraph")
        validated, seen = [], set()
        for citation in citations:
            if not isinstance(citation, dict) or set(citation) != {"source_id", "quote"}:
                raise WikiContractError("invalid_citation")
            if not isinstance(citation["source_id"], str):
                raise WikiContractError("invalid_citation")
            source = by_id.get(citation["source_id"])
            quote = citation["quote"]
            if source is None or not isinstance(quote, str) or len(quote.strip()) < 2 or len(quote) > 1600 or quote not in source.text:
                raise WikiContractError("unsupported_citation")
            key = (source.id, quote)
            if key not in seen:
                validated.append(Citation(source.id, source.revision, source.content_hash, quote))
                seen.add(key)
        paragraphs.append(Paragraph(text.strip(), tuple(validated)))
    if sum(len(paragraph.text) for paragraph in paragraphs) > request.policy.max_output_chars:
        raise WikiContractError("output_budget_exceeded")
    return tuple(paragraphs)


def candidate_hash(candidate: CandidateDraft | dict) -> str:
    value = asdict(candidate) if isinstance(candidate, CandidateDraft) else dict(candidate)
    value.pop("candidate_hash", None)
    return canonical_hash(value)


async def compile_candidate(request: CompileRequest, *, model: WikiModel, cache: CompilationCache | None = None) -> CompileResult:
    validate_request(request)  # Must precede cache access and external model IO.
    key = cache_key(request)
    output = await cache.get(key) if cache is not None else None
    reused = output is not None
    usage = {"input_tokens": 0, "output_tokens": 0, "estimated_cost": 0, "billed_cost": None, "cache_hit": True} if reused else {}
    if output is None:
        output, usage = await model.generate(request)
    paragraphs = validate_output(output, request)
    body = f"# {request.title}\n\n" + "\n\n".join(
        paragraph.text + "\n" + " ".join(f"[source:{citation.source_id}@{citation.revision}]" for citation in paragraph.citations)
        for paragraph in paragraphs
    )
    if len(body) > request.policy.max_output_chars:
        raise WikiContractError("output_budget_exceeded")
    draft = CandidateDraft(CONTRACT_VERSION, 1, request.slug, request.title, request.domain, body, paragraphs,
        tuple(Dependency(source.kind, source.id, source.revision, source.content_hash, source.acl_epoch)
              for source in sorted(request.sources, key=lambda source: source.id)),
        request.target, request.policy.version, request.policy.model, key, "")
    draft = replace(draft, candidate_hash=candidate_hash(draft))
    if cache is not None and not reused:
        await cache.put(key, output)
    return CompileResult(draft, reused, usage, ("cache_hit" if reused else "model_compiled",))
