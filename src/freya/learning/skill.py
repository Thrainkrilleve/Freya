"""
A versioned skill file, and the lookup that reads approved ones.

A skill is one Markdown file. The front matter is the whole schema.
``format`` is the schema version this reader accepts. ``version`` is
the skill's own number and must match the file name ``v<version>.md``.
The directory name must match ``name``.

Lookup returns nothing unless ``FREYA_LEARNING`` is exactly ``1``.
When it is unset, lookup does not read a file and does not change a
turn. A match is an approved skill whose example request is the same
text as the one asked, after the spaces are folded. A template is
never a match. Measuring a candidate for the promotion gate does not
promote it.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from freya.paths import data_dir


FORMAT_VERSION = 1
MAX_STEPS_BYTES = 2000
NAME = re.compile(r"\A[a-z0-9][a-z0-9-]{0,59}\Z")
FILE_NAME = re.compile(r"\Av([1-9][0-9]*)\.md\Z")
STATUSES = ("template", "candidate", "approved", "retired")
FIELDS = (
    "format",
    "version",
    "name",
    "status",
    "applies_to",
    "tools",
    "steps",
    "check",
    "cases",
)
_LIST_FIELDS = ("applies_to", "tools", "cases")


class SkillError(ValueError):
    """A skill file was not accepted. The message does not quote its method."""


@dataclass(frozen=True)
class Skill:
    format: int
    version: int
    name: str
    status: str
    applies_to: tuple
    tools: tuple
    steps: str
    check: str
    cases: tuple
    path: Optional[Path] = None


def learning_enabled(environ=None) -> bool:
    """True only for the exact value ``1``. Unset is off."""
    environ = os.environ if environ is None else environ
    return environ.get("FREYA_LEARNING") == "1"


def skills_root(environ=None) -> Path:
    """Where approved skills would live. Not read unless lookup is on."""
    environ = os.environ if environ is None else environ
    named = str(environ.get("FREYA_SKILLS", "") or "").strip()
    if named:
        return Path(named).expanduser()
    return data_dir() / "skills"


def template_dir() -> Path:
    """Empty skills for the owner to fill in. Not the lookup root."""
    return Path(__file__).resolve().parent / "templates"


def _split_list(value: str) -> tuple:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _front_matter(text: str) -> dict:
    if not text.startswith("---\n"):
        raise SkillError("A skill file starts with a front matter block.")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise SkillError("A skill file starts with a front matter block.")
    found = {}
    for line in text[4:end].split("\n"):
        if line.strip() == "":
            continue
        if ":" not in line:
            raise SkillError("A skill file has a field this reader does not know.")
        key, value = line.split(":", 1)
        key = key.strip()
        if key not in FIELDS or key in found:
            raise SkillError("A skill file has a field this reader does not know.")
        found[key] = value.strip()
    missing = [key for key in FIELDS if key not in found]
    if missing:
        raise SkillError("A skill file is missing a required field.")
    return found


def _integer(value: str, label: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise SkillError(f"The {label} is not a version this reader accepts.")
    return int(value)


def load_text(text: str, path: Optional[Path] = None) -> Skill:
    """Validate one skill file. A wrong schema version or file name is refused."""
    if not isinstance(text, str):
        raise SkillError("A skill file could not be read.")
    fields = _front_matter(text)
    schema = _integer(fields["format"], "format")
    if schema != FORMAT_VERSION:
        raise SkillError(
            f"This skill file uses format {schema}. This reader accepts format {FORMAT_VERSION}."
        )
    version = _integer(fields["version"], "version")
    name = fields["name"]
    if not NAME.match(name):
        raise SkillError("A skill name is 1 to 60 lowercase letters, digits and dashes.")
    status = fields["status"]
    if status not in STATUSES:
        raise SkillError("A skill status is template, candidate, approved, or retired.")
    steps = fields["steps"]
    if len(steps.encode("utf-8")) > MAX_STEPS_BYTES:
        raise SkillError(f"A skill method is at most {MAX_STEPS_BYTES} bytes.")
    applies_to = _split_list(fields["applies_to"])
    tools = _split_list(fields["tools"])
    cases = _split_list(fields["cases"])
    check = fields["check"]
    if status != "template":
        if not steps.strip() or not check.strip() or not applies_to:
            raise SkillError("A skill that is not a template names a method, a check, and an example.")
        if not 3 <= len(cases) <= 10:
            raise SkillError("A skill that is not a template has 3 to 10 cases.")
    if path is not None:
        file_name = FILE_NAME.match(path.name)
        if file_name is None or int(file_name.group(1)) != version:
            raise SkillError("The file name does not match the skill version.")
        if path.parent.name != name:
            raise SkillError("The directory name does not match the skill name.")
    return Skill(
        format=schema,
        version=version,
        name=name,
        status=status,
        applies_to=applies_to,
        tools=tools,
        steps=steps,
        check=check,
        cases=cases,
        path=path,
    )


def load_file(path: Path) -> Skill:
    """Read and validate one skill file."""
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillError("A skill file could not be read.") from exc
    return load_text(text, file)


def match(request: str, skills: Sequence[Skill]) -> list:
    """
    Approved skills whose example is this request.

    Comparison ignores letter case and repeated spaces. The highest
    version of a name wins. Templates, candidates, and retired skills
    are not returned.
    """
    if not isinstance(request, str):
        return []
    needle = " ".join(request.split()).casefold()
    if needle == "":
        return []
    best = {}
    for skill in skills:
        if skill.status != "approved":
            continue
        examples = {" ".join(item.split()).casefold() for item in skill.applies_to}
        if needle not in examples:
            continue
        current = best.get(skill.name)
        if current is None or skill.version > current.version:
            best[skill.name] = skill
    return [best[name] for name in sorted(best)]


def lookup(request: str, root: Optional[Path] = None, environ=None) -> list:
    """
    Approved skills for this request.

    When learning is off this returns an empty list and does not open
    ``root``. An invalid file is refused only when learning is on.
    """
    if not learning_enabled(environ):
        return []
    folder = Path(root) if root is not None else skills_root(environ)
    if not folder.is_dir():
        return []
    skills = [load_file(path) for path in sorted(folder.glob("*/*.md"))]
    return match(request, skills)


def measurement_request(skill: Skill) -> dict:
    """
    The note the promotion gate would measure. It does not run the gate
    and it does not promote the skill.
    """
    if skill.status == "template":
        raise SkillError("A template is not measured. Nothing was promoted.")
    if skill.status != "candidate":
        raise SkillError("Only a candidate skill is measured. Nothing was promoted.")
    if not skill.steps.strip():
        raise SkillError("A skill with no method is not measured. Nothing was promoted.")
    return {
        "name": skill.name,
        "kind": "skill",
        "version": skill.version,
        "format": skill.format,
        "promoted": False,
    }
