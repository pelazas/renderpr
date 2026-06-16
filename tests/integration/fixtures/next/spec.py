"""Phase-3 e2e fixture spec for the Next.js App Router CONTROL app (npm).

Next mocking already works in prod, so a green Next column validates the
harness itself. See README.md for the app's mock-or-500 behavior.
"""

from __future__ import annotations

from pathlib import Path

from tests.integration.spec_types import (
    FixtureSpec,
    default_item_strings,
    default_items,
)

SPEC = FixtureSpec(
    name="next",
    pm="npm",
    repo="pelazas/renderpr-e2e-next",
    expected_framework="next",
    expected_pm="npm",
    expected_install_command=["npm", "ci"],
    # A page + the shared component change so discover_frontend() reports
    # has_frontend=True. Deliberately excludes api/items/route.ts so the writer
    # still mocks that endpoint (a changed route would be wrapped, not mocked).
    diff_files=["src/app/page.tsx", "src/components/ItemList.tsx"],
    mocks={"localhost": {"/api/items": {"body": default_items(), "status": 200}}},
    page_path="/",
    second_page_path="/about",
    expected_items=default_item_strings(),
    mocked_api="/api/items",
    unmocked_api="/api/other",
    source_dir=Path(__file__).parent,
)
