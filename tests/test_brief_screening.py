import json

import pytest

from freya.delegation.screening import (
    MATCH_ALWAYS_PRIVATE,
    MATCH_MEANING,
    MATCH_NAME,
    MATCH_PHRASE,
    Corrections,
    screen_brief,
)


# Made-up memories about made-up people.
MEMORIES = [
    ("1", "Type: person; key: sister; The user's sister Oriel Vantongeren lives in Tromso."),
    ("2", "Type: preference; key: tea; The user drinks smoked lapsang tea every single morning."),
    ("3", "Type: hardware; key: nas; The backup server answers on port 48213."),
    ("4", "Type: fact; key: contact; Write to the user at quill.marrow@example.net."),
]


@pytest.fixture
def corrections(tmp_path):
    return Corrections(tmp_path / "screening_corrections.json")


def kinds(result):
    return sorted({match.kind for match in result.matches})


def test_an_ordinary_brief_is_not_held():
    result = screen_brief(
        "Review this function for off-by-one mistakes and reply with the "
        "line number. The user wants a short answer on Tuesday.",
        MEMORIES,
    )

    assert not result.held


def test_a_name_from_memory_holds_the_brief():
    result = screen_brief(
        "Draft a birthday note for Oriel, who has just moved north.", MEMORIES,
    )

    assert result.held
    assert [(m.kind, m.text, m.memory_id) for m in result.matches] == [
        (MATCH_NAME, "oriel", "1"),
    ]


def test_addresses_and_long_numbers_hold_the_brief():
    address = screen_brief("Send it to Quill.Marrow@Example.net please.", MEMORIES)
    number = screen_brief("Check whether 48213 is open on the firewall.", MEMORIES)

    assert [m.text for m in address.matches] == ["quill.marrow@example.net"]
    assert [m.text for m in number.matches] == ["48213"]


def test_words_copied_from_a_memory_hold_the_brief():
    result = screen_brief(
        "Suggest a gift for someone who drinks smoked lapsang tea every day.",
        MEMORIES,
    )

    assert kinds(result) == [MATCH_PHRASE]
    assert {m.memory_id for m in result.matches} == {"2"}


def test_everyday_and_sentence_opening_words_do_not_hold_a_brief():
    memories = [("9", "Write the summary on Monday. The user likes Freya.")]

    result = screen_brief("Write a note. The deadline is Monday.", memories)

    assert not result.held


def test_closeness_in_meaning_holds_a_brief_when_supplied():
    def closeness(sentence):
        return [("2", 0.91)] if "morning drink" in sentence else [("2", 0.30)]

    held = screen_brief(
        "Find a present. Their morning drink is a smoky black brew.",
        MEMORIES, closeness=closeness,
    )
    passed = screen_brief(
        "Find a present for a keen gardener.", MEMORIES, closeness=closeness,
    )

    assert [(m.kind, m.memory_id, m.score) for m in held.matches] == [
        (MATCH_MEANING, "2", 0.91),
    ]
    assert not passed.held


def test_not_private_clears_one_phrase_and_nothing_else(corrections):
    corrections.mark_not_private(phrase="Oriel")

    cleared = screen_brief("A note for Oriel.", MEMORIES, corrections)
    other = screen_brief("A note for Oriel Vantongeren.", MEMORIES, corrections)

    assert not cleared.held
    assert [m.text for m in other.matches] == ["vantongeren"]


def test_not_private_can_clear_one_whole_memory(corrections):
    corrections.mark_not_private(memory_id="3")

    assert not screen_brief("Is 48213 open?", MEMORIES, corrections).held
    assert screen_brief("A note for Oriel.", MEMORIES, corrections).held


def test_always_private_holds_a_phrase_no_memory_mentions(corrections):
    corrections.mark_always_private(phrase="Project Heron")

    result = screen_brief(
        "Summarise the status of project heron for the board.", [], corrections,
    )

    assert [(m.kind, m.text) for m in result.matches] == [
        (MATCH_ALWAYS_PRIVATE, "project heron"),
    ]


def test_always_private_wins_over_not_private(corrections):
    corrections.mark_not_private(memory_id="1")
    corrections.mark_not_private(phrase="Oriel")
    corrections.mark_always_private(memory_id="1")

    result = screen_brief("A note for Oriel.", MEMORIES, corrections)

    assert result.held
    # However it matched, it is reported as always private.
    assert kinds(result) == [MATCH_ALWAYS_PRIVATE]


def test_corrections_are_kept_listed_and_removable(tmp_path):
    path = tmp_path / "screening_corrections.json"
    first = Corrections(path)
    first.mark_not_private(phrase="  Oriel ")
    first.mark_always_private(memory_id=4)
    first.mark_always_private(phrase="Project Heron")

    second = Corrections(path)

    assert second.listing() == {
        "not_private_phrases": ["oriel"],
        "not_private_memories": [],
        "always_private_phrases": ["project heron"],
        "always_private_memories": ["4"],
    }

    second.remove("not_private_phrases", "oriel")

    assert Corrections(path).listing()["not_private_phrases"] == []
    assert screen_brief("A note for Oriel.", MEMORIES, Corrections(path)).held


def test_bad_corrections_are_refused(corrections):
    with pytest.raises(ValueError):
        corrections.mark_not_private()
    with pytest.raises(ValueError):
        corrections.mark_not_private(phrase="x", memory_id="1")
    with pytest.raises(ValueError):
        corrections.mark_always_private(phrase="  ...  ")
    with pytest.raises(ValueError):
        corrections.remove("everything", "x")


@pytest.mark.parametrize(
    "content",
    ["not json", "[]", json.dumps({"not_private_phrases": "oriel", "not_private_memories": [1, None]})],
)
def test_a_damaged_file_means_no_corrections(tmp_path, content):
    path = tmp_path / "screening_corrections.json"
    path.write_text(content, encoding="utf-8")

    corrections = Corrections(path)

    assert all(not entries for entries in corrections.listing().values())
    assert screen_brief("A note for Oriel.", MEMORIES, corrections).held


def test_a_loose_paraphrase_passes_which_is_the_known_limit():
    # Recorded so nobody mistakes the filter for a guarantee.
    result = screen_brief(
        "Suggest a present for a relative in the far north of Norway.",
        MEMORIES,
    )

    assert not result.held
