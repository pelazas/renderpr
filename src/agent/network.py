import json
import logging
import os

logger = logging.getLogger(__name__)

ECS_TASK_METADATA_ENV = "ECS_CONTAINER_METADATA_URI_V4"


def get_public_ip() -> str:
    metadata_uri = os.environ.get(ECS_TASK_METADATA_ENV)
    if not metadata_uri:
        logger.warning("ECS_CONTAINER_METADATA_URI_V4 not set, cannot get public IP")
        return "localhost"

    try:
        import httpx
        resp = httpx.get(f"{metadata_uri}/task", timeout=10)
        resp.raise_for_status()
        task_data = resp.json()
    except Exception:
        logger.exception("Failed to fetch ECS task metadata")
        return "localhost"

    networks = task_data.get("Networks", [])
    if not networks:
        logger.warning("No network info in task metadata")
        return "localhost"

    private_ips = networks[0].get("IPv4Addresses", [])
    if not private_ips:
        logger.warning("No private IP in task metadata")
        return "localhost"

    private_ip = private_ips[0]

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
        association = iface.get("Association", {})
        public_ip = association.get("PublicIp")
        if public_ip:
            return public_ip

    logger.warning("No public IP found for private IP %s", private_ip)
    return "localhost"
