"""Phase 3 end-to-end fixture spec for the Astro (Yarn Berry) app.

See ``tests/integration/spec_types.py`` for the contract. This SSR Astro app
500s on ``/`` unless RenderPR's mock writer replaces ``/api/items``; the harness
runs the real detection + mock-writer code against it and HTTP-asserts behavior.
"""

from pathlib import Path

from tests.integration.spec_types import (
    FixtureSpec,
    default_item_strings,
    default_items,
)

SPEC = FixtureSpec(
    name="astro",
    pm="yarn",
    repo="pelazas/renderpr-e2e-astro",
    expected_framework="astro",
    expected_pm="yarn",
    expected_install_command=["yarn", "install", "--immutable"],
    diff_files=[
        "src/pages/index.astro",
        "src/components/ItemList.astro",
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
