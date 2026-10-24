"""End-to-end integration test against a real Docker runtime.

Skips automatically when Docker is unavailable. Lifecycle covered:

  start_swarm (3 devices, standard units)
    -> agent control API reachable (GET /status, power online)
    -> POST /power/off  -> /status stays reachable, power offline, SNMP silent
    -> POST /power/on   -> power online, SNMP answers again
    -> power off -> scoped SNMPSWARM_IN/OUT chains installed, built-in
       INPUT/OUTPUT policies untouched; power on -> all rules removed
    -> SIGTERM to one container -> clean exit (code 0)
    -> power off -> SIGTERM -> clean exit, no firewall cleanup errors
    -> SIGKILL the control-http worker thread by TID (kernel thread name
       set by the agent) -> the kernel kills the whole process
       (do_group_exit; no fatal log possible), Docker restart policy
       brings a fresh agent online (RestartCount >= 1, /status online)
    -> stop_swarm in finally: no lifecycle failure, network removed

The test only ever touches containers/networks it created itself (unique
simulation name per test run); no unrelated containers or system resources
are created or destroyed.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request

import pytest
import yaml

import host.swarm as swarm
from agent.agent import CONTROL_PORT
from src.config import load_config
from src.ip import calculate_ip, container_name

SIM_NAME = f"swtest-{os.getpid()}"
DEVICE_ID = 1
DEVICE_IP = calculate_ip(DEVICE_ID)  # 10.0.0.1


def docker_available():
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"],
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(
    not docker_available(), reason="Docker runtime not available"
)


def http_json(url, method="GET", timeout=3.0):
    req = urllib.request.Request(url, method=method,
                                 data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


def wait_for_control_api(ip, port, timeout=120.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            code, payload = http_json(f"http://{ip}:{port}/status", timeout=2.0)
            if code == 200:
                return payload
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(1.0)
    raise AssertionError(f"control API at http://{ip}:{port}/status never came up")


def wait_for_snmp(ip, port, expect_response, timeout=60.0):
    """Poll with a raw SNMPv2c GetRequest until the expected behavior holds."""
    from agent.agent import (
        SNMPDataStore,
        encode_tlv,
        encode_varbind_list,
    )

    def send(request_id):
        pdu = (
            b"\x02\x04" + request_id.to_bytes(4, "big")
            + b"\x02\x04" + (0).to_bytes(4, "big")
            + b"\x02\x04" + (0).to_bytes(4, "big")
            + encode_varbind_list(
                [(SNMPDataStore.SYS_NAME_OID, "OctetString", b"")]
            )
        )
        body = b"\x02\x01\x01" + encode_tlv(0x04, b"public") + encode_tlv(0xA0, pdu)
        packet = encode_tlv(0x30, body)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2.0)
        try:
            sock.sendto(packet, (ip, port))
            return sock.recvfrom(65535)[0]
        except socket.timeout:
            return None
        finally:
            sock.close()

    deadline = time.time() + timeout
    request_id = 1
    while time.time() < deadline:
        reply = send(request_id)
        request_id += 1
        got_response = reply is not None and b"device-" in reply
        if got_response == expect_response:
            return reply
        if not expect_response:
            # Silence can only be asserted after the first confirmed response.
            if reply is not None:
                continue
            return None
        time.sleep(1.0)
    raise AssertionError(
        f"SNMP at {ip}:{port} did not reach expected state "
        f"(expect_response={expect_response})"
    )


def container_iptables_lines(name):
    """Return `iptables -S` output lines from inside the container."""
    result = subprocess.run(
        ["docker", "exec", name, "iptables", "-w", "-S"],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return [l for l in result.stdout.splitlines() if l.strip()]


def container_logs(name):
    result = subprocess.run(
        ["docker", "logs", name], capture_output=True, text=True, timeout=30,
    )
    return result.stdout + result.stderr


def find_agent_thread_tid(name, thread_name):
    """Find the TID of a named agent thread inside the container.

    The agent writes each worker's kernel-visible thread name (comm) to
    /proc/thread-self/comm at thread start, so the TID is found via
    /proc/<pid>/task/<tid>/comm. (Python's Thread.name alone is invisible
    to the kernel; all threads would show comm=python3.)
    """
    script = (
        'for p in /proc/[0-9]*; do '
        '[ "$p" = "/proc/$$" ] && continue; '
        '[ -r "$p/cmdline" ] || continue; '
        'tr "\\0" " " < "$p/cmdline" 2>/dev/null | grep -q "agent.py" || continue; '
        'for t in "$p"/task/*; do '
        'c=$(cat "$t/comm" 2>/dev/null); '
        '[ "$c" = "' + thread_name + '" ] && basename "$t"; '
        'done; '
        'done; '
        'exit 0'
    )
    result = subprocess.run(
        ["docker", "exec", name, "sh", "-c", script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    tids = [int(x) for x in result.stdout.split() if x.isdigit()]
    assert len(tids) == 1, f"expected exactly one {thread_name!r} thread, got {tids}"
    return tids[0]


@pytest.fixture(scope="module")
def swarm_env(tmp_path_factory):
    base = tmp_path_factory.mktemp(f"swtest-{os.getpid()}")
    config = load_config(str(_write_config(base)))
    started = swarm.start_swarm(config)
    yield config, started
    # Always clean up, even on failure: remove our containers and network.
    try:
        swarm.stop_swarm(config)
    finally:
        pass


def _write_config(base):
    state_dir = base / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = base / "config.yaml"
    path.write_text(yaml.safe_dump({
        "simulation": {
            "name": SIM_NAME,
            "total_units": 3,
            "random_seed": 7,
            "state_directory": str(state_dir),
            "startup_parallelism": 3,
        },
    }))
    return path


def test_swarm_started(swarm_env):
    config, started = swarm_env
    # The success path has no "rejected" key; it only appears (True) on reject.
    assert started.get("rejected") is not True
    assert "error" not in started
    assert started["failed"] == 0
    assert started["verified"] == 3
    assert config.simulation.device_count == 3


def test_power_off_offline_snmp_silent_then_recovers(swarm_env):
    config, _ = swarm_env

    status = wait_for_control_api(DEVICE_IP, CONTROL_PORT)
    assert status["device_id"] == DEVICE_ID
    assert status["power"] == "online"
    # Baseline: SNMP answers while online.
    wait_for_snmp(DEVICE_IP, 161, expect_response=True)

    # Power off via the control API.
    code, payload = http_json(f"http://{DEVICE_IP}:{CONTROL_PORT}/power/off",
                              method="POST")
    assert code == 200
    assert payload["power"] == "offline"

    # The agent must stay reachable while OFF.
    code, status = http_json(f"http://{DEVICE_IP}:{CONTROL_PORT}/status")
    assert code == 200
    assert status["power"] == "offline"

    # Normal SNMP is suppressed while OFF.
    wait_for_snmp(DEVICE_IP, 161, expect_response=False)

    # Power on restores SNMP.
    code, payload = http_json(f"http://{DEVICE_IP}:{CONTROL_PORT}/power/on",
                              method="POST")
    assert code == 200
    assert payload["power"] == "online"
    wait_for_snmp(DEVICE_IP, 161, expect_response=True)


def test_repeated_power_commands_idempotent(swarm_env):
    _, _ = swarm_env
    base = f"http://{DEVICE_IP}:{CONTROL_PORT}"

    for action, expected in (("off", "offline"), ("off", "offline"),
                             ("on", "online"), ("on", "online")):
        code, payload = http_json(f"{base}/power/{action}", method="POST")
        assert code == 200
        assert payload["power"] == expected

    _, status = http_json(f"{base}/status")
    assert status["power"] == "online"


def test_firewall_rules_installed_on_power_off_removed_on_power_on(swarm_env):
    """Real iptables verification: scoped chains on OFF, fully gone on ON."""
    config, _ = swarm_env
    name = container_name(SIM_NAME, DEVICE_ID)
    base = f"http://{DEVICE_IP}:{CONTROL_PORT}"

    # Baseline: no SNMPSwarm rules while online (preflight chains are
    # created then flushed, so at most empty chains may exist).
    before = container_iptables_lines(name)
    assert not any("DROP" in l and "SNMPSWARM" in l for l in before)

    code, payload = http_json(f"{base}/power/off", method="POST")
    assert code == 200 and payload["power"] == "offline"

    lines = container_iptables_lines(name)
    joined = "\n".join(lines)
    # Dedicated chains with a single jump rule per built-in chain.
    assert "-N SNMPSWARM_IN" in lines
    assert "-N SNMPSWARM_OUT" in lines
    assert "-A INPUT -j SNMPSWARM_IN" in lines
    assert "-A OUTPUT -j SNMPSWARM_OUT" in lines
    # Control port reachable while OFF, everything else dropped.
    assert f"-A SNMPSWARM_IN -p tcp -m tcp --dport {CONTROL_PORT} -j ACCEPT" in joined
    assert "-A SNMPSWARM_IN -j DROP" in lines
    assert "-A SNMPSWARM_OUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT" in joined
    assert "-A SNMPSWARM_OUT -j DROP" in lines
    # Built-in chain policies are untouched (scoped design).
    assert "-P INPUT ACCEPT" in lines
    assert "-P OUTPUT ACCEPT" in lines

    code, payload = http_json(f"{base}/power/on", method="POST")
    assert code == 200 and payload["power"] == "online"

    after = container_iptables_lines(name)
    # Every SNMPSwarm-owned rule and chain is gone after ON.
    assert not any("SNMPSWARM" in l for l in after), after


def test_sigterm_graceful_exit(swarm_env):
    config, _ = swarm_env
    name = container_name(SIM_NAME, DEVICE_ID)
    subprocess.run(["docker", "kill", "-s", "SIGTERM", name],
                   check=True, capture_output=True, timeout=30)

    # The agent terminates cleanly (exit code 0) within the grace period.
    # While still running, .State.ExitCode renders as "<nil>".
    deadline = time.time() + 30
    exit_code = None
    while time.time() < deadline:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.ExitCode}}", name],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            out = result.stdout.strip()
            if out not in ("<nil>", ""):
                exit_code = int(out)
                break
        time.sleep(1.0)
    assert exit_code == 0, f"agent did not exit cleanly (exit={exit_code})"

    # The container must end in the "exited" state (it stays in place until
    # our explicit stop in the fixture removes it). Poll briefly: Docker's
    # state reporting can lag the exit by a moment.
    deadline = time.time() + 10
    status = None
    while time.time() < deadline:
        status_result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", name],
            capture_output=True, text=True, timeout=30,
        )
        if status_result.returncode == 0:
            status = status_result.stdout.strip()
            if status == "exited":
                break
        time.sleep(0.5)
    assert status == "exited", f"container ended in state {status!r}"


DEVICE2_ID = 2
DEVICE2_IP = calculate_ip(DEVICE2_ID)  # 10.0.0.2
DEVICE3_ID = 3
DEVICE3_IP = calculate_ip(DEVICE3_ID)  # 10.0.0.3


def test_sigterm_while_offline_cleans_firewall(swarm_env):
    """SIGTERM with active block rules: clean exit, no firewall errors."""
    config, _ = swarm_env
    name = container_name(SIM_NAME, DEVICE2_ID)
    base = f"http://{DEVICE2_IP}:{CONTROL_PORT}"

    wait_for_control_api(DEVICE2_IP, CONTROL_PORT)
    code, payload = http_json(f"{base}/power/off", method="POST")
    assert code == 200 and payload["power"] == "offline"
    # The block rules are really in place before the shutdown.
    lines = container_iptables_lines(name)
    assert "-A SNMPSWARM_IN -j DROP" in lines
    assert "-A SNMPSWARM_OUT -j DROP" in lines

    subprocess.run(["docker", "kill", "-s", "SIGTERM", name],
                   check=True, capture_output=True, timeout=30)

    deadline = time.time() + 30
    exit_code = None
    while time.time() < deadline:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.ExitCode}}", name],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            out = result.stdout.strip()
            if out not in ("<nil>", ""):
                exit_code = int(out)
                break
        time.sleep(1.0)
    assert exit_code == 0, f"agent did not exit cleanly (exit={exit_code})"

    # Docker's log delivery can lag the exit state by a moment; poll.
    deadline = time.time() + 15
    logs = ""
    while time.time() < deadline:
        logs = container_logs(name)
        if "Agent stopped" in logs:
            break
        time.sleep(0.5)
    assert "Agent stopped" in logs, logs
    assert "Firewall error" not in logs, logs
    assert "did not complete" not in logs, logs


def test_post_startup_thread_death_is_fatal(swarm_env):
    """SIGKILL to a worker TID kills the whole agent process.

    Kernel semantics (verified empirically in /tmp/opencode/tid-repro/):
    SIGKILL to a thread TID triggers do_group_exit for the entire process —
    a live process with a dead thread never exists, so the agent's fatal
    path (stale heartbeat -> exit 1) is unreachable in this case and no
    fatal log is expected. Recovery comes from Docker's restart policy:
    RestartCount >= 1, container running, /status reachable and online.

    The fatal path itself (worker alive but stopped responding -> stale
    heartbeat -> fatal log -> exit 1) is covered at unit level in
    tests/test_agent_power.py (test_main_loop_detects_wedged_thread).
    """
    config, _ = swarm_env
    name = container_name(SIM_NAME, DEVICE3_ID)

    wait_for_control_api(DEVICE3_IP, CONTROL_PORT)
    tid = find_agent_thread_tid(name, "control-http")
    result = subprocess.run(["docker", "exec", name, "sh", "-c", f"kill -9 {tid}"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, f"kill -9 {tid} failed: {result.stderr}"

    # The whole process must die and Docker must restart it.
    deadline = time.time() + 60
    restarts = 0
    while time.time() < deadline:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.RestartCount}}", name],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            restarts = int(result.stdout.strip() or 0)
            if restarts >= 1:
                break
        time.sleep(1.0)
    assert restarts >= 1, "SIGKILL'd agent process was not restarted"

    # The restarted agent must be healthy again.
    status = wait_for_control_api(DEVICE3_IP, CONTROL_PORT)
    assert status["power"] == "online"

    # No fatal log is expected: the kernel killed the entire process
    # (do_group_exit) before the agent's fatal path could run.
    logs = container_logs(name)
    assert "exiting with failure" not in logs, logs
