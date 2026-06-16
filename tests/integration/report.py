"""Result model + console/Markdown rendering for the Phase 3 e2e harness.

A :class:`FixtureResult` is the outcome of running one framework fixture through
:func:`tests.integration.runner.run_fixture`. Each pipeline stage records a
*cell* — one of ``PASS`` / ``FAIL`` / ``BLOCKED`` — keyed by a stable name. The
ordered cell names are :data:`CELL_ORDER`; the renderers below project a list of
results onto that fixed column set so the table/matrix stay aligned even when a
run aborted early (missing cells render as ``BLOCKED``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

PASS = "PASS"
FAIL = "FAIL"
BLOCKED = "BLOCKED"

# Ordered pipeline cells. Internal keys (left) map to the human column header
# (right) used in the console table and the Markdown matrix.
CELL_ORDER: list[tuple[str, str]] = [
    ("install", "install"),
    ("boot", "boot"),
    ("host_bind", "host-bind"),
    ("server_mock_renders", "server-mock-renders"),
    ("unmocked_fallback", "unmocked-fallback"),
    ("banner", "banner"),
    ("routing", "routing"),
]

CELL_KEYS: list[str] = [key for key, _ in CELL_ORDER]


@dataclass
class FixtureResult:
    framework: str
    pm: str
    cells: dict[str, str] = field(default_factory=dict)
    logs: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    def set(self, cell: str, status: str) -> None:
        self.cells[cell] = status

    def block_remaining(self, after: str) -> None:
        """Mark every cell at/after ``after`` in :data:`CELL_KEYS` as BLOCKED
        unless it already has a recorded status."""
        try:
            start = CELL_KEYS.index(after)
        except ValueError:
            start = 0
        for key in CELL_KEYS[start:]:
            self.cells.setdefault(key, BLOCKED)

    @property
    def passed(self) -> bool:
        """Overall pass iff every non-BLOCKED cell is PASS and at least one cell
        ran. A result with no recorded cells (hard crash) is a failure."""
        statuses = [self.cells.get(key) for key in CELL_KEYS]
        present = [s for s in statuses if s is not None]
        if not present:
            return False
        if self.error and not any(s == PASS for s in present):
            # An early hard error with nothing passing is a failure.
            pass
        return all(s == PASS for s in present if s != BLOCKED) and any(
            s == PASS for s in present
        )


def _cell(result: FixtureResult, key: str) -> str:
    return result.cells.get(key, BLOCKED)


def render_console_table(results: list[FixtureResult]) -> str:
    """A fixed-width ASCII table summarizing every fixture's cells."""
    headers = ["framework", "pm", *[col for _, col in CELL_ORDER], "overall"]
    rows: list[list[str]] = []
    for r in results:
        row = [r.framework, r.pm]
        row += [_cell(r, key) for key in CELL_KEYS]
        row.append(PASS if r.passed else FAIL)
        rows.append(row)

    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(val))

    def fmt(cols: list[str]) -> str:
        return "  ".join(val.ljust(widths[i]) for i, val in enumerate(cols))

    sep = "  ".join("-" * w for w in widths)
    lines = [fmt(headers), sep]
    lines += [fmt(row) for row in rows]
    return "\n".join(lines)


def render_markdown_matrix(results: list[FixtureResult]) -> str:
    """A GitHub-flavored Markdown table — the cols the issue spec asked for."""
    cols = [col for _, col in CELL_ORDER]
    header = "| framework | pm | " + " | ".join(cols) + " | overall |"
    divider = "|" + "|".join(["---"] * (len(cols) + 3)) + "|"
    lines = [header, divider]
    for r in results:
        cells = [_cell(r, key) for key in CELL_KEYS]
        overall = PASS if r.passed else FAIL
        lines.append(
            "| "
            + " | ".join([r.framework, r.pm, *cells, overall])
            + " |"
        )
    return "\n".join(lines)


def render_markdown_report(results: list[FixtureResult]) -> str:
    """Full Markdown document: matrix plus per-fixture error/log notes."""
    all_pass = bool(results) and all(r.passed for r in results)
    status = "PASS" if all_pass else "FAIL"
    parts = [
        "# RenderPR Phase 3 e2e harness report",
        "",
        f"Overall: **{status}** ({sum(r.passed for r in results)}/{len(results)} fixtures passed)",
        "",
        render_markdown_matrix(results),
        "",
    ]
    for r in results:
        if r.passed and not r.error:
            continue
        parts.append(f"## {r.framework} ({r.pm})")
        if r.error:
            parts.append("")
            parts.append(f"Error: {r.error}")
        for cell, log in r.logs.items():
            if not log:
                continue
            parts.append("")
            parts.append(f"<details><summary>{cell} log</summary>")
            parts.append("")
            parts.append("```")
            parts.append(log.strip())
            parts.append("```")
            parts.append("")
            parts.append("</details>")
        parts.append("")
    return "\n".join(parts)


def all_passed(results: list[FixtureResult]) -> bool:
    return bool(results) and all(r.passed for r in results)
