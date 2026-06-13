def test_dev_server_browser_host_uses_localhost() -> None:
    from src.agent.config import DEV_SERVER_HOST

    assert DEV_SERVER_HOST == "localhost"
