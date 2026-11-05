"""
Set up a model decider.

    python -m freya.decider.install laya

Laya gets its own Python environment under data/decider_envs/, because
it needs torch and transformers versions that must not be installed
into Freya's own. Its weights are downloaded once, from the one
reviewed commit, into data/decider_models/; after that it runs with the
model hub offline.

This is an owner action: it downloads about 3 GB from PyPI, the
PyTorch package index and Hugging Face, and needs `uv` on the PATH.
"""
import argparse
import subprocess
import sys

from freya.child_environment import child_environment
from freya.decider.laya_decider import (
    ENVS_DIR,
    LAYA_ENV,
    LAYA_PACKAGE,
    LAYA_REPOSITORY,
    LAYA_REVISION,
    MODELS_DIR,
    environment_python,
)


TORCH_BACKEND = "cu124"
FETCH = (
    "from huggingface_hub import snapshot_download\n"
    f"snapshot_download({LAYA_REPOSITORY!r}, revision={LAYA_REVISION!r})\n"
)


def run(command, environment=None):
    print(">", " ".join(str(part) for part in command), flush=True)
    subprocess.run(command, check=True, env=child_environment(environment))


def install_laya() -> None:
    python = environment_python()

    if not python.is_file():
        run(["uv", "venv", "--python", "3.12", str(ENVS_DIR / LAYA_ENV)])

    run([
        "uv", "pip", "install", "--python", str(python),
        "--torch-backend", TORCH_BACKEND, LAYA_PACKAGE,
    ])
    print("Downloading Laya's weights...", flush=True)
    run([str(python), "-c", FETCH], {"HF_HOME": str(MODELS_DIR)})
    print("Laya is ready. Measure it with: python -m freya.evaluation.decisions --decider laya")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("decider", choices=["laya"])
    parser.parse_args()

    try:
        install_laya()
    except FileNotFoundError:
        sys.exit("uv was not found. Install uv, then run this again.")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"Setting up Laya failed at: {exc.cmd}")


if __name__ == "__main__":
    main()
