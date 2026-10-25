"""No live calls: errors retain useful codes, never raw model or secret text."""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_briefing
from test_discovery import CONFIG, NOW
from test_pipeline import CONFIG as PIPELINE_CONFIG
from test_pipeline import collection_fixture, decision

from news_monitor import analysis, briefing, codex, discovery, run
from news_monitor.collect import write_result
from news_monitor.diagnostics import record_errors


class DiagnosticsTests(unittest.TestCase):
    def test_cli_stderr_exposes_only_an_allowlisted_reason(self):
        with tempfile.TemporaryDirectory() as temp:
            for text, expected in (("usage_limit_reached PRIVATE_SECRET", "model_usage_limit"),
                                   ("429 Too Many Requests PRIVATE_SECRET", "model_rate_limited"),
                                   ("401 Unauthorized PRIVATE_SECRET", "model_authentication_failed"),
                                   # Cursor CLI wording seen on OCI 10/26 after its stored login was revoked.
                                   ("Your stored authentication is invalid. PRIVATE_SECRET", "model_authentication_failed"),
                                   ("Error: Authentication required. Please run 'agent login' first. PRIVATE_SECRET",
                                    "model_authentication_failed"),
                                   ("Not logged in PRIVATE_SECRET", "model_authentication_failed"),
                                   ("arbitrary PRIVATE_SECRET", "model_cli_failed")):
                with self.subTest(text=text), self.assertRaises(analysis.AnalysisError) as result:
                    analysis.capture_model([sys.executable, "-c", "import sys;sys.stderr.write(sys.argv[1]);sys.exit(1)", text],
                                           "", Path(temp), {"timeout_seconds": 3, "max_output_bytes": 65536})
                self.assertEqual(str(result.exception), expected)
                self.assertNotIn("PRIVATE_SECRET", str(result.exception))

    def test_codex_top_level_error_is_classified_without_leaking_message(self):
        event = json.dumps({"type": "error", "message": "usage_limit_reached PRIVATE_SECRET"}) + "\n"

        def capture(command, prompt, workspace, opts, observer):
            observer(event.encode())
            return event

        with patch.object(analysis, "capture_model", side_effect=capture), self.assertRaisesRegex(analysis.AnalysisError, "model_usage_limit"):
            codex.execute("synthetic", "synthetic-model")

    def test_discovery_retains_reason_but_not_untrusted_error_text(self):
        def failure(*args, **kwargs):
            raise analysis.AnalysisError("model_timeout")

        result = discovery.collect_web(CONFIG, NOW, runner=failure)
        self.assertEqual(result["errors"][0]["reason"], "model_timeout")
        self.assertIn("elapsed_seconds", result["errors"][0])

    def test_briefing_diagnostics_identify_validation_phase_and_reason(self):
        fixture = test_briefing.BriefingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        invalid = fixture.grouped()
        invalid["groups"][0]["summary"] = "생산이 999배 늘었다는 제목입니다."
        result = fixture.enrich(invalid)
        status = json.loads((fixture.data / "briefing_status.json").read_text())
        self.assertEqual(result["insights"]["status"], "fallback")
        self.assertEqual(len(status["errors"]), 2)
        self.assertEqual(status["errors"][0]["phase"], "validation")
        self.assertEqual(status["errors"][0]["reason"], "group_number_not_grounded")
        self.assertNotIn("999", json.dumps(status))

    def test_briefing_only_never_collects_or_reclassifies_articles(self):
        cfg = copy.deepcopy(PIPELINE_CONFIG)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_result(collection_fixture(), root)
            run.run_pipeline(cfg, root, root / "dist", reuse=True, runner=lambda *args: json.dumps(decision()))
            with patch.object(run, "_run_collection", side_effect=AssertionError("must not collect")):
                result = run.run_pipeline(cfg, root, root / "dist", briefing_only=True)
            self.assertEqual(result["analysis"]["model_calls"], 0)
            self.assertEqual(result["analysis"]["cache_hits"], 1)

    def test_missing_classification_cache_blocks_briefing_only_before_model(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_result(collection_fixture(), root)
            with self.assertRaisesRegex(ValueError, "classification_cache_required"):
                run.run_pipeline(PIPELINE_CONFIG, root, root / "dist", briefing_only=True,
                                 briefing_runner=lambda *args, **kwargs: self.fail("must not group"))

    def test_limited_refresh_preserves_good_cache_and_hides_arbitrary_exception(self):
        fixture = test_briefing.BriefingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.enrich()
        cached = {path.name: path.read_bytes() for path in (fixture.data / "briefing").glob("*.json")}

        def failure(*args, **kwargs):
            raise analysis.AnalysisError("PRIVATE_SECRET_MUST_NOT_BE_SAVED")

        result = briefing.enrich(fixture.report, fixture.cfg, fixture.data, failure, refresh=True, max_attempts=1)
        self.assertEqual(result["insights"]["model_calls"], 1)
        self.assertEqual(result["insights"]["status"], "fallback")
        self.assertNotIn("PRIVATE_SECRET", (fixture.data / "briefing_status.json").read_text())
        self.assertEqual(cached, {path.name: path.read_bytes() for path in (fixture.data / "briefing").glob("*.json")})
        self.assertTrue(fixture.enrich(runner=lambda *args, **kwargs: self.fail("good cache lost"))["insights"]["cache_hit"])

    def test_nonretryable_limit_stops_without_spending_second_attempt(self):
        fixture = test_briefing.BriefingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)

        def failure(*args, **kwargs):
            raise analysis.AnalysisError("model_usage_limit")

        result = fixture.enrich(runner=failure)
        self.assertEqual(result["insights"]["model_calls"], 1)
        self.assertEqual(json.loads((fixture.data / "briefing_status.json").read_text())["errors"][0]["reason"], "model_usage_limit")

    def test_private_error_history_is_bounded_and_survives_later_success(self):
        cfg = copy.deepcopy(PIPELINE_CONFIG)
        cfg["diagnostics"]["max_events"] = 2
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for reason in ("model_timeout", "group_number_not_grounded", "group_evidence_not_grounded"):
                record_errors(root, cfg, "briefing", NOW.isoformat(), [{"reason": reason}])
            record_errors(root, cfg, "briefing", NOW.isoformat(), [])
            history = json.loads((root / "diagnostics.json").read_text())
            self.assertEqual([row["reason"] for row in history], ["group_number_not_grounded", "group_evidence_not_grounded"])


if __name__ == "__main__":
    unittest.main()
