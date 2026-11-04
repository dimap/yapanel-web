"""Custom assertions, AST auditing, and integrity verifiers for E2E testing."""
from __future__ import annotations

import ast
import threading
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple


def assert_no_fake_tests_in_file(file_path: Path) -> List[str]:
    """Inspect AST of a Python test file and return any detected fake assertions or stubs."""
    issues = []
    with open(file_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=str(file_path))

    for node in ast.walk(tree):
        # 1. Check for functions starting with test_
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            # Check empty body (e.g. only Pass or Docstring)
            body_statements = [
                s for s in node.body
                if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str))
            ]
            if len(body_statements) == 1 and isinstance(body_statements[0], ast.Pass):
                issues.append(f"{file_path.name}:{node.lineno} {node.name} contains only 'pass'")

        # 2. Check for assert True or assert 1 == 1
        if isinstance(node, ast.Assert):
            test_val = node.test
            if isinstance(test_val, ast.Constant) and test_val.value is True:
                issues.append(f"{file_path.name}:{node.lineno} contains 'assert True'")
            elif isinstance(test_val, ast.Compare):
                if (
                    isinstance(test_val.left, ast.Constant)
                    and len(test_val.comparators) == 1
                    and isinstance(test_val.comparators[0], ast.Constant)
                    and test_val.left.value == test_val.comparators[0].value
                ):
                    issues.append(f"{file_path.name}:{node.lineno} contains trivial equality assert")

    return issues


def assert_thread_stability(before_threads: List[threading.Thread], max_delta: int = 0):
    """Verify that thread count after execution does not exceed before count + max_delta."""
    after_threads = [t for t in threading.enumerate() if t.is_alive()]
    leaked = [t for t in after_threads if t not in before_threads]
    if len(leaked) > max_delta:
        names = [f"{t.name} (daemon={t.daemon})" for t in leaked]
        raise AssertionError(f"Thread leak detected! {len(leaked)} new threads spawned: {', '.join(names)}")


def assert_json_schema(data: Any, expected_fields: Dict[str, type]):
    """Verify that data is a dict and contains all expected fields with expected types."""
    if not isinstance(data, dict):
        raise AssertionError(f"Expected dict, got {type(data).__name__}: {data}")
    for key, expected_type in expected_fields.items():
        if key not in data:
            raise AssertionError(f"Missing required key '{key}' in {data}")
        val = data[key]
        if val is not None and not isinstance(val, expected_type):
            raise AssertionError(
                f"Field '{key}' has type {type(val).__name__}, expected {expected_type.__name__}"
            )
