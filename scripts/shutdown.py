"""Shut down this checkout's console through its existing cleanup paths."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, ProxyHandler, build_opener

from restart import REPO, listener_pids, process_identity


def request(base, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {'Content-Type': 'application/json'} if data is not None else {}
    req = Request(base + path, data=data, headers=headers)
    with build_opener(ProxyHandler({})).open(req, timeout=10) as response:
        return json.load(response)


def activity(base):
    projects = request(base, '/api/projects').get('projects')
    if not isinstance(projects, list):
        raise ValueError('Cannot verify console activity; console left running.')
    states = []
    for project in projects:
        id = project['project_id']
        snapshot = request(base, '/api/monitor?' + urlencode({'project': id}))
        state = snapshot.get('workload')
        if not isinstance(state, dict) or 'can_stop' not in state or 'stopping' not in state:
            raise ValueError('This console cannot report workload activity; use Monitor to stop it.')
        states.append((id, state))
    return states


def drain(base, verify, deadline):
    requested = set()
    next_notice = 0
    while True:
        verify()
        states = activity(base)
        busy = False
        for id, state in states:
            copying = (state.get('relocation') or {}).get('state') == 'copying'
            busy |= bool(state['can_stop'] or state['stopping'] or copying)
            if state['can_stop'] and not state['stopping'] and id not in requested:
                verify()
                print(f'Pausing workload {id}; preserving saved progress…', flush=True)
                challenge = request(base, '/api/workloads/prepare-stop', {'project_id': id})
                if challenge.get('project_id') != id or not challenge.get('token'):
                    raise ValueError('Stop challenge does not match the selected workload.')
                request(base, '/api/workloads/stop', {
                    'project_id': id, 'confirmation': id, 'token': challenge['token'],
                })
                requested.add(id)
        if not busy:
            return
        if time.monotonic() >= deadline:
            raise ValueError('Timed out waiting for workload/copy cleanup. Console left running; inspect Monitor and retry.')
        if time.monotonic() >= next_notice:
            print('Waiting for workload shutdown and any directory copy to finish…', flush=True)
            next_notice = time.monotonic() + 10
        time.sleep(.5)


def exited(pid, identity):
    proc = Path('/proc') / str(pid)
    try:
        return (proc.stat().st_ino != identity[1]
                or proc.joinpath('stat').read_text().rpartition(') ')[2].split()[0] == 'Z')
    except FileNotFoundError:
        return True


def shutdown(config, port, timeout):
    pids = listener_pids(port)
    if not pids:
        print(f'No MobyAI console is running on port {port}.', flush=True)
        return
    if len(pids) != 1:
        raise ValueError('Multiple processes own this port; nothing was stopped.')
    pid = pids[0]
    identity = process_identity(pid, config)
    def verify():
        if process_identity(pid, config) != identity:
            raise ValueError('Console identity changed; shutdown stopped.')
    deadline = time.monotonic() + timeout
    drain(f'http://127.0.0.1:{port}', verify, deadline)
    verify()
    print(f'Closing MobyAI console (PID {pid}); waiting for cleanup and trace flushing…', flush=True)
    # SIGINT enters asyncio's cancellation/finally path, including on older
    # installed consoles. No force-kill or broad process-name matching is used.
    os.kill(pid, signal.SIGINT)
    while not exited(pid, identity):
        if time.monotonic() >= deadline:
            raise ValueError('Console is still cleaning up. No force-kill was sent; inspect its terminal/log.')
        time.sleep(.2)
    if listener_pids(port):
        raise ValueError('Console exited, but the port is occupied again. Check for an automatic service restart.')
    print('MobyAI console shut down. Project files and saved progress are preserved.', flush=True)


def main():
    parser = argparse.ArgumentParser(description="Cleanly shut down this checkout's MobyAI console and its console-owned workload. Separately launched CLI jobs and external model servers are not stopped.")
    parser.add_argument('--config', default=os.environ.get('MOBYAI_CONFIG', str(REPO/'config.yaml')))
    parser.add_argument('--port', type=int, default=8743)
    parser.add_argument('--timeout', type=int, default=180, help='Seconds to wait for cleanup (default: 180)')
    args = parser.parse_args()
    config = Path(args.config).expanduser().resolve()
    if not config.is_file():
        raise ValueError(f'Config not found: {config}')
    if not 1 <= args.port <= 65535 or args.timeout <= 0:
        raise ValueError('Use a valid port (1–65535) and a positive timeout.')
    shutdown(config, args.port, args.timeout)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f'Shutdown incomplete: {exc}', file=sys.stderr)
        raise SystemExit(1)
