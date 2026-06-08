import os
import logging

logger = logging.getLogger(__name__)


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info("RenderPR agent starting")
    logger.info("Installation ID: %s", os.getenv("INSTALLATION_ID", "unknown"))
    logger.info("Repository: %s", os.getenv("REPO_FULL_NAME", "unknown"))
    logger.info("PR Number: %s", os.getenv("PR_NUMBER", "unknown"))

    # TODO: Fetch secrets from SSM Parameter Store (GITHUB_PARAM_NAME env var)
    # TODO: Generate GitHub installation access token
    # TODO: Clone PR branch
    # TODO: Install dependencies and start dev server
    # TODO: Run Playwright screenshots
    # TODO: Send to LLM
    # TODO: Post review comment
    # TODO: Enter polling loop

    logger.info("RenderPR agent finished")


if __name__ == "__main__":
    run()
