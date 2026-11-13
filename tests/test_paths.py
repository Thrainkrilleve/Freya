import json
import os
import subprocess
import sys
from pathlib import Path

from freya.paths import DATA_DIR_VARIABLE, DEFAULT_DATA_DIR, data_dir


REPOSITORY = Path(__file__).resolve().parents[1]

# Every module that keeps a DATA_DIR of its own.
MODULES = (
    "freya.core.persistence.control_store",
    "freya.delegation.dispatch",
    "freya.delegation.material",
    "freya.delegation.record",
    "freya.delegation.screening",
    "freya.delegation.service",
    "freya.file_access",
    "freya.image.gallery",
    "freya.launcher",
    "freya.media.config",
    "freya.memory.memory_manager",
    "freya.settings",
    "freya.spatial_avatar.config",
    "freya.spatial_avatar.converter",
    "freya.spatial_avatar.host",
    "freya.spatial_avatar.library",
    "freya.speech.helper",
    "freya.speech.references",
    "freya.speech.store",
    "freya.storage.locations",
    "freya.storage.placement",
    "freya.window",
)

REPORT = (
    "import importlib, json, sys\n"
    "found = {}\n"
    "for name in sys.argv[1:]:\n"
    "    found[name] = str(importlib.import_module(name).DATA_DIR)\n"
    "print(json.dumps(found))\n"
)


def data_dirs_in_a_fresh_process(override=None):
    # A developer's own overrides must not make the fresh process agree
    # for the wrong reason.
    env = {
        k: v for k, v in os.environ.items()
        if k != DATA_DIR_VARIABLE and k != "FREYA_BULK_DIR"
    }
    if override is not None:
        env[DATA_DIR_VARIABLE] = str(override)

    completed = subprocess.run(
        [sys.executable, "-c", REPORT, *MODULES],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPOSITORY,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_the_default_is_the_repositorys_data_folder(monkeypatch):
    monkeypatch.delenv(DATA_DIR_VARIABLE, raising=False)

    assert DEFAULT_DATA_DIR == REPOSITORY / "data"
    assert data_dir() == REPOSITORY / "data"


def test_the_variable_names_another_folder(monkeypatch, tmp_path):
    monkeypatch.setenv(DATA_DIR_VARIABLE, str(tmp_path))

    assert data_dir() == tmp_path


def test_an_empty_variable_means_the_default(monkeypatch):
    monkeypatch.setenv(DATA_DIR_VARIABLE, "  ")

    assert data_dir() == DEFAULT_DATA_DIR


def test_every_module_uses_the_default_unchanged():
    found = data_dirs_in_a_fresh_process()

    assert found == {name: str(REPOSITORY / "data") for name in MODULES}


def test_the_override_moves_every_module(tmp_path):
    found = data_dirs_in_a_fresh_process(tmp_path)

    assert found == {name: str(tmp_path) for name in MODULES}


def test_the_list_above_names_every_such_module():
    source = REPOSITORY / "src"
    keeping = {
        ".".join(path.relative_to(source).with_suffix("").parts)
        for path in (source / "freya").rglob("*.py")
        if "\nDATA_DIR = " in path.read_text(encoding="utf-8")
    }

    assert keeping == set(MODULES)
