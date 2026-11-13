"""End-to-end: broker -> policy -> journal -> loopback model stream -> proposal.

Uses the real CapabilityBroker, SQLite policy and audit log, and a scripted
HTTP server on 127.0.0.1 standing in for Ollama. Nothing leaves the machine
and the source repository is never modified.

Run with: PYTHONPATH=src:devtools python -m pytest devtools/test_coding_loopback_integration.py
"""
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import local_coding_agent
from coding_broker_adapter import register_coding_proposal
from coding_capability_contract import CODING_PROPOSE
from coding_task_journal import TaskJournal
from test_coding_ollama_cancellation import HEADERS_CHUNKED, ScriptedServer, chunk, event

from freya.core.audit import SqliteAuditLog
from freya.core.capabilities import CapabilityBroker, CapabilityRegistry
from freya.core.persistence import ControlStore
from freya.core.policy import SqlitePolicyEngine
from freya.core.providers import ProviderRegistry

PATCH = "--- a/example.py\n+++ b/example.py\n@@ -1 +1 @@\n-answer = 1\n+answer = 2\n"


@unittest.skipUnless(shutil.which("git"), "git is required for patch verification")
class CodingLoopbackIntegrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "example.py").write_bytes(b"answer = 1\n")

        self.store = ControlStore(self.root / "control.db")
        self.addCleanup(self.store.close)
        self.policy = SqlitePolicyEngine(self.store)
        self.audit = SqliteAuditLog(self.store, checkpoint_interval=0)
        self.audit.start()
        providers = ProviderRegistry()
        capabilities = CapabilityRegistry(providers)
        register_coding_proposal(
            SimpleNamespace(providers=providers, capabilities=capabilities),
            repo=self.repo, journal_dir=self.root / "journal",
            output_dir=self.root / "patches", model="test-model")
        self.broker = CapabilityBroker(capabilities, self.policy, self.audit)

    def serve(self, script):
        server = ScriptedServer(script)
        self.addCleanup(server.close)
        port = patch.object(local_coding_agent, "OLLAMA_PORT", server.address[1])
        port.start()
        self.addCleanup(port.stop)
        return server

    def grant(self):
        return self.policy.add_rule("local-owner", CODING_PROPOSE, "allow", "Test grant.")

    def journal_states(self):
        [path] = (self.root / "journal").glob("*.json")
        return [e.state for e in TaskJournal(path.parent, path.stem).read()]

    def audit_outcomes(self):
        return [row["outcome"] for row in self.store.execute(
            "SELECT outcome FROM audit_event WHERE kind = 'outcome' ORDER BY event_id")]

    def test_streamed_proposal_is_verified_but_never_applied(self):
        def script(server, conn):
            body = b"".join(chunk(event(PATCH[i:i + 7])) for i in range(0, len(PATCH), 7))
            conn.sendall(HEADERS_CHUNKED + body + chunk(event("", done=True)))

        server = self.serve(script)
        self.grant()

        with patch("patch_applicability.subprocess.run",
                   wraps=subprocess.run) as run:
            result = self.broker.invoke(CODING_PROPOSE, "change answer", ["example.py"])

        self.assertEqual(result.outcome, "succeeded", result.error)
        self.assertEqual(result.result["state"], "verified")
        self.assertEqual(Path(result.result["patch"]).read_text(encoding="utf-8"), PATCH)
        # The repository is untouched and git was only asked to check.
        self.assertEqual((self.repo / "example.py").read_bytes(), b"answer = 1\n")
        for call in run.call_args_list:
            self.assertIn("--check", call.args[0])
        self.assertEqual(self.journal_states(),
                         ["requested", "generating", "proposed", "verified"])
        self.assertEqual(json.loads(server.request.partition(b"\r\n\r\n")[2])["model"],
                         "test-model")
        self.assertEqual(self.audit_outcomes(), ["succeeded"])

    def test_revoking_the_grant_mid_stream_stops_promptly(self):
        streaming = threading.Event()

        def script(server, conn):
            conn.sendall(HEADERS_CHUNKED + chunk(event(PATCH[:10])))
            streaming.set()
            # Then the model goes quiet, as during a long generation.

        self.serve(script)
        rule_id = self.grant()
        revoked_at = []

        def revoke():
            streaming.wait(10)
            time.sleep(0.2)
            self.policy.remove_rule(rule_id)
            revoked_at.append(time.monotonic())

        revoker = threading.Thread(target=revoke)
        revoker.start()
        result = self.broker.invoke(CODING_PROPOSE, "change answer", ["example.py"])
        finished = time.monotonic()
        revoker.join(10)

        self.assertEqual(result.outcome, "failed-known")
        self.assertIn("revoked", result.error)
        self.assertLess(finished - revoked_at[0], 1.0)
        self.assertFalse(list((self.root / "patches").glob("*.patch")))
        self.assertEqual((self.repo / "example.py").read_bytes(), b"answer = 1\n")
        self.assertEqual(self.journal_states(), ["requested", "generating", "failed"])
        self.assertEqual(self.audit_outcomes(), ["failed-known"])

    def test_model_output_that_is_not_a_patch_is_discarded(self):
        def script(server, conn):
            reply = "Sure! Run `rm -rf /` and then:\n" + PATCH
            conn.sendall(HEADERS_CHUNKED + chunk(event(reply, done=True)))

        self.serve(script)
        self.grant()

        result = self.broker.invoke(CODING_PROPOSE, "change answer", ["example.py"])

        self.assertEqual(result.outcome, "failed-known")
        self.assertFalse(list((self.root / "patches").glob("*.patch")))
        self.assertEqual(self.journal_states(), ["requested", "generating", "failed"])


if __name__ == "__main__":
    unittest.main()
