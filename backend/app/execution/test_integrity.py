"""Deterministic AST-based test-integrity inspector detecting test weakening or tampering."""

import ast
from dataclasses import dataclass, field
from pathlib import Path

from app.execution.schemas import PatchOperationType, TestIntegrityFlag
from app.intelligence.discovery import is_python_test_path

_TEST_CONFIG_FILENAMES: frozenset[str] = frozenset(
    {
        "conftest.py",
        "pytest.ini",
        "tox.ini",
        "noxfile.py",
        ".coveragerc",
    }
)
_SKIP_DECORATOR_TOKENS: tuple[str, ...] = (
    "pytest.mark.skip",
    "pytest.mark.skipif",
    "pytest.mark.xfail",
    "mark.skip",
    "mark.skipif",
    "mark.xfail",
    "unittest.skip",
    "unittest.skipif",
    "unittest.skipunless",
    "unittest.expectedfailure",
)
_SKIP_CALL_TARGETS: frozenset[str] = frozenset(
    {
        "pytest.skip",
        "pytest.xfail",
        "skip",
        "xfail",
        "skiptest",
    }
)


@dataclass
class _TestFileMetrics:
    test_names: set[str] = field(default_factory=set)
    assertion_count: int = 0
    skip_marker_count: int = 0


class _TestMetricsVisitor(ast.NodeVisitor):
    """Collects test definitions, assertion counts, and skip/xfail markers via Python AST."""

    def __init__(self) -> None:
        self.metrics = _TestFileMetrics()
        self._class_stack: list[str] = []

    def _decorator_is_skip(self, decorator: ast.expr) -> bool:
        rendered = ast.unparse(decorator).lower()
        return any(token in rendered for token in _SKIP_DECORATOR_TOKENS)

    def _record_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        is_test_func = node.name.startswith("test_")
        if is_test_func:
            prefix = f"{'.'.join(self._class_stack)}." if self._class_stack else ""
            self.metrics.test_names.add(f"{prefix}{node.name}")

        for dec in node.decorator_list:
            if self._decorator_is_skip(dec):
                self.metrics.skip_marker_count += 1

        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        for dec in node.decorator_list:
            if self._decorator_is_skip(dec):
                self.metrics.skip_marker_count += 1

        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._record_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._record_function(node)

    def visit_Assert(self, node: ast.Assert) -> None:  # noqa: N802
        self.metrics.assertion_count += 1
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        target_lower = ""
        if isinstance(node.func, ast.Name):
            target_lower = node.func.id.lower()
        elif isinstance(node.func, ast.Attribute):
            target_lower = ast.unparse(node.func).lower()
            attr_lower = node.func.attr.lower()
            if attr_lower.startswith("assert") or target_lower in {
                "pytest.raises",
                "pytest.warns",
                "pytest.approx",
            }:
                self.metrics.assertion_count += 1
            if attr_lower in {"skip", "xfail", "skiptest"} and (
                "pytest" in target_lower or target_lower.startswith("self.")
            ):
                self.metrics.skip_marker_count += 1
                self.generic_visit(node)
                return

        if target_lower in _SKIP_CALL_TARGETS:
            self.metrics.skip_marker_count += 1

        self.generic_visit(node)


def _extract_test_metrics(source: str, file_path: str) -> _TestFileMetrics | None:
    try:
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        return None
    visitor = _TestMetricsVisitor()
    visitor.visit(tree)
    return visitor.metrics


def _is_pytest_section_modified(old_text: str, new_text: str) -> bool:
    markers = ("[tool.pytest", "[pytest]", "addopts", "filterwarnings")
    old_lines = {line.strip() for line in old_text.splitlines()}
    new_lines = {line.strip() for line in new_text.splitlines()}
    changed = old_lines.symmetric_difference(new_lines)
    return any(any(marker in line.lower() for marker in markers) for line in changed)


def inspect_test_integrity(
    *,
    file_path: str,
    operation: PatchOperationType,
    old_content: str | None,
    new_content: str | None,
) -> list[TestIntegrityFlag]:
    """Run deterministic AST and config checks to detect removed, skipped, or weakened tests."""
    flags: list[TestIntegrityFlag] = []
    filename = Path(file_path).name.lower()

    if filename in _TEST_CONFIG_FILENAMES:
        flags.append(
            TestIntegrityFlag(
                file_path=file_path,
                rule_id="test_config_modified",
                severity="medium",
                message=f"Test configuration file '{file_path}' was {operation}d.",
            )
        )
    elif (
        filename in {"pyproject.toml", "setup.cfg"}
        and old_content is not None
        and new_content is not None
        and _is_pytest_section_modified(old_content, new_content)
    ):
        flags.append(
            TestIntegrityFlag(
                file_path=file_path,
                rule_id="test_config_modified",
                severity="medium",
                message=f"Pytest configuration section modified inside '{file_path}'.",
            )
        )

    if not is_python_test_path(file_path):
        return flags

    if operation == "delete":
        flags.append(
            TestIntegrityFlag(
                file_path=file_path,
                rule_id="test_file_deleted",
                severity="high",
                message=f"Existing test file '{file_path}' was deleted.",
            )
        )
        return flags

    if old_content is None or new_content is None:
        return flags

    old_metrics = _extract_test_metrics(old_content, file_path)
    new_metrics = _extract_test_metrics(new_content, file_path)
    if old_metrics is None or new_metrics is None:
        return flags

    removed_tests = sorted(old_metrics.test_names - new_metrics.test_names)
    if removed_tests:
        flags.append(
            TestIntegrityFlag(
                file_path=file_path,
                rule_id="test_removed",
                severity="high",
                message=(
                    f"Removed {len(removed_tests)} existing test(s) from '{file_path}'."
                ),
                symbols=removed_tests,
            )
        )

    if new_metrics.assertion_count < old_metrics.assertion_count:
        flags.append(
            TestIntegrityFlag(
                file_path=file_path,
                rule_id="assertions_reduced",
                severity="high",
                message=(
                    f"Assertion count in '{file_path}' decreased from "
                    f"{old_metrics.assertion_count} to {new_metrics.assertion_count}."
                ),
            )
        )

    if new_metrics.skip_marker_count > old_metrics.skip_marker_count:
        flags.append(
            TestIntegrityFlag(
                file_path=file_path,
                rule_id="skip_marker_added",
                severity="high",
                message=(
                    f"Skip/xfail markers in '{file_path}' increased from "
                    f"{old_metrics.skip_marker_count} to {new_metrics.skip_marker_count}."
                ),
            )
        )

    return flags