"""Shared contract for Phase 3 end-to-end fixtures.

Each ``tests/integration/fixtures/<fw>/spec.py`` defines a module-level ``SPEC``
(a :class:`FixtureSpec`). The runner discovers every fixture, clones its private
repo (or uses the committed copy), runs the REAL ``src.agent`` detection +
mock-writer code against it, boots the dev server, and HTTP-asserts the runtime
behavior. Nothing here imports framework code, so fixtures stay decoupled.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class FixtureSpec:
    # Identity
    name: str                       # "sveltekit"
    pm: str                         # "pnpm" — the package manager under test
    repo: str                       # "pelazas/renderpr-e2e-sveltekit" (private)

    # Expected detection (asserted against the real LaunchProfile)
    expected_framework: str         # what stack.detect_framework should return
    expected_pm: str                # what detect_package_manager should return
    expected_install_command: list[str]  # e.g. ["pnpm","install","--frozen-lockfile"]

    # Synthetic PR diff: list of CHANGED frontend files (relative paths). MUST
    # include at least one page/component file so discover_frontend() reports
    # has_frontend=True, and MUST NOT include the mocked api/items route file
    # (else changed_api_paths marks it changed and the writer skips mocking it).
    diff_files: list[str]

    # The mocks dict handed to the writers (shape: {domain: {/api/path: {body,status}}}).
    mocks: dict

    # Runtime HTTP assertions
    page_path: str                  # page whose SSR fetch hits /api/items (e.g. "/")
    second_page_path: str           # 2nd page sharing a component (e.g. "/about")
    expected_items: list[str]       # strings that must appear in the rendered HTML
    mocked_api: str                 # "/api/items" — must return the mock body
    unmocked_api: str               # "/api/other" — must degrade to [] + header

    # The fixture source directory (the committed copy), set by each spec to its
    # own folder via ``Path(__file__).parent``.
    source_dir: Path = field(default=Path("."))


def default_items() -> list[dict]:
    """Canonical mock payload reused by every fixture's /api/items mock."""
    return [
        {"id": 1, "name": "Item 1"},
        {"id": 2, "name": "Item 2"},
        {"id": 3, "name": "Item 3"},
        {"id": 4, "name": "Item 4"},
        {"id": 5, "name": "Item 5"},
    ]


def default_item_strings() -> list[str]:
    return [f"Item {i}" for i in range(1, 6)]
