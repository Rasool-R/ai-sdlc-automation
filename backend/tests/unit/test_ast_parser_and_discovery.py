"""Unit tests for repository discovery, ignore rules, and static Python AST parsing."""

from pathlib import Path

import pytest
from app.core.config import load_settings
from app.core.exceptions import ValidationFailedError
from app.intelligence.ast_parser import PythonAstParser
from app.intelligence.discovery import RepositoryDiscoveryService
from cryptography.fernet import Fernet


def _write_file(base: Path, relative_path: str, content: str | bytes) -> Path:
    target = base / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    return target


def test_discovery_ignores_generated_dirs_binaries_and_enforces_limits(tmp_path: Path) -> None:
    _write_file(tmp_path, "app/main.py", "def run() -> int:\n    return 1\n")
    _write_file(tmp_path, "tests/test_main.py", "def test_run() -> None:\n    assert True\n")
    _write_file(tmp_path, ".git/config", "[core]\n")
    _write_file(tmp_path, "__pycache__/main.cpython-311.pyc", b"\x00\x01\x02")
    _write_file(tmp_path, "node_modules/pkg/index.js", "console.log('ignore');\n")
    _write_file(tmp_path, "assets/logo.bin", b"\x89PNG\r\n\x1a\n\x00\x00")
    _write_file(tmp_path, "data/raw.dat", b"abc\x00def")

    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(CREDENTIAL_ENCRYPTION_KEYS=fernet_key)
    discovery = RepositoryDiscoveryService(settings=settings)

    files, skipped = discovery.discover(tmp_path)
    discovered_paths = {f.relative_path for f in files}
    assert discovered_paths == {"app/main.py", "tests/test_main.py"}
    assert skipped >= 2

    main_file = next(f for f in files if f.relative_path == "app/main.py")
    test_file = next(f for f in files if f.relative_path == "tests/test_main.py")
    assert main_file.is_python is True
    assert main_file.is_test_file is False
    assert len(main_file.content_hash) == 64
    assert test_file.is_test_file is True

    # Enforce MAX_REPO_PYTHON_FILES limit
    strict_settings = load_settings(
        CREDENTIAL_ENCRYPTION_KEYS=fernet_key,
        MAX_REPO_PYTHON_FILES=10,
    )
    for idx in range(11):
        _write_file(tmp_path, f"pkg/mod_{idx}.py", f"VALUE = {idx}\n")

    strict_discovery = RepositoryDiscoveryService(settings=strict_settings)
    with pytest.raises(ValidationFailedError):
        strict_discovery.discover(tmp_path)


def test_ast_parser_extracts_full_metadata_relationships_and_dependencies(
    tmp_path: Path,
) -> None:
    _write_file(
        tmp_path,
        "pyproject.toml",
        (
            "[project]\n"
            'name = "sample-api"\n'
            'dependencies = ["fastapi>=0.115.0", "pydantic==2.10.4"]\n'
            "[project.optional-dependencies]\n"
            'dev = ["pytest>=8.0"]\n'
        ),
    )
    _write_file(
        tmp_path,
        "app/__init__.py",
        '"""Sample FastAPI package."""\n',
    )
    _write_file(
        tmp_path,
        "app/orders.py",
        (
            '"""Order domain services."""\n\n'
            "from typing import Any\n\n"
            "class BaseService:\n"
            "    pass\n\n"
            "class OrderService(BaseService):\n"
            '    """Handles order queries."""\n\n'
            "    async def list_orders(self, limit: int = 20) -> list[dict[str, Any]]:\n"
            "        return helper_fetch(limit)\n\n"
            "def helper_fetch(limit: int) -> list[dict[str, Any]]:\n"
            "    return [{'id': i} for i in range(limit)]\n"
        ),
    )
    _write_file(
        tmp_path,
        "tests/test_orders.py",
        (
            "from app.orders import OrderService, helper_fetch\n\n"
            "def test_helper_fetch() -> None:\n"
            "    assert len(helper_fetch(2)) == 2\n"
        ),
    )
    _write_file(
        tmp_path,
        "app/broken.py",
        "def invalid_syntax(:\n    pass\n",
    )

    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(CREDENTIAL_ENCRYPTION_KEYS=fernet_key)
    parser = PythonAstParser(discovery_service=RepositoryDiscoveryService(settings=settings))

    result = parser.analyze_workspace(
        workspace_dir=tmp_path,
        repository_id="repo-001",
        commit_sha="d" * 40,
    )

    assert result.schema_version == "1.0"
    assert result.commit_sha == "d" * 40
    assert len(result.parse_errors) == 1
    assert result.parse_errors[0].file_path == "app/broken.py"
    assert result.parse_errors[0].error_type == "SyntaxError"

    # Dependencies extracted statically from pyproject.toml
    dep_names = {d.name for d in result.dependencies}
    assert {"fastapi", "pydantic", "pytest"}.issubset(dep_names)

    # Module, class, method, and function extraction
    orders_mod = next(m for m in result.modules if m.file_path == "app/orders.py")
    assert orders_mod.module_name == "app.orders"
    assert orders_mod.docstring == "Order domain services."
    order_cls = next(c for c in orders_mod.classes if c.name == "OrderService")
    assert order_cls.bases == ["BaseService"]
    assert len(order_cls.methods) == 1
    assert order_cls.methods[0].is_async is True
    assert order_cls.methods[0].return_annotation == "list[dict[str, Any]]"
    assert "helper_fetch" in order_cls.methods[0].calls

    # Symbols and relationships with explicit confidence labels
    symbol_qnames = {s.qualified_name for s in result.symbols}
    assert "app.orders.OrderService" in symbol_qnames
    assert "app.orders.OrderService.list_orders" in symbol_qnames
    assert "app.orders.helper_fetch" in symbol_qnames

    inherits_edges = [
        r for r in result.relationships if r.relationship_type == "inherits"
    ]
    assert any(
        e.source_symbol == "app.orders.OrderService"
        and e.target_symbol == "BaseService"
        and e.confidence == "statically_confirmed"
        for e in inherits_edges
    )

    call_edges = [r for r in result.relationships if r.relationship_type == "calls"]
    assert any(
        e.source_symbol == "app.orders.OrderService.list_orders"
        and e.target_symbol == "helper_fetch"
        and e.confidence == "inferred"
        for e in call_edges
    )

    test_edges = [r for r in result.relationships if r.relationship_type == "tests"]
    assert any(
        e.source_symbol == "tests.test_orders"
        and e.target_symbol == "app.orders"
        and e.confidence == "inferred"
        for e in test_edges
    )


def test_ast_parser_never_executes_untrusted_code(tmp_path: Path) -> None:
    marker_file = tmp_path / "pwned_marker.txt"
    malicious_source = (
        "from pathlib import Path\n"
        f"Path({str(marker_file)!r}).write_text('EXECUTED', encoding='utf-8')\n"
        "def benign() -> None:\n"
        "    pass\n"
    )
    _write_file(tmp_path, "evil.py", malicious_source)

    fernet_key = Fernet.generate_key().decode("utf-8")
    settings = load_settings(CREDENTIAL_ENCRYPTION_KEYS=fernet_key)
    parser = PythonAstParser(discovery_service=RepositoryDiscoveryService(settings=settings))

    result = parser.analyze_workspace(
        workspace_dir=tmp_path,
        repository_id="repo-evil",
        commit_sha="e" * 40,
    )

    assert not marker_file.exists()
    assert len(result.parse_errors) == 0
    assert any(s.qualified_name == "evil.benign" for s in result.symbols)