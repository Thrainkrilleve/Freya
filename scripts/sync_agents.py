"""Give every coding agent listed in agents.toml the shared rules and skills.

The maintained source is AGENTS.md and .agents/skills/. Agents that read those
directly (Codex, Gemini CLI) need nothing. Agents that look elsewhere get a
pointer file for the rules (Claude Code reads CLAUDE.md) and a byte-exact copy
of the skills (Claude Code reads .claude/skills/).

    python scripts/sync_agents.py           write the copies and pointer files
    python scripts/sync_agents.py --check   change nothing; exit 1 if anything is out of date
    python scripts/sync_agents.py --list    show the agents and what each one reads

Adding an agent is one table in agents.toml; this script needs no change.

Rules this script keeps:
- It never edits an existing rules file. A missing one is created as a pointer
  to the shared rules; an existing one that never mentions them is reported.
- It only manages copied skills it made (each holds a marker file), so a skill
  someone added by hand to an agent's folder is never touched or deleted.
- Copies are byte-exact, so line endings survive on Windows.

Standard library only, so it runs before `uv sync`.
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = "agents.toml"
MARKER = ".synced-from-agents"
SKIPPED_NAMES = {"__pycache__", "node_modules", ".DS_Store", MARKER}
FRONT_MATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)
SKILL_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass
class Agent:
    key: str
    name: str
    enabled: bool
    rules_file: str | None
    skills_dir: str | None
    roles: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class Registry:
    rules: str
    skills: str
    agents: list[Agent]


@dataclass
class Report:
    changes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def load_registry(root: Path) -> Registry:
    data = tomllib.loads((root / REGISTRY).read_text(encoding="utf-8"))
    source = data.get("source", {})
    agents = []
    for key, entry in data.get("agents", {}).items():
        agents.append(
            Agent(
                key=key,
                name=entry.get("name", key),
                enabled=bool(entry.get("enabled", False)),
                rules_file=entry.get("rules_file"),
                skills_dir=entry.get("skills_dir"),
                roles=list(entry.get("roles", [])),
                notes=entry.get("notes", ""),
            )
        )
    return Registry(
        rules=source.get("rules", "AGENTS.md"),
        skills=source.get("skills", ".agents/skills"),
        agents=agents,
    )


def skill_files(folder: Path) -> dict[str, bytes]:
    """Every file of one skill, keyed by its path inside the skill (with /)."""
    files = {}
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(folder)
        if any(part in SKIPPED_NAMES for part in relative.parts):
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        files[relative.as_posix()] = path.read_bytes()
    return files


def validate_skill(folder: Path, report: Report) -> None:
    """A skill is a folder with a SKILL.md whose front matter names it and says when to use it."""
    skill_md = folder / "SKILL.md"
    if not skill_md.is_file():
        report.problems.append(f"{folder.name}: no SKILL.md")
        return
    match = FRONT_MATTER.match(skill_md.read_text(encoding="utf-8"))
    if match is None:
        report.problems.append(f"{folder.name}: SKILL.md has no front matter")
        return
    fields = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            name, value = line.split(":", 1)
            fields[name.strip()] = value.strip()
    if fields.get("name") != folder.name:
        report.problems.append(f"{folder.name}: front matter name must be '{folder.name}'")
    if not SKILL_NAME.match(folder.name):
        report.problems.append(f"{folder.name}: use lowercase words joined by hyphens")
    if not fields.get("description"):
        report.problems.append(f"{folder.name}: front matter needs a description")


def source_skills(root: Path, registry: Registry, report: Report) -> dict[str, dict[str, bytes]]:
    base = root / registry.skills
    if not base.is_dir():
        report.problems.append(f"{registry.skills} does not exist")
        return {}
    skills = {}
    for folder in sorted(p for p in base.iterdir() if p.is_dir()):
        if folder.name.startswith(".") or folder.name in SKIPPED_NAMES:
            continue
        validate_skill(folder, report)
        skills[folder.name] = skill_files(folder)
    return skills


def sync_skills(
    root: Path,
    agent: Agent,
    source: str,
    skills: dict[str, dict[str, bytes]],
    check: bool,
    report: Report,
) -> None:
    target = root / agent.skills_dir
    for name, files in skills.items():
        folder = target / name
        if folder.exists() and not (folder / MARKER).exists():
            report.problems.append(
                f"{agent.skills_dir}/{name} was not made by this script; rename one of the two"
            )
            continue
        current = skill_files(folder) if folder.exists() else {}
        for relative, content in files.items():
            if current.get(relative) != content:
                report.changes.append(f"{agent.skills_dir}/{name}/{relative}")
                if not check:
                    path = folder / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(content)
        for relative in sorted(set(current) - set(files)):
            report.changes.append(f"{agent.skills_dir}/{name}/{relative} (removed)")
            if not check:
                (folder / relative).unlink()
        marker = folder / MARKER
        if not marker.exists():
            report.changes.append(f"{agent.skills_dir}/{name}/{MARKER}")
            if not check:
                folder.mkdir(parents=True, exist_ok=True)
                marker.write_text(
                    f"Copied from {source}/{name} by "
                    "scripts/sync_agents.py. Edit the source, not this copy.\n",
                    encoding="utf-8",
                    newline="\n",
                )
    if target.is_dir():
        for folder in sorted(p for p in target.iterdir() if p.is_dir()):
            if folder.name not in skills and (folder / MARKER).exists():
                report.changes.append(f"{agent.skills_dir}/{folder.name}/ (skill removed)")
                if not check:
                    shutil.rmtree(folder)


def pointer_text(rules: str) -> str:
    return (
        f"@{rules}\n\n"
        f"The rules for every coding agent in this repository are in `{rules}`; the line\n"
        "above imports them. Shared skills are listed in `agents.toml`. Put anything\n"
        "meant for all agents in those files, not here.\n"
    )


def sync_rules(root: Path, registry: Registry, agent: Agent, check: bool, report: Report) -> None:
    path = root / agent.rules_file
    if not path.exists():
        report.changes.append(f"{agent.rules_file} (created as a pointer to {registry.rules})")
        if not check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(pointer_text(registry.rules), encoding="utf-8", newline="\n")
        return
    if registry.rules not in path.read_text(encoding="utf-8"):
        report.warnings.append(f"{agent.rules_file} never mentions {registry.rules}")


def run(root: Path, check: bool) -> Report:
    report = Report()
    registry = load_registry(root)
    if not (root / registry.rules).is_file():
        report.problems.append(f"{registry.rules} does not exist")
    skills = source_skills(root, registry, report)
    for agent in registry.agents:
        if not agent.enabled:
            continue
        if agent.rules_file and agent.rules_file != registry.rules:
            sync_rules(root, registry, agent, check, report)
        if agent.skills_dir and agent.skills_dir != registry.skills:
            sync_skills(root, agent, registry.skills, skills, check, report)
    return report


def describe(root: Path) -> str:
    registry = load_registry(root)
    lines = [f"Shared rules: {registry.rules}", f"Shared skills: {registry.skills}", ""]
    for agent in registry.agents:
        state = "on " if agent.enabled else "off"
        rules = agent.rules_file or registry.rules
        skills = agent.skills_dir or registry.skills
        roles = ", ".join(agent.roles) or "-"
        lines.append(f"[{state}] {agent.name} ({agent.key}): rules {rules}; skills {skills}")
        lines.append(f"      roles: {roles}")
        if agent.notes:
            lines.append(f"      {agent.notes}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="change nothing; report drift")
    parser.add_argument("--list", action="store_true", help="show the agents")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.list:
        print(describe(args.root))
        return 0

    report = run(args.root, check=args.check)
    for warning in report.warnings:
        print(f"warning: {warning}")
    for problem in report.problems:
        print(f"problem: {problem}")
    if args.check:
        for change in report.changes:
            print(f"out of date: {change}")
        if report.changes:
            print("Run `python scripts/sync_agents.py` after editing the skills or agents.toml.")
    else:
        for change in report.changes:
            print(f"updated: {change}")
    if report.problems or (args.check and report.changes):
        return 1
    if not report.changes:
        print("Every agent's rules and skills are up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
