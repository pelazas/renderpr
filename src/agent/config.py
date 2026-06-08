VIEWPORTS: list[dict[str, int]] = [
    {"width": 375, "height": 812},
    {"width": 768, "height": 1024},
    {"width": 1280, "height": 800},
    {"width": 1920, "height": 1080},
]

POLL_INTERVAL_SECONDS: int = 10
IDLE_TIMEOUT_SECONDS: int = 900
RETRY_MAX_ATTEMPTS: int = 3
RETRY_WINDOW_SECONDS: int = 30
DEV_SERVER_PORT: int = 3000
DEV_SERVER_HOST: str = "localhost"
