import os
import logging

logger = logging.getLogger(__name__)


def run() -> None:
    logging.basicConfig(level=logging.INFO)

    installation_id = os.environ.get("INSTALLATION_ID", "unknown")
    repo_full_name = os.environ.get("REPO_FULL_NAME", "unknown")
    pr_number = os.environ.get("PR_NUMBER", "unknown")

    logger.info("RenderPR agent started")
    logger.info("Installation ID: %s", installation_id)
    logger.info("Repository: %s", repo_full_name)
    logger.info("PR Number: %s", pr_number)

    logger.info("RenderPR agent finished")


if __name__ == "__main__":
    run()
