"""CLI entrypoint for the Phase 3 end-to-end harness.

Discovers the framework fixtures, runs each SERIALLY through
:func:`tests.integration.runner.run_fixture` (serial because the fixtures share
dev-server ports), writes a Markdown report + per-fixture logs, prints the
console table, and exits 0 only when every fixture passes.

Usage::

    python -m tests.integration.entrypoint --frameworks all --out /work/tests/integration/_out
    python -m tests.integration.entrypoint --frameworks next,remix --use-local --out ./_out
"""

from __future__ import annotations

import argparse
import importlib
import sys
import tempfile
from pathlib import Path

from tests.integration import report
from tests.integration.runner import run_fixture
from tests.integration.spec_types import FixtureSpec

# Known fixtures, in run order. Each maps to tests.integration.fixtures.<fw>.spec.
KNOWN_FRAMEWORKS: list[str] = ["sveltekit", "astro", "remix", "next"]


def _load_spec(framework: str) -> FixtureSpec | None:
    """Import ``tests.integration.fixtures.<fw>.spec`` and return its ``SPEC``.

    Returns None (with a stderr note) when the fixture module is absent or
    malformed, so a missing fixture skips rather than crashing the whole sweep.
    """
    module_name = f"tests.integration.fixtures.{framework}.spec"
    try:
        module = importlib.import_module(module_name)
    except Exception as e:  # noqa: BLE001
        print(f"[harness] skipping {framework}: cannot import {module_name}: {e}", file=sys.stderr)
        return None
    spec = getattr(module, "SPEC", None)
    if not isinstance(spec, FixtureSpec):
        print(f"[harness] skipping {framework}: {module_name} has no FixtureSpec SPEC", file=sys.stderr)
        return None
    return spec


def _select_frameworks(arg: str) -> list[str]:
    if arg.strip().lower() == "all":
        return list(KNOWN_FRAMEWORKS)
    requested = [f.strip() for f in arg.split(",") if f.strip()]
    unknown = [f for f in requested if f not in KNOWN_FRAMEWORKS]
    if unknown:
        print(f"[harness] warning: unknown frameworks ignored: {unknown}", file=sys.stderr)
    return [f for f in requested if f in KNOWN_FRAMEWORKS]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tests.integration.entrypoint")
    parser.add_argument(
        "--frameworks",
        default="all",
        help="'all' or a comma-separated subset of: " + ",".join(KNOWN_FRAMEWORKS),
    )
    parser.add_argument(
        "--use-local",
        action="store_true",
        help="Use the committed fixture source tree instead of cloning the private mirror.",
    )
    parser.add_argument(
        "--out",
        required=True,
        help="Output directory for report.md and per-fixture logs.",
    )
    parser.add_argument(
        "--workdir",
        default=None,
        help="Scratch dir for clones/installs (default: a temp dir under --out).",
    )
    args = parser.parse_args(argv)

    frameworks = _select_frameworks(args.frameworks)
    if not frameworks:
        print("[harness] no frameworks selected; nothing to do", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="renderpr-e2e-", dir=str(out_dir)))
    workdir.mkdir(parents=True, exist_ok=True)

    results: list[report.FixtureResult] = []
    for framework in frameworks:
        spec = _load_spec(framework)
        if spec is None:
            r = report.FixtureResult(framework=framework, pm="?")
            r.error = "fixture spec not found"
            r.block_remaining("install")
            results.append(r)
            continue
        print(f"[harness] running fixture: {framework} ({spec.pm})", file=sys.stderr)
        result = run_fixture(spec, use_local=args.use_local, workdir=workdir)
        results.append(result)
        _write_fixture_logs(out_dir, result)

    # Write the Markdown report and print the console table.
    report_md = report.render_markdown_report(results)
    (out_dir / "report.md").write_text(report_md)

    print()
    print(report.render_console_table(results))
    print()
    print(f"[harness] report written to {out_dir / 'report.md'}", file=sys.stderr)

    return 0 if report.all_passed(results) else 1


def _write_fixture_logs(out_dir: Path, result: report.FixtureResult) -> None:
    if not result.logs and not result.error:
        return
    log_file = out_dir / f"{result.framework}.log"
    parts: list[str] = [f"# {result.framework} ({result.pm})", ""]
    if result.error:
        parts.append(f"error: {result.error}")
        parts.append("")
    for cell, log in result.logs.items():
        parts.append(f"## {cell}")
        parts.append(log)
        parts.append("")
    log_file.write_text("\n".join(parts))


if __name__ == "__main__":
    raise SystemExit(main())
