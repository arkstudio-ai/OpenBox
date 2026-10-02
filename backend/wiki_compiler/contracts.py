"""Versioned, serializable values; opaque domains are never authorization."""
from dataclasses import asdict, dataclass

CONTRACT_VERSION = "wiki-candidate-v1"


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    id: str
    revision: int
    text: str
    content_hash: str
    domain: str
    acl_epoch: int
    kind: str = "source"


@dataclass(frozen=True, slots=True)
class Dependency:
    kind: str
    id: str
    revision: int
    content_hash: str
    acl_epoch: int


@dataclass(frozen=True, slots=True)
class TargetSnapshot:
    id: str
    revision: int = 0
    content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class CompilePolicy:
    model: str
    version: str = "source-grounded-wiki-v1"
    prompt_version: str = "markdown-paragraph-citations-v3"
    max_source_chars: int = 16000
    max_output_chars: int = 8000
    max_sources: int = 12
    max_paragraphs: int = 20


@dataclass(frozen=True, slots=True)
class CompileRequest:
    slug: str
    title: str
    domain: str
    sources: tuple[SourceSnapshot, ...]
    target: TargetSnapshot
    policy: CompilePolicy

    def serialize(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Citation:
    source_id: str
    revision: int
    content_hash: str
    quote: str


@dataclass(frozen=True, slots=True)
class Paragraph:
    text: str
    citations: tuple[Citation, ...]


@dataclass(frozen=True, slots=True)
class CandidateDraft:
    schema_version: str
    revision: int
    slug: str
    title: str
    domain: str
    body: str
    paragraphs: tuple[Paragraph, ...]
    dependencies: tuple[Dependency, ...]
    expected_target: TargetSnapshot
    policy_version: str
    model: str
    cache_key: str
    candidate_hash: str

    def serialize(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CompileResult:
    candidate: CandidateDraft
    reused: bool
    usage: dict
    diagnostics: tuple[str, ...] = ()
