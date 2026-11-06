"""
Set up a model decider.

    python -m freya.decider.install laya
    python -m freya.decider.install candidates [--only d1 --only jeb]

Laya gets its own Python environment under data/decider_envs/, because
it needs torch and transformers versions that must not be installed
into Freya's own. Its weights are downloaded once, from the one
reviewed commit, into data/decider_models/; after that it runs with the
model hub offline.

This is an owner action: it downloads about 3 GB from PyPI, the
PyTorch package index and Hugging Face, and needs `uv` on the PATH.

`candidates` pulls the open decision models being measured
(decider/systemone_decider.py) into Ollaya, which the owner installs
first (OLLAYA_INSTALL below). Ollaya fetches each author's weights from
Hugging Face, pinned and checked by sha256: roughly 40 to 50 GB for
all five (Nimble alone is about 18 GB).
"""
import argparse
import shutil
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
from freya.decider.systemone_decider import CANDIDATES


TORCH_BACKEND = "cu124"
OLLAYA_INSTALL = "irm https://ollaya.dev/install.ps1 | iex"
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


def install_candidates(only=None) -> None:
    ollaya = shutil.which("ollaya")

    if ollaya is None:
        sys.exit(
            "Ollaya was not found. Install it first (an owner action), in PowerShell:\n"
            f"    {OLLAYA_INSTALL}\n"
            "then open a new terminal and run this again. Ollaya listens on "
            "127.0.0.1:11435 only; leave OLLAYA_HOST unset."
        )

    for key in only or CANDIDATES:
        candidate = CANDIDATES[key]
        print(f"Pulling {candidate.name} ({candidate.licence})...", flush=True)
        run([ollaya, "pull", candidate.tag])

    print("Done. Compare them with: python -m freya.evaluation.decider_bench --output work\\deciders.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("decider", choices=["laya", "candidates"])
    parser.add_argument(
        "--only", action="append", choices=sorted(CANDIDATES),
        help="With candidates: pull only these (repeatable).",
    )
    args = parser.parse_args()

    if args.decider == "candidates":
        try:
            install_candidates(args.only)
        except subprocess.CalledProcessError as exc:
            sys.exit(f"Pulling a model failed at: {exc.cmd}")
        return

    try:
        install_laya()
    except FileNotFoundError:
        sys.exit("uv was not found. Install uv, then run this again.")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"Setting up Laya failed at: {exc.cmd}")


if __name__ == "__main__":
    main()
