"""Finite HTTP service checks. Stdlib-only so the same probe runs inside Podman.

The harness recognizes ``mobyai-service-check`` as a command, not a shell alias.
Both the service and its probe run in the selected execution environment.
"""
from __future__ import annotations

import argparse
import asyncio
import math
import os
from pathlib import Path
import signal
import socket
import sys
import time
from urllib.parse import urlsplit

COMMAND = "mobyai-service-check"


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parse_check(arguments: list[str]) -> dict:
    parser = _Parser(prog=COMMAND, add_help=False, allow_abbrev=False)
    parser.add_argument("--url", required=True)
    parser.add_argument("--startup-seconds", type=float, default=10)
    parser.add_argument("--observe-seconds", type=float, default=30)
    parser.add_argument("--expected-status", type=int, default=200)
    # An explicit separator prevents service flags from becoming probe options.
    if "--" not in arguments:
        raise ValueError("separate probe options and the server argv with --")
    separator = arguments.index("--")
    options = vars(parser.parse_args(arguments[:separator]))
    options["command"] = arguments[separator + 1:]
    if not options["command"]:
        raise ValueError("a server command is required after --")
    for name in ("startup_seconds", "observe_seconds"):
        value = options[name]
        if not math.isfinite(value) or not 0.1 <= value <= 600:
            raise ValueError(f"{name} must be between 0.1 and 600 seconds")
    if not 200 <= options["expected_status"] <= 299:
        raise ValueError("expected status must be a successful HTTP status (200..299)")
    url = urlsplit(options["url"])
    if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "::1"}
            or not url.port or url.username or url.password or url.fragment):
        raise ValueError("use an HTTP loopback URL with an explicit port, without credentials or a fragment")
    if any(ord(c) < 33 or ord(c) > 126 for c in options["url"]):
        raise ValueError("URL must contain printable ASCII; percent-encode spaces and non-ASCII characters")
    return options


class _Output:
    def __init__(self, limit):
        self.limit = max(1, limit)
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    async def read(self, stream):
        while chunk := await stream.read(65536):
            self.total += len(chunk)
            available = max(0, self.limit // 2 - len(self.head))
            self.head.extend(chunk[:available])
            self.tail.extend(chunk[available:])
            del self.tail[:max(0, len(self.tail) - (self.limit - len(self.head)))]

    def text(self):
        marker = b"\n...[truncated]...\n" if self.total > self.limit else b""
        return (self.head + marker + self.tail).decode(errors="replace")


async def _http_status(url):
    """No proxies, redirects, DNS or response-body buffering."""
    writer = None
    try:
        async with asyncio.timeout(1):
            reader, writer = await asyncio.open_connection(url.hostname, url.port)
            target = (url.path or "/") + ("?" + url.query if url.query else "")
            writer.write(f"GET {target} HTTP/1.1\r\nHost: {url.netloc}\r\nConnection: close\r\n\r\n".encode("ascii"))
            await writer.drain()
            status = (await reader.readline()).split()
            if len(status) < 2 or status[0] not in {b"HTTP/1.0", b"HTTP/1.1"}:
                raise ValueError("invalid HTTP response")
            return int(status[1])
    finally:
        if writer is not None:
            writer.close()
            # A peer cannot make cleanup wait indefinitely.
            try:
                async with asyncio.timeout(1):
                    await writer.wait_closed()
            except (OSError, TimeoutError):
                pass


def _signal_group(process, sig):
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


async def _cleanup(process, readers):
    try:
        _signal_group(process, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 2)
        except TimeoutError:
            pass
    finally:
        # Also kill descendants when the original server has already exited.
        _signal_group(process, signal.SIGKILL)
        await process.wait()
        try:
            await asyncio.wait_for(asyncio.gather(*readers), 2)
        except TimeoutError:
            pass


async def run_check(options, cwd, output_limit=1_048_576):
    started = time.monotonic()
    stdout, stderr = _Output(output_limit), _Output(output_limit)
    process = None
    readers = []
    exit_code = 1
    message = ""
    url = urlsplit(options["url"])
    try:
        # Refuse an occupied port rather than accidentally validating another app.
        family = socket.AF_INET6 if url.hostname == "::1" else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as port:
            port.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            port.bind((url.hostname, url.port))
        spawning = asyncio.create_task(asyncio.create_subprocess_exec(
            *options["command"], cwd=str(cwd), start_new_session=True,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE))
        try:
            process = await asyncio.shield(spawning)
        except asyncio.CancelledError:
            process = await spawning
            raise
        finally:
            if process is not None:
                readers = [asyncio.create_task(stdout.read(process.stdout)),
                           asyncio.create_task(stderr.read(process.stderr))]
        deadline = time.monotonic() + options["startup_seconds"]
        healthy_since = None
        last_status = "no response"
        while True:
            if process.returncode is not None:
                raise ValueError(f"server exited early with code {process.returncode}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if healthy_since is not None:
                    exit_code = 0
                    message = (f"Service check passed: {options['url']} returned HTTP "
                               f"{options['expected_status']} and remained healthy for "
                               f"{options['observe_seconds']:g} seconds.")
                    break
                raise ValueError(f"startup deadline exceeded ({last_status})")
            try:
                status = await asyncio.wait_for(_http_status(url), min(1, remaining))
                last_status = f"HTTP {status}"
                healthy = status == options["expected_status"]
            except (OSError, TimeoutError, ValueError) as exc:
                healthy = False
                last_status = str(exc) or "no response"
            if process.returncode is not None:
                raise ValueError(f"server exited early with code {process.returncode}")
            if healthy_since is None:
                if healthy:
                    healthy_since = time.monotonic()
                    deadline = healthy_since + options["observe_seconds"]
            elif not healthy:
                raise ValueError(f"health check failed during observation ({last_status})")
            await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
    except (OSError, ValueError) as exc:
        message = f"Service check failed: {exc}"
    finally:
        if process is not None:
            cleanup = asyncio.create_task(_cleanup(process, readers))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
    return dict(exit_code=exit_code, stdout=stdout.text(),
                stderr=stderr.text() + "\n" + message,
                duration_seconds=round(time.monotonic() - started, 4),
                stdout_truncated=stdout.total > stdout.limit,
                stderr_truncated=stderr.total > stderr.limit)


def container_command(command):
    """Use image Python, never a host interpreter or host network probe."""
    return ["python3", "-I", "-c", Path(__file__).read_text(encoding="utf-8"), *command[1:]]


def main():
    try:
        options = parse_check(sys.argv[1:])
        result = asyncio.run(run_check(options, Path.cwd()))
    except ValueError as exc:
        print(f"Invalid service check: {exc}", file=sys.stderr)
        return 2
    print(result["stdout"], end="")
    print(result["stderr"], file=sys.stderr)
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
