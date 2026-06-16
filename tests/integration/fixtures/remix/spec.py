"""Phase-3 e2e fixture spec for the Remix (Vite) app.

See ``tests/integration/spec_types.py`` for the ``FixtureSpec`` contract.
"""

from __future__ import annotations

from pathlib import Path

from tests.integration.spec_types import (
    FixtureSpec,
    default_item_strings,
    default_items,
)

SPEC = FixtureSpec(
    name="remix",
    pm="bun",
    repo="pelazas/renderpr-e2e-remix",
    expected_framework="remix",
    expected_pm="bun",
    expected_install_command=["bun", "install", "--frozen-lockfile"],
    diff_files=[
        "app/routes/_index.tsx",
        "app/components/ItemList.tsx",
    ],
    mocks={
        "localhost": {
            "/api/items": {"body": default_items(), "status": 200},
        },
    },
    page_path="/",
    second_page_path="/about",
    expected_items=default_item_strings(),
    mocked_api="/api/items",
    unmocked_api="/api/other",
    source_dir=Path(__file__).parent,
)
