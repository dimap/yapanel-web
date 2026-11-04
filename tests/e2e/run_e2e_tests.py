#!/usr/bin/env python3
"""CLI test runner for yandex-station-web 4-Tier E2E Test Suite.

Usage:
    python tests/e2e/run_e2e_tests.py [--tier {1,2,3,4}] [--verbose] [--ast-check]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Add project root and e2e directory to sys.path
E2E_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = E2E_DIR.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(E2E_DIR.parent) not in sys.path:
    sys.path.insert(0, str(E2E_DIR.parent))

import pytest
from tests.e2e.harness.test_assertions import assert_no_fake_tests_in_file

TIER_MAP = {
    1: ("Tier 1: Feature Coverage (14 features)", E2E_DIR / "tier1_features"),
    2: ("Tier 2: Boundary & Corner Cases", E2E_DIR / "tier2_boundaries"),
    3: ("Tier 3: Cross-Feature Interactions", E2E_DIR / "tier3_interactions"),
    4: ("Tier 4: Real-World Scenarios", E2E_DIR / "tier4_scenarios"),
}


def run_ast_audit() -> int:
    """Run AST integrity inspection across all E2E test files."""
    print("=" * 70)
    print("  RUNNING AST TEST INTEGRITY AUDIT (Zero-Cheating Verification)")
    print("=" * 70)

    total_files = 0
    total_issues = 0

    for tier_num, (tier_title, tier_dir) in TIER_MAP.items():
        if not tier_dir.is_dir():
            continue
        print(f"\nScanning {tier_title} [{tier_dir.name}]:")
        for test_file in sorted(tier_dir.glob("test_*.py")):
            total_files += 1
            issues = assert_no_fake_tests_in_file(test_file)
            if issues:
                print(f"  [FAIL] {test_file.name}:")
                for issue in issues:
                    print(f"         - {issue}")
                    total_issues += 1
            else:
                print(f"  [PASS] {test_file.name}")

    print("\n" + "-" * 70)
    if total_issues == 0:
        print(f"  [SUCCESS] All {total_files} test files passed AST integrity audit.")
        print("            Zero dummy assertions, zero trivial compares, zero empty stubs.")
        print("-" * 70)
        return 0
    else:
        print(f"  [FAILURE] Detected {total_issues} test integrity violations across {total_files} files!")
        print("-" * 70)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dual Track E2E Test Suite Runner for yandex-station-web",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--tier",
        type=int,
        choices=[1, 2, 3, 4],
        default=None,
        help="Run specific tier (1: Features, 2: Boundaries, 3: Interactions, 4: Scenarios)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose pytest output",
    )
    parser.add_argument(
        "-x", "--exitfirst",
        action="store_true",
        help="Exit immediately on first test failure",
    )
    parser.add_argument(
        "-k", "--keyword",
        type=str,
        default=None,
        help="Filter tests by keyword expression",
    )
    parser.add_argument(
        "--ast-check",
        action="store_true",
        help="Run AST test integrity check and exit",
    )

    args = parser.parse_args()

    if args.ast_check:
        return run_ast_audit()

    print("=" * 70)
    print("  YANDEX STATION WEB - DUAL TRACK E2E TEST RUNNER")
    print("=" * 70)

    pytest_args = []
    if args.verbose:
        pytest_args.append("-v")
    if args.exitfirst:
        pytest_args.append("-x")
    if args.keyword:
        pytest_args.extend(["-k", args.keyword])

    if args.tier:
        tier_title, tier_path = TIER_MAP[args.tier]
        print(f"  Target Scope: {tier_title}")
        pytest_args.append(str(tier_path))
    else:
        print("  Target Scope: All Tiers (Tier 1 + Tier 2 + Tier 3 + Tier 4)")
        pytest_args.append(str(E2E_DIR))

    print(f"  Execution Root: {E2E_DIR}")
    print("=" * 70 + "\n")

    start_time = time.time()
    exit_code = pytest.main(pytest_args)
    elapsed = time.time() - start_time

    print("\n" + "=" * 70)
    if exit_code == 0:
        print(f"  [RESULT] ALL E2E TESTS PASSED cleanly in {elapsed:.2f}s")
    else:
        print(f"  [RESULT] TEST SUITE FAILED with exit code {exit_code} (took {elapsed:.2f}s)")
    print("=" * 70)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
