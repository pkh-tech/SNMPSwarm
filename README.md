# SNMPSwarm

**A configurable swarm of simulated SNMP devices for testing network monitoring systems.**

SNMPSwarm starts hundreds of individually addressable SNMPv2c devices in Docker containers. Configure uptime, traffic counters and scheduled outages, then test how your monitoring software responds when devices appear, disappear, or come back online.

- **Deterministic addresses:** Device IDs map consistently to `10.0.x.y` addresses.
- **Configurable simulations:** Define device pools with traffic, uptime and outage behavior using YAML or CLI options.
- **Per-device power control:** Switch a simulated device offline/online via HTTP without stopping its container.
- **Observable behavior:** Ping and SNMP stop responding when a device is simulated offline, while its management HTTP endpoint stays reachable.
- **Scale tested:** A 400-device run verified SNMP and HTTP status on all devices, with additional sampled power-control and concurrency tests. See [test results](#resource-usage).

Designed for local Linux/Docker labs and development environments, **not production network emulation or exposure to public networks**.

> **Lab use:** SNMPv2c does not provide encryption or strong authentication. Do not expose the simulated network directly to untrusted networks.

## Requirements

- Linux host with Docker installed and available to the current user.
- Python 3.10 or newer.
- `ping` available on the host.
- Optional Net-SNMP command-line tools (`snmpget`, `snmpwalk`) for `tools/verify-swarm` and `tools/snmp-dump`.

## Quick start

After cloning or downloading the repository:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

./start-swarm
```

`./start-swarm` automatically reads `./config.yaml` when it exists. Without one, SNMPSwarm uses its built-in defaults.

To start from the commented example configuration:

```bash
cp config.example config.yaml
./start-swarm
```

For the complete CLI reference and examples:

```bash
./start-swarm --help
```

`./start-swarm -help` is also accepted.

## Configuration model

The model is deliberately simple:

1. `simulation.total_units` is the total number of simulated units.
2. Named `pools` take an exact `count` of units from that total.
3. Pools are mutually exclusive: one unit can belong to at most one configured pool.
4. Pool counts are absolute; percent-based allocation is not used.
5. The sum of all pool counts may not exceed `total_units`.
6. Units not consumed by a pool automatically become **`standard unit`**.

A standard unit is built into SNMPSwarm and does not need to be described in YAML:

```text
identity       endpoint (Generic monitored endpoint)
link speed     100 Mbit/s
downtime       none; always online
traffic        0.00-0.05 Mbit/s, 1 interface, 5% variation
initial uptime 0 days
```

A pool only describes how its units differ from that standard.

### Minimal config

```yaml
simulation:
  total_units: 200
```

This produces 200 standard units.

### Pools

```yaml
simulation:
  total_units: 200

pools:
  - name: unstable
    count: 43
    downtime:
      events_per_day: 5
      duration_minutes:
        min: 2
        max: 20
      minimum_uptime_minutes: 15

  - name: mostly_stable
    count: 50
    downtime:
      events_per_day: 1
      duration_minutes:
        min: 1
        max: 5
```

This allocates 43 units to `unstable`, 50 to `mostly_stable`, and leaves 107 as `standard unit`.

Pools are allocated without overlap. Pool assignment is deterministic for the same configuration and random seed. If the configured pool counts exceed `total_units`, startup is rejected before Docker containers are started.

### Direct pool settings

There are no user-facing profile definitions to maintain. A pool sets its properties directly.

```yaml
pools:
  - name: busy_switches
    count: 20

    identity:
      type: switch

    traffic:
      interfaces: 1
      link_speed_mbps: 1000
      rate_mbps:
        min: 2
        max: 10
      variation_percent: 15
      periods:
        - time: "09:00-17:30"
          rate_mbps:
            min: 30
            max: 80

    initial_uptime:
      days:
        min: 100
        max: 700
```

Omitted settings use the built-in standard-unit behaviour. For example, because this pool has no `downtime` section, its units remain always online.

`initial_uptime.days` can be fixed:

```yaml
initial_uptime:
  days: 700
```

or randomized within a range:

```yaml
initial_uptime:
  days:
    min: 30
    max: 500
```

See [`config.example`](config.example) for a fully commented example.

## CLI pools

Pools can be created without editing YAML. Start each pool with `--pool NAME`; pool-specific options that follow apply to that pool until the next `--pool`.

```bash
./start-swarm --no-config --count 100 \
  --pool unstable --pool-count 20 \
    --downtime-events 5 \
    --downtime-duration 2:20 \
    --minimum-uptime 15 \
  --pool busy-switches --pool-count 30 \
    --identity switch \
    --link-speed 1000 \
    --traffic-rate 60:90
```

`--ignore-pools` discards pools from `config.yaml`, keeps the rest of the config, and requires at least one new CLI pool:

```bash
./start-swarm --ignore-pools \
  --pool unstable --pool-count 10 \
    --downtime-events 5 --downtime-duration 2:20
```

For less common pool values, use `--pool-set PATH=VALUE`. For global configuration values, use `--set PATH=VALUE`.

## Config selection

Normal use needs no config argument:

```bash
./start-swarm
```

If `config.yaml` exists in the project directory, it is loaded automatically. Use another file only when needed:

```bash
./start-swarm --config configs/lab.yaml
```

Ignore `config.yaml` completely for a run:

```bash
./start-swarm --no-config --count 50
```

CLI values override values loaded from config.

## Device addressing

Device IDs map to fixed addresses using `10.0.x.y`, where `y` is `0..99`:

```text
23  -> 10.0.0.23
99  -> 10.0.0.99
100 -> 10.0.1.0
167 -> 10.0.1.67
200 -> 10.0.2.0
```

## SNMP behaviour

Each device exposes standard system/interface information used by SNMP monitoring applications, including growing 64-bit `ifHCInOctets` and `ifHCOutOctets` counters.

An emulated offline device keeps its container running so its internal scheduler can bring it online again. While offline, connectivity is blocked, but the device's control API (below) stays reachable so the device can be powered back on. When the emulated device comes back online, its uptime is reset as a real reboot/power cycle would be represented to the monitoring client.

## Power control

Each device runs a small HTTP control API on port **8080** inside its container. It is restricted to loopback and to peers in the management network; requests from anywhere else are rejected.

```bash
# Query a device (10.0.0.47 is device 47)
curl http://10.0.0.47:8080/status
# -> {"device_id": 47, "name": "device-047", "ip": "10.0.0.47", "power": "online"}

# Emulate power off / on
curl -X POST http://10.0.0.47:8080/power/off
curl -X POST http://10.0.0.47:8080/power/on
```

While powered off, normal SNMP responses are suppressed, but `/status` and `/power/on` keep working. Repeated commands are idempotent; power-on resets uptime and counters once, exactly as a power cycle would.

Power off is implemented with iptables rules **inside the device's own
network namespace**: dedicated `SNMPSWARM_IN`/`SNMPSWARM_OUT` chains drop
ping (ICMP) and SNMP (UDP 161) traffic while the device is offline, and the
control port 8080 stays reachable. Power on removes exactly those rules.
The rules never touch the host's iptables tables or any unrelated container:
each device's firewall state is confined to its own netns, and cleanup runs
automatically when the agent terminates (SIGTERM, fatal error, or container
removal).

From the host, the same operations are available as CLI commands (which verify the device's container exists first, then call the device's control API):

```bash
./power-off 47
./power-on 47
```

Both wrappers accept `--config FILE` and `-v`, e.g. `./power-off --config config.yaml 47`. Without a config file they use `./config.yaml` when present, else the built-in defaults.

Manual power state composes with scheduled outages: a manual power-off persists until a manual power-on, even across scheduled outages; a manual power-on during a scheduled outage takes effect when the outage ends.

## Reliability

SNMPSwarm treats its three mandatory components — the HTTP control API, the
SNMP responder, and the firewall configuration — as fail-fast:

- **Startup.** If any mandatory component fails to start (control port
  unbindable, SNMP socket failure, firewall preflight failure), the agent
  logs the specific error and exits non-zero. A container whose mandatory
  components are down is never reported as healthy; there is no silent
  partial operation.
- **After startup.** The agent's main loop watches its worker threads. A
  thread that dies or wedges (stops responding to its heartbeat) causes the
  agent to log a fatal error and exit non-zero. Containers use the
  `unless-stopped` restart policy, so Docker replaces a dead agent with a
  fresh one; the replacement performs a full preflight and re-establishes a
  consistent state.
- **Emulated offline is not a component failure.** A deliberately powered
  off device keeps its process running and its control API reachable; only
  ping and SNMP are blocked. Power state is reported live by `/status`.
- **Failed operations are never reported as successful.** A power
  transition that cannot complete (for example, an iptables failure) returns
  HTTP 500 with an `error` field and leaves the device state unchanged.
- **Shutdown.** `SIGTERM`/`SIGINT` stop the control server and SNMP socket
  and exit with code 0, cleaning up the device's firewall rules. A powered
  off device is not a terminated container: `stop-swarm` still removes it,
  and the stop path intentionally uses `docker rm -f` for robustness.

## Useful tools

The `tools/` directory contains optional user-facing checks:

```bash
./tools/verify-swarm
./tools/snmp-dump
./tools/ram-usage
```

- `verify-swarm` checks managed containers, ping and SNMP responses.
- `snmp-dump` runs an SNMP walk against all running units in a swarm.
- `ram-usage` totals Docker memory used by the swarm's running containers.

Each tool supports `--help`.

## Resource usage

A test run with 200 simultaneous Docker devices used approximately **3.7 GiB** of total container memory on the author's test system (about 19 MiB per container on average); a 400-device run used **8473 MiB** total (about 21.2 MiB per container), with the host's used RAM rising by roughly 10.6 GiB (the remainder being Docker daemon, kernel network-namespace state, and page-cache movement). Actual usage varies by host and configuration.

## Commands

```bash
./start-swarm --help
./status-swarm
./stop-swarm
./power-off 47
./power-on 47
```

Version:

```bash
python3 host/cli.py --version
```

## Development

The automated tests are primarily a regression safety net for development and contributions; normal users do not need to run them.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/pytest
```

The suite includes `tests/test_integration_docker.py`, which exercises the
real Docker runtime (swarm start, power off/on via the control API, SNMP
suppression and recovery, firewall rule scoping and cleanup, SIGTERM
shutdown, and post-startup worker-thread death with Docker restart) using a
uniquely named simulation and cleans up after itself. It skips automatically
when Docker is unavailable. Unit tests mock the Docker SDK and never touch a
daemon; they also cover fatal mandatory-component startup failures,
post-startup worker-thread death and wedge detection, and partial firewall
operation failures.

For scale checks on a running swarm, `tools/swarm-stress-test.sh` polls the
devices' ping/SNMP/HTTP endpoints with a configurable worker pool and
reports per-round statistics.

`CHANGES.md` records recent changes, the test commands and results, and
known issues that were deliberately left unchanged.

GitHub Actions runs the same test suite automatically on pushes and pull requests.

