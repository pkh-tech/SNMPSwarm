"""SNMPSwarm configuration parsing and validation.

Public configuration is intentionally simple:

* ``simulation.total_units`` defines the size of the swarm.
* ``pools`` are mutually exclusive groups of units.
* Every pool uses an explicit positive integer ``count``.
* Pool settings are direct values (downtime, traffic, initial uptime, identity),
  not references to separate profile definitions.
* Units that are not consumed by a configured pool are ``standard unit`` and
  use SNMPSwarm's built-in behaviour.

Internally we still normalize direct pool values into small typed profile
objects because the runtime agent already consumes that shape. Those internal
profiles are an implementation detail and never need to appear in YAML.
"""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import os
from dataclasses import dataclass, field
from typing import Any

import yaml


MAX_DEVICE_NUMBER = 25_599
STANDARD_UNIT_NAME = "standard unit"

# Built-in public defaults. A config file only needs to contain deviations.
BUILTIN_RAW_CONFIG: dict[str, Any] = {
    "simulation": {
        "name": "snmpswarm",
        "total_units": 200,
        "random_seed": 12345,
        "timezone": "UTC",
        "state_directory": "./state/snmpswarm",
        "startup_parallelism": 10,
    },
    "network": {
        "subnet": "10.0.0.0/16",
        "gateway": "10.0.255.254",
        "snmp_port": 161,
        "snmp_version": "2c",
        "community": "public",
    },
    "pools": [],
    "overrides": [],
}

# The standard unit is intentionally boring: a small 100 Mbit endpoint that
# stays online. Everything else is opt-in through pools.
STANDARD_DOWNTIME = {
    "enabled": False,
    "events_per_day": 0.0,
    "duration_minutes": {"min": 1.0, "max": 5.0},
    "minimum_uptime_minutes": 0.0,
}
STANDARD_TRAFFIC = {
    "interfaces": 1,
    "link_speed_mbps": 100,
    "rate_mbps": {"min": 0.0, "max": 0.05},
    "variation_percent": 5.0,
    "periods": [],
}
STANDARD_INITIAL_UPTIME = {"days": 0}
STANDARD_IDENTITY = {
    "type": "endpoint",
    "sys_descr": "Generic monitored endpoint",
    "sys_object_id": "1.3.6.1.4.1.8072.3.2.10",
    "sys_contact": "lab@example.invalid",
    "sys_location": "simulated-network",
    "sys_services": 0,
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DurationRange:
    min: float
    max: float


@dataclass
class RateRange:
    min: float
    max: float


@dataclass
class TrafficPeriod:
    start_hour: int
    start_min: int
    end_hour: int
    end_min: int
    rate_min: float
    rate_max: float


@dataclass
class DowntimeProfile:
    name: str
    enabled: bool
    expected_events_per_day: float
    duration_min: float
    duration_max: float
    minimum_uptime_minutes: float


@dataclass
class TrafficProfile:
    name: str
    interfaces: int
    link_speed_mbps: int
    rate_min: float
    rate_max: float
    variation_percent: float
    periods: list[TrafficPeriod] = field(default_factory=list)


@dataclass
class InitialUptimeProfile:
    name: str
    mode: str  # random | fixed
    days_min: int = 0
    days_max: int = 0
    days_fixed: int = 0


@dataclass
class SnmpIdentityProfile:
    name: str
    sys_descr: str
    sys_object_id: str
    sys_contact: str
    sys_location: str
    sys_services: int


@dataclass
class MainPoolDefaults:
    """Internal runtime defaults for the built-in standard unit."""

    downtime_profile: str
    traffic_profile: str
    initial_uptime_profile: str
    snmp_identity_profile: str


@dataclass
class PoolConfig:
    name: str
    count: int
    resolved_count: int
    downtime_profile: str
    traffic_profile: str
    initial_uptime_profile: str
    snmp_identity_profile: str


@dataclass
class OverridesEntry:
    selector_ids: list[int]
    initial_uptime_mode: str | None = None
    initial_uptime_days: int | None = None


@dataclass
class SimulationConfig:
    name: str
    device_count: int
    random_seed: int
    timezone: str
    state_directory: str
    startup_parallelism: int = 16
    shutdown_parallelism: int = 16
    shutdown_grace_period: float = 10.0


@dataclass
class NetworkConfig:
    subnet: str
    gateway: str
    snmp_port: int
    snmp_version: str
    community: str


@dataclass
class FullConfig:
    simulation: SimulationConfig
    network: NetworkConfig
    pools: list[PoolConfig] = field(default_factory=list)
    downtime_profiles: dict[str, DowntimeProfile] = field(default_factory=dict)
    traffic_profiles: dict[str, TrafficProfile] = field(default_factory=dict)
    initial_uptime_profiles: dict[str, InitialUptimeProfile] = field(default_factory=dict)
    snmp_identity_profiles: dict[str, SnmpIdentityProfile] = field(default_factory=dict)
    defaults: MainPoolDefaults | None = None
    overrides: list[OverridesEntry] = field(default_factory=list)


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, overlay: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _parse_range(value: Any, *, field_name: str, allow_float: bool = True) -> tuple[float, float]:
    if isinstance(value, (int, float)):
        number = float(value)
        return number, number
    if not isinstance(value, dict) or "min" not in value or "max" not in value:
        raise ConfigError(f"{field_name} must be a number or a mapping with min/max")
    try:
        lo = float(value["min"])
        hi = float(value["max"])
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field_name}.min/max must be numeric") from exc
    if lo > hi:
        raise ConfigError(f"{field_name}: min ({lo}) must not exceed max ({hi})")
    return lo, hi


def _parse_traffic_period(p: dict) -> TrafficPeriod:
    if not isinstance(p, dict):
        raise ConfigError(f"Invalid traffic period: {p!r}")
    time_str = str(p.get("time", "")).strip()
    parts = time_str.split("-")
    if len(parts) != 2:
        raise ConfigError(f"Invalid traffic period time format: {time_str!r}")

    def parse_hhmm(value: str) -> tuple[int, int]:
        bits = value.strip().split(":")
        if not 1 <= len(bits) <= 2:
            raise ConfigError(f"Invalid traffic period time component: {value!r}")
        try:
            hour = int(bits[0])
            minute = int(bits[1]) if len(bits) == 2 else 0
        except ValueError as exc:
            raise ConfigError(f"Invalid traffic period time component: {value!r}") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ConfigError(f"Traffic period time out of range: {value!r}")
        return hour, minute

    sh, sm = parse_hhmm(parts[0])
    eh, em = parse_hhmm(parts[1])
    rate_raw = p.get("rate_mbps")
    rate_min, rate_max = _parse_range(rate_raw, field_name=f"traffic period {time_str} rate_mbps")
    if rate_min < 0:
        raise ConfigError(f"Traffic period {time_str}: rate_mbps must be >= 0")
    return TrafficPeriod(sh, sm, eh, em, rate_min, rate_max)


def _period_intervals(period: TrafficPeriod) -> list[tuple[int, int]]:
    start = period.start_hour * 60 + period.start_min
    end = period.end_hour * 60 + period.end_min
    if start == end:
        return [(0, 24 * 60)]
    if start < end:
        return [(start, end)]
    return [(start, 24 * 60), (0, end)]


def _validate_non_overlapping_periods(pool_name: str, periods: list[TrafficPeriod]) -> None:
    expanded = [(_period_intervals(period), idx) for idx, period in enumerate(periods)]
    for left_pos in range(len(expanded)):
        left_intervals, left_idx = expanded[left_pos]
        for right_pos in range(left_pos + 1, len(expanded)):
            right_intervals, right_idx = expanded[right_pos]
            overlaps = any(
                max(a_start, b_start) < min(a_end, b_end)
                for a_start, a_end in left_intervals
                for b_start, b_end in right_intervals
            )
            if overlaps:
                raise ConfigError(
                    f"Pool {pool_name!r}: traffic periods #{left_idx + 1} and "
                    f"#{right_idx + 1} overlap"
                )


def _profile_key(pool_name: str, category: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in pool_name)
    return f"pool:{safe}:{category}"


# ---------------------------------------------------------------------------
# Loading / validation
# ---------------------------------------------------------------------------

def load_config(path: str) -> FullConfig:
    if not os.path.isfile(path):
        raise ConfigError(f"Configuration file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Could not read configuration file {path}: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("Configuration must be a YAML mapping.")
    return validate_config(raw)


def validate_config(raw: dict) -> FullConfig:
    if not isinstance(raw, dict):
        raise ConfigError("Configuration must be a YAML mapping.")

    legacy_keys = [key for key in ("profiles", "main_pool") if key in raw]
    if legacy_keys:
        raise ConfigError(
            "This configuration uses the old profiles/distributions schema "
            f"({', '.join(legacy_keys)}). SNMPSwarm now uses direct values in named pools."
        )

    merged = _deep_merge(BUILTIN_RAW_CONFIG, raw)

    # --- simulation -------------------------------------------------------
    sim_raw = merged.get("simulation", {})
    if not isinstance(sim_raw, dict):
        raise ConfigError("simulation must be a mapping")

    sim_name = str(sim_raw.get("name", "snmpswarm")).strip()
    if not sim_name or not sim_name.replace("-", "").replace("_", "").isalnum():
        raise ConfigError(f"simulation.name contains invalid characters: {sim_name!r}")

    # total_units is the public name. device_count is accepted only as a
    # backwards-compatible alias while the project is transitioning.
    if "total_units" in sim_raw and "device_count" in sim_raw:
        if sim_raw["total_units"] != sim_raw["device_count"]:
            raise ConfigError("simulation.total_units and legacy device_count disagree")
    total_units = sim_raw.get("total_units", sim_raw.get("device_count", 200))
    if not isinstance(total_units, int) or total_units < 1:
        raise ConfigError(f"simulation.total_units must be a positive integer, got {total_units!r}")
    if total_units > MAX_DEVICE_NUMBER:
        raise ConfigError(
            f"simulation.total_units={total_units} exceeds the fixed 10.0.x.y "
            f"address model maximum of {MAX_DEVICE_NUMBER}"
        )

    seed = sim_raw.get("random_seed", 12345)
    if not isinstance(seed, int):
        raise ConfigError(f"simulation.random_seed must be an integer, got {seed!r}")

    tz = str(sim_raw.get("timezone", "UTC")).strip()
    if not os.path.isfile(os.path.join("/usr/share/zoneinfo", tz)):
        raise ConfigError(f"Invalid timezone: {tz!r}")

    state_dir = str(sim_raw.get("state_directory", f"./state/{sim_name}"))
    parallelism = sim_raw.get("startup_parallelism", 10)
    if not isinstance(parallelism, int) or parallelism < 1:
        raise ConfigError("simulation.startup_parallelism must be a positive integer")

    simulation = SimulationConfig(
        name=sim_name,
        device_count=total_units,
        random_seed=seed,
        timezone=tz,
        state_directory=state_dir,
        startup_parallelism=parallelism,
    )

    # --- network ----------------------------------------------------------
    net_raw = merged.get("network", {})
    if not isinstance(net_raw, dict):
        raise ConfigError("network must be a mapping")
    subnet_str = str(net_raw.get("subnet", "10.0.0.0/16")).strip()
    gateway_str = str(net_raw.get("gateway", "10.0.255.254")).strip()
    snmp_port = int(net_raw.get("snmp_port", 161))
    snmp_version = str(net_raw.get("snmp_version", "2c")).strip()
    community = str(net_raw.get("community", "public")).strip()
    if not (1 <= snmp_port <= 65535):
        raise ConfigError("network.snmp_port must be 1..65535")
    if snmp_version != "2c":
        raise ConfigError("network.snmp_version currently must be '2c'")
    if not community:
        raise ConfigError("network.community must be non-empty")

    try:
        net = ipaddress.ip_network(subnet_str, strict=False)
        gw = ipaddress.ip_address(gateway_str)
    except ValueError as exc:
        raise ConfigError(f"Invalid network configuration: {exc}") from exc
    if gw not in net:
        raise ConfigError(f"Gateway {gateway_str} is not in subnet {subnet_str}")
    for did in range(1, total_units + 1):
        dev_addr = ipaddress.ip_address(f"10.0.{did // 100}.{did % 100}")
        if dev_addr not in net:
            raise ConfigError(
                f"network.subnet {subnet_str} does not contain device {did} IP {dev_addr}"
            )
        if dev_addr == gw:
            raise ConfigError(f"Gateway {gateway_str} collides with device {did} IP {dev_addr}")

    network = NetworkConfig(subnet_str, gateway_str, snmp_port, snmp_version, community)

    # --- internal standard unit profiles --------------------------------
    std_dt_name = "__standard_unit_downtime__"
    std_tr_name = "__standard_unit_traffic__"
    std_up_name = "__standard_unit_uptime__"
    std_id_name = "__standard_unit_identity__"

    downtime_profiles: dict[str, DowntimeProfile] = {
        std_dt_name: DowntimeProfile(
            name=std_dt_name,
            enabled=False,
            expected_events_per_day=0.0,
            duration_min=1.0,
            duration_max=5.0,
            minimum_uptime_minutes=0.0,
        )
    }
    traffic_profiles: dict[str, TrafficProfile] = {
        std_tr_name: TrafficProfile(
            name=std_tr_name,
            interfaces=1,
            link_speed_mbps=100,
            rate_min=0.0,
            rate_max=0.05,
            variation_percent=5.0,
            periods=[],
        )
    }
    initial_uptime_profiles: dict[str, InitialUptimeProfile] = {
        std_up_name: InitialUptimeProfile(name=std_up_name, mode="fixed", days_fixed=0)
    }
    snmp_identity_profiles: dict[str, SnmpIdentityProfile] = {
        std_id_name: SnmpIdentityProfile(
            name=std_id_name,
            sys_descr=STANDARD_IDENTITY["sys_descr"],
            sys_object_id=STANDARD_IDENTITY["sys_object_id"],
            sys_contact=STANDARD_IDENTITY["sys_contact"],
            sys_location=STANDARD_IDENTITY["sys_location"],
            sys_services=STANDARD_IDENTITY["sys_services"],
        )
    }
    defaults = MainPoolDefaults(std_dt_name, std_tr_name, std_up_name, std_id_name)

    # --- pools ------------------------------------------------------------
    pools_raw = merged.get("pools", [])
    if pools_raw is None:
        pools_raw = []
    if not isinstance(pools_raw, list):
        raise ConfigError("pools must be a list")

    pools: list[PoolConfig] = []
    seen_names: set[str] = set()
    allocated = 0

    for index, pool_raw in enumerate(pools_raw, start=1):
        if not isinstance(pool_raw, dict):
            raise ConfigError(f"pools[{index}] must be a mapping")
        name = str(pool_raw.get("name", "")).strip()
        if not name:
            raise ConfigError(f"pools[{index}].name is required")
        if name.casefold() == STANDARD_UNIT_NAME.casefold():
            raise ConfigError(f"Pool name {name!r} is reserved for unassigned units")
        if name in seen_names:
            raise ConfigError(f"Duplicate pool name: {name!r}")
        seen_names.add(name)

        if "percent" in pool_raw:
            raise ConfigError(
                f"Pool {name!r}: percent-based pool sizing is not supported; use count"
            )
        if "count" not in pool_raw:
            raise ConfigError(f"Pool {name!r}: count is required")

        count = pool_raw["count"]
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            raise ConfigError(f"Pool {name!r}: count must be a positive integer")
        resolved_count = count

        allocated += resolved_count
        if allocated > total_units:
            raise ConfigError(
                f"Pool allocation exceeds total_units: requested {allocated}, "
                f"available {total_units} (while adding pool {name!r})"
            )

        # Downtime: direct values, field-by-field fallback to standard unit.
        dt_name = defaults.downtime_profile
        if "downtime" in pool_raw:
            raw_downtime = pool_raw.get("downtime")
            if not isinstance(raw_downtime, (dict, type(None))):
                raise ConfigError(f"Pool {name!r}: downtime must be a mapping")
            dt_raw = _deep_merge(STANDARD_DOWNTIME, raw_downtime or {})
            try:
                events = float(dt_raw.get("events_per_day", 0))
                min_uptime = float(dt_raw.get("minimum_uptime_minutes", 0))
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"Pool {name!r}: downtime values must be numeric") from exc
            dmin, dmax = _parse_range(
                dt_raw.get("duration_minutes", {"min": 1, "max": 5}),
                field_name=f"Pool {name!r} downtime.duration_minutes",
            )
            if events < 0 or dmin < 0 or min_uptime < 0:
                raise ConfigError(f"Pool {name!r}: downtime values must be >= 0")
            # If the user does not explicitly set enabled, any positive
            # events_per_day value enables downtime. The standard-unit fallback
            # is always-online, but must not suppress a pool that defines outages.
            enabled = (
                bool(raw_downtime.get("enabled"))
                if "enabled" in raw_downtime
                else events > 0
            )
            dt_name = _profile_key(name, "downtime")
            downtime_profiles[dt_name] = DowntimeProfile(
                dt_name,
                enabled and events > 0,
                events,
                dmin,
                dmax,
                min_uptime,
            )

        # Traffic.
        tr_name = defaults.traffic_profile
        if "traffic" in pool_raw:
            raw_traffic = pool_raw.get("traffic")
            if not isinstance(raw_traffic, dict):
                raise ConfigError(f"Pool {name!r}: traffic must be a mapping")
            tr_raw = _deep_merge(STANDARD_TRAFFIC, raw_traffic)
            interfaces = int(tr_raw.get("interfaces", 1))
            link_speed = int(tr_raw.get("link_speed_mbps", 100))
            variation = float(tr_raw.get("variation_percent", 5))
            rate_value = tr_raw.get("rate_mbps", tr_raw.get("default_rate_mbps"))
            rate_min, rate_max = _parse_range(
                rate_value, field_name=f"Pool {name!r} traffic.rate_mbps"
            )
            if interfaces < 1 or link_speed <= 0 or variation < 0 or rate_min < 0:
                raise ConfigError(f"Pool {name!r}: invalid traffic values")
            if rate_max > link_speed:
                raise ConfigError(
                    f"Pool {name!r}: traffic rate max {rate_max} exceeds link speed {link_speed}"
                )
            periods = [_parse_traffic_period(p) for p in tr_raw.get("periods", [])]
            _validate_non_overlapping_periods(name, periods)
            for period in periods:
                if period.rate_max > link_speed:
                    raise ConfigError(
                        f"Pool {name!r}: period rate max {period.rate_max} exceeds "
                        f"link speed {link_speed}"
                    )
            tr_name = _profile_key(name, "traffic")
            traffic_profiles[tr_name] = TrafficProfile(
                tr_name, interfaces, link_speed, rate_min, rate_max, variation, periods
            )

        # Initial uptime: days: NUMBER means fixed; days: {min,max} means random.
        up_name = defaults.initial_uptime_profile
        if "initial_uptime" in pool_raw:
            up_raw = pool_raw.get("initial_uptime")
            if not isinstance(up_raw, dict):
                raise ConfigError(f"Pool {name!r}: initial_uptime must be a mapping")
            days_raw = up_raw.get("days", 0)
            up_name = _profile_key(name, "uptime")
            if isinstance(days_raw, (int, float)) and not isinstance(days_raw, bool):
                days = int(days_raw)
                if days < 0:
                    raise ConfigError(f"Pool {name!r}: initial_uptime.days must be >= 0")
                initial_uptime_profiles[up_name] = InitialUptimeProfile(
                    up_name, "fixed", days_fixed=days
                )
            elif isinstance(days_raw, dict):
                try:
                    days_min = int(days_raw.get("min", 0))
                    days_max = int(days_raw.get("max", 0))
                except (TypeError, ValueError) as exc:
                    raise ConfigError(f"Pool {name!r}: initial_uptime days must be integers") from exc
                if days_min < 0 or days_max < 0 or days_min > days_max:
                    raise ConfigError(f"Pool {name!r}: invalid initial_uptime.days range")
                initial_uptime_profiles[up_name] = InitialUptimeProfile(
                    up_name, "random", days_min=days_min, days_max=days_max
                )
            else:
                raise ConfigError(
                    f"Pool {name!r}: initial_uptime.days must be a number or min/max mapping"
                )

        # Identity. 'type' provides sane endpoint/switch SNMP defaults, while
        # individual sys_* fields can still be overridden directly.
        id_name = defaults.snmp_identity_profile
        if "identity" in pool_raw:
            id_raw = pool_raw.get("identity")
            if not isinstance(id_raw, dict):
                raise ConfigError(f"Pool {name!r}: identity must be a mapping")
            kind = str(id_raw.get("type", "endpoint")).strip().lower()
            if kind not in ("endpoint", "switch"):
                raise ConfigError(f"Pool {name!r}: identity.type must be endpoint or switch")
            kind_defaults = copy.deepcopy(STANDARD_IDENTITY)
            if kind == "switch":
                kind_defaults.update({
                    "type": "switch",
                    "sys_descr": "Generic managed Ethernet switch",
                    "sys_services": 2,
                })
            values = _deep_merge(kind_defaults, id_raw)
            id_name = _profile_key(name, "identity")
            snmp_identity_profiles[id_name] = SnmpIdentityProfile(
                id_name,
                str(values.get("sys_descr", kind_defaults["sys_descr"])),
                str(values.get("sys_object_id", kind_defaults["sys_object_id"])),
                str(values.get("sys_contact", kind_defaults["sys_contact"])),
                str(values.get("sys_location", kind_defaults["sys_location"])),
                int(values.get("sys_services", kind_defaults["sys_services"])),
            )

        pools.append(PoolConfig(
            name=name,
            count=count,
            resolved_count=resolved_count,
            downtime_profile=dt_name,
            traffic_profile=tr_name,
            initial_uptime_profile=up_name,
            snmp_identity_profile=id_name,
        ))

    # --- advanced per-ID overrides ---------------------------------------
    overrides: list[OverridesEntry] = []
    overrides_raw = merged.get("overrides", []) or []
    if not isinstance(overrides_raw, list):
        raise ConfigError("overrides must be a list")
    for ov in overrides_raw:
        if not isinstance(ov, dict):
            raise ConfigError("override entries must be mappings")
        sel = ov.get("selector", {})
        ids = sel.get("ids", []) if isinstance(sel, dict) else []
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            raise ConfigError(f"Invalid override selector ids: {ids!r}")
        iu = ov.get("initial_uptime", {})
        if not isinstance(iu, dict):
            raise ConfigError("override initial_uptime must be a mapping")
        if "mode" in iu and iu["mode"] not in ("fixed", "random"):
            raise ConfigError(
                f"Invalid override initial_uptime.mode {iu['mode']!r} "
                f"(valid modes: fixed, random)"
            )
        overrides.append(OverridesEntry(
            selector_ids=ids,
            initial_uptime_mode=str(iu.get("mode")) if "mode" in iu else None,
            initial_uptime_days=int(iu.get("days")) if "days" in iu else None,
        ))

    return FullConfig(
        simulation=simulation,
        network=network,
        pools=pools,
        downtime_profiles=downtime_profiles,
        traffic_profiles=traffic_profiles,
        initial_uptime_profiles=initial_uptime_profiles,
        snmp_identity_profiles=snmp_identity_profiles,
        defaults=defaults,
        overrides=overrides,
    )


def compute_config_fingerprint(config: FullConfig) -> str:
    raw = _config_to_dict(config)
    normalized = yaml.dump(raw, sort_keys=True, default_flow_style=False)
    return hashlib.sha256(normalized.encode()).hexdigest()


def _config_to_dict(config: FullConfig) -> dict:
    def convert(obj):
        if hasattr(obj, "__dataclass_fields__"):
            return {f.name: convert(getattr(obj, f.name)) for f in obj.__dataclass_fields__.values()}
        if isinstance(obj, list):
            return [convert(v) for v in obj]
        if isinstance(obj, dict):
            return {k: convert(v) for k, v in obj.items()}
        return obj
    return convert(config)
