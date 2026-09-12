"""Fedora/Linux console restart helper. Never kill a process based only on its port."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener

REPO = Path(__file__).resolve().parents[1]


def listener_pids(port):
    if not shutil.which('ss'):
        raise ValueError('The ss utility is required (Fedora package: iproute).')
    result = subprocess.run(['ss', '-H', '-ltnp', f'sport = :{port}'],
                            check=True, capture_output=True, text=True)
    if not result.stdout.strip():
        return []
    pids = sorted({int(pid) for pid in re.findall(r'pid=(\d+)', result.stdout)})
    if not pids:
        raise ValueError(f'Port {port} is occupied, but its owner cannot be verified. Nothing was stopped.')
    return pids


def matching_console(argv, config, repo=REPO):
    # Accept the installed entrypoint and the module form used by this application.
    executable = str(repo / '.venv/bin/mobyai')
    if executable in argv:
        args = argv[argv.index(executable) + 1:]
    elif len(argv) >= 3 and argv[1:3] == ['-m', 'mobyai'] and Path(argv[0]).parent == repo / '.venv/bin':
        args = argv[3:]
    else:
        return False
    if len(args) < 3 or args[0] != '--config' or args[2] != 'ui':
        return False
    return Path(args[1]).expanduser().resolve() == config


def process_identity(pid, config):
    proc = Path('/proc') / str(pid)
    if proc.stat().st_uid != os.getuid():
        raise ValueError('Console process belongs to another user. Nothing was stopped.')
    argv = proc.joinpath('cmdline').read_bytes().decode().rstrip('\0').split('\0')
    if not matching_console(argv, config):
        raise ValueError(f'PID {pid} is not this checkout\'s console with the selected config. Nothing was stopped.')
    return argv, proc.stat().st_ino


def check_idle(base):
    opener = build_opener(ProxyHandler({}))
    def get(path):
        with opener.open(base + path, timeout=10) as response:
            return json.load(response)
    from urllib.parse import urlencode
    projects = get('/api/projects').get('projects')
    if not isinstance(projects, list):
        raise ValueError('Cannot verify console activity. Nothing was stopped.')
    for project in projects:
        snapshot = get('/api/monitor?' + urlencode({'project': project['project_id']}))
        state = snapshot.get('workload')
        if not isinstance(state, dict) or 'can_stop' not in state or 'stopping' not in state:
            raise ValueError('Console does not report workload activity. Stop it from its original terminal.')
        relocation = state.get('relocation') or {}
        if state['can_stop'] or state['stopping'] or relocation.get('state') == 'copying':
            raise ValueError('A workload or directory copy is active. Use Stop workload in Monitor and complete both confirmations, or wait for copying to finish, then rerun this script.')


def main():
    parser = argparse.ArgumentParser(description='Restart this checkout\'s idle MobyAI console. Active workloads must be stopped in Monitor first. The replacement runs in this terminal.')
    parser.add_argument('--config', default=os.environ.get('MOBYAI_CONFIG', str(REPO/'config.yaml')))
    parser.add_argument('--port', type=int, default=8743)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    config = Path(args.config).expanduser().resolve()
    if not config.is_file():
        raise ValueError(f'Config not found: {config}')
    if not 1 <= args.port <= 65535:
        raise ValueError('Port must be between 1 and 65535')
    pids = listener_pids(args.port)
    if len(pids) > 1:
        raise ValueError('Multiple processes own this port. Nothing was stopped.')
    if pids:
        pid = pids[0]
        identity = process_identity(pid, config)
        check_idle(f'http://127.0.0.1:{args.port}')
        if process_identity(pid, config) != identity:
            raise ValueError('Process identity changed. Nothing was stopped.')
        print(f'Stopping idle MobyAI console (PID {pid})…', flush=True)
        os.kill(pid, signal.SIGINT)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            proc = Path('/proc') / str(pid)
            try:
                if proc.stat().st_ino != identity[1]:
                    break
                # A detached console can briefly remain as a zombie until reaped.
                state = proc.joinpath('stat').read_text().rpartition(') ')[2].split()[0]
                if state == 'Z':
                    break
            except FileNotFoundError:
                break
            time.sleep(.2)
        else:
            raise ValueError('Console did not exit within 60 seconds. No replacement was started; inspect its log.')
        if listener_pids(args.port):
            raise ValueError('Port is still occupied. No replacement was started.')
    else:
        print('No console is running on the selected port; starting it.', flush=True)
    print('Starting MobyAI. Keep this terminal open; Ctrl+C stops the console.', flush=True)
    command = [str(REPO/'scripts/start.sh'), '--config', str(config), 'ui', '--port', str(args.port)]
    if args.no_browser:
        command.append('--no-browser')
    os.execv(command[0], command)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(f'Restart failed: {exc}', file=sys.stderr)
        raise SystemExit(1)
