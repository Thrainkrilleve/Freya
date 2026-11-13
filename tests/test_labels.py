"""
Provisional requirement labels (NEW-x) are given out by docs/labels.md.
Two documents defining the same label happened twice in two weeks; this
keeps it from happening a third time.
"""
import re
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[1] / "docs"
REGISTER = DOCS / "labels.md"

# A label is defined where it is introduced, not where it is cited.
DEFINITION = re.compile(
    r"(?m)^(?:#{1,6}\s+|\|\s*)?\*{0,2}(NEW-[A-Z]+(?:\d+)?)\b\*{0,2}"
    r"(?:\s*[—–-]|\s*\(proposed|\s*\.\*\*|\*\*\s*[—–-])"
)
RANGE = re.compile(r"NEW-([A-Z]+)(?:\d+)?(?:\s+to\s+NEW-([A-Z]+))?")


def label_index(letters):
    """NEW-A is 1, NEW-Z is 26, NEW-AA is 27, as spreadsheet columns."""
    value = 0
    for character in letters:
        value = value * 26 + (ord(character) - ord("A") + 1)
    return value


def label_name(index):
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return "NEW-" + letters


def registered_ranges():
    """{document path: set of labels} as the register gives them."""
    given = {}
    for line in REGISTER.read_text(encoding="utf-8").splitlines():
        if not line.startswith("| NEW-"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        labels, document = cells[0], cells[1].strip("`")
        if document.startswith("unassigned"):
            continue
        found = set()
        for part in labels.split(","):
            match = RANGE.search(part.strip())
            if match is None:
                continue
            start = match.group(1)
            end = match.group(2) or start
            if part.strip().startswith("NEW-LC"):
                found.add(part.strip())
                continue
            for index in range(label_index(start), label_index(end) + 1):
                found.add(label_name(index))
        given[document] = found
    return given


def defined_labels(path):
    return {m.group(1) for m in DEFINITION.finditer(path.read_text(encoding="utf-8"))}


def test_every_registered_document_exists():
    for document in registered_ranges():
        assert (DOCS / document).exists(), document


def test_no_label_is_registered_to_two_documents():
    seen = {}
    for document, labels in registered_ranges().items():
        for label in labels:
            assert label not in seen, f"{label} is given to {seen[label]} and {document}"
            seen[label] = document


def test_each_design_document_defines_only_the_labels_it_was_given():
    given = registered_ranges()
    registered_docs = {DOCS / d for d in given}
    for path in sorted((DOCS / "design").glob("*.md")):
        defined = defined_labels(path)
        if not defined:
            continue
        key = str(path.relative_to(DOCS)).replace("\\", "/")
        allowed = given.get(key, set())
        stray = sorted(defined - allowed)
        assert not stray, f"{key} defines {stray}, which docs/labels.md does not give it"


def test_the_register_names_the_next_free_range():
    text = REGISTER.read_text(encoding="utf-8")
    match = re.search(r"Next free range starts at (NEW-[A-Z]+)", text)
    assert match, "docs/labels.md must say where the next range starts"
    highest = max(
        label_index(label[4:])
        for labels in registered_ranges().values()
        for label in labels
        if not label.startswith("NEW-LC")
    )
    assert label_index(match.group(1)[4:]) == highest + 1
