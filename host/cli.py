#!/usr/bin/env python3
"""SNMPSwarm command-line interface."""

import argparse
import copy
import json
import os
import sys

import yaml

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from host.log import setup_logger
from src.config import BUILTIN_RAW_CONFIG, ConfigError, load_config, validate_config
from src.ip import calculate_ip

DEFAULT_CONFIG = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config.yaml"))


def _read_yaml(path):
    if not os.path.isfile(path):
        raise ConfigError(f"Configuration file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Could not read configuration file {path}: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("Configuration must be a YAML mapping.")
    return raw


def _set_path(raw, path, value):
    parts = [part for part in path.split(".") if part]
    if not parts:
        raise ConfigError("A dotted path cannot be empty")
    node = raw
    for part in parts[:-1]:
        child = node.get(part)
        if child is None:
            child = {}
            node[part] = child
        if not isinstance(child, dict):
            raise ConfigError(f"Cannot descend into {part!r} while setting {path!r}")
        node = child
    node[parts[-1]] = value


def _parse_set(expr):
    if "=" not in expr:
        raise ConfigError(f"Invalid value {expr!r}; expected path=value")
    path, text = expr.split("=", 1)
    path = path.strip()
    if not path:
        raise ConfigError("Path before '=' cannot be empty")
    try:
        value = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML value in {expr!r}: {exc}") from exc
    return path, value


def _parse_min_max(text):
    try:
        left, right = text.split(":", 1)
        lo, hi = float(left), float(right)
    except (ValueError, TypeError) as exc:
        raise argparse.ArgumentTypeError("expected MIN:MAX") from exc
    if lo < 0 or hi < 0 or lo > hi:
        raise argparse.ArgumentTypeError("range must satisfy 0 <= MIN <= MAX")
    return {"min": lo, "max": hi}


def _parse_initial_uptime(text):
    if ":" in text:
        values = _parse_min_max(text)
        return {"days": {"min": int(values["min"]), "max": int(values["max"])}}
    try:
        days = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected DAYS or MIN:MAX") from exc
    if days < 0:
        raise argparse.ArgumentTypeError("initial uptime must be >= 0 days")
    return {"days": days}


def _parse_traffic_period(text):
    # Example: 09:00-17:30=30:80
    if "=" not in text:
        raise argparse.ArgumentTypeError("expected TIME-TIME=MIN:MAX")
    window, rates = text.split("=", 1)
    if "-" not in window:
        raise argparse.ArgumentTypeError("expected TIME-TIME=MIN:MAX")
    parsed_rates = _parse_min_max(rates)
    return {"time": window, "rate_mbps": parsed_rates}


def _current_cli_pool(parser, namespace):
    pools = getattr(namespace, "cli_pools", None)
    if not pools:
        parser.error("pool-specific options must follow --pool NAME")
    return pools[-1]


class PoolStartAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        pools = getattr(namespace, "cli_pools", None)
        if pools is None:
            pools = []
            setattr(namespace, "cli_pools", pools)
        pools.append({"name": values})


class PoolFieldAction(argparse.Action):
    def __init__(self, option_strings, dest, pool_path=None, **kwargs):
        self.pool_path = tuple(pool_path or ())
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        pool = _current_cli_pool(parser, namespace)
        node = pool
        for part in self.pool_path[:-1]:
            node = node.setdefault(part, {})
        node[self.pool_path[-1]] = values


class PoolAppendAction(argparse.Action):
    def __init__(self, option_strings, dest, pool_path=None, **kwargs):
        self.pool_path = tuple(pool_path or ())
        super().__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        pool = _current_cli_pool(parser, namespace)
        node = pool
        for part in self.pool_path[:-1]:
            node = node.setdefault(part, {})
        node.setdefault(self.pool_path[-1], []).append(values)


class PoolSetAction(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        pool = _current_cli_pool(parser, namespace)
        try:
            path, value = _parse_set(values)
            _set_path(pool, path, value)
        except ConfigError as exc:
            parser.error(str(exc))


def _load_start_config(args):
    if args.no_config:
        raw = {}
        source = "built-in defaults (--no-config)"
    elif args.config:
        path = os.path.abspath(os.path.expanduser(args.config))
        raw = _read_yaml(path)
        source = path
    elif os.path.isfile(DEFAULT_CONFIG):
        raw = _read_yaml(DEFAULT_CONFIG)
        source = DEFAULT_CONFIG
    else:
        raw = {}
        source = "built-in defaults (config.yaml not found)"

    sim = raw.setdefault("simulation", {})
    shortcuts = {
        "name": args.name,
        "total_units": args.count,
        "random_seed": args.seed,
        "timezone": args.timezone,
        "state_directory": args.state_directory,
        "startup_parallelism": args.startup_parallelism,
    }
    for key, value in shortcuts.items():
        if value is not None:
            sim[key] = value
    # Avoid disagreement with a transitional legacy alias if --count was used.
    if args.count is not None:
        sim.pop("device_count", None)

    cli_pools = copy.deepcopy(getattr(args, "cli_pools", None) or [])
    if args.ignore_pools:
        if not cli_pools:
            raise ConfigError("--ignore-pools requires at least one --pool NAME")
        raw["pools"] = []

    if cli_pools:
        pools = raw.setdefault("pools", [])
        if not isinstance(pools, list):
            raise ConfigError("pools in the configuration must be a list")
        pools.extend(cli_pools)

    for expr in args.set_values or []:
        path, value = _parse_set(expr)
        _set_path(raw, path, value)

    return validate_config(raw), source


def _load_normal_config(args):
    explicit = getattr(args, "config", None)
    if explicit:
        path = os.path.abspath(os.path.expanduser(explicit))
        return load_config(path), path
    if os.path.isfile(DEFAULT_CONFIG):
        return load_config(DEFAULT_CONFIG), DEFAULT_CONFIG
    return validate_config({}), "built-in defaults (config.yaml not found)"

def cmd_start(args):
    setup_logger(level=args.verbose and "DEBUG" or "INFO")
    from host.log import get_logger
    log = get_logger("cli")

    try:
        config, config_source = _load_start_config(args)
    except ConfigError as e:
        log.error("Configuration error: %s", e)
        sys.exit(1)

    log.info("Starting swarm '%s' with %d devices...", config.simulation.name, config.simulation.device_count)
    log.info("Configuration base: %s", config_source)

    # Validate/resolve CLI configuration before requiring Docker. This keeps
    # argument errors useful even on a host where Docker is not installed yet.
    try:
        from host.swarm import check_docker, start_swarm
    except ModuleNotFoundError as exc:
        if exc.name == "docker":
            print(
                "Error: Python Docker SDK is not installed. Run: "
                ".venv/bin/pip install -r requirements.txt",
                file=sys.stderr,
            )
            sys.exit(1)
        raise
    if not check_docker():
        print("Error: Docker is not available or the daemon is not running.", file=sys.stderr)
        sys.exit(1)

    manifest_path = os.path.join(config.simulation.state_directory, "resolved-devices.yaml")
    result = start_swarm(config, manifest_path)

    print(f"Swarm '{config.simulation.name}':")
    print(f"  Requested:   {result['requested']}")
    print(f"  Started:     {result['started']}")
    print(f"  Verified:    {result['verified']}")
    print(f"  Failed:      {result['failed']}")

    if result.get("rejected"):
        print(f"\nStart rejected: {result.get('error', 'configuration/resource mismatch')}", file=sys.stderr)
        sys.exit(2)

    if result["failed"] > 0:
        print("\nFailed devices:")
        for d in result.get("device_results", []):
            if d.get("startup_status") == "failed":
                print(f"  Device {d['id']:3d} ({d['container_name']}): {d.get('error', 'unknown')}")

    if result["started"] > 0:
        print(f"\nManifest: {manifest_path}")


def cmd_stop(args):
    setup_logger(level=args.verbose and "DEBUG" or "INFO")
    from host.log import get_logger
    log = get_logger("cli")
    try:
        config, _ = _load_normal_config(args)
    except ConfigError as e:
        log.error("Configuration error: %s", e)
        sys.exit(1)
    from host.swarm import stop_swarm
    result = stop_swarm(config)
    print(f"Swarm '{config.simulation.name}' stopped:")
    print(f"  Containers removed: {result['containers_removed']}")
    print(f"  Network removed:    {'yes' if result['network_removed'] else 'no'}")


def _find_device_container(config, device_id, log):
    """Validate that a managed container exists for the device.

    Uses the same single-call SDK discovery as the rest of the host code;
    a Docker failure is reported as such, not as "device not found" (SW-04).
    """
    from host.swarm import DockerError, _list_swarm_containers
    from src.ip import container_name as make_container_name
    container_name = make_container_name(config.simulation.name, device_id)
    try:
        records = _list_swarm_containers(config)
    except DockerError as exc:
        log.error("Docker state unavailable for device %d: %s", device_id, exc)
        print(f"Error: Docker state unavailable for device {device_id}", file=sys.stderr)
        sys.exit(1)
    if not any(r["name"] == container_name for r in records):
        log.error("Container not found for device %d", device_id)
        print(f"Error: Device {device_id} not found", file=sys.stderr)
        sys.exit(1)
    return container_name


def _power_http(ip: str, action: str, port: int = None, timeout: float = 5.0) -> None:
    """Send a power command to the device's HTTP control API. Raises on failure.

    The control port is the swarm-wide CONTROL_PORT constant (8080); the
    ``port`` argument exists for tests.
    """
    import urllib.request

    from host.swarm import CONTROL_PORT
    if port is None:
        port = CONTROL_PORT
    url = f"http://{ip}:{port}/power/{action}"
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


def cmd_power_off(args):
    setup_logger(level=args.verbose and "DEBUG" or "INFO")
    from host.log import get_logger
    log = get_logger("cli")
    try:
        config, _ = _load_normal_config(args)
    except ConfigError as e:
        log.error("Configuration error: %s", e)
        sys.exit(1)

    device_id = args.device_id
    _find_device_container(config, device_id, log)
    try:
        _power_http(calculate_ip(device_id), "off")
    except Exception as exc:
        log.error("Failed to power off device %d: %s", device_id, exc)
        print(f"Error: Failed to power off device {device_id}", file=sys.stderr)
        sys.exit(1)

    print(f"Device {device_id} powered off")


def cmd_power_on(args):
    setup_logger(level=args.verbose and "DEBUG" or "INFO")
    from host.log import get_logger
    log = get_logger("cli")
    try:
        config, _ = _load_normal_config(args)
    except ConfigError as e:
        log.error("Configuration error: %s", e)
        sys.exit(1)

    device_id = args.device_id
    _find_device_container(config, device_id, log)
    try:
        _power_http(calculate_ip(device_id), "on")
    except Exception as exc:
        log.error("Failed to power on device %d: %s", device_id, exc)
        print(f"Error: Failed to power on device {device_id}", file=sys.stderr)
        sys.exit(1)

    print(f"Device {device_id} powered on")


def cmd_status(args):
    setup_logger(level=args.verbose and "DEBUG" or "INFO")
    from host.log import get_logger
    log = get_logger("cli")
    try:
        config, _ = _load_normal_config(args)
    except ConfigError as e:
        log.error("Configuration error: %s", e)
        sys.exit(1)

    from host.swarm import status_swarm
    result = status_swarm(config)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"Swarm: {result['swarm_name']}")
        print(f"  Requested devices:       {result['requested']}")
        print(f"  Containers running:      {result['containers_running']}")
        print(f"  Emulated devices online: {result['devices_online']}")
        print(f"  Emulated devices offline:{result['devices_offline']:2d}")
        print(f"  Failed containers:       {result['failed_containers']}")
        if result.get("containers"):
            print("\nContainers:")
            for c in result["containers"]:
                power = c.get("power_state", "unknown")
                ping = c.get("ping", "unknown")
                pool_name = c.get("pool") or "unknown"
                print(
                    f"  {c['name']:30s} {c['state']}  power={power}  "
                    f"ping={ping}  pool={pool_name}"
                )


def _add_common_config_arg(parser):
    parser.add_argument("-c", "--config", help="Use this YAML file instead of the default ./config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose/debug logging")


def main():
    formatter = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(
        prog="snmpswarm",
        description="SNMPSwarm - simulated SNMP network devices in Docker",
        formatter_class=formatter,
        add_help=True,
    )
    parser.add_argument("--version", action="version", version="SNMPSwarm 1.00")
    _add_common_config_arg(parser)
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    start_help = """\
Start the simulated swarm.

MENTAL MODEL
------------
SNMPSwarm starts with a total number of units. Configured pools consume units
from that total. Pools are exclusive: a unit can belong to only one configured
pool. Anything left over becomes a "standard unit" automatically.

Built-in standard unit:
  identity       endpoint (Generic monitored endpoint)
  link speed     100 Mbit/s
  downtime       none; always online
  traffic        0.00-0.05 Mbit/s, 1 interface, 5% variation
  initial uptime 0 days

A config file does NOT need to repeat those defaults. A pool only specifies the
ways in which its units differ from a standard unit.

Pool size:
  --pool-count N      exact number of units

Example with 200 total units:
  unstable       count 43       -> consumes 43
  mostly-stable  count 50       -> consumes 50
  standard unit                 -> remaining 107

Pool counts are always absolute. The sum of all pool counts may not exceed the
total number of units. Any units left over become "standard unit".

CONFIGURATION
-------------
./config.yaml is loaded automatically when present. --config FILE selects a
different file. --no-config ignores config.yaml completely. Missing settings
always fall back to SNMPSwarm's built-in defaults.

Configuration precedence:
  1. SNMPSwarm built-in defaults
  2. config.yaml (or --config FILE)
  3. CLI options and CLI pools
  4. --set / --pool-set overrides

CLI POOLS
---------
Start a pool with --pool NAME. Pool-specific options that follow belong to the
most recently declared pool, until another --pool NAME is encountered.

Examples:
  ./start-swarm --count 200 \\
    --pool unstable --pool-count 43 \\
      --downtime-events 5 --downtime-duration 2:20 --minimum-uptime 15 \\
    --pool mostly-stable --pool-count 50 \\
      --downtime-events 1 --downtime-duration 1:5

  ./start-swarm --ignore-pools \\
    --pool rocksolid --pool-count 50 \\
    --pool busy-switches --pool-count 30 \\
      --identity switch --link-speed 1000 --traffic-rate 60:90

--ignore-pools discards pools inherited from config.yaml, but keeps simulation,
network and other non-pool settings. It requires at least one new --pool.

Traffic periods can be added to the current pool:
  --traffic-period '09:00-17:30=30:80'
  --traffic-period '23:00-07:00=0.1:2'

Initial uptime:
  --initial-uptime 700       fixed 700 days
  --initial-uptime 30:500    random 30..500 days

For uncommon pool fields use --pool-set PATH=VALUE. For global/config values use
--set PATH=VALUE. Values are parsed as YAML.

Examples:
  ./start-swarm
      Use config.yaml if present; otherwise start built-in standard units.

  ./start-swarm --no-config --count 25
      Start 25 standard units without reading config.yaml.

  ./start-swarm --config configs/lab.yaml
      Use an alternative config file.

  ./start-swarm --set network.community=my-lab
      Change a non-pool setting without editing YAML.
"""

    start_parser = subparsers.add_parser(
        "start-swarm",
        help="Start the simulated network swarm",
        description=start_help,
        formatter_class=formatter,
    )
    # Also accept the user's natural '-help' spelling in addition to -h/--help.
    start_parser.add_argument("-help", action="help", help=argparse.SUPPRESS)
    start_parser.add_argument("--version", action="version", version="SNMPSwarm 1.00")

    start_cfg = start_parser.add_mutually_exclusive_group()
    start_cfg.add_argument("-c", "--config", help="Use FILE instead of the default ./config.yaml", metavar="FILE")
    start_cfg.add_argument("--no-config", action="store_true", help="Ignore config.yaml and use built-in defaults plus CLI options")
    start_parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose/debug logging")

    sim_group = start_parser.add_argument_group("simulation")
    sim_group.add_argument("--name", help="Simulation name")
    sim_group.add_argument("--count", type=int, metavar="N", help="Total number of units")
    sim_group.add_argument("--seed", type=int, help="Random seed for deterministic pool assignment")
    sim_group.add_argument("--timezone", help="Timezone, e.g. Europe/Copenhagen")
    sim_group.add_argument("--state-directory", metavar="PATH", help="Directory for generated swarm state")
    sim_group.add_argument("--startup-parallelism", type=int, metavar="N", help="Maximum parallel container startups")

    pool_group = start_parser.add_argument_group(
        "pools",
        "Pools are exclusive. Start each pool with --pool NAME; following pool options apply to that pool.",
    )
    pool_group.add_argument("--ignore-pools", action="store_true", help="Ignore pools from config.yaml; requires at least one CLI --pool")
    pool_group.add_argument("--pool", metavar="NAME", action=PoolStartAction, help="Start a new named pool; repeatable")
    pool_group.add_argument("--pool-count", metavar="N", type=int, action=PoolFieldAction, pool_path=("count",), help="Exact size of current pool")

    downtime_group = start_parser.add_argument_group("current pool: downtime")
    downtime_group.add_argument("--downtime-events", metavar="N", type=float, action=PoolFieldAction, pool_path=("downtime", "events_per_day"), help="Expected outages per day; 0 means always online")
    downtime_group.add_argument("--downtime-duration", metavar="MIN:MAX", type=_parse_min_max, action=PoolFieldAction, pool_path=("downtime", "duration_minutes"), help="Outage duration range in minutes")
    downtime_group.add_argument("--minimum-uptime", metavar="MINUTES", type=float, action=PoolFieldAction, pool_path=("downtime", "minimum_uptime_minutes"), help="Minimum online time between outages")

    traffic_group = start_parser.add_argument_group("current pool: traffic")
    traffic_group.add_argument("--link-speed", metavar="MBPS", type=int, action=PoolFieldAction, pool_path=("traffic", "link_speed_mbps"), help="Interface link speed")
    traffic_group.add_argument("--traffic-rate", metavar="MIN:MAX", type=_parse_min_max, action=PoolFieldAction, pool_path=("traffic", "rate_mbps"), help="Default traffic rate range in Mbit/s")
    traffic_group.add_argument("--traffic-variation", metavar="PERCENT", type=float, action=PoolFieldAction, pool_path=("traffic", "variation_percent"), help="Traffic variation percentage")
    traffic_group.add_argument("--interfaces", metavar="N", type=int, action=PoolFieldAction, pool_path=("traffic", "interfaces"), help="Number of interfaces")
    traffic_group.add_argument("--traffic-period", metavar="TIME-TIME=MIN:MAX", type=_parse_traffic_period, action=PoolAppendAction, pool_path=("traffic", "periods"), help="Time-specific traffic range; repeatable")

    uptime_group = start_parser.add_argument_group("current pool: initial uptime")
    uptime_group.add_argument("--initial-uptime", metavar="DAYS|MIN:MAX", type=_parse_initial_uptime, action=PoolFieldAction, pool_path=("initial_uptime",), help="Fixed or random initial uptime in days")

    identity_group = start_parser.add_argument_group("current pool: identity")
    identity_group.add_argument("--identity", choices=("endpoint", "switch"), action=PoolFieldAction, pool_path=("identity", "type"), help="SNMP identity type")
    identity_group.add_argument("--sys-descr", action=PoolFieldAction, pool_path=("identity", "sys_descr"), help="SNMP sysDescr override")
    identity_group.add_argument("--sys-contact", action=PoolFieldAction, pool_path=("identity", "sys_contact"), help="SNMP sysContact override")
    identity_group.add_argument("--sys-location", action=PoolFieldAction, pool_path=("identity", "sys_location"), help="SNMP sysLocation override")
    identity_group.add_argument("--sys-services", type=int, action=PoolFieldAction, pool_path=("identity", "sys_services"), help="SNMP sysServices override")

    advanced_group = start_parser.add_argument_group("advanced overrides")
    advanced_group.add_argument("--pool-set", metavar="PATH=VALUE", action=PoolSetAction, help="Set an arbitrary field on the current pool; repeatable")
    advanced_group.add_argument("--set", dest="set_values", action="append", metavar="PATH=VALUE", help="Override an arbitrary non-pool config path; repeatable")
    start_parser.set_defaults(func=cmd_start, cli_pools=None, set_values=None)

    stop_parser = subparsers.add_parser("stop-swarm", help="Stop and remove swarm resources")
    stop_parser.set_defaults(func=cmd_stop)

    status_parser = subparsers.add_parser("status-swarm", help="Show swarm status")
    status_parser.add_argument("--json", action="store_true", help="Output in JSON format")
    status_parser.set_defaults(func=cmd_status)

    power_off_parser = subparsers.add_parser("power-off-device", help="Emulate powering off a device")
    power_off_parser.add_argument("-c", "--config", metavar="FILE", help="Use FILE instead of the default ./config.yaml")
    power_off_parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose/debug logging")
    power_off_parser.add_argument("device_id", type=int, help="Device ID to power off")
    power_off_parser.set_defaults(func=cmd_power_off)

    power_on_parser = subparsers.add_parser("power-on-device", help="Emulate powering on a device")
    power_on_parser.add_argument("-c", "--config", metavar="FILE", help="Use FILE instead of the default ./config.yaml")
    power_on_parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose/debug logging")
    power_on_parser.add_argument("device_id", type=int, help="Device ID to power on")
    power_on_parser.set_defaults(func=cmd_power_on)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
