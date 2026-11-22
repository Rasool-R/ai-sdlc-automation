"""Versioned Pydantic v2 metadata contracts for Python repository intelligence."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

METADATA_SCHEMA_VERSION = "1.0"

RelationshipConfidence = Literal["statically_confirmed", "inferred", "incomplete"]
RelationshipType = Literal[
    "defines",
    "imports",
    "inherits",
    "calls",
    "tests",
    "depends_on",
]
SymbolKind = Literal["module", "class", "function", "method"]


class DiscoveredFile(BaseModel):
    """Represents a validated, non-binary repository file discovered for analysis."""

    relative_path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    content_hash: str = Field(min_length=64, max_length=64)
    language: str = "unknown"
    is_python: bool = False
    is_test_file: bool = False


class ParseErrorRecord(BaseModel):
    """Captures a non-fatal file parsing or metadata validation error."""

    file_path: str = Field(min_length=1)
    line: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=0)
    error_type: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ImportMetadata(BaseModel):
    """Validated metadata for a Python import or from-import statement."""

    module: str = ""
    names: list[str] = Field(default_factory=list)
    aliases: dict[str, str] = Field(default_factory=dict)
    is_from_import: bool = False
    level: int = Field(default=0, ge=0)
    line: int = Field(ge=1)


class MethodMetadata(BaseModel):
    """Validated metadata for a method defined inside a Python class."""

    name: str = Field(min_length=1)
    qualified_name: str = Field(min_length=1)
    arguments: list[str] = Field(default_factory=list)
    return_annotation: str | None = None
    decorators: list[str] = Field(default_factory=list)
    docstring: str | None = None
    is_async: bool = False
    calls: list[str] = Field(default_factory=list)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_line_range(self) -> "MethodMetadata":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class FunctionMetadata(BaseModel):
    """Validated metadata for a top-level Python function."""

    name: str = Field(min_length=1)
    qualified_name: str = Field(min_length=1)
    arguments: list[str] = Field(default_factory=list)
    return_annotation: str | None = None
    decorators: list[str] = Field(default_factory=list)
    docstring: str | None = None
    is_async: bool = False
    calls: list[str] = Field(default_factory=list)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_line_range(self) -> "FunctionMetadata":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class ClassMetadata(BaseModel):
    """Validated metadata for a Python class definition."""

    name: str = Field(min_length=1)
    qualified_name: str = Field(min_length=1)
    bases: list[str] = Field(default_factory=list)
    decorators: list[str] = Field(default_factory=list)
    docstring: str | None = None
    methods: list[MethodMetadata] = Field(default_factory=list)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_line_range(self) -> "ClassMetadata":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class ModuleMetadata(BaseModel):
    """Validated structural metadata extracted from a single Python module via AST."""

    module_name: str = Field(min_length=1)
    file_path: str = Field(min_length=1)
    content_hash: str = Field(min_length=64, max_length=64)
    docstring: str | None = None
    is_test_module: bool = False
    imports: list[ImportMetadata] = Field(default_factory=list)
    classes: list[ClassMetadata] = Field(default_factory=list)
    functions: list[FunctionMetadata] = Field(default_factory=list)
    line_count: int = Field(ge=0)


class FileMetadata(BaseModel):
    """Validated repository file entry linking physical file metadata to parsed module data."""

    relative_path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    content_hash: str = Field(min_length=64, max_length=64)
    language: str = "unknown"
    is_test_file: bool = False
    line_count: int = Field(default=0, ge=0)
    module: ModuleMetadata | None = None


class DependencyMetadata(BaseModel):
    """Statically detected package dependency extracted from repository manifests."""

    name: str = Field(min_length=1)
    specifier: str = ""
    source_file: str = Field(min_length=1)
    group: str = "runtime"
    confidence: RelationshipConfidence = "statically_confirmed"


class CodeSymbol(BaseModel):
    """Indexed logical code unit with exact provenance and content hash."""

    symbol_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    qualified_name: str = Field(min_length=1)
    kind: SymbolKind
    file_path: str = Field(min_length=1)
    commit_sha: str = Field(min_length=7, max_length=64)
    content_hash: str = Field(min_length=64, max_length=64)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    signature: str | None = None
    docstring: str | None = None
    is_test: bool = False

    @model_validator(mode="after")
    def validate_line_range(self) -> "CodeSymbol":
        if self.end_line < self.start_line:
            raise ValueError("end_line must be greater than or equal to start_line")
        return self


class CodeRelationship(BaseModel):
    """Directed structural or dependency edge between two symbols or modules."""

    source_symbol: str = Field(min_length=1)
    target_symbol: str = Field(min_length=1)
    relationship_type: RelationshipType
    confidence: RelationshipConfidence
    file_path: str = Field(min_length=1)
    line: int | None = Field(default=None, ge=1)
    commit_sha: str = Field(min_length=7, max_length=64)


class RepositoryMetadata(BaseModel):
    """Complete versioned structural analysis output for a repository snapshot."""

    schema_version: str = METADATA_SCHEMA_VERSION
    repository_id: str = Field(min_length=1)
    commit_sha: str = Field(min_length=7, max_length=64)
    files: list[FileMetadata] = Field(default_factory=list)
    modules: list[ModuleMetadata] = Field(default_factory=list)
    dependencies: list[DependencyMetadata] = Field(default_factory=list)
    symbols: list[CodeSymbol] = Field(default_factory=list)
    relationships: list[CodeRelationship] = Field(default_factory=list)
    parse_errors: list[ParseErrorRecord] = Field(default_factory=list)
    skipped_files_count: int = Field(default=0, ge=0)