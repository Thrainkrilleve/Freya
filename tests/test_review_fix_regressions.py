import json
from datetime import timedelta

import pytest

from freya.delegation.screening import Corrections, screen_brief
from freya.delegation.dispatch import DispatchRefused
from freya.storage import backup
from test_delegation_dispatch import Harness, ROUTE, NOW


@pytest.mark.parametrize('content', [
    'not json', '[]', '{"always_private_phrases": null}',
    '{"always_private_memories": [9]}',
])
def test_unreadable_privacy_rules_hold_even_an_ordinary_brief(tmp_path, content):
    path = tmp_path / 'corrections.json'
    original = Corrections(path)
    original.mark_always_private(phrase='synthetic cobalt hawk')
    path.write_text(content, encoding='utf-8')
    corrections = Corrections(path)
    assert screen_brief('Review an ordinary function.', [], corrections).held
    assert screen_brief('synthetic cobalt hawk', [], corrections).held


def test_unreadable_rules_cannot_be_overwritten_by_a_new_mark(tmp_path):
    path = tmp_path / 'corrections.json'
    path.write_text('broken', encoding='utf-8')
    corrections = Corrections(path)
    with pytest.raises(ValueError):
        corrections.mark_not_private(phrase='ordinary')
    assert path.read_text('utf-8') == 'broken'
    with pytest.raises(ValueError):
        corrections.remove('always_private_phrases', 'ordinary')


def test_missing_new_rules_and_recovered_rules_still_work(tmp_path):
    path = tmp_path / 'corrections.json'
    assert not screen_brief('ordinary', [], Corrections(path)).held
    path.write_text('broken', encoding='utf-8')
    assert screen_brief('ordinary', [], Corrections(path)).held
    path.write_text(json.dumps({'always_private_phrases': ['cobalt hawk']}), 'utf-8')
    recovered = Corrections(path)
    assert not screen_brief('ordinary', [], recovered).held
    assert screen_brief('cobalt hawk', [], recovered).held


def test_os_read_failure_holds_cloud_screening(tmp_path, monkeypatch):
    path = tmp_path / 'corrections.json'
    def denied(*args, **kwargs):
        raise PermissionError('synthetic denial')
    monkeypatch.setattr(type(path), 'read_text', denied)
    assert screen_brief('ordinary', [], Corrections(path)).held


@pytest.mark.parametrize('release', [False, True])
def test_unreadable_rules_block_authorization_even_with_hold_release(tmp_path, release):
    h = Harness(tmp_path)
    try:
        h.corrections.path.write_text('broken', 'utf-8')
        h.dispatcher._corrections = Corrections(h.corrections.path)
        with pytest.raises(DispatchRefused):
            h.dispatcher.authorize(
                h.compose(), ROUTE, 'task-1', owner_released_hold=release, now=NOW,
            )
        assert h.asked == []
        assert h.used() == 0
    finally:
        h.store.close()


def test_unreadable_rules_block_a_previously_authorized_send(tmp_path):
    h = Harness(tmp_path)
    try:
        brief = h.compose()
        authorization = h.dispatcher.authorize(brief, ROUTE, 'task-1', now=NOW)
        h.corrections.path.write_text('broken', 'utf-8')
        h.dispatcher._corrections = Corrections(h.corrections.path)
        with pytest.raises(DispatchRefused):
            h.dispatcher.send(authorization, brief.text)
        assert h.asked == []
        assert h.used() == 0
    finally:
        h.store.close()


@pytest.mark.parametrize('content', [None, 'broken', '{}', '{"copied": []}'])
def test_partial_backup_does_not_suppress_retry(tmp_path, monkeypatch, content):
    target = tmp_path / 'backups'
    partial = target / NOW.replace(tzinfo=None).strftime(backup.STAMP_FORMAT)
    partial.mkdir(parents=True)
    if content is not None:
        (partial / 'manifest.json').write_text(content, 'utf-8')
    assert backup.latest(target) is None
    calls = []
    monkeypatch.setattr(backup, 'back_up', lambda *args: calls.append(args) or 'attempted')
    result = backup.back_up_on_close(
        tmp_path / 'synthetic-source', NOW.replace(tzinfo=None) + timedelta(minutes=5),
        environ={backup.TARGET_VARIABLE: str(target)},
    )
    assert result == 'attempted'
    assert len(calls) == 1


def test_only_completed_backup_counts_for_retry_timer(tmp_path):
    source, target = tmp_path / 'synthetic-source', tmp_path / 'backups'
    source.mkdir()
    (source / 'freya_settings.json').write_text('{}', 'utf-8')
    moment = NOW.replace(tzinfo=None)
    folder = backup.back_up(source, target, moment, allow_same_drive=True)
    (target / (moment + timedelta(minutes=5)).strftime(backup.STAMP_FORMAT)).mkdir()
    assert backup.latest(target) == moment
    assert not (folder / 'manifest.tmp').exists()
    (folder / 'freya_settings.json').unlink()
    assert backup.latest(target) is None


def test_failed_manifest_publication_is_not_a_completed_backup(tmp_path, monkeypatch):
    source, target = tmp_path / 'synthetic-source', tmp_path / 'backups'
    source.mkdir()
    (source / 'freya_settings.json').write_text('{}', 'utf-8')
    def interrupted(*args):
        raise OSError('synthetic interruption')
    monkeypatch.setattr(backup.os, 'replace', interrupted)
    with pytest.raises(OSError):
        backup.back_up(source, target, NOW.replace(tzinfo=None), allow_same_drive=True)
    assert backup.latest(target) is None
