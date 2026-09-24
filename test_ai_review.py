"""Offline TypeSafe integration tests; never send network requests."""
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import ai_review as ai
import main as app


def answer(payload, config):
    answers = {}
    for key, question in payload["questions"].items():
        options = list(question["criteria"])
        answers[key] = {"type": "choice", "choice": options[0], "confidence": .8,
                        "probabilities": {option: int(index == 0) for index, option in enumerate(options)}}
    return {"model": "jev-test", "answers": answers, "usage": {"input_tokens": 20, "output_tokens": 10}}


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(app, "ROOT", self.root))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(ai.http.client, "HTTPSConnection", side_effect=AssertionError("network forbidden")))
        self.api = self.stack.enter_context(patch.object(ai, "request_review", side_effect=answer))
        self.store = app.Store()
        self.addCleanup(self.store.close)
        self.log = app.JsonLogger("ai-test", self.store)
        self.config = ai.ReviewConfig("shadow", "secret-test-key")
        self.reviewer = ai.ShadowReviewer(self.config, self.log, app.redact)

    def review(self, name="Song.flac"):
        self.reviewer.versions("Artist - Song", [{"filename": name, "fid": "private-fid", "cookie": "private-cookie"}], None)

    def last(self):
        return self.store.db.execute("SELECT * FROM ai_judgments ORDER BY id DESC LIMIT 1").fetchone()

    def test_off_does_nothing(self):
        self.reviewer.config = replace(self.config, mode="off")
        self.review()
        self.assertIsNone(self.last())
        self.api.assert_not_called()

    def test_missing_key_is_observational(self):
        self.reviewer.config = replace(self.config, api_key="")
        self.review()
        self.assertEqual(self.last()["status"], "NOT_CONFIGURED")
        self.assertEqual(self.last()["applied"], 0)
        self.api.assert_not_called()

    def test_success_cache_and_evidence_whitelist(self):
        self.review()
        first = self.last()
        self.assertEqual(first["status"], "SUCCESS")
        self.assertEqual(first["input_tokens"], 20)
        self.assertEqual(first["output_tokens"], 10)
        self.assertNotIn("private", first["state_json"])
        self.assertEqual(json.loads(first["response_json"])["usage"]["input_tokens"], 20)
        self.review()
        self.assertEqual(self.last()["status"], "CACHED")
        self.assertEqual(self.last()["cached_from"], first["id"])
        self.assertEqual(self.last()["input_tokens"], 0)
        self.assertEqual(self.api.call_count, 1)

    def test_changed_evidence_does_not_use_cache(self):
        self.review()
        self.review("Other.flac")
        self.assertEqual(self.api.call_count, 2)

    def test_expired_cache_is_not_used(self):
        self.review()
        self.store.db.execute("UPDATE ai_judgments SET created_at=datetime('now','-2 days')")
        self.store.db.commit()
        self.review()
        self.assertEqual(self.api.call_count, 2)

    def test_timeout_circuit_breaks_without_raising(self):
        self.api.side_effect = TimeoutError("secret-test-key")
        self.review()
        self.assertEqual(self.last()["status"], "ERROR")
        self.assertEqual(self.last()["error"], "TimeoutError")
        self.assertIsNone(self.last()["input_tokens"])
        self.review("Other.flac")
        self.assertEqual(self.last()["status"], "SERVICE_UNAVAILABLE")
        self.assertEqual(self.api.call_count, 1)

    def test_retry_is_bounded(self):
        self.api.side_effect = ai.APIError(429)
        with patch.object(ai.time, "sleep") as sleep:
            self.review()
        self.assertEqual(self.api.call_count, 2)
        self.assertEqual(self.last()["attempts"], 2)
        sleep.assert_called_once_with(1)

    def test_auth_error_is_not_retried(self):
        self.api.side_effect = ai.APIError(401)
        self.review()
        self.assertEqual(self.api.call_count, 1)

    def test_budget_caps_requests(self):
        self.reviewer.config = replace(self.config, max_calls=1)
        self.review()
        self.review("Other.flac")
        self.assertEqual(self.last()["status"], "BUDGET_EXCEEDED")
        self.assertEqual(self.api.call_count, 1)

    def test_invalid_response_is_retained_redacted(self):
        self.api.side_effect = None
        self.api.return_value = {"model": "test", "answers": {}, "debug": "secret-test-key"}
        self.review()
        self.assertEqual(self.last()["status"], "ERROR")
        self.assertNotIn("secret-test-key", self.last()["response_json"])
        self.assertIn("REDACTED", self.last()["response_json"])

    def test_oversized_input_is_not_partially_sent(self):
        self.reviewer.page("A - B", "A", "B", {"lyrics": "line\n" * 121}, "public-url", False)
        self.assertEqual(self.last()["status"], "INPUT_TOO_LARGE")
        self.api.assert_not_called()

    def test_line_evidence_and_rule_result_preserved(self):
        lyrics = '  原文 & <内容>  \n作词：某人\n原文'
        self.reviewer.page("A - B", "A", "B", {"h1": "B", "intro": "A演唱", "lyrics": lyrics}, "public-url", False)
        row = self.last()
        state = json.loads(row["state_json"])
        self.assertEqual([line["text"] for line in state["lyric_lines"]], lyrics.splitlines())
        self.assertFalse(json.loads(row["local_result_json"])["matched_by_rules"])
        self.assertEqual(row["applied"], 0)
        self.assertEqual(len(json.loads(row["questions_json"])), 5)

    def test_nonfinite_probabilities_rejected(self):
        questions = {"x": ai.choice("Question", {"yes": "yes", "no": "no"})}
        result = answer({"questions": questions}, self.config)
        result["answers"]["x"]["confidence"] = float("nan")
        with self.assertRaises(ValueError): ai.validate_response(result, questions)

    def test_interrupted_judgments_recovered(self):
        self.review()
        self.store.db.execute("UPDATE ai_judgments SET status='RUNNING'")
        self.store.db.commit()
        self.store.recover_interrupted()
        self.assertEqual(self.last()["status"], "INTERRUPTED")

    def test_configuration_validation(self):
        for values in ({"TYPESAFE_MODE": "apply"}, {"TYPESAFE_TIMEOUT": "nan"}, {"TYPESAFE_MAX_CALLS": "0"}):
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True):
                with self.assertRaises(ValueError): ai.ReviewConfig.load()

    def test_http_destination_and_no_redirects(self):
        connection = Mock()
        connection.getresponse.return_value.status = 302
        # Access the original function beneath the test's mock.
        with patch.object(ai.http.client, "HTTPSConnection", return_value=connection) as factory:
            with self.assertRaises(ai.APIError):
                ORIGINAL_REQUEST({}, self.config)
        factory.assert_called_once_with("api.typesafe.ai", timeout=15)
        self.assertEqual(connection.request.call_args.args, ("POST", "/v1/systemone"))
        connection.close.assert_called_once()


ORIGINAL_REQUEST = ai.request_review

if __name__ == "__main__":
    unittest.main()
