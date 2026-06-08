import logging

logger = logging.getLogger(__name__)


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info("renderpr-agent-starting")


if __name__ == "__main__":
    run()
