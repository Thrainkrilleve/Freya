"""Loopback HTTP integration tests for the streaming local-model client.

Each test runs a scripted HTTP server on 127.0.0.1 with an ephemeral port.
No model server, external network, or real source code is involved.
"""
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

import local_coding_agent
from local_coding_agent import ask_ollama_cancellable, stream_generation

HEADERS_CHUNKED = (b"HTTP/1.1 200 OK\r\nContent-Type: application/x-ndjson\r\n"
                   b"Transfer-Encoding: chunked\r\n\r\n")


def chunk(data: bytes) -> bytes:
    return b"%x\r\n%s\r\n" % (len(data), data)


def event(text: str, done: bool = False) -> bytes:
    return json.dumps({"response": text, "done": done}).encode() + b"\n"


class ScriptedServer:
    """Accepts one connection, reads the request, then runs `script(server, conn)`.

    When the script returns, the server keeps the connection open (as an
    HTTP/1.1 server may) and records whether the client closes it.
    """

    def __init__(self, script):
        self.script = script
        self.request = b""
        self.conn = None
        self.client_closed = threading.Event()
        self.release = threading.Event()
        self.accepted = threading.Event()
        self.hang_up = False
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.listener.settimeout(0.05)
        self.address = self.listener.getsockname()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        deadline = time.monotonic() + 15
        while not self.release.is_set() and time.monotonic() < deadline:
            try:
                conn, _ = self.listener.accept()
                break
            except socket.timeout:
                continue
            except OSError:
                return
        else:
            return
        self.accepted.set()
        self.conn = conn
        with conn:
            conn.settimeout(10)
            self._read_request(conn)
            try:
                self.script(self, conn)
                if not self.hang_up:
                    self.hold()
            except OSError:
                self.client_closed.set()

    def _read_request(self, conn):
        while b"\r\n\r\n" not in self.request:
            data = conn.recv(65536)
            if not data:
                return
            self.request += data
        head, _, body = self.request.partition(b"\r\n\r\n")
        length = int(next(line.split(b":")[1] for line in head.split(b"\r\n")
                          if line.lower().startswith(b"content-length")))
        while len(body) < length:
            data = conn.recv(65536)
            if not data:
                return
            body += data
        self.request = head + b"\r\n\r\n" + body

    def hold(self, timeout=10):
        """Send nothing until released or the client closes the connection."""
        self.conn.settimeout(0.02)
        deadline = time.monotonic() + timeout
        while not self.release.is_set() and time.monotonic() < deadline:
            try:
                if not self.conn.recv(65536):
                    self.client_closed.set()
                    return
            except socket.timeout:
                continue
            except OSError:
                self.client_closed.set()
                return

    def close(self):
        self.release.set()
        self.thread.join(timeout=15)
        self.listener.close()


class LoopbackStreamingTests(unittest.TestCase):
    def serve(self, script):
        server = ScriptedServer(script)
        self.addCleanup(server.close)
        return server

    def generate(self, server, checkpoint=lambda: None, **limits):
        options = {"timeout": 10, "idle_timeout": 5, "poll_interval": 0.05}
        options.update(limits)
        payload = json.dumps({"model": "mock", "prompt": "synthetic", "stream": True}).encode()
        return stream_generation(server.address, payload, checkpoint, **options)

    # --- Complete replies ---------------------------------------------------

    def test_complete_reply_in_one_packet_returns_without_waiting(self):
        # Regression: the previous client polled the socket while the reply
        # sat in http.client's buffer, so a whole reply delivered at once on
        # a kept-alive connection waited for the full timeout.
        def script(server, conn):
            body = event("hello ") + event("world", done=True)
            conn.sendall(HEADERS_CHUNKED + chunk(body) + b"0\r\n\r\n")
            server.hold()

        server = self.serve(script)
        started = time.monotonic()
        self.assertEqual(self.generate(server), "hello world")
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(server.client_closed.wait(5))

    def test_reply_split_at_every_byte_is_reassembled(self):
        def script(server, conn):
            data = (HEADERS_CHUNKED + chunk(event("hel")) + chunk(event("lo ") + event("wor"))
                    + chunk(event("ld", done=True)) + b"0\r\n\r\n")
            for index in range(len(data)):
                conn.sendall(data[index:index + 1])
            server.hold()

        server = self.serve(script)
        self.assertEqual(self.generate(server), "hello world")

    def test_request_is_a_well_formed_bounded_post(self):
        def script(server, conn):
            conn.sendall(HEADERS_CHUNKED + chunk(event("ok", done=True)))
            server.hold()

        server = self.serve(script)
        self.generate(server)
        head, _, body = server.request.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        self.assertEqual(lines[0], b"POST /api/generate HTTP/1.1")
        self.assertIn(b"Connection: close", lines)
        self.assertEqual(json.loads(body)["prompt"], "synthetic")

    def test_length_delimited_and_close_delimited_bodies(self):
        body = event("a") + event("b", done=True)
        for headers in (b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(body),
                        b"HTTP/1.0 200 OK\r\n\r\n"):
            with self.subTest(headers=headers):
                def script(server, conn, headers=headers):
                    conn.sendall(headers + body)
                    server.hang_up = True

                self.assertEqual(self.generate(self.serve(script)), "ab")

    def test_final_event_without_newline_before_close(self):
        def script(server, conn):
            conn.sendall(b"HTTP/1.1 200 OK\r\n\r\n" + event("a") + event("b", done=True)[:-1])
            server.hang_up = True

        self.assertEqual(self.generate(self.serve(script)), "ab")

    def test_slow_model_load_before_headers_is_tolerated(self):
        # Regression: headers were awaited with a 0.5 second socket timeout.
        def script(server, conn):
            time.sleep(1.0)
            conn.sendall(HEADERS_CHUNKED + chunk(event("ready", done=True)))
            server.hold()

        self.assertEqual(self.generate(self.serve(script)), "ready")

    # --- Revocation and cancellation ----------------------------------------

    def assert_revoked_promptly(self, script, revoke_after):
        server = self.serve(script)
        revoked_at = None

        def checkpoint():
            nonlocal revoked_at
            if time.monotonic() - started >= revoke_after:
                revoked_at = revoked_at or time.monotonic()
                raise PermissionError("revoked")

        started = time.monotonic()
        with self.assertRaises(PermissionError):
            self.generate(server, checkpoint)
        self.assertLess(time.monotonic() - revoked_at, 0.5)
        self.assertTrue(server.client_closed.wait(5), "connection left open")

    def test_revocation_while_waiting_for_headers(self):
        self.assert_revoked_promptly(lambda server, conn: server.hold(), 0.3)

    def test_revocation_while_stalled_inside_a_chunk(self):
        def script(server, conn):
            conn.sendall(HEADERS_CHUNKED + b"40\r\n" + event("part")[:10])
            server.hold()

        self.assert_revoked_promptly(script, 0.3)

    def test_revocation_during_a_continuous_stream(self):
        def script(server, conn):
            conn.sendall(HEADERS_CHUNKED)
            deadline = time.monotonic() + 8
            while not server.release.is_set() and time.monotonic() < deadline:
                conn.sendall(chunk(event("token ")))
                time.sleep(0.01)

        self.assert_revoked_promptly(script, 0.3)

    def test_revoked_after_final_event_returns_nothing(self):
        def script(server, conn):
            conn.sendall(HEADERS_CHUNKED + chunk(event("all", done=True)))
            server.hold()

        server = self.serve(script)
        calls = 0

        def checkpoint():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise PermissionError("revoked")

        with self.assertRaises(PermissionError):
            self.generate(server, checkpoint, poll_interval=1)

    def test_denied_before_connecting(self):
        server = self.serve(lambda server, conn: None)
        with self.assertRaises(PermissionError):
            self.generate(server, lambda: (_ for _ in ()).throw(PermissionError("denied")))
        self.assertFalse(server.accepted.wait(0.3))

    # --- Stalls and timeouts --------------------------------------------------

    def test_stalled_stream_hits_idle_timeout(self):
        def script(server, conn):
            conn.sendall(HEADERS_CHUNKED + chunk(event("partial")))
            server.hold()

        server = self.serve(script)
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "stalled"):
            self.generate(server, idle_timeout=0.5)
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(server.client_closed.wait(5))

    def test_trickling_stream_hits_overall_timeout(self):
        def script(server, conn):
            conn.sendall(HEADERS_CHUNKED)
            deadline = time.monotonic() + 8
            while not server.release.is_set() and time.monotonic() < deadline:
                conn.sendall(b"")
                conn.sendall(chunk(event(".")))
                time.sleep(0.1)

        server = self.serve(script)
        started = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "timed out"):
            self.generate(server, timeout=0.8, idle_timeout=5)
        self.assertLess(time.monotonic() - started, 3)

    def test_connection_closed_before_completion(self):
        def script(server, conn):
            conn.sendall(HEADERS_CHUNKED + chunk(event("partial")))
            server.hang_up = True

        with self.assertRaisesRegex(RuntimeError, "without completion"):
            self.generate(self.serve(script))

    def test_refused_connection_is_reported_without_details(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        address = listener.getsockname()
        listener.close()
        with self.assertRaisesRegex(RuntimeError, "request failed"):
            stream_generation(address, b"{}", lambda: None, timeout=2, idle_timeout=2)

    # --- Malformed and hostile responses -------------------------------------

    def assert_rejected(self, response, error=ValueError):
        def script(server, conn):
            conn.sendall(response)
            server.hold()

        server = self.serve(script)
        with self.assertRaises(error):
            self.generate(server)
        self.assertTrue(server.client_closed.wait(5))

    def test_http_errors_and_redirects_are_rejected(self):
        for status in (301, 307, 400, 404, 500):
            with self.subTest(status=status):
                self.assert_rejected(
                    b"HTTP/1.1 %d X\r\nLocation: http://remote.example/\r\n\r\n" % status,
                    RuntimeError)

    def test_malformed_framing_is_rejected(self):
        for response in (
            b"SMTP 220 hello\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nTransfer-Encoding: chunked\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: -1\r\n\r\n",
            HEADERS_CHUNKED + b"zz\r\n",
            HEADERS_CHUNKED + b"1" * 2000,
            HEADERS_CHUNKED + b"3\r\nabcXY",
            b"HTTP/1.1 200 OK\r\n" + b"X-Pad: " + b"a" * 20_000,
        ):
            with self.subTest(response=response[:60]):
                self.assert_rejected(response)

    def test_malformed_events_are_rejected(self):
        for line, error in ((b"[]\n", ValueError), (b"{not json\n", ValueError),
                            (b'{"response":123,"done":true}\n', ValueError),
                            (b"\xff\xfe\n", ValueError),
                            (b'{"error":"PRIVATE_SOURCE","done":true}\n', RuntimeError)):
            with self.subTest(line=line):
                def script(server, conn, line=line):
                    conn.sendall(HEADERS_CHUNKED + chunk(line))
                    server.hold()

                server = self.serve(script)
                with self.assertRaises(error) as caught:
                    self.generate(server)
                self.assertNotIn("PRIVATE_SOURCE", str(caught.exception))

    def test_generated_text_limit(self):
        def script(server, conn):
            conn.sendall(HEADERS_CHUNKED)
            for _ in range(20):
                conn.sendall(chunk(event("x" * 100)))
            server.hold()

        with patch.object(local_coding_agent, "MAX_RESPONSE_BYTES", 500):
            with self.assertRaisesRegex(ValueError, "size limit"):
                self.generate(self.serve(script))

    def test_endless_line_is_cut_off_at_the_event_limit(self):
        def script(server, conn):
            conn.sendall(HEADERS_CHUNKED)
            deadline = time.monotonic() + 8
            while not server.release.is_set() and time.monotonic() < deadline:
                conn.sendall(chunk(b"a" * 4096))

        with patch.object(local_coding_agent, "MAX_EVENT_BYTES", 64_000):
            with self.assertRaisesRegex(ValueError, "size limit"):
                self.generate(self.serve(script))

    def test_total_body_limit_applies_to_metadata_heavy_streams(self):
        def script(server, conn):
            conn.sendall(HEADERS_CHUNKED)
            padding = json.dumps({"response": "", "done": False, "pad": "p" * 1000}).encode() + b"\n"
            deadline = time.monotonic() + 8
            while not server.release.is_set() and time.monotonic() < deadline:
                conn.sendall(chunk(padding))

        with patch.object(local_coding_agent, "MAX_STREAM_BYTES", 50_000):
            with self.assertRaisesRegex(ValueError, "size limit"):
                self.generate(self.serve(script))


class EndpointBoundaryTests(unittest.TestCase):
    def test_only_fixed_loopback_endpoints_are_used(self):
        with patch("local_coding_agent.stream_generation", return_value="ok") as stream:
            for endpoint, host in (("http://127.0.0.1:11434/api/generate", "127.0.0.1"),
                                   ("http://localhost:11434/api/generate", "localhost")):
                ask_ollama_cancellable(endpoint, "mock", "task", lambda: None)
                self.assertEqual(stream.call_args.args[0], (host, 11434))

    def test_nonlocal_endpoint_rejected_before_connecting(self):
        for endpoint in ("http://remote.example:11434/api/generate",
                         "http://127.0.0.1:11434@remote.example/api/generate",
                         "http://127.0.0.1:11435/api/generate"):
            with self.subTest(endpoint=endpoint):
                with patch("local_coding_agent.socket.create_connection") as connect:
                    with self.assertRaises(ValueError):
                        ask_ollama_cancellable(endpoint, "mock", "task", lambda: None)
                connect.assert_not_called()

    def test_oversized_request_rejected_before_connecting(self):
        with patch("local_coding_agent.socket.create_connection") as connect:
            with self.assertRaisesRegex(ValueError, "request exceeds"):
                ask_ollama_cancellable("http://127.0.0.1:11434/api/generate", "mock",
                                       "x" * (local_coding_agent.MAX_REQUEST_BYTES + 1),
                                       lambda: None)
        connect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
