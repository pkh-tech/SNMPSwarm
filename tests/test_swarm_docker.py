"""SW-02/SW-03/SW-04: host-side Docker discovery and lifecycle safety.

- Discovery is a single SDK call with label filters (no per-container
  ``docker inspect``), so a 1000-container swarm pays one API round trip.
- A Docker failure raises DockerError and is never treated as "zero
  containers": destructive operations abort, and status reports the error
  explicitly instead of marking every device missing.
- A healthy daemon with no matching containers still reports empty results
  and lifecycle operations may proceed.
"""
import copy
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import host.swarm as swarm
from host.swarm import (
    DockerError,
    LABEL_DEVICE_ID,
    LABEL_FINGERPRINT,
    LABEL_MANAGED,
    LABEL_NAME,
    _list_swarm_containers,
    _query_device_power,
)
from src.config import BUILTIN_RAW_CONFIG, validate_config
from src.ip import container_name

SIM_NAME = "unitsim"


def make_config(tmp_path):
    raw = copy.deepcopy(BUILTIN_RAW_CONFIG)
    raw["simulation"].update(
        {
            "name": SIM_NAME,
            "total_units": 3,
            "state_directory": str(tmp_path / "state"),
        }
    )
    return validate_config(raw)


class FakeContainer:
    def __init__(self, cid, name, status, labels):
        self.id = cid
        self.name = name
        self.status = status
        self.labels = labels or {}


def fake_container(did, status="running"):
    name = container_name(SIM_NAME, did)
    return FakeContainer(
        "ab" * 32,
        name,
        status,
        {
            LABEL_MANAGED: "true",
            LABEL_NAME: SIM_NAME,
            LABEL_DEVICE_ID: str(did),
            LABEL_FINGERPRINT: "fp",
        },
    )


class FakeContainers:
    """``responder(call_number)`` returns the list for that call."""

    def __init__(self, responder):
        self._responder = responder
        self.calls = 0
        self.seen_filters = []

    def list(self, all=None, filters=None):
        self.calls += 1
        self.seen_filters.append(filters)
        return self._responder(self.calls)


def install_fake_client(monkeypatch, responder):
    client = type("FakeClient", (), {"containers": FakeContainers(responder)})()
    monkeypatch.setattr(swarm, "_get_docker_client", lambda: client)
    return client.containers


def boom(*args, **kwargs):
    raise AssertionError(f"destructive helper called: {args}")


# --- SW-02/SW-03: single-call discovery ----------------------------------


def test_list_swarm_containers_single_call_with_label_filters(tmp_path, monkeypatch):
    containers = install_fake_client(monkeypatch, lambda n: [fake_container(1), fake_container(2)])
    records = _list_swarm_containers(make_config(tmp_path))

    assert containers.calls == 1, "discovery must be one Docker API call"
    assert containers.seen_filters[0] == {
        "label": [f"{LABEL_MANAGED}=true", f"{LABEL_NAME}={SIM_NAME}"]
    }
    assert [r["name"] for r in records] == [
        container_name(SIM_NAME, 1),
        container_name(SIM_NAME, 2),
    ]
    assert all(r["state"] == "running" for r in records)
    assert all(set(r) == {"id", "name", "state", "labels"} for r in records)
    assert records[0]["labels"][LABEL_DEVICE_ID] == "1"


def test_list_swarm_containers_healthy_empty(tmp_path, monkeypatch):
    install_fake_client(monkeypatch, lambda n: [])
    assert _list_swarm_containers(make_config(tmp_path)) == []


# --- SW-04: failure vs empty ---------------------------------------------


def test_list_swarm_containers_raises_docker_error(tmp_path, monkeypatch):
    def fail_client():
        raise RuntimeError("Cannot connect to the Docker daemon")

    monkeypatch.setattr(swarm, "_get_docker_client", fail_client)
    with pytest.raises(DockerError) as excinfo:
        _list_swarm_containers(make_config(tmp_path))
    assert "Docker daemon" in str(excinfo.value)


def test_stop_swarm_aborts_without_cleanup_on_docker_error(tmp_path, monkeypatch):
    def fail_client():
        raise RuntimeError("daemon unreachable")

    monkeypatch.setattr(swarm, "_get_docker_client", fail_client)
    monkeypatch.setattr(swarm, "_inspect_network", boom)
    monkeypatch.setattr(swarm, "_run", boom)

    result = swarm.stop_swarm(make_config(tmp_path))

    assert result["lifecycle_failure"] is True
    assert result["containers_removed"] == 0
    assert result["network_removed"] is False
    assert result["failure_reasons"][0].startswith("container discovery failed")
    assert result["shim_count_after_removal"] is None
    assert result["shim_count_after_grace"] is None


def test_stop_swarm_proceeds_on_genuinely_empty(tmp_path, monkeypatch):
    install_fake_client(monkeypatch, lambda n: [])
    monkeypatch.setattr(swarm, "_inspect_network", lambda net_name: None)  # no network
    monkeypatch.setattr(swarm, "_scan_shims", lambda: [])

    result = swarm.stop_swarm(make_config(tmp_path))

    assert result["lifecycle_failure"] is False
    assert result["containers_removed"] == 0
    assert result["network_removed"] is True
    assert result["failure_reasons"] == []


def test_stop_swarm_removes_containers_then_empty(tmp_path, monkeypatch):
    calls = {"n": 0}

    def responder(n):
        calls["n"] = n
        return [fake_container(1)] if n == 1 else []

    install_fake_client(monkeypatch, responder)

    run_calls = []

    def fake_run(cmd, check=True, timeout=None):
        run_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(swarm, "_run", fake_run)
    monkeypatch.setattr(swarm, "_inspect_network", lambda net_name: None)
    monkeypatch.setattr(swarm, "_scan_shims", lambda: [])

    result = swarm.stop_swarm(make_config(tmp_path))

    assert result["lifecycle_failure"] is False
    assert result["containers_removed"] == 1
    assert result["containers_removed_successfully"] == 1
    assert result["network_removed"] is True
    assert ["docker", "rm", "-f", container_name(SIM_NAME, 1)] in run_calls


def test_start_swarm_rejects_on_docker_error(tmp_path, monkeypatch):
    def fail_client():
        raise RuntimeError("daemon unreachable")

    monkeypatch.setattr(swarm, "check_docker", lambda: True)
    monkeypatch.setattr(swarm, "_get_docker_client", fail_client)

    result = swarm.start_swarm(make_config(tmp_path))

    assert result["rejected"] is True
    assert result["started"] == 0
    assert "Docker state unavailable" in result["error"]


def test_status_swarm_reports_error_on_docker_error(tmp_path, monkeypatch):
    def fail_client():
        raise RuntimeError("daemon unreachable")

    monkeypatch.setattr(swarm, "_get_docker_client", fail_client)

    result = swarm.status_swarm(make_config(tmp_path))

    assert "Docker state unavailable" in result["error"]
    assert result["containers"] == []
    assert result["containers_running"] == 0


# --- status: healthy empty vs running-with-control-API -------------------


def _write_manifest(tmp_path, config, devices):
    import yaml

    from src.resolver import generate_manifest, write_manifest_atomically

    path = (tmp_path / "state" / "resolved-devices.yaml").resolve()
    manifest = generate_manifest(config, devices)
    manifest["config_fingerprint"] = swarm.compute_config_fingerprint(config)
    write_manifest_atomically(manifest, str(path))
    return str(path)


def _device_entry(did, ip):
    return {
        "id": did,
        "name": f"device-{did:03d}",
        "container_name": container_name(SIM_NAME, did),
        "ip": ip,
        "hostname": f"device-{did:03d}",
        "pool": "standard",
        "downtime_profile": None,
        "traffic_profile": None,
        "initial_uptime_profile": "std",
        "initial_uptime_seconds": 0,
        "snmp_identity_profile": "std",
    }


def test_status_swarm_genuinely_empty_marks_devices_missing(tmp_path, monkeypatch):
    config = make_config(tmp_path)
    _write_manifest(tmp_path, config, [_device_entry(d, f"10.0.0.{d}") for d in (1, 2, 3)])
    install_fake_client(monkeypatch, lambda n: [])

    result = swarm.status_swarm(config)

    assert "error" not in result
    assert result["containers_running"] == 0
    assert result["devices_online"] == 0
    rows = {row["device_id"]: row for row in result["containers"]}
    assert set(rows) == {1, 2, 3}
    assert all(row["state"] == "missing" for row in rows.values())
    assert all(row["effective_online"] is False for row in rows.values())


def test_status_swarm_power_from_control_api(tmp_path, monkeypatch):
    """Live control API is authoritative for power state of running devices."""
    config = make_config(tmp_path)
    _write_manifest(tmp_path, config, [_device_entry(1, "127.0.0.1")])

    # Serve the control API on a free local port and point CONTROL_PORT at it.
    server_port = 0

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"device_id": 1, "name": "device-001",
                               "ip": "127.0.0.1", "power": "offline"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(swarm, "CONTROL_PORT", server_port)
    try:
        def fake_run(cmd, check=True, timeout=None):
            # Ping the (loopback) device: reachable.
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        monkeypatch.setattr(swarm, "_run", fake_run)
        install_fake_client(monkeypatch, lambda n: [fake_container(1)])

        result = swarm.status_swarm(config)
    finally:
        server.shutdown()
        server.server_close()

    row = result["containers"][0]
    assert row["state"] == "running"
    assert row["ping"] == "reachable"
    assert row["power_state"] == "offline"
    assert row["effective_online"] is False


# --- _query_device_power ---------------------------------------------------


def _serve_power(port_holder, payload):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port_holder["port"] = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"power": "online"}, "online"),
        ({"power": "offline"}, "offline"),
        ({"power": "weird"}, "unknown"),
        ({}, "unknown"),
    ],
)
def test_query_device_power_payloads(payload, expected):
    holder = {}
    server, thread = _serve_power(holder, payload)
    try:
        assert _query_device_power("127.0.0.1", port=holder["port"], timeout=2.0) == expected
    finally:
        server.shutdown()
        server.server_close()


def test_query_device_power_unreachable_returns_unknown():
    # Nothing is listening on this port; the call must return "unknown",
    # never raise, so status output degrades gracefully.
    holder = {}
    server, _ = _serve_power(holder, {"power": "online"})
    port = holder["port"]
    server.shutdown()
    server.server_close()
    assert _query_device_power("127.0.0.1", port=port, timeout=0.5) == "unknown"
