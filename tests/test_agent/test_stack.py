import json

import pytest


def _write_pkg(dir_path, deps=None, dev_deps=None, scripts=None):
    pkg = {"name": "x", "version": "1.0.0"}
    if deps is not None:
        pkg["dependencies"] = deps
    if dev_deps is not None:
        pkg["devDependencies"] = dev_deps
    if scripts is not None:
        pkg["scripts"] = scripts
    path = dir_path / "package.json"
    path.write_text(json.dumps(pkg))
    return path


class TestDetectPackageManager:
    @pytest.mark.parametrize(
        "lockfile,expected",
        [
            ("pnpm-lock.yaml", "pnpm"),
            ("yarn.lock", "yarn"),
            ("bun.lockb", "bun"),
            ("bun.lock", "bun"),
            ("package-lock.json", "npm"),
        ],
    )
    def test_detects_by_lockfile(self, tmp_path, lockfile, expected):
        from src.agent.stack import detect_package_manager
        (tmp_path / lockfile).write_text("")
        pm, has_lock = detect_package_manager(tmp_path)
        assert pm == expected
        assert has_lock is True

    def test_no_lockfile_defaults_to_npm(self, tmp_path):
        from src.agent.stack import detect_package_manager
        pm, has_lock = detect_package_manager(tmp_path)
        assert pm == "npm"
        assert has_lock is False

    def test_pnpm_wins_over_npm_lockfile(self, tmp_path):
        from src.agent.stack import detect_package_manager
        (tmp_path / "package-lock.json").write_text("")
        (tmp_path / "pnpm-lock.yaml").write_text("")
        pm, _ = detect_package_manager(tmp_path)
        assert pm == "pnpm"


class TestDetectFramework:
    @pytest.mark.parametrize(
        "deps,expected",
        [
            ({"next": "14.0.0"}, "next"),
            ({"@remix-run/react": "2.0.0"}, "remix"),
            ({"@react-router/dev": "7.0.0"}, "remix"),
            ({"astro": "4.0.0"}, "astro"),
            ({"@sveltejs/kit": "2.0.0"}, "sveltekit"),
            ({"react-scripts": "5.0.0"}, "cra"),
            ({"vite": "5.0.0"}, "vite"),
            ({"lodash": "4.0.0"}, "spa"),
            ({}, "spa"),
        ],
    )
    def test_detects_by_dependencies(self, tmp_path, deps, expected):
        from src.agent.stack import detect_framework
        pkg = _write_pkg(tmp_path, deps=deps)
        assert detect_framework(pkg) == expected

    def test_reads_dev_dependencies(self, tmp_path):
        from src.agent.stack import detect_framework
        pkg = _write_pkg(tmp_path, dev_deps={"vite": "5.0.0"})
        assert detect_framework(pkg) == "vite"

    def test_next_takes_precedence_over_vite(self, tmp_path):
        from src.agent.stack import detect_framework
        pkg = _write_pkg(tmp_path, deps={"next": "14.0.0", "vite": "5.0.0"})
        assert detect_framework(pkg) == "next"

    def test_missing_package_json_is_spa(self, tmp_path):
        from src.agent.stack import detect_framework
        assert detect_framework(tmp_path / "nope.json") == "spa"


class TestParsePackageManagerField:
    @pytest.mark.parametrize(
        "field,expected",
        [
            ("yarn@4.5.0", ("yarn", 4)),
            ("yarn@1.22.19", ("yarn", 1)),
            ("pnpm@9", ("pnpm", 9)),
            ("yarn@3.6.1+sha512.abc", ("yarn", 3)),
        ],
    )
    def test_parses_field(self, tmp_path, field, expected):
        from src.agent.stack import _parse_package_manager_field
        pkg = _write_pkg(tmp_path)
        data = json.loads(pkg.read_text())
        data["packageManager"] = field
        pkg.write_text(json.dumps(data))
        assert _parse_package_manager_field(pkg) == expected

    def test_no_field_returns_none(self, tmp_path):
        from src.agent.stack import _parse_package_manager_field
        pkg = _write_pkg(tmp_path)
        assert _parse_package_manager_field(pkg) is None

    def test_missing_file_returns_none(self, tmp_path):
        from src.agent.stack import _parse_package_manager_field
        assert _parse_package_manager_field(tmp_path / "nope.json") is None

    def test_malformed_field_returns_none(self, tmp_path):
        from src.agent.stack import _parse_package_manager_field
        pkg = _write_pkg(tmp_path)
        data = json.loads(pkg.read_text())
        data["packageManager"] = "yarn"  # no version
        pkg.write_text(json.dumps(data))
        assert _parse_package_manager_field(pkg) is None


class TestDetectYarnMajor:
    def test_package_manager_field_wins(self, tmp_path):
        from src.agent.stack import detect_yarn_major
        pkg = _write_pkg(tmp_path)
        data = json.loads(pkg.read_text())
        data["packageManager"] = "yarn@4.5.0"
        pkg.write_text(json.dumps(data))
        assert detect_yarn_major(tmp_path, pkg) == 4

    def test_yarnrc_yml_implies_berry(self, tmp_path):
        from src.agent.stack import detect_yarn_major
        pkg = _write_pkg(tmp_path)
        (tmp_path / ".yarnrc.yml").write_text("nodeLinker: node-modules\n")
        assert detect_yarn_major(tmp_path, pkg) == 2

    def test_field_takes_priority_over_yarnrc(self, tmp_path):
        from src.agent.stack import detect_yarn_major
        pkg = _write_pkg(tmp_path)
        data = json.loads(pkg.read_text())
        data["packageManager"] = "yarn@1.22.19"
        pkg.write_text(json.dumps(data))
        (tmp_path / ".yarnrc.yml").write_text("")
        assert detect_yarn_major(tmp_path, pkg) == 1

    def test_non_yarn_field_returns_none(self, tmp_path):
        from src.agent.stack import detect_yarn_major
        pkg = _write_pkg(tmp_path)
        data = json.loads(pkg.read_text())
        data["packageManager"] = "pnpm@9"
        pkg.write_text(json.dumps(data))
        assert detect_yarn_major(tmp_path, pkg) is None

    def test_no_signals_returns_none(self, tmp_path):
        from src.agent.stack import detect_yarn_major
        pkg = _write_pkg(tmp_path)
        assert detect_yarn_major(tmp_path, pkg) is None


class TestBuildInstallCommand:
    @pytest.mark.parametrize(
        "pm,has_lock,expected",
        [
            ("npm", True, ["npm", "ci"]),
            ("npm", False, ["npm", "install"]),
            ("pnpm", True, ["pnpm", "install", "--frozen-lockfile"]),
            ("yarn", True, ["yarn", "install", "--frozen-lockfile"]),
            ("bun", True, ["bun", "install", "--frozen-lockfile"]),
            ("pnpm", False, ["pnpm", "install"]),
        ],
    )
    def test_builds_command(self, pm, has_lock, expected):
        from src.agent.stack import build_install_command
        assert build_install_command(pm, has_lock) == expected

    def test_yarn_berry_uses_immutable(self):
        from src.agent.stack import build_install_command
        assert build_install_command("yarn", True, yarn_major=4) == [
            "yarn", "install", "--immutable",
        ]

    def test_yarn_classic_uses_frozen_lockfile(self):
        from src.agent.stack import build_install_command
        assert build_install_command("yarn", True, yarn_major=1) == [
            "yarn", "install", "--frozen-lockfile",
        ]

    def test_yarn_unknown_major_uses_frozen_lockfile(self):
        from src.agent.stack import build_install_command
        assert build_install_command("yarn", True, yarn_major=None) == [
            "yarn", "install", "--frozen-lockfile",
        ]

    def test_yarn_berry_no_lockfile_plain_install(self):
        from src.agent.stack import build_install_command
        assert build_install_command("yarn", False, yarn_major=4) == ["yarn", "install"]

    def test_pnpm_ignores_yarn_major(self):
        from src.agent.stack import build_install_command
        assert build_install_command("pnpm", True, yarn_major=4) == [
            "pnpm", "install", "--frozen-lockfile",
        ]

    def test_yarn_major_2_is_immutable(self):
        from src.agent.stack import build_install_command
        assert build_install_command("yarn", True, yarn_major=2) == [
            "yarn", "install", "--immutable",
        ]


class TestBuildDevCommand:
    def test_next_has_no_host_flag(self):
        from src.agent.stack import build_dev_command
        assert build_dev_command("npm", "next") == ["npm", "run", "dev"]

    def test_npm_vite_uses_double_dash(self):
        from src.agent.stack import build_dev_command
        assert build_dev_command("npm", "vite") == ["npm", "run", "dev", "--", "--host"]

    def test_pnpm_vite_forwards_directly(self):
        from src.agent.stack import build_dev_command
        assert build_dev_command("pnpm", "vite") == ["pnpm", "run", "dev", "--host"]

    @pytest.mark.parametrize("framework", ["vite", "astro", "sveltekit"])
    def test_host_flag_frameworks(self, framework):
        from src.agent.stack import build_dev_command
        assert "--host" in build_dev_command("yarn", framework)


class TestBuildDevEnv:
    def test_next(self):
        from src.agent.stack import build_dev_env
        assert build_dev_env("next") == {"HOST": "0.0.0.0"}

    def test_cra_disables_host_check(self):
        from src.agent.stack import build_dev_env
        env = build_dev_env("cra")
        assert env["DANGEROUSLY_DISABLE_HOST_CHECK"] == "true"
        assert env["HOST"] == "0.0.0.0"

    @pytest.mark.parametrize("framework", ["vite", "astro", "sveltekit", "spa"])
    def test_host_flag_frameworks_need_no_env(self, framework):
        from src.agent.stack import build_dev_env
        assert build_dev_env(framework) == {}


class TestBuildLaunchProfile:
    def test_pnpm_vite_monorepo(self, tmp_path):
        from src.agent.stack import build_launch_profile
        (tmp_path / "pnpm-lock.yaml").write_text("")
        pkg = _write_pkg(tmp_path, deps={"vite": "5.0.0"}, scripts={"dev": "vite"})
        profile = build_launch_profile(tmp_path, pkg)
        assert profile.package_manager == "pnpm"
        assert profile.framework == "vite"
        assert profile.install_command == ["pnpm", "install", "--frozen-lockfile"]
        assert profile.dev_command == ["pnpm", "run", "dev", "--host"]
        assert profile.default_port == 5173

    def test_yarn_berry_threads_immutable(self, tmp_path):
        from src.agent.stack import build_launch_profile
        (tmp_path / "yarn.lock").write_text("")
        pkg = _write_pkg(
            tmp_path, deps={"next": "14.0.0"}, scripts={"dev": "next dev"}
        )
        data = json.loads(pkg.read_text())
        data["packageManager"] = "yarn@4.5.0"
        pkg.write_text(json.dumps(data))
        profile = build_launch_profile(tmp_path, pkg)
        assert profile.package_manager == "yarn"
        assert profile.install_command == ["yarn", "install", "--immutable"]

    def test_yarn_classic_threads_frozen_lockfile(self, tmp_path):
        from src.agent.stack import build_launch_profile
        (tmp_path / "yarn.lock").write_text("")
        pkg = _write_pkg(
            tmp_path, deps={"next": "14.0.0"}, scripts={"dev": "next dev"}
        )
        data = json.loads(pkg.read_text())
        data["packageManager"] = "yarn@1.22.19"
        pkg.write_text(json.dumps(data))
        profile = build_launch_profile(tmp_path, pkg)
        assert profile.install_command == ["yarn", "install", "--frozen-lockfile"]

    def test_npm_next_defaults(self, tmp_path):
        from src.agent.stack import build_launch_profile
        (tmp_path / "package-lock.json").write_text("")
        pkg = _write_pkg(tmp_path, deps={"next": "14.0.0"}, scripts={"dev": "next dev"})
        profile = build_launch_profile(tmp_path, pkg)
        assert profile.package_manager == "npm"
        assert profile.framework == "next"
        assert profile.install_command == ["npm", "ci"]
        assert profile.dev_command == ["npm", "run", "dev"]
        assert profile.dev_env == {"HOST": "0.0.0.0"}
        assert profile.default_port == 3000
