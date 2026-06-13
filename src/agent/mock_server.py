import json
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

BACKUP_SUFFIX = ".renderpr.bak"
NEXT_CONFIG_NAMES = (
    "next.config.ts",
    "next.config.mjs",
    "next.config.js",
    "next.config.cjs",
)


def _backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_name(f"{path.name}{BACKUP_SUFFIX}")
    if not backup.exists():
        shutil.copy2(path, backup)
    return backup


def _mock_route_path(api_path: str) -> Path | None:
    parts = [part for part in api_path.strip("/").split("/") if part]
    if not parts or parts[0] != "api" or any(part in {".", ".."} for part in parts):
        return None
    return Path("src/app") / Path(*parts) / "route.ts"


def write_server_mocks(repo_dir: Path | str, mocks: dict | None) -> list[str]:
    if not mocks:
        return []

    repo_path = Path(repo_dir)
    generated: list[str] = []

    for endpoints in mocks.values():
        if not isinstance(endpoints, dict):
            continue
        for api_path, mock_data in endpoints.items():
            if not isinstance(api_path, str) or not isinstance(mock_data, dict) or "body" not in mock_data:
                continue
            route_rel = _mock_route_path(api_path)
            if route_rel is None:
                continue

            route_path = repo_path / route_rel
            route_path.parent.mkdir(parents=True, exist_ok=True)
            backup = _backup_file(route_path)
            if backup is not None:
                generated.append(str(backup.relative_to(repo_path)))

            status = int(mock_data.get("status", 200))
            body = json.dumps(mock_data["body"])
            route_path.write_text(
                "export async function GET() {\n"
                f"  return Response.json({body}, {{ status: {status} }});\n"
                "}\n"
            )
            generated.append(str(route_rel))
            logger.info("Server mock route written: %s for %s", route_rel, api_path)

    return generated


def _find_next_config(repo_path: Path) -> Path | None:
    for name in NEXT_CONFIG_NAMES:
        path = repo_path / name
        if path.exists():
            return path
    return None


def write_next_allowed_origin(repo_dir: Path | str, public_ip: str) -> list[str]:
    if not public_ip or public_ip == "localhost":
        return []

    repo_path = Path(repo_dir)
    generated: list[str] = []
    existing = _find_next_config(repo_path)

    if existing is None:
        config = repo_path / "next.config.mjs"
        config.write_text(
            "const nextConfig = {\n"
            f"  allowedDevOrigins: ['{public_ip}'],\n"
            "};\n\n"
            "export default nextConfig;\n"
        )
        generated.append(str(config.relative_to(repo_path)))
        logger.info("Temporary Next config written with allowedDevOrigins for %s", public_ip)
        return generated

    content = existing.read_text()
    if f"'{public_ip}'" in content or f'"{public_ip}"' in content:
        return []

    backup = _backup_file(existing)
    if backup is not None:
        generated.append(str(backup.relative_to(repo_path)))

    assignment = f"nextConfig.allowedDevOrigins = ['{public_ip}'];\n"
    if "export default nextConfig" in content:
        content = content.replace("export default nextConfig", f"{assignment}\nexport default nextConfig", 1)
    else:
        content = f"{content.rstrip()}\n\n{assignment}"

    existing.write_text(content)
    generated.append(str(existing.relative_to(repo_path)))
    logger.info("Temporary Next config patched with allowedDevOrigins for %s", public_ip)
    return generated


def restore_runtime_files(repo_dir: Path | str, generated_files: list[str]) -> None:
    repo_path = Path(repo_dir)
    generated = list(dict.fromkeys(generated_files))
    restored: set[str] = set()

    for rel in generated:
        if not rel.endswith(BACKUP_SUFFIX):
            continue
        backup = repo_path / rel
        original_rel = rel[: -len(BACKUP_SUFFIX)]
        original = repo_path / original_rel
        if not backup.exists():
            continue
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, original)
            backup.unlink()
            restored.add(original_rel)
            logger.info("Restored runtime file from backup: %s", original_rel)
        except OSError:
            logger.warning("Failed to restore runtime backup %s", rel, exc_info=True)

    for rel in generated:
        if rel.endswith(BACKUP_SUFFIX) or rel in restored:
            continue
        path = repo_path / rel
        if not path.exists():
            continue
        try:
            path.unlink()
            logger.info("Deleted runtime-generated file: %s", rel)
        except OSError:
            logger.warning("Failed to delete runtime file %s", rel, exc_info=True)
