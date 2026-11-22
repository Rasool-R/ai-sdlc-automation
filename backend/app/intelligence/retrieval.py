"""Staged hybrid retrieval engine combining structural, dependency, and semantic signals."""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.exceptions import AppError, AuthorizationError
from app.core.secret_scanner import enforce_data_sharing_policy, scan_outbound_payload
from app.integrations.embeddings.client import EmbeddingClient
from app.integrations.qdrant.client import QdrantIndexClient, VectorSearchHit
from app.models.intelligence import CodeRelationshipModel, CodeSymbolModel
from app.models.repository import RepositoryModel

logger = logging.getLogger(__name__)

RETRIEVAL_SCHEMA_VERSION = "1.0"

_STOP_WORDS: frozenset[str] = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "that",
        "this",
        "from",
        "into",
        "add",
        "update",
        "fix",
        "existing",
        "code",
        "file",
        "files",
        "endpoint",
        "function",
        "class",
        "method",
        "python",
    }
)
_PATH_PATTERN = re.compile(r"[A-Za-z0-9_./-]+\.py\b")
_IDENT_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*")

MAX_EXCERPT_LINES = 120
DEFAULT_CONTEXT_PADDING = 5


class RetrievedRelationship(BaseModel):
    """Directed structural relationship relevant to the retrieved code context."""

    source_symbol: str
    target_symbol: str
    relationship_type: str
    confidence: str
    file_path: str
    line: int | None = None


class RetrievedContextItem(BaseModel):
    """Single traceable code unit returned by hybrid retrieval with full provenance."""

    symbol_id: str
    name: str
    qualified_name: str
    kind: str
    file_path: str
    commit_sha: str
    content_hash: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    is_test: bool = False
    signature: str | None = None
    docstring: str | None = None
    excerpt: str = ""
    score: float = Field(ge=0.0)
    match_sources: list[str] = Field(default_factory=list)
    dependency_distance: int = Field(default=0, ge=0)
    estimated_tokens: int = Field(default=0, ge=0)


class RetrievalResponse(BaseModel):
    """Bounded, version-pinned retrieval bundle ready for agent analysis."""

    schema_version: str = RETRIEVAL_SCHEMA_VERSION
    repository_id: str
    commit_sha: str
    query: str
    retrieval_mode: Literal["hybrid", "structural_only"]
    items: list[RetrievedContextItem] = Field(default_factory=list)
    relationships: list[RetrievedRelationship] = Field(default_factory=list)
    total_estimated_tokens: int = Field(default=0, ge=0)
    files_included: int = Field(default=0, ge=0)
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)


@dataclass
class _CandidateState:
    symbol: CodeSymbolModel
    exact_match_score: float = 0.0
    lexical_score: float = 0.0
    semantic_score: float = 0.0
    dependency_distance: int | None = None
    match_sources: set[str] = field(default_factory=set)

    def compute_rank_score(self) -> float:
        dep_bonus = 0.0
        if self.dependency_distance is not None:
            dep_bonus = 0.55 / (1.0 + self.dependency_distance)
        source_type_weight = 0.05 if not self.symbol.is_test else 0.0
        kind_weight = 0.08 if self.symbol.kind in {"function", "method", "class"} else 0.02
        return (
            self.exact_match_score * 2.0
            + self.lexical_score * 1.1
            + self.semantic_score * 1.0
            + dep_bonus
            + source_type_weight
            + kind_weight
        )


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _extract_query_terms(
    query: str,
    hint_paths: list[str] | None,
    hint_symbols: list[str] | None,
) -> tuple[set[str], set[str], set[str]]:
    explicit_paths = {p.strip().lstrip("./") for p in (hint_paths or []) if p.strip()}
    for path_match in _PATH_PATTERN.findall(query):
        explicit_paths.add(path_match.lstrip("./"))

    explicit_symbols = {s.strip() for s in (hint_symbols or []) if s.strip()}
    lexical_terms: set[str] = set()

    for raw_ident in _IDENT_PATTERN.findall(query):
        cleaned = raw_ident.strip(".")
        if not cleaned:
            continue
        if "." in cleaned and not cleaned.endswith(".py"):
            explicit_symbols.add(cleaned)
        lowered = cleaned.lower()
        if len(lowered) >= 3 and lowered not in _STOP_WORDS:
            lexical_terms.add(lowered)
            for part in re.split(r"[._]+", lowered):
                if len(part) >= 3 and part not in _STOP_WORDS:
                    lexical_terms.add(part)

    return explicit_paths, explicit_symbols, lexical_terms


class HybridRetrievalService:
    """Executes staged hybrid retrieval strictly scoped to (repository_id, commit_sha)."""

    def __init__(
        self,
        db: Session,
        *,
        settings: Settings | None = None,
        embedding_client: EmbeddingClient | None = None,
        qdrant_client: QdrantIndexClient | None = None,
    ) -> None:
        self._db = db
        self._settings = settings or get_settings()
        self._embeddings = embedding_client or EmbeddingClient(settings=self._settings)
        self._qdrant = qdrant_client or QdrantIndexClient(settings=self._settings)

    def retrieve(
        self,
        *,
        repository: RepositoryModel,
        commit_sha: str,
        query: str,
        workspace_dir: Path | None = None,
        hint_paths: list[str] | None = None,
        hint_symbols: list[str] | None = None,
        max_tokens: int | None = None,
        max_files: int | None = None,
        top_k: int = 30,
        context_padding: int = DEFAULT_CONTEXT_PADDING,
    ) -> RetrievalResponse:
        """Run the 7-stage retrieval pipeline with strict version isolation and budgets."""
        token_budget = max_tokens or self._settings.MAX_RETRIEVAL_TOKENS
        file_budget = max_files or self._settings.MAX_RETRIEVAL_FILES
        warnings: list[str] = []

        explicit_paths, explicit_symbols, lexical_terms = _extract_query_terms(
            query,
            hint_paths,
            hint_symbols,
        )

        # Stage 1: Resolve exact paths, symbols, and lexical matches in PostgreSQL
        all_version_symbols = list(
            self._db.scalars(
                select(CodeSymbolModel).where(
                    CodeSymbolModel.repository_id == repository.id,
                    CodeSymbolModel.commit_sha == commit_sha,
                )
            ).all()
        )
        symbol_by_id = {sym.symbol_id: sym for sym in all_version_symbols}
        symbols_by_qname: dict[str, list[CodeSymbolModel]] = {}
        for sym in all_version_symbols:
            symbols_by_qname.setdefault(sym.qualified_name, []).append(sym)

        candidates: dict[str, _CandidateState] = {}

        def _get_or_add(sym: CodeSymbolModel) -> _CandidateState:
            existing = candidates.get(sym.symbol_id)
            if existing is None:
                existing = _CandidateState(symbol=sym)
                candidates[sym.symbol_id] = existing
            return existing

        for sym in all_version_symbols:
            if sym.file_path in explicit_paths:
                state = _get_or_add(sym)
                state.exact_match_score = max(state.exact_match_score, 1.0)
                state.match_sources.add("exact_path")

            if sym.qualified_name in explicit_symbols or sym.name in explicit_symbols:
                state = _get_or_add(sym)
                state.exact_match_score = max(state.exact_match_score, 1.2)
                state.match_sources.add("exact_symbol")

            if lexical_terms:
                searchable = " ".join(
                    part
                    for part in (
                        sym.file_path,
                        sym.name,
                        sym.qualified_name,
                        sym.signature or "",
                        sym.docstring or "",
                    )
                    if part
                ).lower()
                matched_count = sum(1 for term in lexical_terms if term in searchable)
                if matched_count > 0:
                    ratio = matched_count / len(lexical_terms)
                    state = _get_or_add(sym)
                    state.lexical_score = max(state.lexical_score, ratio)
                    state.match_sources.add("lexical")

        # Stage 2: Query PostgreSQL structural dependency edges around seed symbols
        seed_qnames = {state.symbol.qualified_name for state in candidates.values()}
        seed_modules = {
            sym.qualified_name
            for sym in all_version_symbols
            if sym.kind == "module"
            and sym.file_path in {state.symbol.file_path for state in candidates.values()}
        }
        frontier = seed_qnames.union(seed_modules)
        collected_relationships: list[RetrievedRelationship] = []

        if frontier:
            rel_rows = list(
                self._db.scalars(
                    select(CodeRelationshipModel).where(
                        CodeRelationshipModel.repository_id == repository.id,
                        CodeRelationshipModel.commit_sha == commit_sha,
                        or_(
                            CodeRelationshipModel.source_symbol.in_(frontier),
                            CodeRelationshipModel.target_symbol.in_(frontier),
                        ),
                    )
                ).all()
            )
            for rel in rel_rows:
                collected_relationships.append(
                    RetrievedRelationship(
                        source_symbol=rel.source_symbol,
                        target_symbol=rel.target_symbol,
                        relationship_type=rel.relationship_type,
                        confidence=rel.confidence,
                        file_path=rel.file_path,
                        line=rel.line,
                    )
                )
                neighbor_qname = (
                    rel.target_symbol if rel.source_symbol in frontier else rel.source_symbol
                )
                for neighbor_sym in symbols_by_qname.get(neighbor_qname, []):
                    state = _get_or_add(neighbor_sym)
                    if state.dependency_distance is None or state.dependency_distance > 1:
                        state.dependency_distance = 1
                    state.match_sources.add(f"dependency:{rel.relationship_type}")

        # Stage 3: Semantic retrieval via Qdrant with strict structural-only fallback
        retrieval_mode: Literal["hybrid", "structural_only"] = "structural_only"
        semantic_hits = self._run_semantic_search(
            repository=repository,
            commit_sha=commit_sha,
            query=query,
            limit=top_k,
            warnings=warnings,
        )
        if semantic_hits is not None:
            retrieval_mode = "hybrid"
            for hit in semantic_hits:
                matched_sym = symbol_by_id.get(hit.symbol_id)
                if matched_sym is None:
                    continue
                state = _get_or_add(matched_sym)
                state.semantic_score = max(state.semantic_score, max(0.0, hit.score))
                state.match_sources.add("semantic")

        # Stage 4 & 5: Merge, deduplicate, and rank candidates deterministically
        ranked_states = sorted(
            candidates.values(),
            key=lambda st: (
                -st.compute_rank_score(),
                st.symbol.file_path,
                st.symbol.start_line,
                st.symbol.symbol_id,
            ),
        )

        # Stage 6 & 7: Expand bounded surrounding context and enforce token/file limits
        items: list[RetrievedContextItem] = []
        included_files: set[str] = set()
        used_tokens = 0
        truncated = False
        file_lines_cache: dict[str, list[str]] = {}
        resolved_workspace = workspace_dir.resolve() if workspace_dir is not None else None

        for state in ranked_states[:top_k]:
            sym = state.symbol
            if sym.file_path not in included_files and len(included_files) >= file_budget:
                truncated = True
                continue

            excerpt = self._extract_bounded_excerpt(
                symbol=sym,
                workspace_dir=resolved_workspace,
                file_lines_cache=file_lines_cache,
                padding=context_padding,
            )
            item_tokens = _estimate_tokens(excerpt)

            if used_tokens + item_tokens > token_budget:
                truncated = True
                remaining_chars = max(0, (token_budget - used_tokens) * 4)
                if remaining_chars < 120:
                    break
                excerpt = (
                    excerpt[:remaining_chars]
                    + "\n# [truncated due to retrieval token budget]"
                )
                item_tokens = _estimate_tokens(excerpt)
                if used_tokens + item_tokens > token_budget:
                    break

            included_files.add(sym.file_path)
            used_tokens += item_tokens
            items.append(
                RetrievedContextItem(
                    symbol_id=sym.symbol_id,
                    name=sym.name,
                    qualified_name=sym.qualified_name,
                    kind=sym.kind,
                    file_path=sym.file_path,
                    commit_sha=sym.commit_sha,
                    content_hash=sym.content_hash,
                    start_line=sym.start_line,
                    end_line=sym.end_line,
                    is_test=sym.is_test,
                    signature=sym.signature,
                    docstring=sym.docstring,
                    excerpt=excerpt,
                    score=round(state.compute_rank_score(), 4),
                    match_sources=sorted(state.match_sources),
                    dependency_distance=state.dependency_distance or 0,
                    estimated_tokens=item_tokens,
                )
            )

        if len(ranked_states) > top_k:
            truncated = True

        return RetrievalResponse(
            repository_id=repository.id,
            commit_sha=commit_sha,
            query=query,
            retrieval_mode=retrieval_mode,
            items=items,
            relationships=collected_relationships[:100],
            total_estimated_tokens=used_tokens,
            files_included=len(included_files),
            truncated=truncated,
            warnings=warnings,
        )

    def _run_semantic_search(
        self,
        *,
        repository: RepositoryModel,
        commit_sha: str,
        query: str,
        limit: int,
        warnings: list[str],
    ) -> list[VectorSearchHit] | None:
        if not self._embeddings.is_enabled:
            warnings.append("Structural-only retrieval: embedding provider disabled.")
            return None

        try:
            enforce_data_sharing_policy(
                is_private=repository.is_private,
                llm_data_sharing=repository.llm_data_sharing,
                provider=self._embeddings.provider,
                target_purpose="embedding",
            )
        except AuthorizationError:
            warnings.append(
                "Structural-only retrieval: private repository denies external embedding queries."
            )
            return None

        if not self._qdrant.is_available():
            warnings.append("Structural-only retrieval: Qdrant is unavailable.")
            return None

        scan_result = scan_outbound_payload(query, mode="redact")
        try:
            vectors = self._embeddings.embed_texts([scan_result.sanitized_text])
        except AppError as exc:
            warnings.append(
                f"Structural-only retrieval: embedding query failed ({exc.code})."
            )
            return None

        if not vectors or not vectors[0]:
            return None

        return self._qdrant.search(
            repository_id=repository.id,
            commit_sha=commit_sha,
            query_vector=vectors[0],
            limit=limit,
        )

    def _extract_bounded_excerpt(
        self,
        *,
        symbol: CodeSymbolModel,
        workspace_dir: Path | None,
        file_lines_cache: dict[str, list[str]],
        padding: int,
    ) -> str:
        if workspace_dir is not None:
            if symbol.file_path not in file_lines_cache:
                candidate_path = (workspace_dir / symbol.file_path).resolve()
                if candidate_path.is_relative_to(workspace_dir) and candidate_path.is_file():
                    try:
                        file_lines_cache[symbol.file_path] = candidate_path.read_text(
                            encoding="utf-8"
                        ).splitlines()
                    except (OSError, UnicodeDecodeError):
                        file_lines_cache[symbol.file_path] = []
                else:
                    file_lines_cache[symbol.file_path] = []

            lines = file_lines_cache[symbol.file_path]
            if lines:
                start_line = max(1, symbol.start_line - padding)
                end_line = min(
                    len(lines),
                    symbol.end_line + padding,
                    start_line + MAX_EXCERPT_LINES - 1,
                )
                numbered = [
                    f"{line_no}: {lines[line_no - 1]}"
                    for line_no in range(start_line, end_line + 1)
                ]
                return "\n".join(numbered)

        parts = [f"# {symbol.file_path}:{symbol.start_line}-{symbol.end_line}"]
        if symbol.signature:
            parts.append(symbol.signature)
        if symbol.docstring:
            parts.append(f'"""{symbol.docstring}"""')
        return "\n".join(parts)