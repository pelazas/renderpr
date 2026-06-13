import pytest

from src.agent.renderpr_config import ConfigError, find_config_file, load_config


def test_missing_file_returns_zero_config_defaults(tmp_path):
    assert load_config(tmp_path) == {"env": {}, "auth": None}


def test_loads_env_and_auth(tmp_path):
    (tmp_path / ".renderpr.yml").write_text(
        "env:\n"
        "  from: .env.example\n"
        "  vars: [NEXT_PUBLIC_API_URL, NEXTAUTH_SECRET]\n"
        "auth:\n"
        "  type: nextauth\n"
        "  user:\n"
        "    email: preview@example.com\n"
        "    role: admin\n"
    )
    config = load_config(tmp_path)
    assert config["env"]["from"] == ".env.example"
    assert config["env"]["vars"] == ["NEXT_PUBLIC_API_URL", "NEXTAUTH_SECRET"]
    assert config["auth"]["type"] == "nextauth"
    assert config["auth"]["user"]["role"] == "admin"


def test_alternate_yaml_extension(tmp_path):
    (tmp_path / ".renderpr.yaml").write_text("auth:\n  type: clerk\n")
    assert load_config(tmp_path)["auth"]["type"] == "clerk"


def test_find_config_file_prefers_yml(tmp_path):
    (tmp_path / ".renderpr.yaml").write_text("env: {}\n")
    (tmp_path / ".renderpr.yml").write_text("env: {}\n")
    assert find_config_file(tmp_path).name == ".renderpr.yml"


def test_invalid_yaml_raises(tmp_path):
    (tmp_path / ".renderpr.yml").write_text("env: [: : :\n")
    with pytest.raises(ConfigError):
        load_config(tmp_path)


def test_top_level_must_be_mapping(tmp_path):
    (tmp_path / ".renderpr.yml").write_text("- just\n- a\n- list\n")
    with pytest.raises(ConfigError, match="must be a mapping"):
        load_config(tmp_path)


def test_unknown_auth_type_raises(tmp_path):
    (tmp_path / ".renderpr.yml").write_text("auth:\n  type: magic-login\n")
    with pytest.raises(ConfigError, match="auth.type"):
        load_config(tmp_path)


def test_env_must_be_mapping(tmp_path):
    (tmp_path / ".renderpr.yml").write_text("env: not-a-mapping\n")
    with pytest.raises(ConfigError, match="'env' must be a mapping"):
        load_config(tmp_path)


def test_env_vars_must_be_string_list(tmp_path):
    (tmp_path / ".renderpr.yml").write_text("env:\n  vars: [1, 2, 3]\n")
    with pytest.raises(ConfigError, match="env.vars"):
        load_config(tmp_path)


def test_auth_user_must_be_mapping(tmp_path):
    (tmp_path / ".renderpr.yml").write_text("auth:\n  type: jwt\n  user: bob\n")
    with pytest.raises(ConfigError, match="auth.user"):
        load_config(tmp_path)


def test_empty_file_is_zero_config(tmp_path):
    (tmp_path / ".renderpr.yml").write_text("")
    assert load_config(tmp_path) == {"env": {}, "auth": None}
