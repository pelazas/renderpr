"""FixtureSpec for the SvelteKit Phase-3 e2e fixture."""

from __future__ import annotations

from pathlib import Path

from tests.integration.spec_types import (
    FixtureSpec,
    default_item_strings,
    default_items,
)

SPEC = FixtureSpec(
    name="sveltekit",
    pm="pnpm",
    repo="pelazas/renderpr-e2e-sveltekit",
    expected_framework="sveltekit",
    expected_pm="pnpm",
    expected_install_command=["pnpm", "install", "--frozen-lockfile"],
    diff_files=["src/routes/+page.svelte", "src/lib/ItemList.svelte"],
    mocks={"localhost": {"/api/items": {"body": default_items(), "status": 200}}},
    page_path="/",
    second_page_path="/about",
    expected_items=default_item_strings(),
    mocked_api="/api/items",
    unmocked_api="/api/other",
    source_dir=Path(__file__).parent,
)
