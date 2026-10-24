from collections import Counter

import pytest

from src.config import (
    ConfigError,
    STANDARD_UNIT_NAME,
    validate_config,
)
from src.resolver import resolve_device_profiles


def test_empty_config_is_all_standard_units():
    cfg = validate_config({"simulation": {"total_units": 10}})
    devices = resolve_device_profiles(cfg)
    assert len(devices) == 10
    assert {d["pool"] for d in devices} == {STANDARD_UNIT_NAME}


def test_count_pools_consume_from_total_without_overlap():
    cfg = validate_config({
        "simulation": {"total_units": 200},
        "pools": [
            {"name": "unstable", "count": 43, "downtime": {"events_per_day": 5}},
            {"name": "mostly", "count": 50, "downtime": {"events_per_day": 1}},
        ],
    })
    devices = resolve_device_profiles(cfg)
    counts = Counter(d["pool"] for d in devices)
    assert counts == {"unstable": 43, "mostly": 50, STANDARD_UNIT_NAME: 107}
    assert len({d["id"] for d in devices}) == 200


def test_pool_requires_count():
    with pytest.raises(ConfigError, match="count is required"):
        validate_config({
            "simulation": {"total_units": 10},
            "pools": [{"name": "bad"}],
        })


@pytest.mark.parametrize("value", [0, -1, 1.5, "5", True])
def test_pool_count_must_be_positive_integer(value):
    with pytest.raises(ConfigError, match="count must be a positive integer"):
        validate_config({
            "simulation": {"total_units": 10},
            "pools": [{"name": "bad", "count": value}],
        })


def test_percent_pool_sizing_is_rejected():
    with pytest.raises(ConfigError, match="percent-based pool sizing is not supported"):
        validate_config({
            "simulation": {"total_units": 10},
            "pools": [{"name": "bad", "percent": 50}],
        })


def test_pool_overallocation_is_rejected():
    with pytest.raises(ConfigError, match="Pool allocation exceeds total_units"):
        validate_config({
            "simulation": {"total_units": 10},
            "pools": [
                {"name": "a", "count": 8},
                {"name": "b", "count": 3},
            ],
        })


def test_single_pool_cannot_exceed_total():
    with pytest.raises(ConfigError, match="Pool allocation exceeds total_units"):
        validate_config({
            "simulation": {"total_units": 10},
            "pools": [{"name": "a", "count": 11}],
        })


def test_duplicate_pool_names_are_rejected():
    with pytest.raises(ConfigError, match="Duplicate pool name"):
        validate_config({
            "simulation": {"total_units": 10},
            "pools": [
                {"name": "same", "count": 2},
                {"name": "same", "count": 2},
            ],
        })


def test_standard_unit_name_is_reserved():
    with pytest.raises(ConfigError, match="reserved"):
        validate_config({
            "simulation": {"total_units": 10},
            "pools": [{"name": "standard unit", "count": 2}],
        })


def test_omitted_pool_fields_use_standard_unit_behaviour():
    cfg = validate_config({
        "simulation": {"total_units": 10},
        "pools": [{"name": "uptime-only", "count": 2, "initial_uptime": {"days": 30}}],
    })
    pool = cfg.pools[0]
    assert pool.downtime_profile == cfg.defaults.downtime_profile
    assert pool.traffic_profile == cfg.defaults.traffic_profile
    assert pool.snmp_identity_profile == cfg.defaults.snmp_identity_profile


def test_direct_downtime_values_are_resolved():
    cfg = validate_config({
        "simulation": {"total_units": 10},
        "pools": [{
            "name": "unstable",
            "count": 2,
            "downtime": {
                "events_per_day": 5,
                "duration_minutes": {"min": 2, "max": 20},
                "minimum_uptime_minutes": 15,
            },
        }],
    })
    profile = cfg.downtime_profiles[cfg.pools[0].downtime_profile]
    assert profile.enabled is True
    assert profile.expected_events_per_day == 5
    assert profile.duration_min == 2
    assert profile.duration_max == 20
    assert profile.minimum_uptime_minutes == 15


def test_initial_uptime_range_is_applied_to_pool_devices():
    cfg = validate_config({
        "simulation": {"total_units": 20, "random_seed": 123},
        "pools": [{
            "name": "old",
            "count": 10,
            "initial_uptime": {"days": {"min": 30, "max": 100}},
        }],
    })
    devices = resolve_device_profiles(cfg)
    pool_devices = [d for d in devices if d["pool"] == "old"]
    assert len(pool_devices) == 10
    for device in pool_devices:
        days = device["initial_uptime_seconds"] / 86400
        assert 30 <= days <= 100


def test_identity_and_traffic_overrides_are_pool_local():
    cfg = validate_config({
        "simulation": {"total_units": 10},
        "pools": [{
            "name": "switches",
            "count": 3,
            "identity": {"type": "switch"},
            "traffic": {"link_speed_mbps": 1000, "rate_mbps": {"min": 10, "max": 20}},
        }],
    })
    pool = cfg.pools[0]
    identity = cfg.snmp_identity_profiles[pool.snmp_identity_profile]
    traffic = cfg.traffic_profiles[pool.traffic_profile]
    assert identity.sys_descr == "Generic managed Ethernet switch"
    assert traffic.link_speed_mbps == 1000
    assert (traffic.rate_min, traffic.rate_max) == (10, 20)
    # Built-in fallback remains a 100 Mbit endpoint.
    standard_identity = cfg.snmp_identity_profiles[cfg.defaults.snmp_identity_profile]
    standard_traffic = cfg.traffic_profiles[cfg.defaults.traffic_profile]
    assert standard_identity.sys_descr == "Generic monitored endpoint"
    assert standard_traffic.link_speed_mbps == 100


def test_overlapping_traffic_periods_are_rejected():
    with pytest.raises(ConfigError, match="overlap"):
        validate_config({
            "simulation": {"total_units": 10},
            "pools": [{
                "name": "busy",
                "count": 2,
                "traffic": {
                    "link_speed_mbps": 100,
                    "periods": [
                        {"time": "09:00-12:00", "rate_mbps": {"min": 10, "max": 20}},
                        {"time": "11:00-13:00", "rate_mbps": {"min": 10, "max": 20}},
                    ],
                },
            }],
        })


def test_pool_assignment_is_deterministic_for_same_seed():
    raw = {
        "simulation": {"total_units": 30, "random_seed": 42},
        "pools": [{"name": "a", "count": 10}, {"name": "b", "count": 5}],
    }
    first = resolve_device_profiles(validate_config(raw))
    second = resolve_device_profiles(validate_config(raw))
    assert [(d["id"], d["pool"]) for d in first] == [(d["id"], d["pool"]) for d in second]


def test_config_example_is_valid_v100_config():
    from pathlib import Path
    import yaml

    example = Path(__file__).resolve().parents[1] / "config.example"
    raw = yaml.safe_load(example.read_text())
    cfg = validate_config(raw)
    assert cfg.simulation.device_count == 200
    assert sum(pool.resolved_count for pool in cfg.pools) == 113
