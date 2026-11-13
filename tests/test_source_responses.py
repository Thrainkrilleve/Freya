from types import SimpleNamespace

import pytest

from freya.delegation.dispatch import Route, DispatchRefused
from freya.delegation.presentation import reply_publisher
from freya.events import EventBus
from freya.gateway.answer_media import media_document, public_https
from test_delegation_dispatch import Harness, ROUTE, NOW
from test_gateway import get, sign_in, Gateway, Recorder, ImageGallery
import test_cloud_delegation_service as cloud_tests


def test_trusted_route_supplies_source_and_bus_replays_the_answer():
    bus = EventBus()
    route = Route('test-route', 'test-vendor', lambda text: None, source_name='Codex')
    reply = SimpleNamespace(text='## Answer\n![Picture](https://example.org/picture.png)', model='test-model')
    reply_publisher(bus.publish)(route, reply)
    event = bus.history()[0]
    assert event['type'] == 'source_response'
    assert event['source'] == {'name': 'Codex', 'provider': 'test-vendor', 'route': 'test-route', 'model': 'test-model'}
    assert event['text'] == reply.text
    assert not event['truncated']
    seen = []
    assert bus.subscribe_with_history(seen.append) == [event]
    bus.publish('assistant_start')
    bus.publish('assistant_chunk', text='Here is how I would use that.')
    bus.publish('assistant_end')
    assert [item['type'] for item in seen] == ['assistant_start', 'assistant_chunk', 'assistant_end']


def test_long_output_is_explicitly_marked_shortened():
    bus = EventBus()
    route = Route('route', 'vendor', lambda text: None, source_name='Claude')
    reply_publisher(bus.publish)(route, SimpleNamespace(text='x' * 64001, model=None))
    event = bus.history()[0]
    assert event['truncated'] and len(event['text']) == 64000


def test_completed_dispatch_emits_one_attributed_answer_without_an_extra_model_call(tmp_path):
    h = Harness(tmp_path)
    bus = EventBus()
    h.dispatcher._on_reply = reply_publisher(bus.publish)
    try:
        brief = h.compose()
        authorization = h.dispatcher.authorize(brief, ROUTE, 'task-1', now=NOW)
        answer = h.dispatcher.send(authorization, brief.text)
        assert len(h.asked) == 1
        assert bus.history()[0]['text'] == answer.text
        with pytest.raises(DispatchRefused):
            h.dispatcher.send(authorization, brief.text)
        assert len(bus.history()) == 1
    finally:
        h.store.close()


def test_display_failure_does_not_replay_or_change_completed_accounting(tmp_path):
    h = Harness(tmp_path)
    def failed(*args):
        raise RuntimeError('synthetic display failure')
    h.dispatcher._on_reply = failed
    try:
        brief = h.compose()
        authorization = h.dispatcher.authorize(brief, ROUTE, 'task-1', now=NOW)
        assert h.dispatcher.send(authorization, brief.text).text
        assert h.used() == 120 and len(h.asked) == 1
    finally:
        h.store.close()


def test_service_publishes_the_actual_route_name_after_a_fake_provider_reply(tmp_path, monkeypatch, fake_ollama):
    bus = EventBus()
    build = cloud_tests.build_cloud_delegation
    monkeypatch.setattr(cloud_tests, 'build_cloud_delegation', lambda *args, **kwargs: build(*args, **kwargs, publish=bus.publish))
    setup = cloud_tests.Setup(tmp_path)
    try:
        setup.open_everything()
        answer = setup.send()
        event = bus.history()[0]
        assert event['source']['name'] == 'Claude'
        assert event['source']['route'] == 'anthropic-plan'
        assert event['text'] == answer.text == '46'
        assert setup.ran()  # Only the synthetic fixture program was invoked.
    finally:
        setup.close()


@pytest.mark.parametrize('url', [
    'javascript:alert(1)', 'data:image/png;base64,AA', 'file:///secret',
    'http://example.org/image.png', 'https://localhost/image.png',
    'https://127.0.0.1/image.png', 'https://10.0.0.1/image.png',
    'https://[::1]/image.png', 'https://example.local/image.png',
    'https://user:password@example.org/image.png', 'https://example.org:8443/image.png',
    'https://example.org/has space', 'https://example.org\\@localhost/image.png',
    # The same hosts spelt in forms a browser accepts.
    'https://localhost./image.png', 'https://x.localhost./image.png',
    'https://example.local./image.png', 'https://127.1/image.png',
    'https://10.1/image.png', 'https://192.168.1/image.png',
    'https://0177.0.0.1/image.png', 'https://0x7f.1/image.png',
])
def test_media_rejects_unsafe_urls(url):
    assert public_https(url) is None
    assert media_document('image', url) is None


def test_media_document_is_script_free_escaped_and_sandboxed():
    document, policy = media_document('image', 'https://example.org/picture.png?a="quoted"&b=2')
    assert '&quot;' in document and '&amp;' in document
    assert '<script' not in document and 'onerror' not in document
    assert "script-src 'none'" in policy and 'sandbox' in policy
    assert media_document('video', 'https://example.org/movie.mp4')
    assert media_document('video', 'https://example.org/page.html') is None


def test_gateway_media_requires_session_and_does_not_fetch_remote_content(tmp_path):
    bus = EventBus()
    recorder = Recorder(bus)
    gateway = Gateway(bus, ImageGallery(tmp_path / 'images'), recorder.actions(), port=0)
    gateway.start()
    try:
        path = '/answer-media?kind=image&url=https%3A%2F%2Fexample.org%2Fpicture.png'
        assert get(gateway, path)[0].status == 403
        cookie = sign_in(gateway)
        response, body = get(gateway, path, cookie=cookie)
        assert response.status == 200 and b'https://example.org/picture.png' in body
        assert len(response.headers.get_all('Content-Security-Policy')) == 1
        assert "script-src 'none'" in response.getheader('Content-Security-Policy')
        assert 'sandbox' in response.getheader('Content-Security-Policy')
        assert get(gateway, '/answer-media?kind=image&url=file%3A%2F%2F%2Fsecret', cookie=cookie)[0].status == 400
        assert get(gateway, path + '&kind=video', cookie=cookie)[0].status == 400
        assert get(gateway, '/rich-chat.js', cookie=cookie)[0].status == 200
    finally:
        gateway.stop()
