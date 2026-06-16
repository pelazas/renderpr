from src.agent.mock_server import MockWriter, register_mock_writer, write_vite_allowed_hosts


class RemixMockWriter(MockWriter):
    framework = "remix"

    def write_dev_origin_allowlist(self, repo_dir, public_ip):
        return write_vite_allowed_hosts(repo_dir, public_ip)


register_mock_writer("remix", RemixMockWriter())
