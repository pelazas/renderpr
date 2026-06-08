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
DEV_SERVER_START_TIMEOUT: int = 60
DEV_SERVER_POLL_INTERVAL: int = 2

LLM_RETRY_BASE_DELAY: int = 2
LLM_RETRY_MAX_DELAY: int = 30
LLM_RETRY_JITTER: float = 0.1
LLM_CLIENT_TIMEOUT: int = 120

PLAYWRIGHT_TIMEOUT: int = 30000
PLAYWRIGHT_NAVIGATION_TIMEOUT: int = 15000

REPO_DIR: str = "/app/repo"
LLM_MODEL: str = "google/gemini-2.5-flash"
OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
