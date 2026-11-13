"""Synthetic transport tests; no model server or network connections."""
import http.client
import json
import unittest
import traceback
from unittest.mock import Mock, patch

from local_coding_agent import MAX_RESPONSE_BYTES, ask_ollama


class OllamaTransportTests(unittest.TestCase):
    endpoint = "http://127.0.0.1:11434/api/generate"

    def connection(self, status=200, body=b'{"response":"proposal"}'):
        connection = Mock()
        response = connection.getresponse.return_value
        response.status = status
        response.read.return_value = body
        return connection, response

    def test_only_exact_loopback_endpoints_are_accepted(self):
        for endpoint in ("http://remote.example:11434/api/generate",
                         self.endpoint + "?redirect=1", "file:///demo.py",
                         "http://127.0.0.1:11434@remote.example/api/generate"):
            with self.subTest(endpoint=endpoint):
                with patch("local_coding_agent.http.client.HTTPConnection") as factory:
                    with self.assertRaises(ValueError):
                        ask_ollama(endpoint, "mock", "synthetic source")
                    factory.assert_not_called()

    def test_direct_loopback_post_ignores_proxy_environment(self):
        connection, response = self.connection()
        with patch.dict("os.environ", {"HTTP_PROXY": "http://remote.example:8080",
                                      "http_proxy": "http://remote.example:8080"}):
            with patch("local_coding_agent.http.client.HTTPConnection",
                       return_value=connection) as factory:
                self.assertEqual(ask_ollama(self.endpoint, "mock", "synthetic source"),
                                 "proposal")
        factory.assert_called_once_with("127.0.0.1", 11434, timeout=240)
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ("POST", "/api/generate"))
        self.assertEqual(json.loads(kwargs["body"])["prompt"], "synthetic source")
        response.read.assert_called_once_with(MAX_RESPONSE_BYTES + 1)
        connection.close.assert_called_once()

    def test_localhost_alias_uses_direct_loopback_transport(self):
        connection, _ = self.connection()
        with patch("local_coding_agent.http.client.HTTPConnection",
                   return_value=connection) as factory:
            ask_ollama("http://localhost:11434/api/generate", "mock", "synthetic")
        factory.assert_called_once_with("localhost", 11434, timeout=240)

    def test_redirects_and_http_errors_are_rejected_without_reading_body(self):
        for status in (301, 302, 303, 307, 308, 400, 500):
            connection, response = self.connection(status)
            with self.subTest(status=status):
                with patch("local_coding_agent.http.client.HTTPConnection",
                           return_value=connection) as factory:
                    with self.assertRaises(RuntimeError):
                        ask_ollama(self.endpoint, "mock", "synthetic source")
                self.assertEqual(factory.call_count, 1)
                response.read.assert_not_called()
                connection.close.assert_called_once()

    def test_transport_errors_close_connection_without_logging_payload(self):
        for error in (OSError("PRIVATE_SOURCE"), http.client.HTTPException("PRIVATE_SOURCE")):
            connection, _ = self.connection()
            connection.request.side_effect = error
            with self.subTest(error=type(error).__name__):
                with patch("local_coding_agent.http.client.HTTPConnection", return_value=connection):
                    with self.assertRaises(RuntimeError) as caught:
                        ask_ollama(self.endpoint, "mock", "synthetic source")
                self.assertNotIn("PRIVATE_SOURCE", str(caught.exception))
                self.assertNotIn("PRIVATE_SOURCE", "".join(traceback.format_exception(caught.exception)))
                connection.close.assert_called_once()

    def test_oversized_response_is_rejected(self):
        connection, _ = self.connection(body=b"x" * (MAX_RESPONSE_BYTES + 1))
        with patch("local_coding_agent.http.client.HTTPConnection", return_value=connection):
            with self.assertRaises(ValueError):
                ask_ollama(self.endpoint, "mock", "synthetic source")
        connection.close.assert_called_once()

    def test_malformed_and_nontext_responses_are_rejected(self):
        for body in (b"{", b"[]", b"{}", b'{"response":123}', b'{"response":null}'):
            connection, _ = self.connection(body=body)
            with self.subTest(body=body):
                with patch("local_coding_agent.http.client.HTTPConnection", return_value=connection):
                    with self.assertRaises(ValueError):
                        ask_ollama(self.endpoint, "mock", "synthetic source")
                connection.close.assert_called_once()

    def test_server_error_with_response_is_rejected(self):
        connection, _ = self.connection(body=b'{"response":"partial","error":"PRIVATE_SOURCE"}')
        with patch("local_coding_agent.http.client.HTTPConnection", return_value=connection):
            with self.assertRaises(RuntimeError) as caught:
                ask_ollama(self.endpoint, "mock", "synthetic source")
        self.assertNotIn("PRIVATE_SOURCE", str(caught.exception))
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
