"""Per-device HTTP control API and power-state behavior (agent side).

Covers:
- PowerController transition semantics (manual OFF/ON, idempotency, the
  manual AND scheduler online model, uptime/counter reset exactly once).
- ControlHTTPServer endpoints: /status, /power/on, /power/off, 404, 405.
- Peer authorization rules (_is_authorized).
- SNMP is silent while OFF and recovers after ON (application-level drop).
- SIGTERM/SIGINT cause a graceful exit (code 0); SIGUSR1/SIGUSR2 are no
  longer power mechanisms (default disposition: the process terminates).
- Concurrency: parallel manual power-on resets state exactly once.
"""
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

import agent.agent as agent
from agent.agent import (
    CONTROL_PORT,
    ControlHTTPServer,
    PowerController,
    SNMPDataStore,
    SNMPUDPHandler,
    _is_authorized,
    run_main_loop,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# --- helpers -----------------------------------------------------------------


class FakeData:
    """Minimal stand-in for SNMPDataStore (no real uptime/counter objects)."""

    def __init__(self):
        self.is_online = True
        self.outage_scheduler = None
        self.power_on_calls = 0
        self.power_off_calls = 0
        self.uptime_tracker = None  # power_on must not touch it via the fake

    def power_off(self):
        if not self.is_online:
            return False
        self.is_online = False
        self.power_off_calls += 1
        return True

    def power_on(self):
        if self.is_online:
            return False
        self.is_online = True
        self.power_on_calls += 1
        return True


class FakeScheduler:
    def __init__(self):
        self.is_online = True


def make_controller(monkeypatch, data=None,
                    block_result=True, unblock_result=True):
    """Build a PowerController with network blocking stubbed out.

    `_block_network`/`_unblock_network` are instance methods; patching them
    on the class with a plain function replaces them process-wide for the
    test's lifetime (monkeypatch restores on teardown).
    """
    data = data or FakeData()
    controller = PowerController(data, 7, "device-007", "127.0.0.1", "powertest")
    block_calls = []
    unblock_calls = []
    monkeypatch.setattr(PowerController, "_block_network",
                        lambda self: block_calls.append(1) or block_result)
    monkeypatch.setattr(PowerController, "_unblock_network",
                        lambda self: unblock_calls.append(1) or unblock_result)
    return controller, data, block_calls, unblock_calls


def free_udp_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def http_json(url, method="GET", timeout=5.0):
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


def http_json_allow_error(url, method="GET", timeout=5.0):
    """Like http_json, but returns (status, payload) on HTTP errors too."""
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


# --- PowerController transition semantics -------------------------------------


def test_manual_off_then_on_roundtrip(monkeypatch):
    controller, data, block_calls, unblock_calls = make_controller(monkeypatch)

    assert controller.status()["power"] == "online"
    assert controller.manual_power_off()["power"] == "offline"
    assert data.is_online is False
    assert data.power_off_calls == 1
    assert block_calls == [1]

    assert controller.manual_power_on()["power"] == "online"
    assert data.is_online is True
    assert data.power_on_calls == 1  # reset exactly once
    assert unblock_calls == [1]


def test_repeated_commands_are_idempotent(monkeypatch):
    controller, data, block_calls, unblock_calls = make_controller(monkeypatch)

    controller.manual_power_off()
    controller.manual_power_off()
    assert data.power_off_calls == 1
    assert len(block_calls) == 1

    controller.manual_power_on()
    controller.manual_power_on()
    assert data.power_on_calls == 1
    assert len(unblock_calls) == 1


def test_manual_off_persists_across_scheduled_outage(monkeypatch):
    controller, data, _, _ = make_controller(monkeypatch)
    data.outage_scheduler = FakeScheduler()

    controller.manual_power_off()
    assert controller.status()["power"] == "offline"

    # Scheduled outage starts: the scheduler flips offline first, then the
    # main loop dispatches the event. No-op here (already offline), no 2nd reset.
    data.outage_scheduler.is_online = False
    controller.scheduler_power_off()
    assert data.power_off_calls == 1
    # Scheduled outage ends: must NOT bring a manually-off device back up.
    data.outage_scheduler.is_online = True
    controller.scheduler_power_on()
    assert data.power_on_calls == 0
    assert controller.status()["power"] == "offline"

    # Only a manual ON restores the device.
    assert controller.manual_power_on()["power"] == "online"
    assert data.power_on_calls == 1


def test_manual_on_during_outage_waits_for_scheduler(monkeypatch):
    controller, data, _, _ = make_controller(monkeypatch)
    data.outage_scheduler = FakeScheduler()

    data.outage_scheduler.is_online = False  # outage began
    controller.scheduler_power_off()
    assert controller.status()["power"] == "offline"

    # Manual ON during the outage: stays offline until the outage ends.
    assert controller.manual_power_on()["power"] == "offline"
    assert data.power_on_calls == 0

    data.outage_scheduler.is_online = True  # outage ended
    controller.scheduler_power_on()
    assert controller.status()["power"] == "online"
    assert data.power_on_calls == 1


def test_scheduler_events_alone_roundtrip(monkeypatch):
    controller, data, block_calls, unblock_calls = make_controller(monkeypatch)

    controller.scheduler_power_off()
    assert data.is_online is False
    assert block_calls == [1]
    controller.scheduler_power_on()
    assert data.is_online is True
    assert unblock_calls == [1]
    # No double transitions.
    controller.scheduler_power_off()
    controller.scheduler_power_off()
    assert data.power_off_calls == 2


def test_concurrent_power_on_resets_exactly_once(monkeypatch):
    controller, data, _, _ = make_controller(monkeypatch)
    data.is_online = False  # device starts offline (e.g. fresh manual off path)
    controller.manual_power_off()

    errors = []

    def worker():
        try:
            controller.manual_power_on()
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert data.power_on_calls == 1
    assert controller.status()["power"] == "online"


# --- ControlHTTPServer endpoints ----------------------------------------------


@pytest.fixture()
def control_server(monkeypatch):
    data = SNMPDataStore({"device_id": 42, "simulation_name": "http-test",
                          "ip": "127.0.0.1", "community": "public"})
    controller = PowerController(data, 42, "device-042", "127.0.0.1", "http-test")
    monkeypatch.setattr(PowerController, "_block_network", lambda self: True)
    monkeypatch.setattr(PowerController, "_unblock_network", lambda self: True)
    server = ControlHTTPServer(controller, management_subnet="127.0.0.0/8")
    server.start(port=0)  # ephemeral port
    port = server._server.server_address[1]
    yield data, controller, f"http://127.0.0.1:{port}"
    server.stop()


def test_status_endpoint(control_server):
    _, _, base = control_server
    code, payload = http_json(f"{base}/status")
    assert code == 200
    assert payload == {"device_id": 42, "name": "device-042",
                       "ip": "127.0.0.1", "power": "online"}


def test_power_off_then_on_via_http(control_server):
    data, _, base = control_server
    code, payload = http_json(f"{base}/power/off", method="POST")
    assert code == 200
    assert payload["power"] == "offline"
    assert data.is_online is False
    _, payload = http_json(f"{base}/status")
    assert payload["power"] == "offline"

    code, payload = http_json(f"{base}/power/on", method="POST")
    assert code == 200
    assert payload["power"] == "online"
    assert data.is_online is True


def test_http_method_and_path_errors(control_server):
    _, _, base = control_server

    with pytest.raises(urllib.error.HTTPError) as exc:
        http_json(f"{base}/power/on")  # GET on a POST endpoint
    assert exc.value.code == 405

    with pytest.raises(urllib.error.HTTPError) as exc:
        http_json(f"{base}/status", method="POST")
    assert exc.value.code == 405

    with pytest.raises(urllib.error.HTTPError) as exc:
        http_json(f"{base}/nope")
    assert exc.value.code == 404

    with pytest.raises(urllib.error.HTTPError) as exc:
        http_json(f"{base}/power/reboot", method="POST")
    assert exc.value.code == 404


# --- Firewall failure atomicity (regression) ----------------------------------
#
# A failed firewall operation must never be reported as a successful power
# transition: the power state stays unchanged and the failure is surfaced
# (status "error" key / HTTP 500).


def test_block_failure_keeps_device_online_and_reports_error(monkeypatch):
    controller, data, _, _ = make_controller(monkeypatch, block_result=False)

    res = controller.manual_power_off()
    assert res["power"] == "online"          # state unchanged
    assert "error" in res                    # failure reported
    assert data.is_online is True
    assert data.power_off_calls == 0

    # Scheduler events must not bypass the failed firewall either.
    controller.scheduler_power_off()
    assert data.is_online is True
    assert data.power_off_calls == 0
    assert controller.status()["power"] == "online"


def test_unblock_failure_keeps_device_offline_and_reports_error(monkeypatch):
    controller, data, _, unblock_calls = make_controller(monkeypatch)

    assert controller.manual_power_off()["power"] == "offline"
    assert data.is_online is False

    # Unblock now fails: power-on must abort without touching state.
    monkeypatch.setattr(PowerController, "_unblock_network",
                        lambda self: unblock_calls.append(1) or False)
    res = controller.manual_power_on()
    assert res["power"] == "offline"         # state unchanged
    assert "error" in res                    # failure reported
    assert data.is_online is False
    assert data.power_on_calls == 0

    controller.scheduler_power_on()
    assert data.is_online is False
    assert data.power_on_calls == 0


@pytest.fixture()
def failing_control_server(monkeypatch):
    """Control server with independently switchable firewall results.

    The stubs sleep briefly so concurrent HTTP requests overlap on the
    server side (wider race window for the concurrency test).
    """
    data = SNMPDataStore({"device_id": 43, "simulation_name": "http-fail-test",
                          "ip": "127.0.0.1", "community": "public"})
    controller = PowerController(data, 43, "device-043", "127.0.0.1", "http-fail-test")
    results = {"block": True, "unblock": True}
    counts = {"on": 0, "off": 0}

    def _block(self):
        time.sleep(0.02)
        return results["block"]

    def _unblock(self):
        time.sleep(0.02)
        return results["unblock"]

    real_power_on = data.power_on
    real_power_off = data.power_off

    def counting_power_on():
        counts["on"] += 1
        return real_power_on()

    def counting_power_off():
        counts["off"] += 1
        return real_power_off()

    monkeypatch.setattr(PowerController, "_block_network", _block)
    monkeypatch.setattr(PowerController, "_unblock_network", _unblock)
    monkeypatch.setattr(data, "power_on", counting_power_on)
    monkeypatch.setattr(data, "power_off", counting_power_off)
    server = ControlHTTPServer(controller, management_subnet="127.0.0.0/8")
    server.start(port=0)  # ephemeral port
    port = server._server.server_address[1]
    yield data, controller, f"http://127.0.0.1:{port}", results, counts
    server.stop()


def test_http_500_when_firewall_block_fails(failing_control_server):
    data, _, base, results, counts = failing_control_server
    results["block"] = False

    code, payload = http_json_allow_error(f"{base}/power/off", method="POST")
    assert code == 500                       # never reported as success
    assert "error" in payload
    assert payload["power"] == "online"      # device still online
    assert data.is_online is True

    code, payload = http_json(f"{base}/status")
    assert code == 200
    assert payload["power"] == "online"
    assert counts["off"] == 0                # no state transition happened


def test_http_500_when_firewall_unblock_fails(failing_control_server):
    data, _, base, results, counts = failing_control_server

    code, payload = http_json(f"{base}/power/off", method="POST")
    assert code == 200
    assert payload["power"] == "offline"
    assert counts["off"] == 1

    results["unblock"] = False
    code, payload = http_json_allow_error(f"{base}/power/on", method="POST")
    assert code == 500
    assert "error" in payload
    assert payload["power"] == "offline"     # device stays offline
    assert data.is_online is False
    assert counts["on"] == 0                 # no uptime/counter reset


def test_concurrent_http_power_off_requests(failing_control_server):
    data, _, base, _, counts = failing_control_server
    box = []
    box_lock = threading.Lock()

    def worker():
        code, payload = http_json_allow_error(f"{base}/power/off", method="POST")
        with box_lock:
            box.append((code, payload))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every concurrent request gets a valid answer; exactly one transition.
    assert all(code == 200 for code, _ in box)
    assert all(p["power"] == "offline" for _, p in box)
    assert data.is_online is False
    assert counts["off"] == 1


# --- _is_authorized -----------------------------------------------------------


@pytest.mark.parametrize(
    "peer,subnet,expected",
    [
        ("127.0.0.1", None, True),        # loopback always allowed
        ("::1", None, True),
        ("10.0.0.5", "10.0.0.0/24", True),
        ("10.0.1.5", "10.0.0.0/24", False),
        ("10.0.0.5", None, False),        # no subnet -> loopback only
        ("not-an-ip", "10.0.0.0/24", False),
        ("10.0.0.5", "bogus", False),     # invalid subnet -> deny
        ("192.168.7.9", "192.168.0.0/16", True),
    ],
)
def test_is_authorized(peer, subnet, expected):
    assert _is_authorized(peer, subnet) is expected


# --- SNMP silent while OFF, recovers after ON ----------------------------------


def build_get_request(community: str, request_id: int, oids: list[tuple]) -> bytes:
    pdu_content = (
        b"\x02\x04" + request_id.to_bytes(4, "big")
        + b"\x02\x04" + (0).to_bytes(4, "big")
        + b"\x02\x04" + (0).to_bytes(4, "big")
        + agent.encode_varbind_list([(oid, "OctetString", b"") for oid in oids])
    )
    pdu = agent.encode_tlv(0xA0, pdu_content)
    body = b"\x02\x01\x01" + agent.encode_tlv(0x04, community.encode()) + pdu
    return agent.encode_tlv(0x30, body)


def send_snmp(port: int, community: str, request_id: int, timeout: float):
    payload = build_get_request(community, request_id, [SNMPDataStore.SYS_NAME_OID])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(payload, ("127.0.0.1", port))
        return sock.recvfrom(65535)[0]
    except socket.timeout:
        return None
    finally:
        sock.close()


def test_snmp_silent_while_off_recovers_after_on(monkeypatch):
    data = SNMPDataStore({"device_id": 7, "simulation_name": "snmptest",
                          "ip": "127.0.0.1", "community": "public"})
    controller = PowerController(data, 7, "device-007", "127.0.0.1", "snmptest")
    monkeypatch.setattr(PowerController, "_block_network", lambda self: True)
    monkeypatch.setattr(PowerController, "_unblock_network", lambda self: True)

    port = free_udp_port()
    handler = SNMPUDPHandler(data, port=port)
    handler.start()
    try:
        # Online: responds with sysName.
        reply = send_snmp(port, "public", 1, timeout=3.0)
        assert reply is not None
        assert b"device-007" in reply

        # OFF: the agent drops SNMP packets (no reply).
        controller.manual_power_off()
        assert send_snmp(port, "public", 2, timeout=1.0) is None
        assert send_snmp(port, "public", 3, timeout=1.0) is None

        # ON: responds again.
        controller.manual_power_on()
        reply = send_snmp(port, "public", 4, timeout=3.0)
        assert reply is not None
        assert b"device-007" in reply
    finally:
        handler.stop()


# --- SIGTERM/SIGINT graceful shutdown; SIGUSR* no longer used -------------------


def _agent_config_path(tmp_path, snmp_port, control_port):
    path = tmp_path / "agent-config.json"
    path.write_text(json.dumps({
        "device_id": 1,
        "simulation_name": "sigtest",
        "ip": "127.0.0.1",
        "snmp_port": snmp_port,
        "control_port": control_port,
        "management_subnet": "127.0.0.0/8",
    }))
    return path


def _wait_for_control(port, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, payload = http_json(f"http://127.0.0.1:{port}/status", timeout=1.0)
            if code == 200:
                return payload
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.2)
    raise AssertionError("control API never became reachable")


FAKE_IPTABLES = """#!/bin/sh
# Hermetic iptables stub: the test user cannot touch the host netns, so the
# subprocess tests run the agent against a stub that records every
# invocation (one per line) to $IPTABLES_LOG and pretends to succeed.
if [ -n "${IPTABLES_LOG:-}" ]; then
    printf '%s\\n' "$*" >> "$IPTABLES_LOG"
fi
exit 0
"""

FAKE_IPTABLES_FAIL = """#!/bin/sh
# Hermetic iptables stub that always fails with rc __RC__ (recording the
# invocation first, like the success stub).
if [ -n "${IPTABLES_LOG:-}" ]; then
    printf '%s\\n' "$*" >> "$IPTABLES_LOG"
fi
exit __RC__
"""


def _iptables_log_path(tmp_path):
    return tmp_path / "iptables-calls.log"


def _run_agent(tmp_path, snmp_port, control_port, write_config=True,
               iptables_rc=0, iptables_log=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "iptables"
    if iptables_rc == 0:
        stub.write_text(FAKE_IPTABLES)
    else:
        stub.write_text(FAKE_IPTABLES_FAIL.replace("__RC__", str(iptables_rc)))
    stub.chmod(0o755)
    iptables_log = iptables_log or _iptables_log_path(tmp_path)
    if iptables_log.exists():
        iptables_log.unlink()
    config_path = tmp_path / "agent-config.json"
    if write_config:
        config_path = _agent_config_path(tmp_path, snmp_port, control_port)
    env = dict(
        os.environ,
        AGENT_CONFIG=str(config_path),
        PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        IPTABLES_LOG=str(iptables_log),
    )
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "agent", "agent.py")],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc, iptables_log


def _iptables_calls(iptables_log):
    if not iptables_log.exists():
        return []
    return [line for line in iptables_log.read_text().splitlines() if line]


@pytest.mark.parametrize("signum,expected", [
    (signal.SIGTERM, "SIGTERM"),
    (signal.SIGINT, "SIGINT"),
])
def test_graceful_shutdown_on_sigterm_sigint(tmp_path, signum, expected):
    control_port = free_udp_port()
    proc, iptables_log = _run_agent(tmp_path, free_udp_port(), control_port)
    try:
        payload = _wait_for_control(control_port)
        assert payload["power"] == "online"
        proc.send_signal(signum)
        out, _ = proc.communicate(timeout=20)
        assert proc.returncode == 0, out
        assert f"Received {expected}" in out
        assert "Agent stopped" in out
        # Termination must clean up every SNMPSwarm-owned chain: flush,
        # remove the jump rule, delete the chain (per direction).
        calls = _iptables_calls(iptables_log)
        for chain, builtin in (("SNMPSWARM_IN", "INPUT"),
                               ("SNMPSWARM_OUT", "OUTPUT")):
            assert f"-w -F {chain}" in calls
            assert f"-w -D {builtin} -j {chain}" in calls
            assert f"-w -X {chain}" in calls
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def test_sigusr1_is_no_longer_a_power_mechanism(tmp_path):
    """Old design used SIGUSR1/SIGUSR2 for power off/on. Those handlers are
    gone: the default disposition applies and the process terminates. A
    SIGUSR1 to a running agent must therefore NOT keep it alive in a
    powered-off state."""
    snmp_port = free_udp_port()
    control_port = free_udp_port()
    proc, _ = _run_agent(tmp_path, snmp_port, control_port)
    try:
        _wait_for_control(control_port)
        proc.send_signal(signal.SIGUSR1)
        proc.wait(timeout=10)
        assert proc.returncode != 0, "SIGUSR1 must not be handled as power-off"
        out = proc.stdout.read()
        assert "Powering OFF" not in out
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


# --- Fatal init failures (regression) -----------------------------------------
#
# A missing config, a control API bind failure, or a firewall preflight
# failure must all exit non-zero with a clear message: the device must never
# run as a partially functional component.


def test_missing_config_exits_fatal(tmp_path):
    proc, _ = _run_agent(tmp_path, free_udp_port(), free_udp_port(),
                         write_config=False)
    out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 1, out
    assert "config not found" in out.lower()


def test_control_api_bind_failure_exits_fatal(tmp_path):
    control_port = free_udp_port()
    first, _ = _run_agent(tmp_path, free_udp_port(), control_port)
    try:
        _wait_for_control(control_port)
        # Second agent on the same control port: bind must fail fatally.
        second, _ = _run_agent(tmp_path, free_udp_port(), control_port)
        out, _ = second.communicate(timeout=20)
        assert second.returncode == 1, out
        assert "control API startup failure" in out
        # The first agent must still be healthy.
        code, payload = http_json(f"http://127.0.0.1:{control_port}/status")
        assert code == 200
        assert payload["power"] == "online"
    finally:
        if first.poll() is None:
            first.terminate()
            first.communicate(timeout=20)


def test_firewall_preflight_failure_exits_fatal(tmp_path):
    proc, iptables_log = _run_agent(tmp_path, free_udp_port(), free_udp_port(),
                                    iptables_rc=4)
    out, _ = proc.communicate(timeout=20)
    assert proc.returncode == 1, out
    assert "firewall preflight failed" in out
    # The preflight actually attempted the SNMPSwarm-owned chains.
    assert any("SNMPSWARM_IN" in c for c in _iptables_calls(iptables_log))


# --- Wedged (not dead) mandatory worker (regression) ----------------------------
#
# A worker thread the kernel stops (SIGSTOP to its TID) or that hangs forever
# keeps Thread.is_alive() True; only the stale heartbeat can catch it. The
# fatal path must then run end to end: "stopped responding" log + exit 1.


def _find_tid_by_comm(pid, comm):
    task_dir = f"/proc/{pid}/task"
    try:
        entries = os.listdir(task_dir)
    except OSError:
        return None
    for tid in entries:
        try:
            with open(f"{task_dir}/{tid}/comm") as f:
                if f.read().strip() == comm:
                    return int(tid)
        except OSError:
            continue
    return None


def _ptrace_attach(tid):
    """Stop *tid* alone (tracing stop) without affecting sibling threads.

    SIGSTOP to a TID is not usable for this: the kernel performs a *group
    stop*, freezing every thread in the process (verified empirically), so
    the main loop can never observe the stale heartbeat. PTRACE_ATTACH
    produces a per-thread tracing stop: the target thread runs no more
    code while siblings (and the GIL) are unaffected.
    """
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.ptrace(16, tid, 0, 0) != 0:  # PTRACE_ATTACH
        return False
    status = ctypes.c_int()
    libc.waitpid(tid, ctypes.byref(status), 0)
    return (status.value & 0x7F) == 0x7F  # WIFSTOPPED


@pytest.mark.skipif(not os.path.exists("/proc/self"),
                    reason="requires /proc (Linux)")
def test_wedged_control_thread_exits_fatal(tmp_path):
    """Wedge the control-http thread and assert the real fatal path:
    stale heartbeat -> 'Control API thread stopped responding' -> exit 1.

    A wedged thread keeps Thread.is_alive() True and never beats its
    heartbeat again; only the stale heartbeat can catch it. The thread is
    wedged with a per-thread ptrace stop (see _ptrace_attach).
    """
    snmp_port = free_udp_port()
    control_port = free_udp_port()
    proc, _ = _run_agent(tmp_path, snmp_port, control_port)
    try:
        _wait_for_control(control_port)
        tid = _find_tid_by_comm(proc.pid, "control-http")
        assert tid is not None, "control-http TID not found"
        assert _ptrace_attach(tid), "PTRACE_ATTACH on control-http failed"
        # Detection takes ~HEARTBEAT_TIMEOUT plus one 0.5s main-loop poll;
        # the fatal path then includes the ~5s bounded control.stop() wait
        # (the wedged serve thread never processes the shutdown request).
        #
        # Tracer protocol: this process is the tracer of the control TID,
        # so it must reap the TID's death status itself. Until it does,
        # the agent's waitpid() for that thread never returns and the agent
        # cannot observe the exit -- even after the process is otherwise
        # dead, its exit status stays pending. Reap nonblocking
        # (WNOHANG=1) while polling the process.
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        status = ctypes.c_int()
        deadline = time.monotonic() + 30
        while proc.poll() is None:
            if time.monotonic() > deadline:
                # Diagnostic path: release the tracer first (PTRACE_DETACH
                # + SIGCONT) so that the TID's death status is delivered to
                # the agent again instead of to us; otherwise a killed agent
                # would stay un-reapable and hang the suite.
                libc.ptrace(17, tid, 0, 18)  # PTRACE_DETACH, SIGCONT
                proc.kill()
                proc.wait(timeout=5)
                pytest.fail("Agent did not exit after the control thread wedged")
            libc.waitpid(tid, ctypes.byref(status), 1)  # WNOHANG
            time.sleep(0.2)
        rc = proc.wait(timeout=5)
        out = proc.stdout.read()
        assert rc == 1, out
        assert "Control API thread stopped responding" in out, out
        assert "exiting with failure" in out, out
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


# --- Post-startup mandatory-component failure detection (regression) ----------
#
# If the control API or SNMP thread dies after startup, the main loop must
# detect it and the process must exit non-zero (container not healthy).


def _start_real_components(monkeypatch, device_id=56):
    data = SNMPDataStore({"device_id": device_id, "simulation_name": "loop-test",
                          "ip": "127.0.0.1", "community": "public"})
    controller = PowerController(data, device_id, f"device-{device_id:03d}",
                                 "127.0.0.1", "loop-test")
    monkeypatch.setattr(PowerController, "_block_network", lambda self: True)
    monkeypatch.setattr(PowerController, "_unblock_network", lambda self: True)
    snmp = SNMPUDPHandler(data, port=free_udp_port())
    snmp.start()
    control = ControlHTTPServer(controller, management_subnet="127.0.0.0/8")
    control.start(port=0)
    return data, controller, snmp, control


def _start_main_loop(data, controller, snmp, control, stop_event):
    box = {}

    def run():
        box["err"] = run_main_loop(stop_event, data, controller, control, snmp)

    t = threading.Thread(target=run)
    t.start()
    return t, box


def test_is_alive_lifecycle():
    data = SNMPDataStore({"device_id": 55, "simulation_name": "alive-test",
                          "ip": "127.0.0.1", "community": "public"})
    snmp = SNMPUDPHandler(data, port=free_udp_port())
    assert not snmp.is_alive()
    snmp.start()
    assert snmp.is_alive()
    snmp.stop()
    snmp._thread.join(timeout=5)
    assert not snmp.is_alive()

    controller = PowerController(data, 55, "device-055", "127.0.0.1", "alive-test")
    control = ControlHTTPServer(controller, management_subnet=None)
    assert not control.is_alive()
    control.start(port=0)
    assert control.is_alive()
    control.stop()
    assert not control.is_alive()


def test_main_loop_detects_control_thread_death(monkeypatch):
    data, controller, snmp, control = _start_real_components(monkeypatch)
    stop_event = threading.Event()
    try:
        t, box = _start_main_loop(data, controller, snmp, control, stop_event)
        time.sleep(1.0)
        assert t.is_alive()  # loop is running while both components are alive
        control.stop()  # serve_forever returns -> control thread dies
        t.join(timeout=10)
        stop_event.set()
        assert not t.is_alive(), "loop must exit when a mandatory thread dies"
        assert box["err"] == "Control API thread terminated unexpectedly"
    finally:
        stop_event.set()
        snmp.stop()
        if control._server is not None:
            control.stop()


def test_main_loop_detects_snmp_thread_death(monkeypatch):
    data, controller, snmp, control = _start_real_components(monkeypatch,
                                                             device_id=57)
    stop_event = threading.Event()
    try:
        t, box = _start_main_loop(data, controller, snmp, control, stop_event)
        time.sleep(1.0)
        assert t.is_alive()
        snmp.stop()  # receive loop exits -> SNMP thread dies
        t.join(timeout=10)
        stop_event.set()
        assert not t.is_alive(), "loop must exit when a mandatory thread dies"
        assert box["err"] == "SNMP thread terminated unexpectedly"
    finally:
        stop_event.set()
        control.stop()


def test_main_loop_detects_wedged_thread(monkeypatch):
    """A worker that is alive but no longer beats its heartbeat (wedged:
    stuck in C code, SIGSTOP'd, ...) must be reported fatal via the stale
    heartbeat, even though Thread.is_alive() keeps returning True."""
    data, controller, snmp, control = _start_real_components(monkeypatch,
                                                              device_id=59)
    stop_event = threading.Event()
    try:
        t, box = _start_main_loop(data, controller, snmp, control, stop_event)
        time.sleep(1.0)
        assert t.is_alive()
        # Wedge the control worker: the Thread object stays alive, but no
        # heartbeat ever beats again (a stopped thread runs no more Python).
        # Backdating the stamp simulates the observable state after
        # HEARTBEAT_TIMEOUT of silence without waiting it out.
        control._heartbeat.beat = lambda: None
        control._heartbeat._stamp = time.monotonic() - agent.HEARTBEAT_TIMEOUT - 1
        t.join(timeout=10)
        stop_event.set()
        assert not t.is_alive(), "loop must exit when a mandatory thread stops responding"
        assert control.is_alive(), (
            "wedged thread must still claim to be alive (the is_alive blind spot)")
        assert box["err"] == "Control API thread stopped responding"
    finally:
        stop_event.set()
        snmp.stop()
        if control._server is not None:
            control.stop()


def test_main_loop_clean_stop_returns_none(monkeypatch):
    data, controller, snmp, control = _start_real_components(monkeypatch,
                                                             device_id=58)
    stop_event = threading.Event()
    try:
        t, box = _start_main_loop(data, controller, snmp, control, stop_event)
        time.sleep(0.6)
        stop_event.set()
        t.join(timeout=10)
        assert not t.is_alive()
        assert box["err"] is None
    finally:
        snmp.stop()
        control.stop()
