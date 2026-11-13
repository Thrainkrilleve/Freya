"""
Set up one of the large speech engines.

    python -m freya.speech.install chatterbox
    python -m freya.speech.install qwen3-tts

Each engine gets its own Python environment under data/speech_envs/,
because the engines pin library versions that conflict with Freya's
and with each other's; nothing is installed into Freya's own
environment. Model files are downloaded once into data/speech_models/.
After that the engines run without contacting the network.

This downloads several gigabytes per engine, from PyPI, the PyTorch
package index, GitHub (Chatterbox's source), and Hugging Face. It needs
`uv` on the PATH.
"""
import argparse
import subprocess
import sys

from freya.child_environment import child_environment
from freya.speech.helper import ENVS_DIR, MODELS_DIR, environment_python


# Chatterbox's last PyPI release (0.1.7) cannot load the Nano or
# Multilingual V3 models; its own repository can, with the same
# dependencies. The commit is pinned so the install is repeatable.
CHATTERBOX_COMMIT = "5de7a54aa4e5e2baadb0182dde554908b48b85c2"

ENGINES = {
    "chatterbox": {
        "torch_backend": "cu124",
        "steps": [
            ["chatterbox-tts==0.1.7"],
            [
                "--no-deps",
                "--reinstall-package",
                "chatterbox-tts",
                "chatterbox-tts @ git+https://github.com/resemble-ai/"
                f"chatterbox.git@{CHATTERBOX_COMMIT}",
            ],
            # Chatterbox's watermarker imports pkg_resources, which
            # newer setuptools no longer ships. Without it the
            # watermarker cannot load and Chatterbox will not start.
            ["setuptools<81"],
        ],
        "fetch": (
            "from chatterbox.tts_turbo import ChatterboxTurboTTS as T\n"
            "from chatterbox.mtl_tts import ChatterboxMultilingualTTS as M\n"
            "T.from_pretrained(device='cpu')\n"
            "T.from_pretrained(device='cpu', nano=True)\n"
            "M.from_pretrained(device='cpu', t3_model='v3')\n"
        ),
    },
    "qwen3-tts": {
        "torch_backend": "cu126",
        "steps": [["qwen-tts==0.1.1"]],
        "fetch": (
            "from huggingface_hub import snapshot_download\n"
            "for name in ('1.7B-VoiceDesign', '1.7B-Base', '0.6B-Base'):\n"
            "    snapshot_download('Qwen/Qwen3-TTS-12Hz-' + name)\n"
        ),
    },
}


def run(command, environment=None):
    print(">", " ".join(str(part) for part in command), flush=True)
    # These programs run code from the packages they download.
    subprocess.run(command, check=True, env=child_environment(environment))


def install(engine_id: str) -> None:
    engine = ENGINES[engine_id]
    python = environment_python(engine_id)

    if not python.is_file():
        run(["uv", "venv", "--python", "3.12", str(ENVS_DIR / engine_id)])

    for step in engine["steps"]:
        run(
            [
                "uv", "pip", "install",
                "--python", str(python),
                "--torch-backend", engine["torch_backend"],
                *step,
            ]
        )

    print("Downloading model files...", flush=True)
    run(
        [str(python), "-c", engine["fetch"]],
        {"HF_HOME": str(MODELS_DIR)},
    )
    print(f"{engine_id} is ready. Restart Freya to use it.")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("engine", choices=sorted(ENGINES))
    arguments = parser.parse_args()

    try:
        install(arguments.engine)
    except FileNotFoundError:
        sys.exit("uv was not found. Install uv, then run this again.")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"Setting up {arguments.engine} failed at: {exc.cmd}")


if __name__ == "__main__":
    main()
