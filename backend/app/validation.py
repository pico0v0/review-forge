"""Run only locally configured project checks, with bounded output and process-tree timeouts."""

import asyncio
import os
import signal
import subprocess
from pathlib import Path


async def run_check(
    command: str, cwd: Path, timeout: float, task_id: str, secret_env_names: list[str]
) -> dict:
    env = os.environ.copy()
    for name in secret_env_names:
        env.pop(name, None)
    env["AI_REVIEW_TASK_ID"] = task_id
    env["AI_REVIEW_ROUND"] = "1"
    kwargs = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    process = await asyncio.create_subprocess_shell(
        command,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **kwargs,
    )
    output = bytearray()

    async def drain() -> None:
        while chunk := await process.stdout.read(8192):
            output.extend(chunk)
            del output[:-100_000]

    async def kill_tree() -> None:
        if process.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()

    timed_out = False
    reader = asyncio.create_task(drain())
    try:
        await asyncio.wait_for(asyncio.shield(process.wait()), timeout)
    except TimeoutError:
        timed_out = True
        await kill_tree()
    except asyncio.CancelledError:
        await asyncio.shield(kill_tree())
        raise
    finally:
        # Descendants may inherit the pipe; do not wait forever after the shell exits.
        try:
            await asyncio.wait_for(asyncio.shield(reader), 2)
        except TimeoutError:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
    return {
        "command": command,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "passed": process.returncode == 0 and not timed_out,
        "output": output.decode("utf-8", errors="replace"),
    }
