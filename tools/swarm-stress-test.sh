#!/usr/bin/env bash
# Requirements: Linux, Python 3, Docker CLI, Net-SNMP snmpget.
# Example: ./tools/swarm-stress-test.sh --count 1000
set -euo pipefail
exec python3 - "$@" <<'SNMPSWARM_PYTHON'
"""Poll running SNMPSwarm devices. Python stdlib + Docker CLI + snmpget."""
import argparse
import concurrent.futures as cf
import json
import math
import shutil
import statistics
import subprocess
import time
import threading


def cpu():
    with open('/proc/stat') as f:
        v = list(map(int, f.readline().split()[1:9]))
    return sum(v), v[3], v[4]


def main():
    p = argparse.ArgumentParser(prog="swarm-stress-test.sh", description=__doc__)
    p.add_argument('--count', type=int, help='Number of running devices to poll, lowest IPs first; default: all')
    p.add_argument('--name', default='snmpswarm')
    p.add_argument('--workers', type=int, default=32)
    p.add_argument('--community', default='public')
    p.add_argument('--timeout', type=float, default=1)
    p.add_argument('--interval', type=float, default=5, help='Seconds between round starts; 0 = continuous')
    p.add_argument('--rounds', type=int, default=0, help='0 = run until Ctrl+C')
    a = p.parse_args()
    if a.workers < 1 or a.timeout <= 0 or a.interval < 0 or a.rounds < 0:
        p.error('Invalid workers, timeout, interval or rounds')
    if a.count is not None and a.count < 1:
        p.error('--count must be a positive integer')
    for cmd in ('docker', 'snmpget'):
        if not shutil.which(cmd):
            p.error(f'Missing command: {cmd}' + (' (install: sudo apt install snmp)' if cmd == 'snmpget' else ''))
    ids = subprocess.check_output(['docker', 'ps', '-q', '--filter', 'label=network-swarm.managed=true', '--filter', f'label=network-swarm.name={a.name}'], text=True).split()
    if not ids:
        p.error(f'No running containers for {a.name}')
    if a.count is not None and len(ids) < a.count:
        p.error(f'Requested {a.count} devices, but only {len(ids)} running containers found for {a.name}')
    targets = []
    net = f'swarm-{a.name}-net'
    for i in range(0, len(ids), 100):
        for c in json.loads(subprocess.check_output(['docker', 'inspect', *ids[i:i+100]], text=True)):
            ip = c['NetworkSettings']['Networks'].get(net, {}).get('IPAddress')
            if not ip:
                p.error(f"No address on {net} for {c['Name']}")
            targets.append((c['Name'].lstrip('/'), ip))

    targets.sort(key=lambda t: tuple(map(int, t[1].split('.'))))
    if a.count is not None:
        targets = targets[:a.count]
    stopped = threading.Event()

    def poll(target):
        name, ip = target
        if stopped.is_set():
            return name, False, 0, 'Cancelled'
        start = time.monotonic()
        try:
            r = subprocess.run(['snmpget', '-v2c', '-c', a.community, '-t', str(a.timeout), '-r', '0', '-Oqv', ip, '.1.3.6.1.2.1.1.3.0'], capture_output=True, text=True, timeout=a.timeout + 3)
            ok = r.returncode == 0 and bool(r.stdout.strip()) and not any(s in r.stdout.lower() for s in ('no such', 'end of mib'))
            error = (r.stderr or r.stdout).strip().replace('\n', ' ')[:160] if not ok else ''
        except subprocess.TimeoutExpired:
            ok, error = False, 'snmpget process timeout'
        return name, ok, (time.monotonic() - start) * 1000, error

    print(f'{len(targets)} devices | {a.workers} parallel requests | sysUpTime | interval {a.interval:g}s', flush=True)
    print('CPU = whole host during polling (100% = all cores). Latency includes local snmpget process overhead.', flush=True)
    print('Round   OK/total       Fail   Round(s)  Polls/s  CPU%  IOwait%  Avg(ms)  P95(ms)  Max(ms)', flush=True)
    with cf.ThreadPoolExecutor(max_workers=a.workers) as pool:
        n = 0
        while a.rounds == 0 or n < a.rounds:
            n += 1
            before = cpu()
            start = time.monotonic()
            try:
                results = list(pool.map(poll, targets))
            except KeyboardInterrupt:
                stopped.set()
                raise
            elapsed = time.monotonic() - start
            after = cpu()
            total, idle, wait = [y-x for x,y in zip(before,after)]
            busy = 100 * (total-idle-wait) / total if total else 0
            iowait = 100 * wait / total if total else 0
            good = sorted(r[2] for r in results if r[1])
            failures = [r for r in results if not r[1]]
            stats = f'{statistics.mean(good):8.1f} {good[math.ceil(.95*len(good))-1]:8.1f} {good[-1]:8.1f}' if good else '     n/a      n/a      n/a'
            print(f'{n:5}   {len(good):4}/{len(targets):<7} {len(failures):5}   {elapsed:8.2f} {len(results)/elapsed:8.1f} {busy:5.1f} {iowait:8.1f} {stats}', flush=True)
            for name, _, _, error in failures[:3]:
                print(f'  FAIL {name}: {error}', flush=True)
            if a.rounds == 0 or n < a.rounds:
                time.sleep(max(0, a.interval - elapsed))


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nStopped.')
    except OSError as e:
        raise SystemExit(f'Error: {e}')
    except subprocess.CalledProcessError as e:
        raise SystemExit(f'Docker command failed (exit {e.returncode}).')
SNMPSWARM_PYTHON

