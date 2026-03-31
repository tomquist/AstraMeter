import asyncio

from .base import Powermeter


class Script(Powermeter):
    def __init__(self, command: str):
        self.script = command

    async def get_powermeter_watts_async(self) -> list[float]:
        proc = await asyncio.create_subprocess_shell(
            self.script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode().strip()
            raise RuntimeError(
                f"Script exited with code {proc.returncode}: {self.script}"
                + (f"\n{err}" if err else "")
            )
        power = stdout.decode().strip().split("\n")
        return [float(p) for p in power]
