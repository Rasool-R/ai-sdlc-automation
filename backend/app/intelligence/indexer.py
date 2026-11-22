"""Incremental structural and semantic repository indexer with privacy enforcement."""

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.exceptions import AppError, AuthorizationError
from app.core.secret_scanner import enforce_data_sharing_policy, scan_outbound_payload
from app.integrations.embeddings.client import EmbeddingClient
from app.integrations.qdrant.client import QdrantIndexClient, SemanticChunkRecord
from app.intelligence.ast_parser import PythonAstParser, path_to_module_name
from app.intelligence.discovery import RepositoryDiscoveryService
from app.intelligence.schemas import CodeRelationship, CodeSymbol, ModuleMetadata, ParseErrorRecord
from app.models.intelligence import (
    CodeRelationshipModel,
    CodeSymbolModel,
    RepositoryIndexSnapshotModel,
)
from app.models.repository import RepositoryModel

logger = logging.getLogger(__name__)

EMBEDDING_BATCH_SIZE = 32
MAX_EMBEDDING_SNIPPET_LINES = 45
MAX_EMBEDDING_CHARS = 3500


class IndexResult(BaseModel):
    """Structured outcome of an incremental or full repository indexing operation."""

    snapshot_id: str
    repository_id: str
    commit_sha: str
    status: str = "completed"
    files_indexed_count: int = Field(ge=0)
    symbols_count: int = Field(ge=0)
    relationships_count: int = Field(ge=0)
    parse_errors_count: int = Field(ge=0)
    semantic_indexed: bool = False
    reused_snapshot: bool = False
    unchanged_files_skipped: int = Field(default=0, ge=0)
    reindexed_files: int = Field(default=0, ge=0)
    deleted_files: int = Field(default=0, ge=0)
    secrets_redacted_count: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)


def _build_logical_unit_text(
    symbol: CodeSymbol,
    file_lines_cache: dict[str, list[str]],
    workspace_dir: Path,
) -> str:
    """Build a bounded text representation of a logical code unit for semantic embedding."""
    if symbol.file_path not in file_lines_cache:
        full_path = workspace_dir / symbol.file_path
        try:
            file_lines_cache[symbol.file_path] = full_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            file_lines_cache[symbol.file_path] = []

    lines = file_lines_cache[symbol.file_path]
    header_parts = [
        f"path: {symbol.file_path}",
        f"kind: {symbol.kind}",
        f"symbol: {symbol.qualified_name}",
    ]
    if symbol.signature:
        header_parts.append(f"signature: {symbol.signature}")
    if symbol.docstring:
        header_parts.append(f"docstring: {symbol.docstring[:500]}")

    start_idx = max(0, symbol.start_line - 1)
    end_idx = min(len(lines), start_idx + MAX_EMBEDDING_SNIPPET_LINES, symbol.end_line)
    snippet = "\n".join(lines[start_idx:end_idx])
    if snippet:
        header_parts.append(f"code:\n{snippet}")

    combined = "\n".join(header_parts)
    return combined[:MAX_EMBEDDING_CHARS]


class RepositoryIndexer:
    """Indexes Python workspaces into PostgreSQL structural tables and optional Qdrant vectors."""

    def __init__(
        self,
        db: Session,
        *,
        settings: Settings | None = None,
        parser: PythonAstParser | None = None,
        embedding_client: EmbeddingClient | None = None,
        qdrant_client: QdrantIndexClient | None = None,
    ) -> None:
        self._db = db
        self._settings = settings or get_settings()
        self._parser = parser or PythonAstParser(
            discovery_service=RepositoryDiscoveryService(settings=self._settings)
        )
        self._embeddings = embedding_client or EmbeddingClient(settings=self._settings)
        self._qdrant = qdrant_client or QdrantIndexClient(settings=self._settings)

    def index_workspace(
        self,
        *,
        repository: RepositoryModel,
        workspace_dir: Path,
        commit_sha: str,
        force_reindex: bool = False,
    ) -> IndexResult:
        """Index a versioned workspace incrementally using SHA-256 content hashes."""
        existing_snapshot = self._db.scalar(
            select(RepositoryIndexSnapshotModel).where(
                RepositoryIndexSnapshotModel.repository_id == repository.id,
                RepositoryIndexSnapshotModel.commit_sha == commit_sha,
            )
        )
        if (
            existing_snapshot is not None
            and existing_snapshot.status == "completed"
            and not force_reindex
        ):
            summary = existing_snapshot.metadata_summary or {}
            return IndexResult(
                snapshot_id=existing_snapshot.id,
                repository_id=repository.id,
                commit_sha=commit_sha,
                status=existing_snapshot.status,
                files_indexed_count=existing_snapshot.files_indexed_count,
                symbols_count=existing_snapshot.symbols_count,
                relationships_count=existing_snapshot.relationships_count,
                parse_errors_count=existing_snapshot.parse_errors_count,
                semantic_indexed=existing_snapshot.semantic_indexed,
                reused_snapshot=True,
                unchanged_files_skipped=existing_snapshot.files_indexed_count,
                reindexed_files=0,
                deleted_files=0,
                secrets_redacted_count=int(summary.get("secrets_redacted_count", 0)),
                warnings=list(summary.get("warnings", [])),
            )

        previous_snapshot = self._db.scalar(
            select(RepositoryIndexSnapshotModel)
            .where(
                RepositoryIndexSnapshotModel.repository_id == repository.id,
                RepositoryIndexSnapshotModel.status == "completed",
                RepositoryIndexSnapshotModel.commit_sha != commit_sha,
            )
            .order_by(RepositoryIndexSnapshotModel.created_at.desc())
            .limit(1)
        )

        discovered_files, skipped_files_count = self._parser.discovery_service.discover(
            workspace_dir
        )
        current_hashes = {item.relative_path: item.content_hash for item in discovered_files}
        previous_hashes: dict[str, str] = (
            dict(previous_snapshot.file_hashes)
            if previous_snapshot is not None and not force_reindex
            else {}
        )

        unchanged_paths = sorted(
            path
            for path, content_hash in current_hashes.items()
            if previous_hashes.get(path) == content_hash
        )
        changed_or_added_paths = {
            path
            for path, content_hash in current_hashes.items()
            if previous_hashes.get(path) != content_hash
        }
        deleted_paths = sorted(
            path for path in previous_hashes if path not in current_hashes
        )

        known_non_test_modules = {
            path_to_module_name(item.relative_path)
            for item in discovered_files
            if item.is_python and not item.is_test_file
        }

        reused_symbols: list[CodeSymbol] = []
        reused_relationships: list[CodeRelationship] = []
        if previous_snapshot is not None and unchanged_paths:
            prev_symbol_rows = list(
                self._db.scalars(
                    select(CodeSymbolModel).where(
                        CodeSymbolModel.snapshot_id == previous_snapshot.id,
                        CodeSymbolModel.file_path.in_(unchanged_paths),
                    )
                ).all()
            )
            for sym_row in prev_symbol_rows:
                reused_symbols.append(
                    CodeSymbol(
                        symbol_id=sym_row.symbol_id,
                        name=sym_row.name,
                        qualified_name=sym_row.qualified_name,
                        kind=sym_row.kind,
                        file_path=sym_row.file_path,
                        commit_sha=commit_sha,
                        content_hash=sym_row.content_hash,
                        start_line=sym_row.start_line,
                        end_line=sym_row.end_line,
                        signature=sym_row.signature,
                        docstring=sym_row.docstring,
                        is_test=sym_row.is_test,
                    )
                )

            prev_rel_rows = list(
                self._db.scalars(
                    select(CodeRelationshipModel).where(
                        CodeRelationshipModel.snapshot_id == previous_snapshot.id,
                        CodeRelationshipModel.file_path.in_(unchanged_paths),
                    )
                ).all()
            )
            for rel_row in prev_rel_rows:
                reused_relationships.append(
                    CodeRelationship(
                        source_symbol=rel_row.source_symbol,
                        target_symbol=rel_row.target_symbol,
                        relationship_type=rel_row.relationship_type,
                        confidence=rel_row.confidence,
                        file_path=rel_row.file_path,
                        line=rel_row.line,
                        commit_sha=commit_sha,
                    )
                )

        parsed_modules: list[ModuleMetadata] = []
        parse_errors: list[ParseErrorRecord] = []
        for item in discovered_files:
            if item.relative_path not in changed_or_added_paths or not item.is_python:
                continue
            full_path = workspace_dir / item.relative_path
            try:
                source_text = full_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                parse_errors.append(
                    ParseErrorRecord(
                        file_path=item.relative_path,
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                )
                continue

            parsed_mod, err = self._parser.parse_python_source(
                source=source_text,
                file_path=item.relative_path,
                content_hash=item.content_hash,
                is_test_module=item.is_test_file,
            )
            if err is not None:
                parse_errors.append(err)
            if parsed_mod is not None:
                parsed_modules.append(parsed_mod)

        new_symbols, new_relationships = self._parser.build_symbols_and_relationships(
            modules=parsed_modules,
            commit_sha=commit_sha,
            known_non_test_modules=known_non_test_modules,
        )

        all_symbols = reused_symbols + new_symbols
        all_relationships = reused_relationships + new_relationships
        dependencies = self._parser.extract_dependencies(workspace_dir, discovered_files)

        warnings: list[str] = []
        semantic_indexed, secrets_redacted = self._run_semantic_indexing(
            repository=repository,
            workspace_dir=workspace_dir,
            commit_sha=commit_sha,
            symbols=all_symbols,
            stale_paths=sorted(changed_or_added_paths.union(deleted_paths)),
            warnings=warnings,
        )

        metadata_summary: dict[str, Any] = {
            "skipped_binary_or_ignored_files": skipped_files_count,
            "unchanged_files_skipped": len(unchanged_paths),
            "reindexed_files": len(changed_or_added_paths),
            "deleted_files": len(deleted_paths),
            "secrets_redacted_count": secrets_redacted,
            "dependencies": [dep.model_dump() for dep in dependencies],
            "parse_errors": [err.model_dump() for err in parse_errors],
            "warnings": warnings,
        }

        if existing_snapshot is not None:
            self._db.execute(
                delete(CodeSymbolModel).where(CodeSymbolModel.snapshot_id == existing_snapshot.id)
            )
            self._db.execute(
                delete(CodeRelationshipModel).where(
                    CodeRelationshipModel.snapshot_id == existing_snapshot.id
                )
            )
            snapshot = existing_snapshot
            snapshot.status = "completed"
            snapshot.file_hashes = current_hashes
            snapshot.files_indexed_count = len(discovered_files)
            snapshot.symbols_count = len(all_symbols)
            snapshot.relationships_count = len(all_relationships)
            snapshot.parse_errors_count = len(parse_errors)
            snapshot.semantic_indexed = semantic_indexed
            snapshot.metadata_summary = metadata_summary
        else:
            snapshot = RepositoryIndexSnapshotModel(
                repository_id=repository.id,
                commit_sha=commit_sha,
                status="completed",
                file_hashes=current_hashes,
                files_indexed_count=len(discovered_files),
                symbols_count=len(all_symbols),
                relationships_count=len(all_relationships),
                parse_errors_count=len(parse_errors),
                semantic_indexed=semantic_indexed,
                metadata_summary=metadata_summary,
            )
            self._db.add(snapshot)

        self._db.flush()

        for sym in all_symbols:
            self._db.add(
                CodeSymbolModel(
                    repository_id=repository.id,
                    snapshot_id=snapshot.id,
                    commit_sha=commit_sha,
                    symbol_id=sym.symbol_id,
                    name=sym.name,
                    qualified_name=sym.qualified_name,
                    kind=sym.kind,
                    file_path=sym.file_path,
                    content_hash=sym.content_hash,
                    start_line=sym.start_line,
                    end_line=sym.end_line,
                    signature=sym.signature,
                    docstring=sym.docstring,
                    is_test=sym.is_test,
                )
            )

        for rel in all_relationships:
            self._db.add(
                CodeRelationshipModel(
                    repository_id=repository.id,
                    snapshot_id=snapshot.id,
                    commit_sha=commit_sha,
                    source_symbol=rel.source_symbol,
                    target_symbol=rel.target_symbol,
                    relationship_type=rel.relationship_type,
                    confidence=rel.confidence,
                    file_path=rel.file_path,
                    line=rel.line,
                )
            )

        self._db.flush()

        return IndexResult(
            snapshot_id=snapshot.id,
            repository_id=repository.id,
            commit_sha=commit_sha,
            status=snapshot.status,
            files_indexed_count=snapshot.files_indexed_count,
            symbols_count=snapshot.symbols_count,
            relationships_count=snapshot.relationships_count,
            parse_errors_count=snapshot.parse_errors_count,
            semantic_indexed=semantic_indexed,
            reused_snapshot=False,
            unchanged_files_skipped=len(unchanged_paths),
            reindexed_files=len(changed_or_added_paths),
            deleted_files=len(deleted_paths),
            secrets_redacted_count=secrets_redacted,
            warnings=warnings,
        )

    def _run_semantic_indexing(
        self,
        *,
        repository: RepositoryModel,
        workspace_dir: Path,
        commit_sha: str,
        symbols: list[CodeSymbol],
        stale_paths: list[str],
        warnings: list[str],
    ) -> tuple[bool, int]:
        """Optionally embed logical units into Qdrant with privacy, redaction, and fallback."""
        if not symbols:
            return False, 0

        if not self._embeddings.is_enabled:
            warnings.append(
                "Semantic indexing skipped because embedding provider is disabled or unconfigured."
            )
            return False, 0

        try:
            enforce_data_sharing_policy(
                is_private=repository.is_private,
                llm_data_sharing=repository.llm_data_sharing,
                provider=self._embeddings.provider,
                target_purpose="embedding",
            )
        except AuthorizationError:
            warnings.append(
                "Semantic indexing skipped: private repository denies external data-sharing."
            )
            return False, 0

        if not self._qdrant.is_available():
            warnings.append(
                "Qdrant is unavailable; continuing with structural-only PostgreSQL index."
            )
            return False, 0

        if stale_paths:
            self._qdrant.delete_by_file_paths(
                repository_id=repository.id,
                commit_sha=commit_sha,
                file_paths=stale_paths,
            )

        file_lines_cache: dict[str, list[str]] = {}
        sanitized_texts: list[str] = []
        redacted_count = 0

        for sym in symbols:
            raw_unit_text = _build_logical_unit_text(sym, file_lines_cache, workspace_dir)
            scan_result = scan_outbound_payload(raw_unit_text, mode="redact")
            redacted_count += len(scan_result.findings)
            sanitized_texts.append(scan_result.sanitized_text)

        chunks: list[SemanticChunkRecord] = []
        try:
            for start in range(0, len(symbols), EMBEDDING_BATCH_SIZE):
                batch_symbols = symbols[start : start + EMBEDDING_BATCH_SIZE]
                batch_texts = sanitized_texts[start : start + EMBEDDING_BATCH_SIZE]
                vectors = self._embeddings.embed_texts(batch_texts)
                for sym, vec in zip(batch_symbols, vectors, strict=True):
                    chunks.append(
                        SemanticChunkRecord(
                            repository_id=repository.id,
                            commit_sha=commit_sha,
                            symbol_id=sym.symbol_id,
                            qualified_name=sym.qualified_name,
                            kind=sym.kind,
                            file_path=sym.file_path,
                            content_hash=sym.content_hash,
                            start_line=sym.start_line,
                            end_line=sym.end_line,
                            is_test=sym.is_test,
                            vector=vec,
                        )
                    )
        except AppError as exc:
            logger.warning(
                "Embedding generation failed; falling back to structural-only index",
                extra={"structured_data": {"code": exc.code, "repository_id": repository.id}},
            )
            warnings.append(
                f"Semantic embedding generation failed ({exc.code}); structural index used."
            )
            return False, redacted_count

        upserted = self._qdrant.upsert_chunks(chunks)
        if not upserted:
            warnings.append("Qdrant upsert failed; structural index remains authoritative.")
            return False, redacted_count

        return True, redacted_count