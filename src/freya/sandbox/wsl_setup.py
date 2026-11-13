"""
Sets up the WSL2 sandbox distribution. Owner action: it installs a Linux
distribution and Podman, and downloads a Python image (a few hundred MB).

Two steps need an administrator and are only printed, never run:
installing WSL itself (and the restart after it), and installing the
distribution. Everything after that runs inside the distribution, with
fixed commands; nothing here comes from a model.
"""

import subprocess
from importlib import resources

from freya.sandbox.wsl import (
    IMAGE,
    SANDBOX_UID,
    SANDBOX_USER,
    distribution_name,
    in_distribution,
    wsl_environment,
)


IMAGE_FOLDER = f"/home/{SANDBOX_USER}/freya-image"

# What the sandbox distribution may do with Windows: nothing. Windows
# drives are not attached, Windows programs cannot be started, and the
# Windows PATH is not added. systemd gives the sandbox user a session,
# which rootless Podman needs to apply memory and process limits.
WSL_CONF = f"""\
# Written by Freya's sandbox setup. This distribution runs code a model
# wrote, inside containers. Keep it sharing nothing with Windows.
[automount]
enabled = false
mountFsTab = false

[interop]
enabled = false
appendWindowsPath = false

[boot]
systemd = true

[user]
default = {SANDBOX_USER}
"""


def owner_steps(distribution) -> list:
    """The two administrator steps, as text for the owner to run."""
    return [
        "wsl --install --no-distribution      (as administrator, then restart Windows)",
        f"wsl --install Debian --name {distribution} --no-launch",
    ]


def _image_file(name) -> str:
    return (resources.files("freya.sandbox") / "image" / name).read_text(encoding="utf-8")


def setup_steps(distribution) -> list:
    """
    (description, argv, stdin text or None) for each step run inside the
    distribution. Every argv is fixed.
    """

    def root(*argv):
        return in_distribution(distribution, "root", argv)

    def user(*argv):
        return in_distribution(distribution, SANDBOX_USER, argv)

    return [
        ("Install Podman", root("/bin/sh", "-c",
            "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
            "--no-install-recommends podman uidmap fuse-overlayfs ca-certificates"), None),
        ("Create the sandbox user", root("/bin/sh", "-c",
            f"id -u {SANDBOX_USER} >/dev/null 2>&1 || useradd --create-home "
            f"--uid {SANDBOX_UID} --shell /bin/bash {SANDBOX_USER}"), None),
        ("Stop the distribution sharing anything with Windows",
            root("/usr/bin/tee", "/etc/wsl.conf"), WSL_CONF),
        ("Apply those settings", ["wsl.exe", "--terminate", distribution], None),
        ("Give the sandbox user a session", root("/usr/bin/loginctl", "enable-linger", SANDBOX_USER), None),
        ("Make the image folder", user("/usr/bin/mkdir", "-p", IMAGE_FOLDER), None),
        ("Copy the image description",
            user("/usr/bin/tee", f"{IMAGE_FOLDER}/Containerfile"), _image_file("Containerfile")),
        ("Copy the runner", user("/usr/bin/tee", f"{IMAGE_FOLDER}/runner.py"), _image_file("runner.py")),
        ("Build the sandbox image", user("podman", "build", "--tag", IMAGE, IMAGE_FOLDER), None),
        ("Check the image is there", user("podman", "image", "exists", IMAGE), None),
    ]


def run_setup(distribution=None, run=subprocess.run, say=print) -> bool:
    distribution = distribution or distribution_name()

    say("Before this, as the owner:")
    for step in owner_steps(distribution):
        say("  " + step)
    say("")

    for description, argv, text in setup_steps(distribution):
        say(f"- {description}")
        completed = run(
            argv,
            input=(text or "").encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=wsl_environment(),
            check=False,
        )

        if completed.returncode != 0:
            detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
            say(f"  Failed (exit {completed.returncode}). {detail[-800:]}")
            return False

    say("")
    say("The sandbox is set up. Run `python -m freya.sandbox probe` next.")
    return True
