"""Static Python AST parser extracting validated metadata, symbols, and relationships.

Source code is strictly parsed via `ast.parse` and is never imported or executed.
"""

import ast
import re
import tomllib
from pathlib import Path

from pydantic import ValidationError

from app.intelligence.discovery import RepositoryDiscoveryService
from app.intelligence.schemas import (
    ClassMetadata,
    CodeRelationship,
    CodeSymbol,
    DependencyMetadata,
    DiscoveredFile,
    FileMetadata,
    FunctionMetadata,
    ImportMetadata,
    MethodMetadata,
    ModuleMetadata,
    ParseErrorRecord,
    RepositoryMetadata,
)

_REQUIREMENT_PATTERN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]+\])?(.*)$")


def path_to_module_name(relative_path: str) -> str:
    """Convert a POSIX relative Python path into a dotted module name."""
    path_obj = Path(relative_path)
    parts = list(path_obj.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return path_obj.stem
    return ".".join(parts)


def _format_arguments(args_node: ast.arguments) -> list[str]:
    formatted: list[str] = []

    def _fmt_arg(arg: ast.arg, prefix: str = "") -> str:
        if arg.annotation is not None:
            return f"{prefix}{arg.arg}: {ast.unparse(arg.annotation)}"
        return f"{prefix}{arg.arg}"

    for arg in args_node.posonlyargs:
        formatted.append(_fmt_arg(arg))
    if args_node.posonlyargs:
        formatted.append("/")

    for arg in args_node.args:
        formatted.append(_fmt_arg(arg))

    if args_node.vararg is not None:
        formatted.append(_fmt_arg(args_node.vararg, prefix="*"))
    elif args_node.kwonlyargs:
        formatted.append("*")

    for arg in args_node.kwonlyargs:
        formatted.append(_fmt_arg(arg))

    if args_node.kwarg is not None:
        formatted.append(_fmt_arg(args_node.kwarg, prefix="**"))

    return formatted


def _build_signature(
    name: str,
    arguments: list[str],
    return_annotation: str | None,
    *,
    is_async: bool,
) -> str:
    prefix = "async def " if is_async else "def "
    args_str = ", ".join(arguments)
    ret_str = f" -> {return_annotation}" if return_annotation else ""
    return f"{prefix}{name}({args_str}){ret_str}"


class _LocalCallCollector(ast.NodeVisitor):
    """Collect statically discoverable call targets within a single function or method body."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._seen: set[str] = set()

    def visit_FunctionDef(self, _node: ast.FunctionDef) -> None:  # noqa: N802
        return

    def visit_AsyncFunctionDef(self, _node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        return

    def visit_ClassDef(self, _node: ast.ClassDef) -> None:  # noqa: N802
        return

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        target: str | None = None
        if isinstance(node.func, ast.Name):
            target = node.func.id
        elif isinstance(node.func, ast.Attribute):
            target = ast.unparse(node.func)

        if target and target not in self._seen:
            self._seen.add(target)
            self.calls.append(target)

        self.generic_visit(node)


def _collect_calls(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    collector = _LocalCallCollector()
    for stmt in func_node.body:
        collector.visit(stmt)
    return collector.calls


def _parse_requirement_entry(raw_entry: str) -> tuple[str, str] | None:
    stripped = raw_entry.split("#", 1)[0].strip()
    if not stripped or stripped.startswith(("-", "--")):
        return None
    match = _REQUIREMENT_PATTERN.match(stripped)
    if not match:
        return None
    name = match.group(1).strip()
    specifier = match.group(2).strip()
    return name, specifier


class PythonAstParser:
    """Parses Python repositories statically via AST into validated Pydantic metadata."""

    def __init__(self, *, discovery_service: RepositoryDiscoveryService | None = None) -> None:
        self._discovery = discovery_service or RepositoryDiscoveryService()

    @property
    def discovery_service(self) -> RepositoryDiscoveryService:
        """Return the configured repository file discovery service."""
        return self._discovery

    def parse_python_source(
        self,
        *,
        source: str,
        file_path: str,
        content_hash: str,
        is_test_module: bool = False,
    ) -> tuple[ModuleMetadata | None, ParseErrorRecord | None]:
        """Parse a single Python source string using AST without executing any code."""
        try:
            tree = ast.parse(source, filename=file_path)
        except SyntaxError as exc:
            return None, ParseErrorRecord(
                file_path=file_path,
                line=exc.lineno if exc.lineno and exc.lineno >= 1 else None,
                column=exc.offset if exc.offset is not None and exc.offset >= 0 else None,
                error_type="SyntaxError",
                message=exc.msg or str(exc),
            )
        except ValueError as exc:
            return None, ParseErrorRecord(
                file_path=file_path,
                error_type=type(exc).__name__,
                message=str(exc),
            )

        module_name = path_to_module_name(file_path)
        imports: list[ImportMetadata] = []
        classes: list[ClassMetadata] = []
        functions: list[FunctionMetadata] = []

        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    alias_map = {alias.name: alias.asname} if alias.asname else {}
                    imports.append(
                        ImportMetadata(
                            module=alias.name,
                            names=[],
                            aliases=alias_map,
                            is_from_import=False,
                            level=0,
                            line=node.lineno,
                        )
                    )
            elif isinstance(node, ast.ImportFrom):
                names = [alias.name for alias in node.names]
                alias_map = {alias.name: alias.asname for alias in node.names if alias.asname}
                imports.append(
                    ImportMetadata(
                        module=node.module or "",
                        names=names,
                        aliases=alias_map,
                        is_from_import=True,
                        level=node.level,
                        line=node.lineno,
                    )
                )
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                functions.append(self._extract_function(node, module_name))
            elif isinstance(node, ast.ClassDef):
                classes.append(self._extract_class(node, module_name))

        line_count = len(source.splitlines())
        try:
            module_meta = ModuleMetadata(
                module_name=module_name,
                file_path=file_path,
                content_hash=content_hash,
                docstring=ast.get_docstring(tree),
                is_test_module=is_test_module,
                imports=imports,
                classes=classes,
                functions=functions,
                line_count=line_count,
            )
        except ValidationError as exc:
            return None, ParseErrorRecord(
                file_path=file_path,
                error_type="MetadataValidationError",
                message=str(exc),
            )

        return module_meta, None

    def _extract_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        module_name: str,
    ) -> FunctionMetadata:
        return FunctionMetadata(
            name=node.name,
            qualified_name=f"{module_name}.{node.name}",
            arguments=_format_arguments(node.args),
            return_annotation=ast.unparse(node.returns) if node.returns is not None else None,
            decorators=[ast.unparse(dec) for dec in node.decorator_list],
            docstring=ast.get_docstring(node),
            is_async=isinstance(node, ast.AsyncFunctionDef),
            calls=_collect_calls(node),
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
        )

    def _extract_class(self, node: ast.ClassDef, module_name: str) -> ClassMetadata:
        class_qname = f"{module_name}.{node.name}"
        methods: list[MethodMetadata] = []
        for item in node.body:
            if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                methods.append(
                    MethodMetadata(
                        name=item.name,
                        qualified_name=f"{class_qname}.{item.name}",
                        arguments=_format_arguments(item.args),
                        return_annotation=(
                            ast.unparse(item.returns) if item.returns is not None else None
                        ),
                        decorators=[ast.unparse(dec) for dec in item.decorator_list],
                        docstring=ast.get_docstring(item),
                        is_async=isinstance(item, ast.AsyncFunctionDef),
                        calls=_collect_calls(item),
                        start_line=item.lineno,
                        end_line=item.end_lineno or item.lineno,
                    )
                )

        return ClassMetadata(
            name=node.name,
            qualified_name=class_qname,
            bases=[ast.unparse(base) for base in node.bases],
            decorators=[ast.unparse(dec) for dec in node.decorator_list],
            docstring=ast.get_docstring(node),
            methods=methods,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
        )

    def extract_dependencies(
        self,
        workspace_dir: Path,
        discovered_files: list[DiscoveredFile],
    ) -> list[DependencyMetadata]:
        """Extract statically declared package dependencies from manifest files."""
        dependencies: list[DependencyMetadata] = []
        for file_entry in discovered_files:
            rel_path = file_entry.relative_path
            filename = Path(rel_path).name
            full_path = workspace_dir / rel_path

            if filename == "pyproject.toml":
                try:
                    raw_toml = tomllib.loads(full_path.read_text(encoding="utf-8"))
                except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
                    continue
                project_section = raw_toml.get("project", {})
                if isinstance(project_section, dict):
                    runtime_deps = project_section.get("dependencies", [])
                    if isinstance(runtime_deps, list):
                        for entry in runtime_deps:
                            if isinstance(entry, str):
                                parsed = _parse_requirement_entry(entry)
                                if parsed:
                                    dependencies.append(
                                        DependencyMetadata(
                                            name=parsed[0],
                                            specifier=parsed[1],
                                            source_file=rel_path,
                                            group="runtime",
                                            confidence="statically_confirmed",
                                        )
                                    )
                    opt_deps = project_section.get("optional-dependencies", {})
                    if isinstance(opt_deps, dict):
                        for group_name, entries in opt_deps.items():
                            if isinstance(entries, list):
                                for entry in entries:
                                    if isinstance(entry, str):
                                        parsed = _parse_requirement_entry(entry)
                                        if parsed:
                                            dependencies.append(
                                                DependencyMetadata(
                                                    name=parsed[0],
                                                    specifier=parsed[1],
                                                    source_file=rel_path,
                                                    group=str(group_name),
                                                    confidence="statically_confirmed",
                                                )
                                            )
            elif filename.startswith("requirements") and filename.endswith(".txt"):
                try:
                    lines = full_path.read_text(encoding="utf-8").splitlines()
                except (OSError, UnicodeDecodeError):
                    continue
                group = "dev" if "dev" in filename else "runtime"
                for line in lines:
                    parsed = _parse_requirement_entry(line)
                    if parsed:
                        dependencies.append(
                            DependencyMetadata(
                                name=parsed[0],
                                specifier=parsed[1],
                                source_file=rel_path,
                                group=group,
                                confidence="statically_confirmed",
                            )
                        )
        return dependencies

    def _resolve_relative_import(self, current_module: str, target_module: str, level: int) -> str:
        if level == 0:
            return target_module
        parts = current_module.split(".")
        if level > len(parts):
            return target_module
        base_parts = parts[:-level]
        if target_module:
            base_parts.append(target_module)
        return ".".join(p for p in base_parts if p)

    def build_symbols_and_relationships(
        self,
        *,
        modules: list[ModuleMetadata],
        commit_sha: str,
        known_non_test_modules: set[str] | None = None,
    ) -> tuple[list[CodeSymbol], list[CodeRelationship]]:
        """Derive validated symbols and directed structural edges from parsed modules."""
        symbols: list[CodeSymbol] = []
        relationships: list[CodeRelationship] = []
        non_test_modules = (
            set(known_non_test_modules)
            if known_non_test_modules is not None
            else {mod.module_name for mod in modules if not mod.is_test_module}
        )

        for mod in modules:
            end_line = max(mod.line_count, 1)
            symbols.append(
                CodeSymbol(
                    symbol_id=f"{mod.file_path}::module::{mod.module_name}",
                    name=mod.module_name.split(".")[-1],
                    qualified_name=mod.module_name,
                    kind="module",
                    file_path=mod.file_path,
                    commit_sha=commit_sha,
                    content_hash=mod.content_hash,
                    start_line=1,
                    end_line=end_line,
                    docstring=mod.docstring,
                    is_test=mod.is_test_module,
                )
            )

            for imp in mod.imports:
                resolved_target = self._resolve_relative_import(
                    mod.module_name,
                    imp.module,
                    imp.level,
                )
                confidence = "statically_confirmed" if resolved_target else "incomplete"
                target_label = resolved_target or "." * max(imp.level, 1)
                relationships.append(
                    CodeRelationship(
                        source_symbol=mod.module_name,
                        target_symbol=target_label,
                        relationship_type="imports",
                        confidence=confidence,
                        file_path=mod.file_path,
                        line=imp.line,
                        commit_sha=commit_sha,
                    )
                )

                if mod.is_test_module and resolved_target:
                    matched_source = self._match_internal_module(resolved_target, non_test_modules)
                    if matched_source:
                        relationships.append(
                            CodeRelationship(
                                source_symbol=mod.module_name,
                                target_symbol=matched_source,
                                relationship_type="tests",
                                confidence="inferred",
                                file_path=mod.file_path,
                                line=imp.line,
                                commit_sha=commit_sha,
                            )
                        )

            for func in mod.functions:
                symbols.append(
                    CodeSymbol(
                        symbol_id=f"{mod.file_path}::function::{func.qualified_name}",
                        name=func.name,
                        qualified_name=func.qualified_name,
                        kind="function",
                        file_path=mod.file_path,
                        commit_sha=commit_sha,
                        content_hash=mod.content_hash,
                        start_line=func.start_line,
                        end_line=func.end_line,
                        signature=_build_signature(
                            func.name,
                            func.arguments,
                            func.return_annotation,
                            is_async=func.is_async,
                        ),
                        docstring=func.docstring,
                        is_test=mod.is_test_module,
                    )
                )
                relationships.append(
                    CodeRelationship(
                        source_symbol=mod.module_name,
                        target_symbol=func.qualified_name,
                        relationship_type="defines",
                        confidence="statically_confirmed",
                        file_path=mod.file_path,
                        line=func.start_line,
                        commit_sha=commit_sha,
                    )
                )
                for callee in func.calls:
                    relationships.append(
                        CodeRelationship(
                            source_symbol=func.qualified_name,
                            target_symbol=callee,
                            relationship_type="calls",
                            confidence="inferred",
                            file_path=mod.file_path,
                            line=func.start_line,
                            commit_sha=commit_sha,
                        )
                    )

            for cls in mod.classes:
                symbols.append(
                    CodeSymbol(
                        symbol_id=f"{mod.file_path}::class::{cls.qualified_name}",
                        name=cls.name,
                        qualified_name=cls.qualified_name,
                        kind="class",
                        file_path=mod.file_path,
                        commit_sha=commit_sha,
                        content_hash=mod.content_hash,
                        start_line=cls.start_line,
                        end_line=cls.end_line,
                        signature=(
                            f"class {cls.name}({', '.join(cls.bases)})"
                            if cls.bases
                            else f"class {cls.name}"
                        ),
                        docstring=cls.docstring,
                        is_test=mod.is_test_module,
                    )
                )
                relationships.append(
                    CodeRelationship(
                        source_symbol=mod.module_name,
                        target_symbol=cls.qualified_name,
                        relationship_type="defines",
                        confidence="statically_confirmed",
                        file_path=mod.file_path,
                        line=cls.start_line,
                        commit_sha=commit_sha,
                    )
                )
                for base_name in cls.bases:
                    relationships.append(
                        CodeRelationship(
                            source_symbol=cls.qualified_name,
                            target_symbol=base_name,
                            relationship_type="inherits",
                            confidence="statically_confirmed",
                            file_path=mod.file_path,
                            line=cls.start_line,
                            commit_sha=commit_sha,
                        )
                    )
                for method in cls.methods:
                    symbols.append(
                        CodeSymbol(
                            symbol_id=f"{mod.file_path}::method::{method.qualified_name}",
                            name=method.name,
                            qualified_name=method.qualified_name,
                            kind="method",
                            file_path=mod.file_path,
                            commit_sha=commit_sha,
                            content_hash=mod.content_hash,
                            start_line=method.start_line,
                            end_line=method.end_line,
                            signature=_build_signature(
                                method.name,
                                method.arguments,
                                method.return_annotation,
                                is_async=method.is_async,
                            ),
                            docstring=method.docstring,
                            is_test=mod.is_test_module,
                        )
                    )
                    relationships.append(
                        CodeRelationship(
                            source_symbol=cls.qualified_name,
                            target_symbol=method.qualified_name,
                            relationship_type="defines",
                            confidence="statically_confirmed",
                            file_path=mod.file_path,
                            line=method.start_line,
                            commit_sha=commit_sha,
                        )
                    )
                    for callee in method.calls:
                        relationships.append(
                            CodeRelationship(
                                source_symbol=method.qualified_name,
                                target_symbol=callee,
                                relationship_type="calls",
                                confidence="inferred",
                                file_path=mod.file_path,
                                line=method.start_line,
                                commit_sha=commit_sha,
                            )
                        )

            if mod.is_test_module:
                stem = Path(mod.file_path).stem
                candidate_stem = stem.removeprefix("test_").removesuffix("_test")
                if candidate_stem:
                    for src_mod_name in sorted(non_test_modules):
                        if src_mod_name.split(".")[-1] == candidate_stem:
                            relationships.append(
                                CodeRelationship(
                                    source_symbol=mod.module_name,
                                    target_symbol=src_mod_name,
                                    relationship_type="tests",
                                    confidence="inferred",
                                    file_path=mod.file_path,
                                    line=1,
                                    commit_sha=commit_sha,
                                )
                            )

        deduped: list[CodeRelationship] = []
        seen_edges: set[tuple[str, str, str, str, int | None]] = set()
        for rel in relationships:
            key = (
                rel.source_symbol,
                rel.target_symbol,
                rel.relationship_type,
                rel.file_path,
                rel.line,
            )
            if key not in seen_edges:
                seen_edges.add(key)
                deduped.append(rel)

        return symbols, deduped

    def _match_internal_module(
        self,
        imported_module: str,
        non_test_modules: set[str],
    ) -> str | None:
        if imported_module in non_test_modules:
            return imported_module
        for candidate in sorted(non_test_modules):
            if imported_module.endswith(f".{candidate}") or candidate.endswith(
                f".{imported_module}"
            ):
                return candidate
        return None

    def analyze_workspace(
        self,
        *,
        workspace_dir: Path,
        repository_id: str,
        commit_sha: str,
    ) -> RepositoryMetadata:
        """Discover and statically analyze all supported files in an isolated workspace."""
        discovered_files, skipped_count = self._discovery.discover(workspace_dir)
        files_meta: list[FileMetadata] = []
        modules_meta: list[ModuleMetadata] = []
        parse_errors: list[ParseErrorRecord] = []

        for item in discovered_files:
            full_path = workspace_dir / item.relative_path
            try:
                text_content = full_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                parse_errors.append(
                    ParseErrorRecord(
                        file_path=item.relative_path,
                        error_type=type(exc).__name__,
                        message=str(exc),
                    )
                )
                continue

            line_count = len(text_content.splitlines())
            parsed_module: ModuleMetadata | None = None

            if item.is_python:
                parsed_module, parse_error = self.parse_python_source(
                    source=text_content,
                    file_path=item.relative_path,
                    content_hash=item.content_hash,
                    is_test_module=item.is_test_file,
                )
                if parse_error is not None:
                    parse_errors.append(parse_error)
                if parsed_module is not None:
                    modules_meta.append(parsed_module)

            files_meta.append(
                FileMetadata(
                    relative_path=item.relative_path,
                    size_bytes=item.size_bytes,
                    content_hash=item.content_hash,
                    language=item.language,
                    is_test_file=item.is_test_file,
                    line_count=line_count,
                    module=parsed_module,
                )
            )

        dependencies = self.extract_dependencies(workspace_dir, discovered_files)
        symbols, relationships = self.build_symbols_and_relationships(
            modules=modules_meta,
            commit_sha=commit_sha,
        )

        return RepositoryMetadata(
            repository_id=repository_id,
            commit_sha=commit_sha,
            files=files_meta,
            modules=modules_meta,
            dependencies=dependencies,
            symbols=symbols,
            relationships=relationships,
            parse_errors=parse_errors,
            skipped_files_count=skipped_count,
        )