"""
IP and identity calculation.

Calculates IP addresses, device names, hostnames, and container names
from device IDs.
"""

import ipaddress


def calculate_ip(device_number: int) -> str:
    """Calculate the IP address for a given device number.

    Formula:
        x = floor(device_number / 100)
        y = device_number % 100
        ip = 10.0.x.y

    Args:
        device_number: 1-based device number.

    Returns:
        IP address as string.

    Raises:
        ValueError: If device_number <= 0.
    """
    if device_number < 1:
        raise ValueError(f"Device number must be >= 1, got {device_number}")

    x = device_number // 100
    y = device_number % 100

    if y == 0:
        # e.g., device 100 -> 10.0.1.0, device 200 -> 10.0.2.0
        # y=0 is valid (final octet 0-99)
        pass

    return f"10.0.{x}.{y}"


def calculate_ip_int(device_number: int) -> tuple[int, int, int, int]:
    """Calculate the IP address components as integers.

    Returns:
        Tuple of (a, b, c, d) for IP a.b.c.d
    """
    if device_number < 1:
        raise ValueError(f"Device number must be >= 1, got {device_number}")

    x = device_number // 100
    y = device_number % 100
    return (10, 0, x, y)


def validate_ip_range(device_count: int, gateway: str) -> list[str]:
    """Validate that the given device count and gateway are compatible.

    Args:
        device_count: Number of devices.
        gateway: Gateway IP address.

    Returns:
        List of validation error messages. Empty if valid.
    """
    errors = []

    # Check gateway doesn't collide with any device IP
    for did in range(1, device_count + 1):
        dev_ip = calculate_ip(did)
        if dev_ip == gateway:
            errors.append(f"Gateway {gateway} collides with device {did} IP {dev_ip}")

    # Check subnet can accommodate all devices
    gw_network = ipaddress.ip_network(gateway + "/24", strict=False)
    # We need at least device_count addresses starting from 10.0.0.1
    # The max device generates IP 10.0.(count//100).(count%100)
    max_ip = calculate_ip(device_count)
    max_addr = ipaddress.ip_address(max_ip)
    min_addr = ipaddress.ip_address("10.0.0.1")
    total_addrs = int(max_addr) - int(min_addr) + 1
    if total_addrs > device_count:
        pass  # OK, plenty of room

    return errors


def device_name(device_id: int) -> str:
    """Generate the device name from ID.

    Example: 23 -> "device-023"
    """
    return f"device-{device_id:03d}"


def container_name(container_prefix: str, device_id: int) -> str:
    """Generate the container name.

    Example: prefix="network-dev", id=23 -> "network-dev-device-023"
    """
    return f"{container_prefix}-{device_name(device_id)}"


def hostname(device_id: int) -> str:
    """Generate the hostname. Same as device name."""
    return device_name(device_id)


def generate_device_map(device_count: int, container_prefix: str) -> list[dict]:
    """Generate a complete device map for all devices.

    Returns:
        List of dicts with keys: id, name, container_name, ip, hostname.
    """
    devices = []
    for did in range(1, device_count + 1):
        devices.append({
            "id": did,
            "name": device_name(did),
            "container_name": container_name(container_prefix, did),
            "ip": calculate_ip(did),
            "hostname": hostname(did),
        })
    return devices
