def test_dev_server_browser_host_uses_localhost() -> None:
    from src.agent.config import DEV_SERVER_HOST

    assert DEV_SERVER_HOST == "localhost"


def test_npm_shrinkwrap_is_a_recognized_lockfile() -> None:
    from src.agent.config import LOCKFILES

    assert LOCKFILES["npm-shrinkwrap.json"] == "npm"
    assert LOCKFILES["package-lock.json"] == "npm"


def test_remix_default_port_is_vite() -> None:
    from src.agent.config import FRAMEWORK_DEFAULT_PORTS

    assert FRAMEWORK_DEFAULT_PORTS["remix"] == 5173


def test_dev_server_start_timeout_is_120() -> None:
    from src.agent.config import DEV_SERVER_START_TIMEOUT

    assert DEV_SERVER_START_TIMEOUT == 120


def test_candidate_ports_include_vite_and_astro_secondaries() -> None:
    from src.agent.config import DEV_SERVER_CANDIDATE_PORTS

    assert DEV_SERVER_CANDIDATE_PORTS == (3000, 5173, 5174, 4321, 4322, 8080, 4200)
