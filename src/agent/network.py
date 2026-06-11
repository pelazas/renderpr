import logging
import os

logger = logging.getLogger(__name__)

ECS_TASK_METADATA_ENV = "ECS_CONTAINER_METADATA_URI_V4"


def _fetch_json(url: str) -> dict | None:
    try:
        import httpx

        resp = httpx.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        logger.exception("Failed to fetch metadata from %s", url)
        return None


def _extract_private_ip(data: dict) -> str | None:
    networks = data.get("Networks") or data.get("networks") or []
    if not networks:
        return None
    ips = networks[0].get("IPv4Addresses") or networks[0].get("ipv4Addresses") or []
    return ips[0] if ips else None


def get_public_ip() -> str:
    metadata_uri = os.environ.get(ECS_TASK_METADATA_ENV)
    if not metadata_uri:
        logger.warning("ECS_CONTAINER_METADATA_URI_V4 not set, cannot get public IP")
        return "localhost"

    private_ip: str | None = None

    # Try container-level metadata first (always has Networks)
    container_data = _fetch_json(metadata_uri)
    if container_data:
        private_ip = _extract_private_ip(container_data)

    # Fall back to task-level metadata
    if not private_ip:
        task_data = _fetch_json(f"{metadata_uri}/task")
        if task_data:
            private_ip = _extract_private_ip(task_data)

    if not private_ip:
        logger.warning("Could not find private IP in container or task metadata")
        return "localhost"

    import boto3

    ec2 = boto3.client("ec2")
    try:
        eni_resp = ec2.describe_network_interfaces(
            Filters=[{"Name": "addresses.private-ip-address", "Values": [private_ip]}],
        )
    except Exception:
        logger.exception("Failed to describe network interfaces")
        return "localhost"

    interfaces = eni_resp.get("NetworkInterfaces", [])
    for iface in interfaces:
        public_ip = iface.get("Association", {}).get("PublicIp")
        if public_ip:
            return public_ip

    logger.warning("No public IP found for private IP %s", private_ip)
    return "localhost"


def get_task_arn() -> str:
    metadata_uri = os.environ.get(ECS_TASK_METADATA_ENV)
    if not metadata_uri:
        logger.warning("ECS_CONTAINER_METADATA_URI_V4 not set, cannot get task ARN")
        return ""

    task_data = _fetch_json(f"{metadata_uri}/task")
    if task_data:
        arn = task_data.get("TaskARN") or task_data.get("taskArn")
        if arn:
            return arn

    logger.warning("Could not find TaskARN in task metadata")
    return ""
