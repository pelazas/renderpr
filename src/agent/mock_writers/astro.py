from src.agent.mock_server import MockWriter, register_mock_writer, write_astro_allowed_hosts


class AstroMockWriter(MockWriter):
    framework = "astro"

    def write_dev_origin_allowlist(self, repo_dir, public_ip):
        return write_astro_allowed_hosts(repo_dir, public_ip)


register_mock_writer("astro", AstroMockWriter())
