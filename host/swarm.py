"""
Docker orchestration for the swarm.

Manages Docker network, container lifecycle, and labels.
Uses the Docker low-level API for optimal high-concurrency container creation.
"""

import concurrent.futures
import hashlib
import json
import logging
import os
import shlex
import subprocess
from subprocess import CalledProcessError
import threading
import time
from typing import Any

import docker
from docker.types import EndpointConfig, Mount

from src.config import ConfigError, FullConfig, compute_config_fingerprint

# Global Docker client (thread-safe) initialized on first use
_docker_client_lock = threading.Lock()
_docker_client = None


def _get_docker_client():
    """Get a Docker SDK client with increased connection pool size.
    
    For high-concurrency scenarios (128+ parallel workers), we replace the
    UnixHTTPAdapter's default max_pool_size=10 with 256 to avoid
    "Connection pool is full, discarding connection" warnings.
    
    Returns a shared client instance (thread-safe for read-only access).
    The Docker SDK's API objects (containers, networks, etc.) are thread-safe
    for basic operations like run(), get(), etc.
    """
    global _docker_client
    if _docker_client is None:
        with _docker_client_lock:
            if _docker_client is None:
                # Initialize Docker SDK client with extended timeout
                client = docker.from_env(timeout=300)
                
                # Replace the UnixHTTPAdapter with one configured for larger connection pools.
                # The default max_pool_size=10 is insufficient for 128 parallel workers.
                from docker.transport.unixconn import UnixHTTPAdapter
                
                # Get the actual socket path from the existing adapter.
                existing_adapter = client.api.adapters.get("http+docker://")
                if existing_adapter is not None:
                    socket_path = existing_adapter.socket_path
                else:
                    socket_path = "/var/run/docker.sock"
                
                new_adapter = UnixHTTPAdapter(
                    socket_url=f"http+unix://{socket_path}",
                    timeout=300,
                    pool_connections=256,
                    max_pool_size=256,
                )
                client.api.adapters["http+docker://"] = new_adapter
                
                _docker_client = client
                logger.debug("Docker SDK client initialized (timeout=300s, pool_size=256)")
    
    return _docker_client


def _get_docker_api():
    """Get the Docker low-level API client with increased connection pool size.
    
    For high-concurrency scenarios (128+ parallel workers), we replace the
    UnixHTTPAdapter's default max_pool_size=10 with 256 to avoid
    "Connection pool is full, discarding connection" warnings.
    
    Returns the low-level API client for create_container + start operations.
    The low-level API enables combining container creation with network
    attachment in a single operation via EndpointConfig/IPAMConfig.
    """
    client = _get_docker_client()
    return client.api


def _create_fresh_docker_client():
    """Create a fresh Docker SDK client with increased connection pool size.
    
    Used by worker threads to avoid potential contention on the shared client.
    Each worker gets its own client instance with proper pool sizing.
    """
    # Initialize Docker SDK client with extended timeout
    client = docker.from_env(timeout=300)
    
    # Replace the UnixHTTPAdapter with one configured for larger connection pools.
    from docker.transport.unixconn import UnixHTTPAdapter
    
    # Get the actual socket path from the existing adapter.
    existing_adapter = client.api.adapters.get("http+docker://")
    if existing_adapter is not None:
        socket_path = existing_adapter.socket_path
    else:
        socket_path = "/var/run/docker.sock"
    
    new_adapter = UnixHTTPAdapter(
        socket_url=f"http+unix://{socket_path}",
        timeout=300,
        pool_connections=128,
        max_pool_size=128,
    )
    client.api.adapters["http+docker://"] = new_adapter
    
    return client


def _remove_container_if_exists(client, container_name: str) -> None:
    """Remove a container if it already exists (for retry cleanup)."""
    try:
        existing = client.containers.get(container_name)
        if existing:
            logger.debug("Removing existing container %s for retry", container_name)
            existing.remove(force=True)
    except docker.errors.NotFound:
        pass  # Container doesn't exist, which is fine
    except docker.errors.APIError as e:
        logger.warning("Failed to remove container %s: %s", container_name, e)
from src.resolver import run_resolution, load_manifest, write_manifest_atomically

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Timing instrumentation
# ---------------------------------------------------------------------------

class PhaseTimer:
    """Accumulates wall-clock timing for named phases of swarm lifecycle."""

    def __init__(self):
        self._phases: dict[str, float] = {}  # phase_name -> cumulative seconds
        self._start_times: dict[str, float] = {}
        self._sequence: list[str] = []

    def start(self, phase: str) -> None:
        self._start_times[phase] = time.monotonic()

    def stop(self, phase: str) -> None:
        st = self._start_times.pop(phase, None)
        if st is not None:
            elapsed = time.monotonic() - st
            self._phases[phase] = self._phases.get(phase, 0.0) + elapsed
            if phase not in self._sequence:
                self._sequence.append(phase)

    def report(self) -> dict[str, float]:
        """Return {phase: cumulative_seconds} sorted by execution order."""
        ordered = {}
        for p in self._sequence:
            if p in self._phases:
                ordered[p] = self._phases[p]
        return ordered

    def total(self) -> float:
        return sum(self._phases.values())

# Docker labels
LABEL_MANAGED = "network-swarm.managed"
LABEL_NAME = "network-swarm.name"
LABEL_DEVICE_ID = "network-swarm.device-id"
LABEL_FINGERPRINT = "network-swarm.fingerprint"


# Default timeout for bounded Docker/subprocess commands (seconds).
# Used as a safety net when callers don't supply their own timeout.
_DOCKER_CMD_TIMEOUT = 30

# Default overall timeout for swarm lifecycle operations (seconds).
# Used as a safety net when the caller doesn't supply one via
# ``config.simulation._startup_timeout`` or ``_shutdown_timeout``.
_STARTUP_OVERALL_TIMEOUT = 300
_SHUTDOWN_OVERALL_TIMEOUT = 300


def _run(cmd: list[str], check: bool = True, timeout: int | None = None) -> subprocess.CompletedProcess:
    """Run a subprocess command and return the result.

    All subprocess calls should go through this function.  A bounded
    *timeout* (seconds) is applied so that no single command blocks
    indefinitely.  When *timeout* is omitted the module-level default
    ``_DOCKER_CMD_TIMEOUT`` is used (currently 30 s).  Set it explicitly
    to ``None`` to disable the timeout (not recommended).
    """
    effective_timeout = timeout if timeout is not None else _DOCKER_CMD_TIMEOUT
    logger.debug("Running: %s (timeout=%ss)", " ".join(cmd), effective_timeout)
    return subprocess.run(cmd, capture_output=True, text=True, check=check, timeout=effective_timeout)


def _docker(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a Docker CLI command."""
    return _run(["docker"] + cmd, check=check)


def check_docker() -> bool:
    """Check if Docker is available and daemon is running."""
    try:
        result = _docker(["info"], check=False)
        return result.returncode == 0
    except (FileNotFoundError, OSError):
        return False


def _compute_build_fingerprint(project_dir: str) -> str:
    """Compute a fingerprint of the agent source and Dockerfile for image reuse safety.

    Returns SHA-256 hex digest of all .py files in agent/ + src/ plus Dockerfile.
    Uses the project root as the context to match the Docker build.
    """
    hasher = hashlib.sha256()
    for subdir in ("agent", "src"):
        for root, dirs, files in os.walk(os.path.join(project_dir, subdir)):
            for fname in sorted(files):
                if fname.endswith(".py") or fname == "Dockerfile":
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, "rb") as f:
                            hasher.update(f.read())
                    except OSError:
                        pass
    # Include Dockerfile fingerprint
    dockerfile_path = os.path.join(project_dir, "agent", "Dockerfile")
    try:
        with open(dockerfile_path, "rb") as f:
            hasher.update(f.read())
    except OSError:
        pass
    return hasher.hexdigest()[:16]


def build_or_get_image(config: FullConfig | None = None) -> bool:
    """Build the container image or check if it exists with source fingerprint validation.

    If the source hasn't changed since last build, reuse existing image.
    A fingerprint is stored in the manifest for comparison.
    
    Uses project root as build context so Dockerfile can COPY both src/ and agent/.
    
    The stored image fingerprint is compared with the current source fingerprint
    so changed runtime code triggers a rebuild.
    """
    project_dir = os.path.join(os.path.dirname(__file__), "..")
    current_fp = _compute_build_fingerprint(project_dir)

    # Check if we already have a matching image with the same fingerprint
    try:
        result = _docker(["image", "inspect", "network-swarm-agent:latest"], check=False)
        if result.returncode == 0:
            # Extract the fingerprint from image label
            fp_result = _docker([
                "image", "inspect", "network-swarm-agent:latest",
                "--format", "{{index .Config.Labels \"network-swarm.build-fingerprint\"}}"
            ], check=False)
            stored_fp = fp_result.stdout.strip()
            if stored_fp == current_fp:
                logger.info("Using existing image: network-swarm-agent:latest (fp=%s, up-to-date)", current_fp)
                return True
            else:
                logger.info(
                    "Image exists but fingerprint differs (stored=%s, current=%s). Rebuilding.",
                    stored_fp, current_fp
                )
    except (FileNotFoundError, OSError):
        pass

    # Build the image (project root as context, agent/Dockerfile as Dockerfile)
    try:
        result = _docker([
            "build", "-t", "network-swarm-agent:latest",
            "-f", os.path.join(project_dir, "agent", "Dockerfile"),
            "--label", f"network-swarm.build-fingerprint={current_fp}",
            project_dir
        ], check=False)
        if result.returncode == 0:
            logger.info("Built image: network-swarm-agent:latest (fingerprint: %s)", current_fp)
            return True
        else:
            logger.error("Failed to build image: %s", result.stderr)
            return False
    except (FileNotFoundError, OSError) as e:
        logger.error("Docker build failed: %s", e)
        return False


def _network_name(config: FullConfig) -> str:
    return f"swarm-{config.simulation.name}-net"


def _inspect_network(net_name: str) -> dict | None:
    """Return Docker network inspect data, or None when the network is absent."""
    result = _run(["docker", "network", "inspect", net_name], check=False)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse docker network inspect for {net_name}: {exc}") from exc
    if not data:
        return None
    return data[0]


def _network_owned_by_swarm(info: dict, simulation_name: str) -> bool:
    labels = info.get("Labels") or {}
    return (
        labels.get(LABEL_MANAGED) == "true"
        and labels.get(LABEL_NAME) == simulation_name
    )


def _network_fingerprint(info: dict) -> str:
    return str((info.get("Labels") or {}).get(LABEL_FINGERPRINT, ""))


def _network_ipam(info: dict) -> tuple[str | None, str | None]:
    configs = ((info.get("IPAM") or {}).get("Config") or [])
    if not configs:
        return None, None
    first = configs[0] or {}
    return first.get("Subnet"), first.get("Gateway")


class DockerError(RuntimeError):
    """Docker state could not be determined (daemon unreachable, API/command failure).

    Callers must treat this as distinct from a healthy daemon that reports
    zero matching containers: destructive operations must not proceed on a
    query that failed (SW-04).
    """


def _list_swarm_containers(config: FullConfig) -> list[dict]:
    """List containers carrying this swarm's exact ownership labels.

    One Docker API call (``containers.list`` with label filters) returns the
    id, name, state, and labels of every matching container, so discovery of
    a 1000-container swarm does not pay N extra ``docker inspect`` calls
    (SW-02/SW-03).

    Returns:
        List of ``{"id", "name", "state", "labels"}`` dicts. An empty list
        means the daemon is healthy and no container matches (SW-04).

    Raises:
        DockerError: when Docker state cannot be determined.
    """
    try:
        client = _get_docker_client()
        containers = client.containers.list(
            all=True,
            filters={
                "label": [
                    f"{LABEL_MANAGED}=true",
                    f"{LABEL_NAME}={config.simulation.name}",
                ],
            },
        )
        return [
            {
                "id": c.id,
                "name": c.name,
                "state": c.status,
                "labels": dict(c.labels or {}),
            }
            for c in containers
        ]
    except Exception as exc:
        raise DockerError(f"Could not query Docker for swarm containers: {exc}") from exc


CONTROL_PORT = 8080  # Control API port: keep in sync with CONTROL_PORT in agent/agent.py


def _query_device_power(ip: str, port: int | None = None, timeout: float = 2.0) -> str:
    """Query a device's control API for its effective power state.

    Returns "online" or "offline" on success, or "unknown" when the agent is
    unreachable or returns an unexpected payload.
    """
    import urllib.request

    if port is None:
        port = CONTROL_PORT
    try:
        with urllib.request.urlopen(f"http://{ip}:{port}/status", timeout=timeout) as resp:
            payload = json.load(resp)
    except Exception:
        return "unknown"
    state = payload.get("power")
    return state if state in ("online", "offline") else "unknown"


def check_subnet_conflict(config: FullConfig) -> list[str]:
    """Detect overlapping Docker networks/routes without modifying the host."""
    import ipaddress

    errors: list[str] = []
    desired = ipaddress.ip_network(config.network.subnet, strict=False)
    expected_name = _network_name(config)

    # Docker networks.  The expected same-swarm network is validated separately
    # by _create_network(); every other overlap is a hard conflict.
    try:
        result = _run(["docker", "network", "ls", "--format", "{{.Name}}"], check=False)
        for net_name in [x.strip() for x in result.stdout.splitlines() if x.strip()]:
            info = _inspect_network(net_name)
            if info is None:
                continue
            for ipam in ((info.get("IPAM") or {}).get("Config") or []):
                subnet = (ipam or {}).get("Subnet")
                if not subnet:
                    continue
                try:
                    existing = ipaddress.ip_network(subnet, strict=False)
                except ValueError:
                    continue
                if desired.overlaps(existing) and net_name != expected_name:
                    errors.append(
                        f"Docker network {net_name} subnet {existing} overlaps requested {desired}"
                    )
    except (FileNotFoundError, OSError) as exc:
        errors.append(f"Could not inspect Docker networks: {exc}")

    # Host routes. Ignore the route created by our already-existing owned Docker
    # network; all other overlapping routes are reported and never altered.
    expected_info = _inspect_network(expected_name)
    expected_owned = bool(expected_info and _network_owned_by_swarm(expected_info, config.simulation.name))
    try:
        route_result = _run(["ip", "-j", "route", "show"], check=False)
        if route_result.returncode == 0 and route_result.stdout.strip():
            for route in json.loads(route_result.stdout):
                dst = route.get("dst")
                if not dst or dst == "default":
                    continue
                try:
                    route_net = ipaddress.ip_network(dst, strict=False)
                except ValueError:
                    continue
                if not desired.overlaps(route_net):
                    continue
                # An exact route to an already-owned expected Docker network is
                # normal and must not break idempotent startup.
                if expected_owned and route_net == desired:
                    continue
                errors.append(f"Host route {route_net} overlaps requested subnet {desired}")
    except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not inspect host routes: %s", exc)

    return errors


def _create_network(config: FullConfig, config_fp: str) -> str:
    """Create or safely reuse the exact owned Docker network for this swarm."""
    net_name = _network_name(config)
    info = _inspect_network(net_name)
    if info is not None:
        if not _network_owned_by_swarm(info, config.simulation.name):
            raise RuntimeError(
                f"Network {net_name} already exists but is not owned by swarm "
                f"{config.simulation.name!r}; refusing to reuse or remove it"
            )
        stored_fp = _network_fingerprint(info)
        if stored_fp != config_fp:
            raise RuntimeError(
                f"Network {net_name} has configuration fingerprint {stored_fp or '<missing>'}, "
                f"expected {config_fp}; stop the existing swarm before changing configuration"
            )
        subnet, gateway = _network_ipam(info)
        if subnet != config.network.subnet or gateway != config.network.gateway:
            raise RuntimeError(
                f"Owned network {net_name} IPAM mismatch: subnet={subnet}, gateway={gateway}; "
                f"expected subnet={config.network.subnet}, gateway={config.network.gateway}"
            )
        logger.info("Reusing owned network %s (fp=%s)", net_name, config_fp)
        return net_name

    cmd = [
        "network", "create",
        "--driver", "bridge",
        "--subnet", config.network.subnet,
        "--gateway", config.network.gateway,
        "--label", f"{LABEL_MANAGED}=true",
        "--label", f"{LABEL_NAME}={config.simulation.name}",
        "--label", f"{LABEL_FINGERPRINT}={config_fp}",
        net_name,
    ]
    result = _docker(cmd, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"Failed to create network {net_name}: {(result.stderr or result.stdout).strip()}"
        )
    logger.info("Created network %s (fp=%s)", net_name, config_fp)
    return net_name


def start_swarm(config: FullConfig, manifest_path: str | None = None) -> dict:
    """Start the full swarm.

    Startup is idempotent and keyed by the configuration fingerprint.
    If the configuration hasn't changed (same fingerprint) and containers are
    already running with matching fingerprint, skip restart.

    Returns:
        Dict with keys: requested, started, verified, failed, device_results
    """
    from host.log import get_logger
    log = get_logger("swarm")

    timer = PhaseTimer()
    requested = config.simulation.device_count

    if manifest_path is None:
        manifest_path = os.path.join(config.simulation.state_directory, "resolved-devices.yaml")

    # --- Phase: total startup ---
    timer.start("total_startup")

    # Compute configuration fingerprint
    timer.start("config_fingerprint")
    config_fp = compute_config_fingerprint(config)
    timer.stop("config_fingerprint")
    log.info("Configuration fingerprint: %s", config_fp)

    # Check Docker before querying any Docker state.
    timer.start("docker_check")
    if not check_docker():
        log.error("Docker is not available or daemon is not running")
        timer.stop("docker_check")
        timer.stop("total_startup")
        return {
            "requested": requested, "started": 0, "verified": 0,
            "failed": requested, "device_results": [],
            "error": "Docker is not available", "_timings": timer.report(),
        }
    timer.stop("docker_check")

    # Resolve deterministic device identities before idempotency/reconciliation.
    timer.start("config_resolution")
    devices, manifest = run_resolution(config)
    manifest["config_fingerprint"] = config_fp
    timer.stop("config_resolution")

    # Resource labels, not a mutable manifest alone, are authoritative
    # for whether an existing swarm may be reused. Different/unknown
    # fingerprints are rejected without changing the running swarm.
    timer.start("idempotency_check")
    try:
        existing_containers = _list_swarm_containers(config)
    except DockerError as exc:
        # Discovery failure is not "no containers": refuse to mutate anything.
        msg = f"Docker state unavailable during idempotency check; refusing to mutate: {exc}"
        log.error(msg)
        timer.stop("idempotency_check")
        timer.stop("total_startup")
        return {
            "requested": requested, "started": 0, "verified": 0,
            "failed": requested, "device_results": [], "rejected": True,
            "error": msg, "_timings": timer.report(),
        }
    expected_names = {dev["container_name"] for dev in devices}
    network_info = _inspect_network(_network_name(config))

    if network_info is not None:
        if not _network_owned_by_swarm(network_info, config.simulation.name):
            msg = (
                f"Network {_network_name(config)} exists but is not owned by swarm "
                f"{config.simulation.name!r}; refusing to mutate it"
            )
            log.error(msg)
            timer.stop("idempotency_check")
            timer.stop("total_startup")
            return {
                "requested": requested, "started": 0, "verified": 0,
                "failed": requested, "device_results": [], "rejected": True,
                "error": msg, "_timings": timer.report(),
            }
        net_fp = _network_fingerprint(network_info)
        if net_fp != config_fp:
            msg = (
                f"Swarm {config.simulation.name!r} is already using fingerprint "
                f"{net_fp or '<missing>'}; requested fingerprint is {config_fp}. "
                "Stop the existing swarm before changing configuration."
            )
            log.error(msg)
            timer.stop("idempotency_check")
            timer.stop("total_startup")
            return {
                "requested": requested, "started": 0, "verified": 0,
                "failed": requested, "device_results": [], "rejected": True,
                "error": msg, "_timings": timer.report(),
            }

    if existing_containers:
        bad_fp = [
            c for c in existing_containers
            if c["labels"].get(LABEL_FINGERPRINT, "") != config_fp
        ]
        if bad_fp:
            old = sorted({c["labels"].get(LABEL_FINGERPRINT, "<missing>") for c in bad_fp})
            msg = (
                f"Swarm {config.simulation.name!r} already has managed containers with "
                f"different fingerprint(s) {old}; requested {config_fp}. "
                "No resources were changed."
            )
            log.error(msg)
            timer.stop("idempotency_check")
            timer.stop("total_startup")
            return {
                "requested": requested, "started": 0, "verified": 0,
                "failed": requested, "device_results": [], "rejected": True,
                "error": msg, "_timings": timer.report(),
            }

        unexpected = [c["name"] for c in existing_containers if c["name"] not in expected_names]
        if unexpected:
            msg = (
                f"Managed swarm resources do not match the resolved device set: {unexpected}. "
                "Refusing implicit deletion; stop the swarm explicitly first."
            )
            log.error(msg)
            timer.stop("idempotency_check")
            timer.stop("total_startup")
            return {
                "requested": requested, "started": 0, "verified": 0,
                "failed": requested, "device_results": [], "rejected": True,
                "error": msg, "_timings": timer.report(),
            }

        existing_by_name = {c["name"]: c for c in existing_containers}
        complete_running = (
            set(existing_by_name) == expected_names
            and all(c["state"] == "running" for c in existing_containers)
            and network_info is not None
        )
        if complete_running:
            log.info("Swarm already running with matching fingerprint %s; reusing it.", config_fp)
            device_results = []
            for dev in devices:
                d = dict(dev)
                d["startup_status"] = "started"
                d["verify_status"] = "reused"
                d["reused"] = True
                device_results.append(d)
            timer.stop("idempotency_check")
            timer.stop("total_startup")
            return {
                "requested": requested, "started": 0, "verified": requested,
                "failed": 0, "device_results": device_results,
                "idempotent": True, "reused": requested,
                "_timings": timer.report(),
            }
    timer.stop("idempotency_check")

    # Check subnet/route conflicts. No cleanup is performed here.
    timer.start("subnet_conflict_check")
    conflicts = check_subnet_conflict(config)
    if conflicts:
        for conflict in conflicts:
            log.error("Conflict: %s", conflict)
        timer.stop("subnet_conflict_check")
        timer.stop("total_startup")
        return {
            "requested": requested, "started": 0, "verified": 0,
            "failed": requested, "device_results": [], "rejected": True,
            "error": "; ".join(conflicts), "_timings": timer.report(),
        }
    timer.stop("subnet_conflict_check")

    # Capture pre-start shim baseline BEFORE any container creation.
    # This baseline represents the host's shim count prior to the swarm's
    # containers being started. It is stored in the manifest so that
    # stop_swarm() can load it and use identity-based orphan detection.
    timer.start("shim_baseline_capture")
    pre_start_shims = _scan_shims()
    manifest["shim_baseline"] = {
        "count": len(pre_start_shims),
        "container_ids": [s["container_id"] for s in pre_start_shims if s["container_id"] is not None],
        "captured_at": "start_swarm_pre_container_creation",
    }
    timer.stop("shim_baseline_capture")
    log.info("Pre-start shim baseline: %d (captured before container creation)", len(pre_start_shims))

    timer.start("manifest_write")
    write_manifest_atomically(manifest, manifest_path)
    timer.stop("manifest_write")
    log.info("Manifest written to %s", manifest_path)
    log.info("Resolved %d devices", len(devices))

    # Build or reuse the fingerprinted image
    timer.start("image_build")
    if not build_or_get_image(config):
        timer.stop("total_startup")
        return {"requested": requested, "started": 0, "verified": 0, "failed": requested, "device_results": [], "_timings": timer.report()}
    timer.stop("image_build")

    # Create the owned Docker network
    timer.start("network_creation")
    net_name = _create_network(config, config_fp)
    timer.stop("network_creation")

    # Start containers with bounded concurrency
    device_results = []
    failed_devices = []
    max_workers = config.simulation.startup_parallelism

    # Pre-build all device configs outside the ThreadPoolExecutor to reduce per-thread overhead.
    # _build_device_config is CPU-bound and serializes JSON; doing it once per device
    # before spawning workers avoids contention and I/O duplication.
    timer.start("config_prebuild")
    for dev in devices:
        dev["_device_config"] = _build_device_config(config, dev)
        # Pre-create the state directory and write config so the worker only needs to run docker
        state_dir = os.path.join(config.simulation.state_directory, "devices", str(dev["id"]))
        os.makedirs(state_dir, exist_ok=True)
        config_path = os.path.join(state_dir, "config.json")
        tmp_path = config_path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(dev["_device_config"], f)
        os.rename(tmp_path, config_path)
    timer.stop("config_prebuild")

    # Pre-fetch the network object outside the ThreadPoolExecutor to avoid per-call lookup overhead
    timer.start("network_lookup")
    client = _get_docker_client()
    network = client.networks.get(net_name)
    timer.stop("network_lookup")

    timer.start("container_creation")
    _startup_mono_start = time.monotonic()

    # Thread-safe startup progress tracking (instrumentation only)
    _startup_lock = threading.Lock()
    _startup_completed = [0]
    _startup_last_milestone_num = [0]
    # Start the first interval at container-creation start so 25/200 reports
    # the real time taken for the first 25 containers rather than 0.0s.
    _startup_last_milestone_time = [_startup_mono_start]

    def _record_startup_success(total: int) -> None:
        """Record one successful start and log each 25-container milestone.

        Counter mutation and milestone state are protected by one lock.  Values
        needed by the logger are copied while holding the lock, then the actual
        log call happens after releasing it so worker threads are not serialized
        on logging I/O.
        """
        should_log = False
        completed = 0
        elapsed = 0.0
        last_25 = 0.0
        avg_rate = 0.0

        with _startup_lock:
            _startup_completed[0] += 1
            completed = _startup_completed[0]

            if completed % 25 == 0:
                milestone_num = completed // 25
                if milestone_num > _startup_last_milestone_num[0]:
                    now = time.monotonic()
                    elapsed = now - _startup_mono_start
                    last_25 = now - _startup_last_milestone_time[0]
                    avg_rate = completed / elapsed if elapsed > 0 else 0.0
                    _startup_last_milestone_num[0] = milestone_num
                    _startup_last_milestone_time[0] = now
                    should_log = True

        if should_log:
            log.info(
                "Startup progress: %d/%d | elapsed=%.1fs | last_25=%.1fs | avg=%.2f containers/s",
                completed, total, elapsed, last_25, avg_rate,
            )

    def _log_startup_final(total: int) -> None:
        """Log final startup completion."""
        with _startup_lock:
            completed = _startup_completed[0]
        elapsed = time.monotonic() - _startup_mono_start
        avg_rate = completed / elapsed if elapsed > 0 else 0
        log.info("Startup complete: %d/%d | elapsed=%.1fs | avg=%.2f containers/s",
                 completed, total, elapsed, avg_rate)

    # Bounded overall timeout for container creation phase.
    # Prevents the phase from running indefinitely when many containers
    # fail or the system is under heavy load.
    _overall_timeout = getattr(config.simulation, "_startup_timeout", _STARTUP_OVERALL_TIMEOUT)
    _phase_start = time.time()

    def _start_and_tag(device, max_retries=3):
        """Create/start one device without ever deleting an unrelated name conflict."""
        last_error = None

        # Reconcile an existing exact-name container only when all ownership
        # labels and the fingerprint match this resolved device.
        try:
            existing = client.containers.get(device["container_name"])
        except docker.errors.NotFound:
            existing = None

        if existing is not None:
            labels = existing.labels or {}
            owned = (
                labels.get(LABEL_MANAGED) == "true"
                and labels.get(LABEL_NAME) == config.simulation.name
                and labels.get(LABEL_DEVICE_ID) == str(device["id"])
                and labels.get(LABEL_FINGERPRINT) == config_fp
            )
            if not owned:
                err = RuntimeError(
                    f"Container name {device['container_name']} already exists but is not "
                    "the matching owned resource; refusing to remove or replace it"
                )
                device["startup_status"] = "failed"
                device["error"] = str(err)
                return device, err
            try:
                existing.reload()
                if existing.status != "running":
                    existing.start()
                device["startup_status"] = "started"
                device["reused"] = True
                _record_startup_success(len(devices))
                log.info("Reused container %s (IP: %s)", device["container_name"], device["ip"])
                return device, None
            except docker.errors.APIError as exc:
                device["startup_status"] = "failed"
                device["error"] = str(exc)
                return device, exc

        for attempt in range(1, max_retries + 1):
            if time.time() - _phase_start > _overall_timeout:
                last_error = RuntimeError("container_creation phase timeout")
                break
            try:
                _start_container_preconfig(network, config, device, config_fp)
                device["startup_status"] = "started"
                device["reused"] = False
                log.info("Started container %s (IP: %s)", device["container_name"], device["ip"])
                _record_startup_success(len(devices))
                return device, None
            except docker.errors.APIError as exc:
                last_error = exc
                # A name conflict is ownership-sensitive and must never trigger
                # blind deletion/retry.
                if "already in use" in str(exc).lower():
                    break
                if attempt < max_retries:
                    time.sleep(0.5 * attempt)
            except Exception as exc:
                last_error = exc
                if attempt < max_retries:
                    time.sleep(0.5 * attempt)

        log.error("Failed to start container %s (IP: %s): %s",
                  device["container_name"], device["ip"], last_error)
        device["startup_status"] = "failed"
        device["error"] = str(last_error)
        return device, last_error

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_start_and_tag, dev): dev for dev in devices}
        for fut in concurrent.futures.as_completed(futures):
            result, error = fut.result()
            device_results.append(result)
            if error is not None:
                failed_devices.append(result)

    # Log final startup completion (even if total < 25)
    _log_startup_final(len(devices))

    timer.stop("container_creation")

    # Verify started containers with bounded concurrency
    timer.start("readiness_verification")
    verified = 0
    failed_verification = []

    def _verify_and_tag(device):
        """Verify a container responds to ping. Thread-safe per-device mutation."""
        try:
            is_up = _verify_container(device)
            device["verify_status"] = "verified" if is_up else "unverified"
            return device, is_up, None
        except Exception as e:
            device["verify_status"] = "error"
            return device, False, e

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        verify_futures = {pool.submit(_verify_and_tag, dev): dev for dev in device_results
                          if dev["startup_status"] == "started"}
        for fut in concurrent.futures.as_completed(verify_futures):
            device, is_up, error = fut.result()
            if is_up:
                verified += 1
            else:
                log.warning("Container %s %s", device["container_name"],
                            "unverified" if not error else f"error: {error}")
                failed_verification.append(device)

    timer.stop("readiness_verification")

    started = sum(1 for d in device_results if d["startup_status"] == "started")

    log.info("Start complete: %d requested, %d started, %d verified, %d failed",
             requested, started, verified, len(failed_devices))

    # Update manifest with final status
    timer.start("final_manifest_write")
    manifest["devices"] = device_results
    write_manifest_atomically(manifest, manifest_path)
    timer.stop("final_manifest_write")

    timer.stop("total_startup")

    timings = timer.report()
    log.info("Phase timings (s): %s", json.dumps({k: round(v, 3) for k, v in timings.items()}))

    return {
        "requested": requested,
        "started": started,
        "verified": verified,
        "failed": len(failed_devices),
        "device_results": device_results,
        "_timings": timings,
    }


def _build_device_config(config: FullConfig, device: dict) -> dict:
    """Build the agent config JSON for a device.
    
    Profile fields in the manifest are strings (profile names). We resolve them
    against the config's profile dictionaries to get actual profile objects.
    
    This passes the complete resolved device configuration to each agent,
    including full outage profile, traffic profile details, and SNMP identity.
    """
    # Resolve traffic profile name -> TrafficProfile object
    traffic_profile_name = device.get("traffic_profile", config.defaults.traffic_profile)
    traffic_profile = config.traffic_profiles.get(traffic_profile_name)
    if traffic_profile is None:
        raise ConfigError(
            f"Device {device['id']}: unknown traffic profile {traffic_profile_name!r}; "
            f"known profiles: {sorted(config.traffic_profiles)}"
        )

    # Resolve downtime profile for outage scheduler
    downtime_profile_name = device.get("downtime_profile", config.defaults.downtime_profile)
    downtime_profile = config.downtime_profiles.get(downtime_profile_name)
    if downtime_profile is None:
        raise ConfigError(
            f"Device {device['id']}: unknown downtime profile {downtime_profile_name!r}; "
            f"known profiles: {sorted(config.downtime_profiles)}"
        )

    # Resolve SNMP identity profile
    snmp_identity_name = device.get("snmp_identity_profile", config.defaults.snmp_identity_profile)
    snmp_identity = config.snmp_identity_profiles.get(snmp_identity_name)
    if snmp_identity is None:
        raise ConfigError(
            f"Device {device['id']}: unknown SNMP identity profile {snmp_identity_name!r}; "
            f"known profiles: {sorted(config.snmp_identity_profiles)}"
        )
    
    link_speed = traffic_profile.link_speed_mbps if traffic_profile else 1000
    num_ifaces = traffic_profile.interfaces if traffic_profile else 1
    
    # Compute initial traffic rate from the profile's rate range
    if traffic_profile:
        import random
        rng = random.Random(config.simulation.random_seed + device["id"])
        rate_mbps = rng.uniform(traffic_profile.rate_min, traffic_profile.rate_max)
        initial_rate_kbs = int(rate_mbps * 1000)  # Mbps -> Kbps
    else:
        initial_rate_kbs = 1000
    
    # Build complete outage profile for the scheduler
    outage_profile = None
    if downtime_profile:
        outage_profile = {
            "name": downtime_profile.name,
            "enabled": downtime_profile.enabled,
            "expected_events_per_day": downtime_profile.expected_events_per_day,
            "duration_min_minutes": downtime_profile.duration_min,
            "duration_max_minutes": downtime_profile.duration_max,
            "minimum_uptime_minutes": downtime_profile.minimum_uptime_minutes,
        }
    
    # Build complete traffic profile for the counters
    full_traffic_profile = None
    if traffic_profile:
        full_traffic_profile = {
            "name": traffic_profile.name,
            "interfaces": traffic_profile.interfaces,
            "link_speed_mbps": traffic_profile.link_speed_mbps,
            "rate_min": traffic_profile.rate_min,
            "rate_max": traffic_profile.rate_max,
            "variation_percent": traffic_profile.variation_percent,
            "timezone": config.simulation.timezone,
            "random_seed": config.simulation.random_seed + device["id"],
            "periods": [
                {
                    "start_hour": p.start_hour,
                    "start_min": p.start_min,
                    "end_hour": p.end_hour,
                    "end_min": p.end_min,
                    "rate_min": p.rate_min,
                    "rate_max": p.rate_max,
                }
                for p in traffic_profile.periods
            ],
        }
    
    # Build SNMP identity
    snmp_identity_config = None
    if snmp_identity:
        snmp_identity_config = {
            "sys_descr": snmp_identity.sys_descr,
            "sys_object_id": snmp_identity.sys_object_id,
            "sys_contact": snmp_identity.sys_contact,
            "sys_location": snmp_identity.sys_location,
            "sys_services": snmp_identity.sys_services,
        }
    
    return {
        "simulation_name": config.simulation.name,
        "device_id": device["id"],
        "ip": device["ip"],
        "sys_name": device["hostname"],
        "sys_descr": snmp_identity_config.get("sys_descr", f"Simulated network device #{device['id']}"),
        "sys_object_id": snmp_identity_config.get("sys_object_id", "1.3.6.1.4.1.8072.3.2.10"),
        "sys_contact": snmp_identity_config.get("sys_contact", "admin@localhost"),
        "sys_location": snmp_identity_config.get("sys_location", config.simulation.name),
        "sys_services": snmp_identity_config.get("sys_services", 2),
        "link_speed_mbps": link_speed,
        "num_interfaces": num_ifaces,
        "community": config.network.community,
        "snmp_port": config.network.snmp_port,
        "snmp_version": config.network.snmp_version,
        "management_subnet": config.network.subnet,
        "simulation_timezone": config.simulation.timezone,
        "traffic_rate_bps": initial_rate_kbs * 1000,  # informational legacy field
        "initial_uptime_profile": device.get("initial_uptime_profile"),
        "initial_uptime_seconds": device.get("initial_uptime_seconds", 0),
        "downtime_profile_name": downtime_profile_name,
        "outage_profile": outage_profile,
        "traffic_profile_name": traffic_profile_name,
        "traffic_profile": full_traffic_profile,
        "snmp_identity_profile": snmp_identity_name,
        "random_seed": config.simulation.random_seed + device["id"],
    }


def _start_container(config: FullConfig, net_name: str, device: dict):
    """Start a single device container."""
    device_config = _build_device_config(config, device)
    
    # Create state directory for this device
    state_dir = os.path.join(config.simulation.state_directory, "devices", str(device["id"]))
    os.makedirs(state_dir, exist_ok=True)
    
    # Write device config as JSON for the agent to read
    config_path = os.path.join(state_dir, "config.json")
    
    # Atomic write
    tmp_path = config_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(device_config, f)
    os.rename(tmp_path, config_path)
    
    # Mount the config into the container
    container_config_path = "/etc/network-swarm/config.json"
    
    cmd = [
        "run", "-d",
        "--name", device["container_name"],
        "--hostname", device["hostname"],
        "--network", net_name,
        "--ip", device["ip"],
        "--label", f"{LABEL_MANAGED}=true",
        "--label", f"{LABEL_NAME}={config.simulation.name}",
        "--label", f"{LABEL_DEVICE_ID}={device['id']}",
        "--label", f"{LABEL_FINGERPRINT}={compute_config_fingerprint(config)}",
        "--restart=unless-stopped",
        "--cap-add=NET_ADMIN",  # Allow ICMP raw sockets
        "-v", f"{config_path}:{container_config_path}:ro",
        "-e", f"AGENT_CONFIG={container_config_path}",
        "network-swarm-agent:latest",
    ]
    _docker(cmd)


def _start_container_preconfig(network: object, config: FullConfig, device: dict, config_fp: str) -> object:
    """Start a single device container using a pre-built device config.
    
    Uses the Docker low-level API to create + connect in a single operation,
    eliminating the separate network.connect() call overhead.
    
    Args:
        network: Docker network object (pre-fetched outside ThreadPoolExecutor)
        config: Full configuration
        device: Device dict with IP, name, etc.
    
    Returns:
        Container ID string
    """
    state_dir = os.path.join(config.simulation.state_directory, "devices", str(device["id"]))
    config_path = os.path.abspath(os.path.join(state_dir, "config.json"))
    container_config_path = "/etc/network-swarm/config.json"
    
    api = _get_docker_api()
    
    # Create bind mount for config file
    mounts = [Mount(
        container_config_path,
        config_path,
        type="bind",
        read_only=True
    )]
    
    # Host config
    host_config = api.create_host_config(
        cap_add=["NET_ADMIN"],
        restart_policy={"Name": "unless-stopped"},
        mounts=mounts,
    )
    
    # Network config with IPAM
    endpoint_config = EndpointConfig(version="1.24", ipv4_address=device["ip"])
    networking_config = api.create_networking_config({
        network.id: endpoint_config,
    })
    
    # Labels
    labels = {
        LABEL_MANAGED: "true",
        LABEL_NAME: config.simulation.name,
        LABEL_DEVICE_ID: str(device["id"]),
        LABEL_FINGERPRINT: config_fp,
    }
    
    # Create container with network config (single API call = create + connect)
    container = api.create_container(
        "network-swarm-agent:latest",
        name=device["container_name"],
        hostname=device["hostname"],
        detach=True,
        host_config=host_config,
        networking_config=networking_config,
        labels=labels,
        environment={"AGENT_CONFIG": container_config_path},
    )
    
    # Start the container
    api.start(container["Id"])
    
    return container["Id"]


def _verify_container(device: dict) -> bool:
    """Verify a container is responding to ping."""
    try:
        result = _run([
            "ping", "-c", "1", "-W", "2", device["ip"]
        ], check=False)
        return result.returncode == 0
    except (FileNotFoundError, OSError):
        return False


def _count_shims() -> int:
    """Count running containerd-shim processes via /proc scanning.

    Iterates all PIDs under /proc, reads cmdline for each process,
    and counts those whose cmdline contains 'containerd-shim'.

    This is the ONLY correct method for shim counting because:

    - An orphaned shim survives AFTER its Docker container is removed.
    - Docker container count != shim count during/after shutdown.
    - Lifecycle safety checks must be based on REAL process enumeration,
      not Docker metadata.
    """
    count = 0
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = entry
            try:
                cmdline_path = f"/proc/{pid}/cmdline"
                with open(cmdline_path, "rb") as f:
                    cmdline = f.read().decode("utf-8", errors="replace")
                if "containerd-shim" in cmdline:
                    count += 1
            except (FileNotFoundError, PermissionError, OSError):
                continue
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return count


def _scan_shims() -> list[dict]:
    """Scan for running containerd-shim processes and extract identity info.

    Returns a list of dicts, each containing:
      - pid: int (process ID)
      - container_id: str | None (parsed from '-id <container-id>' arg)
      - cmdline: str (raw command line)

    This function parses containerd-shim-runc-v2 command lines to extract
    the Docker container ID, which is passed as '-id <hash>' argument.

    The container_id is the primary key for managed orphan detection:
    after stop_swarm() removes managed containers, any surviving shim whose
    -id matches a removed container is an orphan.
    """
    shims = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = entry
            try:
                cmdline_path = f"/proc/{pid}/cmdline"
                with open(cmdline_path, "rb") as f:
                    cmdline = f.read().decode("utf-8", errors="replace")
                if "containerd-shim" not in cmdline:
                    continue

                # Parse container ID from '-id <container-hash>' argument
                container_id = None
                parts = cmdline.split("\x00")  # cmdline is null-separated
                for i, part in enumerate(parts):
                    if part == "-id" and i + 1 < len(parts):
                        container_id = parts[i + 1]
                        break

                shims.append({
                    "pid": int(pid),
                    "container_id": container_id,
                    "cmdline": cmdline,
                })
            except (FileNotFoundError, PermissionError, OSError):
                continue
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return shims


def _identify_managed_orphans(shims: list[dict], managed_docker_ids: set[str]) -> list[dict]:
    """Identify shim processes whose container_id matches any managed Docker container ID.

    Args:
        shims: List of shim dicts from _scan_shims().
        managed_docker_ids: Set of Docker container IDs (64-char hex) for containers
            that were managed by this swarm.

    Returns:
        List of shim dicts whose container_id is in managed_docker_ids.
        These are managed shims that survived their container removal — i.e., orphans.
    """
    orphans = []
    for shim in shims:
        cid = shim.get("container_id")
        if cid is not None and cid in managed_docker_ids:
            orphans.append(shim)
    return orphans


def stop_swarm(config: FullConfig) -> dict:
    """Stop and remove all resources for the swarm.

    Uses bounded parallel container removal with sequential network cleanup
          (network must be removed after all containers to preserve ownership safety).

    Post-condition on return:
      - managed containers = 0
      - managed network = 0
      - managed containerd shims = 0 (shims terminate naturally within grace period)
      - Docker responsive
      - containerd responsive

    Shim tracking: records shim count before shutdown, after Docker removal,
    and after bounded grace period for natural termination.
    """
    from host.log import get_logger
    from src.resolver import load_manifest
    log = get_logger("swarm")

    timer = PhaseTimer()
    timer.start("total_shutdown")

    # Load pre-start shim baseline from manifest (if available).
    # This was captured by start_swarm() BEFORE container creation,
    # so it represents the TRUE host baseline, not the swarm's own shims.
    manifest_path = os.path.join(config.simulation.state_directory, "resolved-devices.yaml")
    swarm_manifest = load_manifest(manifest_path) if os.path.exists(manifest_path) else {}
    shim_baseline_info = swarm_manifest.get("shim_baseline", None)
    shim_baseline_count = shim_baseline_info["count"] if shim_baseline_info else None
    log.info("Shim baseline from manifest: %s", shim_baseline_count)

    # Find containers by label. A single SDK call returns name + id + state +
    # labels for every match (no per-container inspect; SW-02/SW-03).
    #
    # Docker container IDs (64-char hex) are used for identity-based orphan
    # detection: shims are passed '-id <container-id>', so after removal any
    # surviving shim with a matching -id is a managed orphan.
    #
    # A discovery failure is NOT "zero containers" (SW-04): abort before any
    # destructive cleanup (no container removal, no network removal).
    timer.start("container_discovery")
    try:
        records = _list_swarm_containers(config)
    except DockerError as exc:
        log.error("Container discovery failed; aborting shutdown without removing the network: %s", exc)
        timer.stop("container_discovery")
        timer.stop("total_shutdown")
        return {
            "containers_removed": 0,
            "containers_removed_successfully": 0,
            "containers_removal_failed": 0,
            "network_removed": False,
            "network_preserved_unrelated": False,
            "lifecycle_failure": True,
            "failure_reasons": [f"container discovery failed: {exc}"],
            "shim_count_baseline": shim_baseline_count,
            "shim_count_after_removal": None,
            "shim_count_after_grace": None,
            "managed_orphan_shim_ids": [],
            "managed_orphan_shim_pids": [],
            "shim_running_count": None,
            "removal_diagnostics": [],
            "_timings": timer.report(),
        }
    timer.stop("container_discovery")

    container_names = [r["name"] for r in records]
    container_id_map = {r["name"]: r["id"] for r in records}
    log.info("Found %d managed containers to remove", len(container_names))
    log.info("Collected %d container IDs for identity tracking", len(container_id_map))

    # Bounded parallel container removal
    timer.start("container_removal")
    removed = 0
    failed_removals = 0
    removal_diagnostics = []  # Track per-container removal results

    def _remove_container(name: str) -> tuple[str, bool, str]:
        """Remove a single container. Returns (name, success, detail)."""
        try:
            result = _run(["docker", "rm", "-f", name], check=False, timeout=120)
            if result.returncode != 0:
                stderr = (result.stderr or "").strip()
                log.warning("docker rm failed for %s (rc=%d): %s", name, result.returncode, stderr)
                return name, False, f"docker rm rc={result.returncode}: {stderr}"
            log.info("Removed container %s", name)
            return name, True, "removed"
        except subprocess.TimeoutExpired:
            log.warning("Timeout removing container %s", name)
            return name, False, "timeout"
        except Exception as e:
            log.warning("Failed to remove container %s: %s", name, e)
            return name, False, f"error: {e}"

    # Use shutdown_parallelism for container removal concurrency.
    # This is independent of startup_parallelism to prevent high-concurrency
    # shutdown from causing containerd task teardown cancellations.
    max_workers = getattr(config.simulation, "shutdown_parallelism", config.simulation.startup_parallelism)

    # Thread-safe shutdown progress tracking (instrumentation only)
    _shutdown_mono_start = time.monotonic()
    _shutdown_lock = threading.Lock()
    _shutdown_removed = [0]
    _shutdown_last_milestone_num = [0]
    # Same rule as startup: the first last_25 interval starts when removal starts.
    _shutdown_last_milestone_time = [_shutdown_mono_start]
    _total_containers = len(container_names)

    def _record_shutdown_success() -> None:
        """Record one successful removal and log each 25-container milestone."""
        should_log = False
        count = 0
        elapsed = 0.0
        last_25 = 0.0
        avg_rate = 0.0

        with _shutdown_lock:
            _shutdown_removed[0] += 1
            count = _shutdown_removed[0]

            if count % 25 == 0:
                milestone_num = count // 25
                if milestone_num > _shutdown_last_milestone_num[0]:
                    now = time.monotonic()
                    elapsed = now - _shutdown_mono_start
                    last_25 = now - _shutdown_last_milestone_time[0]
                    avg_rate = count / elapsed if elapsed > 0 else 0.0
                    _shutdown_last_milestone_num[0] = milestone_num
                    _shutdown_last_milestone_time[0] = now
                    should_log = True

        if should_log:
            log.info(
                "Shutdown progress: %d/%d | elapsed=%.1fs | last_25=%.1fs | avg=%.2f containers/s",
                count, _total_containers, elapsed, last_25, avg_rate,
            )

    def _log_shutdown_final() -> None:
        """Log final shutdown completion."""
        with _shutdown_lock:
            count = _shutdown_removed[0]
        elapsed = time.monotonic() - _shutdown_mono_start
        avg_rate = count / elapsed if elapsed > 0 else 0
        log.info("Shutdown complete: %d/%d | elapsed=%.1fs | avg=%.2f containers/s",
                 count, _total_containers, elapsed, avg_rate)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        remove_futures = {pool.submit(_remove_container, name): name for name in container_names}
        for fut in concurrent.futures.as_completed(remove_futures):
            name, success, detail = fut.result()
            if success:
                removed += 1
                _record_shutdown_success()
            else:
                failed_removals += 1
            removal_diagnostics.append({"container": name, "success": success, "detail": detail})

    timer.stop("container_removal")

    # Verify and retry: check if any containers remain and retry removal
    max_retries = 3
    for retry in range(max_retries):
        try:
            remaining = [r["name"] for r in _list_swarm_containers(config)]
        except DockerError as exc:
            # Cannot verify: do not assume success. The final verification
            # below reports the failure explicitly (SW-04).
            log.warning("Retry %d: could not verify remaining containers: %s", retry + 1, exc)
            break
        if not remaining:
            break
        log.info("Retry %d: %d containers still remain, retrying removal", retry + 1, len(remaining))
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            retry_futures = {pool.submit(_remove_container, name): name for name in remaining}
            for fut in concurrent.futures.as_completed(retry_futures):
                name, success, detail = fut.result()
                if success:
                    removed += 1
                    _record_shutdown_success()
                else:
                    failed_removals += 1
                removal_diagnostics.append({"container": name, "success": success, "detail": detail})

    # Log final shutdown after all retries
    _log_shutdown_final()

    # Identity-based managed shim orphan detection.
    #
    # After container removal, we scan /proc for containerd-shim processes and
    # parse their '-id <container-id>' argument. Any shim whose -id matches a
    # managed container Docker ID is a managed orphan.
    #
    # This is more reliable than count-based comparison because:
    # - A count comparison can produce a false clean result if an unrelated
    #   pre-existing shim exits while one managed swarm shim remains orphaned.
    # - Identity-based detection checks exactly which containers the shims belong to.

    grace_period = getattr(config.simulation, "shutdown_grace_period", 10)
    timer.start("grace_period")

    # Build the set of managed Docker container IDs (for orphan matching)
    managed_docker_ids = set(container_id_map.values())
    log.info("Managed Docker IDs for orphan tracking: %d", len(managed_docker_ids))

    # Scan for surviving shims immediately after removal
    post_removal_shims = _scan_shims()
    managed_orphans_after_removal = _identify_managed_orphans(post_removal_shims, managed_docker_ids)
    shim_after_removal = len(post_removal_shims)
    log.info("Shim count after Docker removal: %d (managed orphans: %d)",
             shim_after_removal, len(managed_orphans_after_removal))

    shim_after_grace = shim_after_removal
    managed_orphan_shim_ids = []
    managed_orphan_shim_pids = []

    if len(managed_orphans_after_removal) > 0:
        # Managed orphan shims detected — enter polling grace period
        log.info("Managed orphan shim(s) detected (%d), waiting up to %ds for natural termination...",
                 len(managed_orphans_after_removal), grace_period)
        steps = 10
        step_delay = grace_period / steps
        for step in range(steps):
            time.sleep(step_delay)
            post_grace_shims = _scan_shims()
            current_orphans = _identify_managed_orphans(post_grace_shims, managed_docker_ids)
            if len(current_orphans) == 0:
                shim_after_grace = len(post_grace_shims)
                managed_orphan_shim_ids = []
                managed_orphan_shim_pids = []
                log.info("All managed shim orphans terminated after %d steps (%.1fs elapsed), total shims=%d",
                         step + 1, (step + 1) * step_delay, shim_after_grace)
                break
            shim_after_grace = len(post_grace_shims)
            managed_orphan_shim_ids = [s["container_id"] for s in current_orphans if s["container_id"] is not None]
            managed_orphan_shim_pids = [s["pid"] for s in current_orphans]
        else:
            # Grace period expired
            managed_orphan_shim_ids = [s["container_id"] for s in current_orphans if s["container_id"] is not None]
            managed_orphan_shim_pids = [s["pid"] for s in current_orphans]
            log.warning("Grace period expired with %d managed orphan shim(s) (total shims=%d, orphans: %s)",
                        len(managed_orphan_shim_ids), shim_after_grace, managed_orphan_shim_ids[:10])
    else:
        # No managed orphan shims detected after removal — skip grace period
        shim_after_grace = shim_after_removal
        log.info("No managed orphan shims detected after removal (total shims=%d); skipping grace period",
                 shim_after_removal)
    timer.stop("grace_period")

    # Remove only the exact owned network. A same-named unrelated network is
    # preserved and reported instead of being treated as ours.
    timer.start("network_cleanup")
    net_name = _network_name(config)
    net_removed = False
    network_preserved_unrelated = False
    NET_TIMEOUT = 10
    info = _inspect_network(net_name)
    if info is None:
        # Idempotent cleanup: there is no owned resource left to remove.
        net_removed = True
        log.info("Network %s already absent", net_name)
    elif not _network_owned_by_swarm(info, config.simulation.name):
        network_preserved_unrelated = True
        log.warning(
            "Network %s exists but lacks this swarm's ownership labels; preserving it",
            net_name,
        )
    else:
        rm_result = _run(["docker", "network", "rm", net_name], check=False, timeout=NET_TIMEOUT)
        if rm_result.returncode == 0:
            net_removed = True
            log.info("Removed owned network %s", net_name)
        else:
            log.warning("Failed to remove owned network %s: %s", net_name, rm_result.stderr)
    timer.stop("network_cleanup")

    # ---- Lifecycle failure check ----
    # After grace period, if managed containers remain or network still exists
    # or managed shim orphans survive, this is a lifecycle failure.
    lifecycle_failure = False
    failure_reasons = []

    # Check for remaining managed containers. A failed query is reported as a
    # lifecycle failure rather than being treated as "none remaining" (SW-04).
    try:
        remaining_containers = [r["name"] for r in _list_swarm_containers(config)]
    except DockerError as exc:
        lifecycle_failure = True
        failure_reasons.append(f"could not verify container removal: {exc}")
        remaining_containers = []
    if remaining_containers:
        lifecycle_failure = True
        failure_reasons.append(f"{len(remaining_containers)} managed containers still exist")

    # Check whether an owned network remains. An unrelated same-name network is
    # intentionally preserved and is not a managed-resource lifecycle leak.
    if not net_removed and not network_preserved_unrelated:
        final_info = _inspect_network(net_name)
        if final_info is not None and _network_owned_by_swarm(final_info, config.simulation.name):
            lifecycle_failure = True
            failure_reasons.append("owned swarm network still exists")

    # Check managed shim orphans by identity — any surviving shim whose -id
    # matches a removed managed container is an orphan.
    if len(managed_orphan_shim_ids) > 0:
        lifecycle_failure = True
        failure_reasons.append(f"managed shim orphans: ids={managed_orphan_shim_ids[:10]}")

    if lifecycle_failure:
        log.error("LIFECYCLE FAILURE: %s", "; ".join(failure_reasons))

    timer.stop("total_shutdown")
    timings = timer.report()
    log.info("Shutdown phase timings (s): %s", json.dumps({k: round(v, 3) for k, v in timings.items()}))

    return {
        "containers_removed": len(container_names),
        "containers_removed_successfully": removed,
        "containers_removal_failed": failed_removals,
        "network_removed": net_removed,
        "network_preserved_unrelated": network_preserved_unrelated,
        "lifecycle_failure": lifecycle_failure,
        "failure_reasons": failure_reasons,
        "shim_count_baseline": shim_baseline_count,
        "shim_count_after_removal": shim_after_removal,
        "shim_count_after_grace": shim_after_grace,
        "managed_orphan_shim_ids": managed_orphan_shim_ids,
        "managed_orphan_shim_pids": managed_orphan_shim_pids,
        "shim_running_count": shim_after_grace,
        "removal_diagnostics": removal_diagnostics,
        "_timings": timings,
    }


def status_swarm(config: FullConfig) -> dict:
    """Return manifest-aligned runtime status for the selected swarm.

    The resolved YAML manifest is the identity/profile source. Docker labels are
    the ownership/device mapping source. Observable ping plus the agent's
    control API (GET /status) determine emulated online/offline state.
    """
    from host.log import get_logger
    log = get_logger("swarm")

    manifest_path = os.path.join(config.simulation.state_directory, "resolved-devices.yaml")
    manifest = load_manifest(manifest_path) if os.path.exists(manifest_path) else {}
    manifest_devices = list(manifest.get("devices", []) or [])
    device_map = {int(dev["id"]): dev for dev in manifest_devices if "id" in dev}

    resolved_count = len(manifest_devices) if manifest_devices else config.simulation.device_count

    # Discovery failure must be reported as an explicit error, not as "every
    # device missing" (SW-04). A single SDK call covers all containers
    # (SW-02/SW-03).
    try:
        records = _list_swarm_containers(config)
    except DockerError as exc:
        log.error("Docker state unavailable: %s", exc)
        return {
            "swarm_name": config.simulation.name,
            "requested": resolved_count,
            "containers_running": 0,
            "devices_online": 0,
            "devices_offline": 0,
            "failed_containers": 0,
            "containers": [],
            "error": f"Docker state unavailable: {exc}",
            "manifest_path": manifest_path,
            "config_fingerprint": manifest.get("config_fingerprint", ""),
        }

    container_by_device: dict[int, dict] = {}
    extra_records: list[dict] = []
    for record in records:
        labels = record["labels"]
        try:
            device_id = int(labels.get(LABEL_DEVICE_ID, 0))
        except (TypeError, ValueError):
            device_id = 0
        rec = {
            "cid": record["id"],
            "name": record["name"],
            "state": record["state"],
            "device_id": device_id,
            "fingerprint": labels.get(LABEL_FINGERPRINT, ""),
            "dev_entry": device_map.get(device_id),
        }
        if device_id > 0 and device_id not in container_by_device:
            container_by_device[device_id] = rec
        else:
            extra_records.append(rec)

    def active_profiles(dev_entry: dict | None) -> dict:
        if not dev_entry:
            return {}
        return {
            "downtime": dev_entry.get("downtime_profile"),
            "traffic": dev_entry.get("traffic_profile"),
            "initial_uptime": dev_entry.get("initial_uptime_profile"),
            "snmp_identity": dev_entry.get("snmp_identity_profile"),
        }

    def verify_record(record: dict) -> dict:
        dev_entry = record.get("dev_entry")
        state = record.get("state", "unknown")
        name = record.get("name")

        ping_status = "unknown"
        is_reachable = False
        if dev_entry and state == "running":
            try:
                ping_result = _run(
                    ["ping", "-c", "1", "-W", "2", dev_entry["ip"]],
                    check=False,
                    timeout=10,
                )
                is_reachable = ping_result.returncode == 0
                ping_status = "reachable" if is_reachable else "unreachable"
            except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
                ping_status = "unreachable"

        # Live power state comes from the device's control API. The manifest
        # value is only a fallback for older manifests written before the
        # control API existed (the manifest no longer tracks power changes).
        power_state = dev_entry.get("power_state", "unknown") if dev_entry else "unknown"
        if dev_entry and state == "running":
            queried = _query_device_power(dev_entry["ip"])
            if queried != "unknown":
                power_state = queried

        profile_map = active_profiles(dev_entry)
        profile_list = [f"{key}:{value}" for key, value in profile_map.items() if value]
        effective_online = state == "running" and power_state == "online" and is_reachable

        return {
            "id": record.get("cid"),
            "name": name,
            "state": state,
            "device_id": record.get("device_id"),
            "ip": dev_entry.get("ip") if dev_entry else None,
            "pool": dev_entry.get("pool") if dev_entry else None,
            "ping": ping_status,
            "power_state": power_state,
            "effective_online": effective_online,
            "profiles": profile_list,
            "active_profiles": profile_map,
            "config_fingerprint": record.get("fingerprint", ""),
        }

    records_to_verify = list(container_by_device.values()) + extra_records
    max_workers = max(1, min(len(records_to_verify) or 1, getattr(config.simulation, "startup_parallelism", 32)))
    containers: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(verify_record, record) for record in records_to_verify]
        for fut in concurrent.futures.as_completed(futures):
            containers.append(fut.result())

    # Every resolved device receives a row, including failed/missing containers.
    present_device_ids = {c["device_id"] for c in containers if c.get("device_id")}
    for device_id, dev_entry in device_map.items():
        if device_id in present_device_ids:
            continue
        profile_map = active_profiles(dev_entry)
        containers.append({
            "id": None,
            "name": dev_entry.get("container_name"),
            "state": "missing",
            "device_id": device_id,
            "ip": dev_entry.get("ip"),
            "pool": dev_entry.get("pool"),
            "ping": "unreachable",
            "power_state": "unknown",
            "effective_online": False,
            "profiles": [f"{k}:{v}" for k, v in profile_map.items() if v],
            "active_profiles": profile_map,
            "config_fingerprint": "",
        })

    containers.sort(key=lambda c: (c.get("device_id") or 10**9, c.get("name") or ""))

    expected_rows = [c for c in containers if c.get("device_id") in device_map]
    running_count = sum(1 for c in expected_rows if c["state"] == "running")
    online_count = sum(1 for c in expected_rows if c["effective_online"])
    offline_count = sum(
        1 for c in expected_rows
        if c["state"] == "running" and not c["effective_online"]
    )
    failed_count = sum(1 for c in expected_rows if c["state"] != "running")

    return {
        "swarm_name": config.simulation.name,
        "requested": resolved_count,
        "containers_running": running_count,
        "devices_online": online_count,
        "devices_offline": offline_count,
        "failed_containers": failed_count,
        "containers": containers,
        "manifest_path": manifest_path,
        "config_fingerprint": manifest.get("config_fingerprint", ""),
    }
