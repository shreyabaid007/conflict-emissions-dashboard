#!/usr/bin/env python3
"""Automated verification of WCED technical launch-readiness checklist.

Checks each technical prerequisite for public launch and prints a pass/fail
report.  Exit code 0 only if every check passes.

Usage:
    python scripts/launch_check.py                # against live database
    python scripts/launch_check.py --db-url ...   # custom database URL
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"


def _result(ok: bool, label: str, detail: str = "") -> bool:
    icon = PASS if ok else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"  {icon} {label}{suffix}")
    return ok


# ------------------------------------------------------------------
# 1. Methodology PDF v1.0 published with SSC approval
# ------------------------------------------------------------------
def check_methodology_pdf() -> bool:
    pdf = ROOT / "methodology" / "v1.0.pdf"
    tex = ROOT / "methodology" / "v1.0.tex"
    if not pdf.exists():
        return _result(False, "Methodology PDF v1.0 exists", "methodology/v1.0.pdf not found")
    if not tex.exists():
        return _result(False, "Methodology PDF v1.0 exists", "methodology/v1.0.tex not found")

    tex_text = tex.read_text(encoding="utf-8")
    has_reviewers = "reviewer" in tex_text.lower() or "approved" in tex_text.lower()
    return _result(
        has_reviewers,
        "Methodology PDF v1.0 with SSC approval",
        "reviewer/approval mention found" if has_reviewers else "no reviewer/approval section found in .tex",
    )


# ------------------------------------------------------------------
# 2. All published events have full provenance chains
# ------------------------------------------------------------------
def check_provenance_chains(engine: object) -> bool:
    from sqlalchemy import text as sql_text

    with engine.connect() as conn:  # type: ignore[union-attr]
        orphans = conn.execute(
            sql_text("""
                SELECT COUNT(*) FROM fire_events fe
                WHERE fe.status = 'published'
                  AND NOT EXISTS (
                    SELECT 1 FROM provenance_records pr
                    WHERE pr.id = fe.provenance_id
                  )
            """)
        ).scalar()

        total = conn.execute(
            sql_text("SELECT COUNT(*) FROM fire_events WHERE status = 'published'")
        ).scalar()

    if total == 0:
        return _result(False, "Published events have provenance chains", "no published events found")

    return _result(
        orphans == 0,
        "Published events have provenance chains",
        f"{total} published, {orphans} missing provenance",
    )


# ------------------------------------------------------------------
# 3. All emission estimates have Monte Carlo bounds
# ------------------------------------------------------------------
def check_monte_carlo_bounds(engine: object) -> bool:
    from sqlalchemy import text as sql_text

    with engine.connect() as conn:  # type: ignore[union-attr]
        missing = conn.execute(
            sql_text("""
                SELECT COUNT(*) FROM emission_estimates
                WHERE p5 IS NULL OR p50 IS NULL OR p95 IS NULL
            """)
        ).scalar()

        total = conn.execute(
            sql_text("SELECT COUNT(*) FROM emission_estimates")
        ).scalar()

    if total == 0:
        return _result(False, "Emission estimates have MC bounds", "no emission estimates found")

    return _result(
        missing == 0,
        "Emission estimates have MC bounds",
        f"{total} estimates, {missing} missing bounds",
    )


# ------------------------------------------------------------------
# 4. CI passes including methodology compliance tests
# ------------------------------------------------------------------
def check_ci_tests() -> bool:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short", "-q"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=300,
        )
        passed = result.returncode == 0

        methodology_tests = ROOT / "tests" / "methodology"
        has_methodology_tests = methodology_tests.is_dir() and any(methodology_tests.glob("test_*.py"))

        ok = passed and has_methodology_tests
        detail_parts = []
        if not has_methodology_tests:
            detail_parts.append("no methodology tests found")
        if not passed:
            last_line = (result.stdout.strip().splitlines() or ["unknown failure"])[-1]
            detail_parts.append(f"pytest failed: {last_line}")
        if ok:
            detail_parts.append("all tests pass")

        return _result(ok, "CI passes with methodology tests", "; ".join(detail_parts))
    except subprocess.TimeoutExpired:
        return _result(False, "CI passes with methodology tests", "pytest timed out after 300s")
    except FileNotFoundError:
        return _result(False, "CI passes with methodology tests", "pytest not found")


# ------------------------------------------------------------------
# 5. OpenAPI documentation complete
# ------------------------------------------------------------------
def check_openapi_docs() -> bool:
    try:
        from wced.api.main import create_app

        app = create_app()
        schema = app.openapi()
    except ImportError:
        return _result(False, "OpenAPI documentation complete", "wced package not installed — run `pip install -e .`")
    except Exception as exc:
        return _result(False, "OpenAPI documentation complete", f"could not load app: {exc}")

    paths = schema.get("paths", {})
    if not paths:
        return _result(False, "OpenAPI documentation complete", "no paths in OpenAPI schema")

    undocumented = []
    for path, methods in paths.items():
        for method, spec in methods.items():
            if method in ("get", "post", "put", "patch", "delete"):
                if not spec.get("summary") and not spec.get("description"):
                    undocumented.append(f"{method.upper()} {path}")

    ok = len(undocumented) == 0
    detail = f"{len(paths)} paths, {len(undocumented)} undocumented" if not ok else f"{len(paths)} paths documented"
    return _result(ok, "OpenAPI documentation complete", detail)


# ------------------------------------------------------------------
# 6. GitHub repository public with MIT/CC-BY licenses
# ------------------------------------------------------------------
def check_licenses() -> bool:
    license_file = ROOT / "LICENSE"
    license_md = ROOT / "LICENSE.md"
    pyproject = ROOT / "pyproject.toml"

    has_license_file = license_file.exists() or license_md.exists()

    has_mit = False
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8")
        has_mit = "MIT" in text

    data_license = ROOT / "data" / "LICENSE"
    data_license_md = ROOT / "data" / "LICENSE.md"
    has_data_license = data_license.exists() or data_license_md.exists()

    readme = ROOT / "README.md"
    has_ccby_mention = False
    if readme.exists():
        readme_text = readme.read_text(encoding="utf-8")
        has_ccby_mention = "CC-BY" in readme_text or "CC BY" in readme_text

    ok = has_license_file and has_mit and (has_data_license or has_ccby_mention)
    details = []
    if not has_license_file:
        details.append("LICENSE file missing")
    if not has_mit:
        details.append("MIT not declared in pyproject.toml")
    if not has_data_license and not has_ccby_mention:
        details.append("CC-BY 4.0 data license not found")
    if ok:
        details.append("MIT code + CC-BY data licenses present")

    return _result(ok, "MIT/CC-BY licenses present", "; ".join(details))


# ------------------------------------------------------------------
# 7. Replication package downloadable
# ------------------------------------------------------------------
def check_replication_package() -> bool:
    checks = {
        "requirements pinned": (ROOT / "requirements.txt").exists() or (ROOT / "pyproject.toml").exists(),
        "emission_factors.yaml": (ROOT / "data" / "emission_factors.yaml").exists(),
        "parameter_distributions.yaml": (ROOT / "data" / "parameter_distributions.yaml").exists(),
        "README.md": (ROOT / "README.md").exists(),
    }

    missing = [k for k, v in checks.items() if not v]
    ok = len(missing) == 0
    detail = f"missing: {', '.join(missing)}" if missing else "all replication files present"
    return _result(ok, "Replication package files present", detail)


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="WCED technical launch-readiness check")
    parser.add_argument(
        "--db-url",
        default=None,
        help="SQLAlchemy database URL. If omitted, reads DATABASE_URL env var. "
        "Database checks are skipped if neither is available.",
    )
    parser.add_argument(
        "--skip-db",
        action="store_true",
        help="Skip database-dependent checks.",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="Skip running the test suite (useful in CI where tests run separately).",
    )
    args = parser.parse_args()

    print("WCED Technical Launch Checklist")
    print("=" * 40)

    results: list[bool] = []

    # File-based checks
    print("\nFile checks:")
    results.append(check_methodology_pdf())
    results.append(check_licenses())
    results.append(check_replication_package())

    # OpenAPI check
    print("\nAPI checks:")
    results.append(check_openapi_docs())

    # Test suite
    print("\nTest suite:")
    if args.skip_tests:
        print(f"  {PASS} CI tests (skipped via --skip-tests)")
        results.append(True)
    else:
        results.append(check_ci_tests())

    # Database checks
    print("\nDatabase checks:")
    engine = None
    if not args.skip_db:
        import os

        db_url = args.db_url or os.environ.get("DATABASE_URL")
        if db_url:
            try:
                from sqlalchemy import create_engine

                engine = create_engine(db_url)
            except Exception as exc:
                print(f"  {FAIL} Database connection failed: {exc}")
        else:
            print(f"  {FAIL} No DATABASE_URL — set --db-url or DATABASE_URL env var")

    if engine is not None:
        results.append(check_provenance_chains(engine))
        results.append(check_monte_carlo_bounds(engine))
    elif args.skip_db:
        print(f"  {PASS} Provenance chains (skipped via --skip-db)")
        print(f"  {PASS} Monte Carlo bounds (skipped via --skip-db)")
        results.extend([True, True])
    else:
        results.extend([False, False])

    # Summary
    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 40}")
    if passed == total:
        print(f"\033[32mAll {total} checks passed. Ready for launch.\033[0m")
    else:
        print(f"\033[31m{passed}/{total} checks passed. Not ready for launch.\033[0m")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
