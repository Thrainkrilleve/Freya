import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from freya.storage.locations import store_path
from typing import Callable, List, Optional

import psutil

from freya.child_environment import child_environment
from freya.spatial_avatar.sources import (
    AVATAR_FILES,
    CHARACTER_FILES,
    best_inside,
)


DATA_DIR = Path(__file__).resolve().parents[3] / "data"
WORK_DIR = store_path("avatar_work")
SCRIPTS_DIR = Path(__file__).resolve().parent / "blender"

INSPECT_TIMEOUT_SECONDS = 300
CONVERT_TIMEOUT_SECONDS = 1200
EXTRACT_TIMEOUT_SECONDS = 600
# The most an archive may unpack to, and the most files it may hold.
MAX_UNPACKED_BYTES = 8 * 1024 * 1024 * 1024
MAX_UNPACKED_FILES = 5000
# How many files inside an archive are inspected before giving up.
MAX_TRIES = 3


class ConversionError(Exception):
    """
    A file could not be made into an avatar. Safe to show the owner.
    """


def find_blender() -> Optional[Path]:
    """
    Blender's program, or None when it is not installed. FREYA_BLENDER
    names it directly; otherwise the newest version in the usual place
    is used.
    """
    configured = os.environ.get("FREYA_BLENDER", "").strip()

    if configured:
        path = Path(configured)

        return path if path.is_file() else None

    root = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Blender Foundation"

    try:
        found = sorted(root.glob("Blender */blender.exe"), reverse=True)
    except OSError:
        return None

    return found[0] if found else None


def _kill_family(process) -> None:
    try:
        parent = psutil.Process(process.pid)
        family = parent.children(recursive=True) + [parent]
    except psutil.Error:
        return

    for member in family:
        try:
            member.kill()
        except psutil.Error:
            pass

    psutil.wait_procs(family, timeout=10)


def run_blender(blender: Path, script: str, request: dict, timeout: float, log: Path,
                started=None) -> None:
    """
    Run one of Freya's own scripts inside Blender, with no window and
    with scripts embedded in model files switched off. Blender is ended
    if it runs longer than `timeout` seconds.
    """
    handle, request_path = tempfile.mkstemp(suffix=".json", prefix="freya_avatar_")
    os.close(handle)
    Path(request_path).write_text(json.dumps(request), encoding="utf-8")
    log.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(log, "a", encoding="utf-8", errors="replace") as output:
            process = subprocess.Popen(
                [
                    str(blender),
                    "--background",
                    # Never run a script that came inside a model file.
                    "--disable-autoexec",
                    "--python",
                    str(SCRIPTS_DIR / script),
                    "--",
                    request_path,
                ],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                env=child_environment(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

            if started is not None:
                started(process)

            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_family(process)
                raise ConversionError("Blender took too long and was stopped.")
    except OSError as exc:
        raise ConversionError(f"Blender could not be started: {exc}")
    finally:
        try:
            os.unlink(request_path)
        except OSError:
            pass


def unpack(archive: Path, destination: Path) -> List[Path]:
    """
    Unpack an archive into a fresh folder and return the files in it.
    Refuses an archive that would write outside the folder, that holds
    links, or that unpacks to an unreasonable size.
    """
    destination.mkdir(parents=True, exist_ok=False)
    root = destination.resolve()

    if archive.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(archive) as bundle:
                members = bundle.infolist()

                if len(members) > MAX_UNPACKED_FILES:
                    raise ConversionError("The archive holds too many files.")

                if sum(member.file_size for member in members) > MAX_UNPACKED_BYTES:
                    raise ConversionError("The archive is too large to unpack.")

                for member in members:
                    target = (destination / member.filename).resolve()

                    if target != root and root not in target.parents:
                        raise ConversionError(
                            "The archive tries to write outside its own folder."
                        )

                bundle.extractall(destination)
        except (OSError, zipfile.BadZipFile) as exc:
            raise ConversionError(f"The archive could not be unpacked: {exc}")
    else:
        tar = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "tar.exe"

        if not tar.is_file():
            raise ConversionError("This kind of archive cannot be unpacked here.")

        try:
            done = subprocess.run(
                [str(tar), "-xf", str(archive), "-C", str(destination)],
                capture_output=True,
                timeout=EXTRACT_TIMEOUT_SECONDS,
                env=child_environment(),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConversionError(f"The archive could not be unpacked: {exc}")

        if done.returncode != 0:
            raise ConversionError("The archive could not be unpacked.")

    files = []
    total = 0

    for path in destination.rglob("*"):
        resolved = path.resolve()

        if path.is_symlink() or (resolved != root and root not in resolved.parents):
            raise ConversionError("The archive holds links that point elsewhere.")

        if path.is_file():
            files.append(path)
            total += path.stat().st_size

    if len(files) > MAX_UNPACKED_FILES or total > MAX_UNPACKED_BYTES:
        raise ConversionError("The archive unpacks to more than Freya will handle.")

    return files


def plan(report: dict) -> dict:
    """
    From an inspection report, decide whether the model can become an
    avatar and say why, in words for the owner.
    """
    if not report.get("ok"):
        return {
            "possible": False,
            "notes": report.get("problems") or ["The file could not be opened."],
        }

    notes = []
    possible = True

    if not report.get("rig"):
        possible = False
        notes.append(
            "It has no skeleton, so it cannot move. Giving it one is "
            "modelling work Freya does not do."
        )
    else:
        kind = report.get("rig_kind")

        if kind == "auto-rig-pro":
            notes.append(
                f"It has an animation rig with {report.get('rig_bones')} bones. "
                "Freya will replace it with the plain humanoid skeleton an "
                "avatar needs."
            )
        else:
            notes.append(
                "It has a skeleton. Freya will work out which bone is which."
            )

        height = report.get("height") or 0

        if height and not 0.9 <= height <= 2.6:
            notes.append(
                f"It is {height:g} units tall, so it was not made in metres. "
                "Freya will resize it."
            )

        shapes = report.get("face_shapes", 0)

        if shapes >= 40:
            notes.append(f"It has {shapes} face shapes: expressions and lip movement will work.")
        elif shapes:
            notes.append(f"It has only {shapes} face shapes, so some expressions will be missing.")
        else:
            notes.append(
                "It has no standard face shapes, so it will have no "
                "expressions or lip movement."
            )

    if report.get("embedded_scripts"):
        notes.append(
            "The file contains scripts. Freya does not run them."
        )

    return {"possible": possible, "notes": notes}


def slug(name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

    return text[:48] or "avatar"


class AvatarConverter:
    """
    Makes a found file ready to be an avatar: unpacks it if it is an
    archive, copies it if it is already a VRM, and otherwise inspects it
    and converts it in Blender.

    Blender runs as a separate program on Freya's own scripts, with
    scripts inside the model file switched off, and only ever writes
    into the avatar folder. The original file is never changed.
    """

    def __init__(self, models_dir, work_dir=WORK_DIR, blender: Optional[Path] = None,
                 run=run_blender):
        self.models_dir = Path(models_dir)
        self.work_dir = Path(work_dir)
        self._blender = blender
        self._run = run
        self._process = None
        self._cancelled = False

    def _started(self, process) -> None:
        self._process = process

        if self._cancelled:
            _kill_family(process)

    def cancel(self) -> None:
        """
        End a conversion that is under way, at shutdown.
        """
        self._cancelled = True
        process = self._process

        if process is not None and process.poll() is None:
            _kill_family(process)

    def blender(self) -> Optional[Path]:
        # Remembered once found; looked for again only while missing.
        if self._blender is None:
            self._blender = find_blender()

        return self._blender

    def _free_name(self, name: str) -> Path:
        base = slug(name)
        path = self.models_dir / f"{base}.vrm"
        count = 2

        while path.exists():
            path = self.models_dir / f"{base}-{count}.vrm"
            count += 1

        return path

    def inspect(self, source: Path, log: Path) -> dict:
        blender = self.blender()

        if blender is None:
            raise ConversionError(
                "Blender is needed to convert this file, and it is not installed."
            )

        handle, report_path = tempfile.mkstemp(suffix=".json", prefix="freya_report_")
        os.close(handle)

        try:
            self._run(
                blender,
                "inspect_model.py",
                {"source": str(source), "report": report_path},
                INSPECT_TIMEOUT_SECONDS,
                log,
                started=self._started,
            )

            try:
                report = json.loads(Path(report_path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise ConversionError(
                    f"Blender could not read the file. Its output is in {log}."
                )
        finally:
            try:
                os.unlink(report_path)
            except OSError:
                pass

        if not isinstance(report, dict):
            raise ConversionError("Blender's report could not be read.")

        return report

    def convert(self, source: Path, report: dict, name: str, author: str, log: Path) -> dict:
        output = self._free_name(name)
        handle, result_path = tempfile.mkstemp(suffix=".json", prefix="freya_result_")
        os.close(handle)

        try:
            self._run(
                self.blender(),
                "convert_model.py",
                {
                    "source": str(source),
                    "output": str(output),
                    "result": result_path,
                    "rig": report["rig"],
                    "rig_kind": report["rig_kind"],
                    "name": name,
                    "author": author,
                    "face_units": report.get("face_shape_names", []),
                    "copyright": (
                        "Belongs to the model's original author. Converted "
                        "to VRM for private use; not for redistribution."
                    ),
                },
                CONVERT_TIMEOUT_SECONDS,
                log,
                started=self._started,
            )

            try:
                result = json.loads(Path(result_path).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                result = {}
        finally:
            try:
                os.unlink(result_path)
            except OSError:
                pass

        if not result.get("ok") or not output.is_file():
            output.unlink(missing_ok=True)
            failed = [
                f"{s['step']}: {s.get('error', 'failed')}"
                for s in result.get("steps", [])
                if not s.get("ok")
            ]

            raise ConversionError(
                "The conversion did not finish. "
                + " ".join(failed + result.get("warnings", []))
                + f" Blender's output is in {log}."
            )

        return {**result, "file": output.name}

    def prepare(self, source: Path, name: str = "", author: str = "",
                progress: Callable[[str], None] = lambda text: None) -> dict:
        """
        Make one found file ready. Returns what was done: the file
        written, and notes on what the model has and lacks. Raises
        ConversionError, with nothing left behind, when it cannot be
        done.
        """
        self.models_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        job = Path(tempfile.mkdtemp(prefix="job_", dir=self.work_dir))
        log = self.work_dir / "blender.log"
        log.write_text("", encoding="utf-8")

        try:
            candidates = [source]

            if source.suffix.lower() in (".zip", ".7z", ".rar"):
                progress("Unpacking the archive…")
                files = unpack(source, job / "unpacked")
                wanted = best_inside([str(path) for path in files])
                candidates = [Path(path) for path in wanted]

                if not candidates:
                    raise ConversionError("The archive has no character file in it.")

            notes: List[str] = []

            for candidate in candidates[:MAX_TRIES]:
                label = name or candidate.stem.replace("_", " ").replace("+", " ").strip()

                if candidate.suffix.lower() in AVATAR_FILES:
                    progress("Adding the avatar…")
                    output = self._free_name(label)
                    shutil.copyfile(candidate, output)

                    return {"file": output.name, "notes": ["It was already a VRM avatar."]}

                if candidate.suffix.lower() not in CHARACTER_FILES:
                    continue

                progress(f"Looking inside {candidate.name}…")
                report = self.inspect(candidate, log)
                decision = plan(report)

                if not decision["possible"]:
                    notes.append(f"{candidate.name}: " + " ".join(decision["notes"]))
                    continue

                progress(f"Converting {candidate.name}. This takes a few minutes…")
                result = self.convert(candidate, report, label, author, log)

                return {
                    "file": result["file"],
                    "notes": decision["notes"] + result.get("warnings", []),
                    "source": candidate.name,
                }

            raise ConversionError(
                "None of the files could become an avatar. " + " ".join(notes)
            )
        finally:
            shutil.rmtree(job, ignore_errors=True)
