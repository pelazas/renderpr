
from src.agent.network import get_public_ip


class TestGetPublicIp:
    def test_no_metadata_env_returns_localhost(self, monkeypatch):
        monkeypatch.delenv("ECS_CONTAINER_METADATA_URI_V4", raising=False)
        assert get_public_ip() == "localhost"

    def test_metadata_fetch_failure_returns_localhost(self, monkeypatch):
        monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://localhost")
        monkeypatch.setattr("httpx.get", lambda *a, **kw: (_ for _ in ()).throw(ConnectionError("fail")))
        assert get_public_ip() == "localhost"

    def test_no_networks_returns_localhost(self, monkeypatch):
        monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://localhost")

        def mock_get(*a, **kw):
            class Resp:
                status_code = 200

                def json(self):
                    return {"Networks": []}

                def raise_for_status(self):
                    pass

            return Resp()

        monkeypatch.setattr("httpx.get", mock_get)
        assert get_public_ip() == "localhost"

    def test_public_ip_found(self, monkeypatch):
        monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://localhost")

        def mock_get(*a, **kw):
            class Resp:
                status_code = 200

                def json(self):
                    return {
                        "Networks": [
                            {"IPv4Addresses": ["10.0.1.5"]}
                        ]
                    }

                def raise_for_status(self):
                    pass

            return Resp()

        monkeypatch.setattr("httpx.get", mock_get)

        def mock_ec2(self, *a, **kw):
            return type("EC2", (), {
                "describe_network_interfaces": lambda **kw: {
                    "NetworkInterfaces": [
                        {"Association": {"PublicIp": "54.123.45.67"}}
                    ]
                }
            })

        import boto3
        monkeypatch.setattr(boto3, "client", mock_ec2)
        assert get_public_ip() == "54.123.45.67"

    def test_no_public_ip_in_association_returns_localhost(self, monkeypatch):
        monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://localhost")

        def mock_get(*a, **kw):
            class Resp:
                status_code = 200

                def json(self):
                    return {
                        "Networks": [
                            {"IPv4Addresses": ["10.0.1.5"]}
                        ]
                    }

                def raise_for_status(self):
                    pass

            return Resp()

        monkeypatch.setattr("httpx.get", mock_get)

        def mock_ec2(self, *a, **kw):
            return type("EC2", (), {
                "describe_network_interfaces": lambda **kw: {
                    "NetworkInterfaces": [
                        {"Association": {}}
                    ]
                }
            })

        import boto3
        monkeypatch.setattr(boto3, "client", mock_ec2)
        assert get_public_ip() == "localhost"
