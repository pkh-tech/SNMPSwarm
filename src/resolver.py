"""Deterministic pool allocation and manifest generation."""

from __future__ import annotations

import os
import random
from datetime import datetime, timezone

import yaml

from src.config import ConfigError, FullConfig, STANDARD_UNIT_NAME, compute_config_fingerprint
from src.ip import calculate_ip, container_name, device_name, hostname


def _generate_initial_uptime(config: FullConfig, profile_name: str, device_id: int, seed: int) -> int:
    """Resolve the initial uptime (seconds) for one device.

    Raises ConfigError (instead of silently returning 0) when the profile
    does not exist or has an invalid mode: a silent zero would make the
    whole simulation start with a 0-second uptime for the affected devices.
    """
    profile = config.initial_uptime_profiles.get(profile_name)
    if profile is None:
        raise ConfigError(
            f"Device {device_id}: unknown initial_uptime profile {profile_name!r}; "
            f"known profiles: {sorted(config.initial_uptime_profiles)}"
        )
    rng = random.Random(f"{seed}-{profile.name}-{device_id}")
    if profile.mode == "fixed":
        return profile.days_fixed * 86400
    if profile.mode == "random":
        days = rng.randint(profile.days_min, profile.days_max)
        return days * 86400
    raise ConfigError(
        f"Device {device_id}: invalid initial_uptime mode {profile.mode!r} in "
        f"profile {profile.name!r} (valid modes: fixed, random)"
    )


def resolve_device_profiles(config: FullConfig) -> list[dict]:
    """Resolve exclusive pools into concrete device assignments.

    Every unit starts as ``standard unit``. Pools are then processed in YAML/CLI
    order and consume randomly selected units from the remaining unassigned set.
    A unit can therefore belong to at most one configured pool.
    """
    sim = config.simulation
    defaults = config.defaults
    assert defaults is not None

    devices: list[dict] = []
    for did in range(1, sim.device_count + 1):
        devices.append({
            "id": did,
            "name": device_name(did),
            "container_name": container_name(sim.name, did),
            "ip": calculate_ip(did),
            "hostname": hostname(did),
            "pool": STANDARD_UNIT_NAME,
            "downtime_profile": defaults.downtime_profile,
            "traffic_profile": defaults.traffic_profile,
            "initial_uptime_profile": defaults.initial_uptime_profile,
            "initial_uptime_seconds": _generate_initial_uptime(
                config, defaults.initial_uptime_profile, did, sim.random_seed
            ),
            "snmp_identity_profile": defaults.snmp_identity_profile,
            "startup_status": "started",
        })

    rng = random.Random(sim.random_seed)
    available = list(range(1, sim.device_count + 1))
    rng.shuffle(available)

    for pool in config.pools:
        selected = [available.pop() for _ in range(pool.resolved_count)]
        for did in selected:
            dev = devices[did - 1]
            dev["pool"] = pool.name
            dev["downtime_profile"] = pool.downtime_profile
            dev["traffic_profile"] = pool.traffic_profile
            dev["initial_uptime_profile"] = pool.initial_uptime_profile
            dev["initial_uptime_seconds"] = _generate_initial_uptime(
                config, pool.initial_uptime_profile, did, sim.random_seed
            )
            dev["snmp_identity_profile"] = pool.snmp_identity_profile

    # Keep the existing advanced per-ID initial uptime override capability.
    for override in config.overrides:
        for did in override.selector_ids:
            if 1 <= did <= sim.device_count:
                if override.initial_uptime_days is not None:
                    devices[did - 1]["initial_uptime_seconds"] = override.initial_uptime_days * 86400
                if override.initial_uptime_mode is not None:
                    devices[did - 1]["initial_uptime_profile"] = override.initial_uptime_mode

    return devices


def generate_manifest(config: FullConfig, devices: list[dict]) -> dict:
    return {
        "simulation_name": config.simulation.name,
        "config_fingerprint": compute_config_fingerprint(config),
        "random_seed": config.simulation.random_seed,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "devices": devices,
    }


def write_manifest_atomically(manifest: dict, path: str):
    dir_path = os.path.dirname(path)
    if dir_path:
        os.makedirs(dir_path, exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        yaml.dump(manifest, fh, default_flow_style=False, sort_keys=False)
    os.replace(tmp_path, path)


def load_manifest(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def run_resolution(config: FullConfig) -> list[dict]:
    devices = resolve_device_profiles(config)
    manifest = generate_manifest(config, devices)
    return devices, manifest
