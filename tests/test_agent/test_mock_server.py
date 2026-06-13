import json

from src.agent.mock_server import restore_runtime_files, write_next_allowed_origin, write_server_mocks


def test_write_server_mocks_creates_next_api_route(tmp_path):
    generated = write_server_mocks(
        tmp_path,
        {"localhost": {"/api/users": {"body": [{"id": 1, "name": "Alice"}], "status": 200}}},
    )

    route = tmp_path / "src/app/api/users/route.ts"
    assert route.exists()
    assert route.read_text() == (
        "export async function GET() {\n"
        "  return Response.json([{\"id\": 1, \"name\": \"Alice\"}], { status: 200 });\n"
        "}\n"
    )
    assert "src/app/api/users/route.ts" in generated


def test_write_server_mocks_backs_up_existing_route(tmp_path):
    route = tmp_path / "src/app/api/users/route.ts"
    route.parent.mkdir(parents=True)
    route.write_text("export async function GET() { return Response.json([]); }\n")

    generated = write_server_mocks(
        tmp_path,
        {"localhost": {"/api/users": {"body": {"ok": True}, "status": 201}}},
    )

    backup = tmp_path / "src/app/api/users/route.ts.renderpr.bak"
    assert backup.read_text() == "export async function GET() { return Response.json([]); }\n"
    assert "src/app/api/users/route.ts" in generated
    assert "src/app/api/users/route.ts.renderpr.bak" in generated
    assert "{\"ok\": true}" in route.read_text()
    assert "status: 201" in route.read_text()


def test_restore_runtime_files_restores_backup(tmp_path):
    route = tmp_path / "src/app/api/users/route.ts"
    backup = tmp_path / "src/app/api/users/route.ts.renderpr.bak"
    route.parent.mkdir(parents=True)
    route.write_text("mock\n")
    backup.write_text("original\n")

    restore_runtime_files(tmp_path, ["src/app/api/users/route.ts", "src/app/api/users/route.ts.renderpr.bak"])

    assert route.read_text() == "original\n"
    assert not backup.exists()


def test_restore_runtime_files_deletes_created_route(tmp_path):
    route = tmp_path / "src/app/api/users/route.ts"
    route.parent.mkdir(parents=True)
    route.write_text("mock\n")

    restore_runtime_files(tmp_path, ["src/app/api/users/route.ts"])

    assert not route.exists()


def test_write_next_allowed_origin_patches_existing_config(tmp_path):
    config = tmp_path / "next.config.ts"
    config.write_text("const nextConfig = {};\n\nexport default nextConfig;\n")

    generated = write_next_allowed_origin(tmp_path, "54.1.2.3")

    assert "next.config.ts" in generated
    assert "next.config.ts.renderpr.bak" in generated
    assert "nextConfig.allowedDevOrigins = ['54.1.2.3'];" in config.read_text()


def test_write_next_allowed_origin_creates_config_when_missing(tmp_path):
    generated = write_next_allowed_origin(tmp_path, "54.1.2.3")

    config = tmp_path / "next.config.mjs"
    assert config.exists()
    assert "allowedDevOrigins: ['54.1.2.3']" in config.read_text()
    assert "next.config.mjs" in generated


def test_write_server_mocks_ignores_invalid_paths(tmp_path):
    generated = write_server_mocks(
        tmp_path,
        {"localhost": {"http://bad": {"body": {"ok": True}}, "/api/users": {"status": 200}}},
    )

    assert generated == []
    assert not (tmp_path / "src/app/api/users/route.ts").exists()


def test_generated_route_body_is_valid_json(tmp_path):
    write_server_mocks(
        tmp_path,
        {"localhost": {"/api/users": {"body": {"ok": True}, "status": 200}}},
    )

    route = tmp_path / "src/app/api/users/route.ts"
    body = route.read_text().split("Response.json(", 1)[1].split(", { status", 1)[0]
    assert json.loads(body) == {"ok": True}
