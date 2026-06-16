import os
from typing import Final

VIEWPORTS: Final[list[dict[str, int]]] = [
    {"width": 375, "height": 812},
    {"width": 768, "height": 1024},
    {"width": 1280, "height": 800},
    {"width": 1920, "height": 1080},
]

VIEWPORT_LABELS: Final[dict[int, str]] = {
    375: "Mobile XS",
    768: "Tablet",
    1280: "Desktop",
    1920: "Desktop XL",
}

def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))

POLL_INTERVAL_SECONDS: int = _env_int("POLL_INTERVAL", 10)
IDLE_TIMEOUT_SECONDS: int = _env_int("IDLE_TIMEOUT", 900)
RETRY_MAX_ATTEMPTS: int = 3
RETRY_WINDOW_SECONDS: int = 30
DEV_SERVER_PORT: int = 3000
DEV_SERVER_HOST: str = "localhost"
DEV_SERVER_START_TIMEOUT: int = 120
DEV_SERVER_POLL_INTERVAL: int = 2

# Diff-based frontend detection
FRONTEND_EXTENSIONS: Final[tuple[str, ...]] = (
    ".tsx", ".jsx", ".vue", ".svelte", ".astro",
    ".css", ".scss", ".less", ".html",
)

# Package.json discovery
MAX_PACKAGE_SCAN_DEPTH: Final[int] = 5

# Package-manager detection: lockfile -> package manager, in precedence order.
LOCKFILES: Final[dict[str, str]] = {
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "bun.lockb": "bun",
    "bun.lock": "bun",
    "package-lock.json": "npm",
    "npm-shrinkwrap.json": "npm",
}
DEFAULT_PACKAGE_MANAGER: str = "npm"

# Framework dev-server default ports, used as a fallback when the printed URL
# can't be sniffed from the dev server's stdout.
FRAMEWORK_DEFAULT_PORTS: Final[dict[str, int]] = {
    "next": 3000,
    "cra": 3000,
    "remix": 5173,
    "vite": 5173,
    "sveltekit": 5173,
    "astro": 4321,
    "spa": 3000,
}

# Ports probed (in order) when the dev server's URL can't be sniffed.
DEV_SERVER_CANDIDATE_PORTS: Final[tuple[int, ...]] = (3000, 5173, 5174, 4321, 4322, 8080, 4200)

# Matches the "Local: http://localhost:PORT" banner dev servers print on boot.
DEV_SERVER_URL_REGEX: str = r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1?\]):(\d+)"

# CloudFront signed-URL lifetime for screenshots — matches the 7-day S3 retention
# lifecycle so links in PR comments stay valid for the screenshots' whole life.
CLOUDFRONT_SIGNED_URL_TTL_SECONDS: int = 7 * 24 * 60 * 60

# S3 npm cache
NPM_CACHE_ENABLED: bool = True
NPM_CACHE_PREFIX: str = "npm-cache"
NPM_CACHE_HASH_ALGO: str = "sha256"
NPM_CI_TIMEOUT_SECONDS: int = 300

LLM_RETRY_BASE_DELAY: int = 2
LLM_RETRY_MAX_DELAY: int = 30
LLM_RETRY_JITTER: float = 0.1
LLM_CLIENT_TIMEOUT: int = 120

DEV_SERVER_HEALTH_TIMEOUT: int = 10
DEV_SERVER_HEALTH_POLL_INTERVAL: float = 1.0

PLAYWRIGHT_TIMEOUT: int = 30000
PLAYWRIGHT_NAVIGATION_TIMEOUT: int = 15000
PLAYWRIGHT_CLICK_TIMEOUT: int = 5000
SETTLE_AFTER_NAVIGATION_MS: int = 1000

# When a PR's change is confined to one element (e.g. a navbar), the screenshot
# is cropped to that element plus this padding. If the element covers more than
# this fraction of the viewport it's treated as a page-wide change (no crop).
CHANGED_REGION_MAX_AREA_FRACTION: float = 0.65
CHANGED_REGION_PADDING_PX: int = 24

REPO_DIR: str = "/app/repo"
LLM_MODEL: str = "google/gemini-2.5-flash"
OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"

# How many times the code-change agent may regenerate an edit when the previous
# attempt produced no visible change (verified via screenshot diff).
EDIT_MAX_ATTEMPTS: int = 3

# --- Env/secret injection & auth-gated apps ---

# Per-repo user secrets live in SSM SecureString, one parameter per secret under
# this prefix: /renderpr/secrets/{installation_id}/{repo_full_name}/{KEY}
SECRETS_SSM_PREFIX: str = "/renderpr/secrets"

# Repo-level config file (layered over auto-detection) and dotenv conventions.
RENDERPR_CONFIG_NAMES: Final[tuple[str, ...]] = (".renderpr.yml", ".renderpr.yaml")
ENV_EXAMPLE_NAMES: Final[tuple[str, ...]] = (".env.example", ".env.sample", ".env.template")
ENV_LOCAL_FILENAME: str = ".env.local"

# Auth strategies recognised in `.renderpr.yml` `auth.type`.
AUTH_TYPES: Final[tuple[str, ...]] = (
    "nextauth", "jwt", "supabase", "clerk", "auth0", "firebase",
)

# Synthetic user minted into forged/provider sessions when config omits one.
SYNTHETIC_USER_EMAIL: str = "preview@renderpr.dev"
SYNTHETIC_USER_NAME: str = "RenderPR Preview"

# Session lifetime baked into forged tokens (seconds).
SYNTHETIC_SESSION_TTL_SECONDS: int = 60 * 60 * 24 * 7

# Timeout for provider admin/token API calls (Supabase/Clerk/Firebase).
AUTH_PROVIDER_TIMEOUT: int = 30

# Login-wall detection: if, after navigation, the URL matches one of these markers
# (and no auth was configured), the page is treated as an auth wall and the review
# is degraded rather than run against a login screen.
LOGIN_WALL_URL_MARKERS: Final[tuple[str, ...]] = (
    "/login", "/signin", "/sign-in", "/auth/login", "/account/login", "/users/sign_in",
)
# A rendered body shorter than this (chars of visible text) also reads as a wall/blank.
LOGIN_WALL_MIN_BODY_CHARS: int = 64
