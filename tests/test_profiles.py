"""SW-01: unknown/invalid initial_uptime profiles are explicit errors.

Valid profiles must resolve exactly as before (fixed = exact days,
random = deterministic value within bounds); unknown profile names and
invalid modes must raise ConfigError instead of silently returning 0.
"""
import copy

import pytest

from src.config import (
    BUILTIN_RAW_CONFIG,
    ConfigError,
    InitialUptimeProfile,
    validate_config,
)
from src.config import _profile_key
from src.resolver import _generate_initial_uptime


def make_config(tmp_path, pools=None, overrides=None):
    raw = copy.deepcopy(BUILTIN_RAW_CONFIG)
    raw["simulation"].update(
        {
            "name": "profile-test",
            "total_units": 10,
            "state_directory": str(tmp_path / "state"),
        }
    )
    if pools:
        raw["pools"] = pools
    if overrides is not None:
        raw["overrides"] = overrides
    return validate_config(raw)


# --- valid profiles behave exactly as before -----------------------------


def test_fixed_profile_returns_exact_days(tmp_path):
    config = make_config(
        tmp_path,
        pools=[{"name": "edge", "count": 2, "initial_uptime": {"days": 3}}],
    )
    name = _profile_key("edge", "uptime")
    assert name in config.initial_uptime_profiles
    assert _generate_initial_uptime(config, name, 4, seed=99) == 3 * 86400
    # Deterministic: every call returns the same value regardless of seed.
    assert _generate_initial_uptime(config, name, 7, seed=1) == 3 * 86400


def test_random_profile_within_bounds_and_deterministic(tmp_path):
    config = make_config(
        tmp_path,
        pools=[{"name": "edge", "count": 2, "initial_uptime": {"days": {"min": 1, "max": 5}}}],
    )
    name = _profile_key("edge", "uptime")
    profile = config.initial_uptime_profiles[name]
    assert profile.mode == "random"

    value = _generate_initial_uptime(config, name, 3, seed=42)
    assert 1 * 86400 <= value <= 5 * 86400
    assert value % 86400 == 0
    # Same seed/device/profile -> same value.
    assert _generate_initial_uptime(config, name, 3, seed=42) == value


def test_standard_unit_profile_still_works(tmp_path):
    config = make_config(tmp_path)
    default_name = config.defaults.initial_uptime_profile
    assert default_name in config.initial_uptime_profiles
    # Standard unit: 0 days -> 0 seconds, exactly as before the fix.
    assert _generate_initial_uptime(config, default_name, 1, seed=0) == 0


# --- invalid profiles are explicit errors --------------------------------


def test_unknown_profile_raises_config_error(tmp_path):
    config = make_config(tmp_path)
    with pytest.raises(ConfigError) as excinfo:
        _generate_initial_uptime(config, "no-such-profile", 1, seed=0)
    message = str(excinfo.value)
    assert "no-such-profile" in message
    # The error lists the known profiles so the operator can fix the config.
    for known in config.initial_uptime_profiles:
        assert known in message


def test_invalid_mode_raises_config_error(tmp_path):
    config = make_config(tmp_path)
    default_name = config.defaults.initial_uptime_profile
    original = config.initial_uptime_profiles[default_name]
    config.initial_uptime_profiles[default_name] = InitialUptimeProfile(
        name=original.name, mode="bogus"
    )
    try:
        with pytest.raises(ConfigError) as excinfo:
            _generate_initial_uptime(config, default_name, 1, seed=0)
        assert "bogus" in str(excinfo.value)
        assert "valid modes: fixed, random" in str(excinfo.value)
    finally:
        config.initial_uptime_profiles[default_name] = original


# --- override initial_uptime.mode is validated ----------------------------


def test_override_invalid_mode_rejected(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        make_config(
            tmp_path,
            overrides=[
                {
                    "selector": {"ids": [1]},
                    "initial_uptime": {"mode": "bogus", "days": 2},
                }
            ],
        )
    assert "bogus" in str(excinfo.value)
    assert "fixed, random" in str(excinfo.value)


@pytest.mark.parametrize("mode", ["fixed", "random"])
def test_override_valid_modes_accepted(tmp_path, mode):
    config = make_config(
        tmp_path,
        overrides=[
            {"selector": {"ids": [1]}, "initial_uptime": {"mode": mode, "days": 2}}
        ],
    )
    assert len(config.overrides) == 1
    assert config.overrides[0].initial_uptime_mode == mode
    assert config.overrides[0].initial_uptime_days == 2


def test_override_without_mode_still_accepted(tmp_path):
    config = make_config(
        tmp_path,
        overrides=[{"selector": {"ids": [1]}, "initial_uptime": {"days": 2}}],
    )
    assert config.overrides[0].initial_uptime_mode is None
    assert config.overrides[0].initial_uptime_days == 2
