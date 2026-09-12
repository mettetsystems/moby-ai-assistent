"""Hold a logind sleep inhibitor only while a workload is executing."""
from contextlib import asynccontextmanager
import asyncio
import shutil
import sys


async def _release(process):
    if process.stdin is not None:
        process.stdin.close()
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        process.kill()
        await process.wait()


@asynccontextmanager
async def keep_awake(*, enabled, stdout):
    process = None
    acquired = False
    try:
        if enabled:
            binary = shutil.which("systemd-inhibit")
            if binary is None:
                print("Power: WARNING — systemd-inhibit unavailable; sleep protection is inactive.", file=stdout, flush=True)
            else:
                try:
                    # The child's stdin closes even if the parent crashes. The
                    # helper only announces readiness after logind grants the lock.
                    process = await asyncio.create_subprocess_exec(
                        binary, "--what=idle:sleep", "--mode=block", "--who=MobyAI",
                        "--why=Long-horizon workload running", sys.executable, "-u", "-c",
                        "import sys; print('ready', flush=True); sys.stdin.buffer.read()",
                        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE, start_new_session=True,
                    )
                    ready = await asyncio.wait_for(process.stdout.readline(), timeout=5)
                    acquired = ready.strip() == b"ready"
                    if not acquired:
                        await _release(process)
                        process = None
                        print("Power: WARNING — sleep inhibitor was denied; sleep protection is inactive.", file=stdout, flush=True)
                except (OSError, TimeoutError) as exc:
                    if process is not None:
                        await _release(process)
                        process = None
                    print(f"Power: WARNING — sleep protection is inactive ({type(exc).__name__}).", file=stdout, flush=True)
                if acquired:
                    print("Power: automatic idle/suspend inhibited while this workload runs.", file=stdout, flush=True)
        yield acquired
    finally:
        if process is not None:
            await _release(process)
        if acquired:
            print("Power: sleep inhibitor released.", file=stdout, flush=True)
