from src.agent.env_inject import (
    build_injected_env,
    find_env_example,
    parse_env_keys,
    required_env_keys,
    write_env_local,
)


def test_parse_env_keys_handles_comments_blanks_and_export(tmp_path):
    example = tmp_path / ".env.example"
    example.write_text(
        "# a comment\n"
        "\n"
        "NEXT_PUBLIC_API_URL=\n"
        "export NEXTAUTH_SECRET=changeme\n"
        "   DATABASE_URL = postgres://x\n"
        "NOT_AN_ASSIGNMENT\n"
        "123BAD=nope\n"
    )
    assert parse_env_keys(example) == ["NEXT_PUBLIC_API_URL", "NEXTAUTH_SECRET", "DATABASE_URL"]


def test_find_env_example_alternate_names_and_override(tmp_path):
    (tmp_path / ".env.sample").write_text("X=\n")
    assert find_env_example(tmp_path).name == ".env.sample"
    (tmp_path / "custom.env").write_text("Y=\n")
    assert find_env_example(tmp_path, override="custom.env").name == "custom.env"
    assert find_env_example(tmp_path, override="missing.env") is None


def test_required_env_keys_unions_example_and_config_vars(tmp_path):
    (tmp_path / ".env.example").write_text("A=\nB=\n")
    keys = required_env_keys(tmp_path, {"vars": ["B", "C"]})
    assert keys == ["A", "B", "C"]


def test_build_injected_env_reports_missing(tmp_path):
    (tmp_path / ".env.example").write_text("A=\nB=\nC=\n")
    injected, missing = build_injected_env(tmp_path, {}, {"A": "1", "C": "3"})
    assert injected == {"A": "1", "C": "3"}
    assert missing == ["B"]


def test_build_injected_env_excludes_undeclared_secrets(tmp_path):
    """Provider/auth secrets not declared by the app must NOT be injected into
    the app env (only declared .env.example / env.vars are)."""
    (tmp_path / ".env.example").write_text("PUBLIC_SUPABASE_URL=\nPUBLIC_SUPABASE_ANON_KEY=\n")
    secrets = {
        "PUBLIC_SUPABASE_URL": "https://x.supabase.co",
        "PUBLIC_SUPABASE_ANON_KEY": "anon",
        "SUPABASE_SERVICE_ROLE_KEY": "service-role-secret",
        "SUPABASE_URL": "https://x.supabase.co",
    }
    injected, missing = build_injected_env(tmp_path, {}, secrets)
    assert injected == {
        "PUBLIC_SUPABASE_URL": "https://x.supabase.co",
        "PUBLIC_SUPABASE_ANON_KEY": "anon",
    }
    assert "SUPABASE_SERVICE_ROLE_KEY" not in injected
    assert "SUPABASE_URL" not in injected
    assert missing == []


def test_write_env_local_quotes_and_returns_relative_path(tmp_path):
    repo = tmp_path
    frontend = tmp_path / "packages" / "web"
    frontend.mkdir(parents=True)
    rel = write_env_local(frontend, {"NEXT_PUBLIC_URL": "https://x.io", "K": 'a"b'}, repo)
    assert rel == "packages/web/.env.local"
    content = (frontend / ".env.local").read_text()
    assert 'NEXT_PUBLIC_URL="https://x.io"\n' in content
    assert 'K="a\\"b"\n' in content


def test_write_env_local_escapes_multiline_json(tmp_path):
    sa_json = '{\n  "type": "service_account",\n  "key": "abc"\n}'
    write_env_local(tmp_path, {"FIREBASE_SA": sa_json}, tmp_path)
    content = (tmp_path / ".env.local").read_text()
    # single physical line, newlines escaped
    assert content.count("\n") == 1
    assert "\\n" in content


def test_write_env_local_empty_returns_none(tmp_path):
    assert write_env_local(tmp_path, {}, tmp_path) is None
    assert not (tmp_path / ".env.local").exists()
