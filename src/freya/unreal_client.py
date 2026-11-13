import os
import subprocess
from pathlib import Path
from typing import Optional

import psutil

from freya.child_environment import child_environment


# The packaged Unreal GUI, if the owner has built one. FREYA_UNREAL_CLIENT
# names its program; unset means Freya does not start it.
CLIENT_VARIABLE = "FREYA_UNREAL_CLIENT"

# How the program is told where Freya is and how to prove it was started
# by her. The key works once. Other programs of the same Windows user
# could read it; this is a prototype arrangement, not the paired,
# authenticated client the architecture calls for.
PORT_VARIABLE = "FREYA_GATEWAY_PORT"
KEY_VARIABLE = "FREYA_PAIRING_KEY"


def configured_client() -> Optional[Path]:
    """
    The Unreal GUI program the owner has named, or None.
    """
    named = os.environ.get(CLIENT_VARIABLE, "").strip()

    if not named:
        return None

    path = Path(named)

    return path if path.is_file() else None


class UnrealClient:
    """
    Starts the Unreal GUI as a separate program and stops it again.

    It is a client like any other: it reaches Freya only through her
    gateway, with a key she hands it as it starts. If it crashes or is
    closed, Freya carries on.
    """

    def __init__(self, program: Path, start=subprocess.Popen):
        self.program = Path(program)
        self._start = start
        self._process = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def launch(self, port: int, pairing_key: str) -> bool:
        """
        Start the program. Returns False, with Freya unaffected, when it
        cannot be started.
        """
        if self.running:
            return True

        try:
            self._process = self._start(
                [str(self.program), "-windowed"],
                cwd=str(self.program.parent),
                env=child_environment(
                    {
                        PORT_VARIABLE: str(port),
                        KEY_VARIABLE: pairing_key,
                    }
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            print(f"The Unreal window could not be started: {exc}")
            self._process = None

            return False

        return True

    def stop(self, timeout: float = 5.0) -> None:
        """
        Close the program Freya started, and anything it started.
        """
        process, self._process = self._process, None

        if process is None or process.poll() is not None:
            return

        try:
            parent = psutil.Process(process.pid)
            family = parent.children(recursive=True) + [parent]
        except psutil.Error:
            return

        for member in family:
            try:
                member.terminate()
            except psutil.Error:
                pass

        _, alive = psutil.wait_procs(family, timeout=timeout)

        for member in alive:
            try:
                member.kill()
            except psutil.Error:
                pass
