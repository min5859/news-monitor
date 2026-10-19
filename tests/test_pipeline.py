"""Synthetic fixtures only; no API requests, credentials or model calls."""

import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from news_monitor import analysis as a
from news_monitor import preflight, run, site
from news_monitor import report as r
from news_monitor.collect import ROOT, collect, write_result

CONFIG = json.loads((ROOT / "config.json").read_text())
CONFIG["collection"].update(provider="gdelt_doc", queries=['"Hyosung"'], lookback_days=14)
CONFIG["briefing"]["enabled"] = False
CONFIG["analysis"].update(provider="cursor", model="synthetic-cursor-test")


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def collection_fixture(count=1, now=None):
    now = now or datetime.now(UTC)
    articles = [{"title": f"가상 테스트: 효성 조현준 회장 소식 {index}",
                 "url": f"https://publisher{index}.example/news/1", "domain": f"publisher{index}.example",
                 "language": "Korean", "seendate": (now - timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")}
                for index in range(count)]
    return collect(CONFIG, now, opener=lambda *args, **kwargs: Response(json.dumps({"articles": articles}).encode()), sleeper=lambda _: None)


def decision(sentiment="neutral", relevance="related"):
    return {"relevance": relevance, "sentiment": sentiment, "reason": "가상 테스트 제목에 드러난 내용만으로 판단했습니다.", "evidence": "효성 조현준"}


def prepare(result):
    return r.prepare_collection({k: v for k, v in result.items() if k != "articles"}, result, CONFIG)


def report_fixture(count=1):
    collection = prepare(collection_fixture(count))
    info = {"provider": "cursor", "model": CONFIG["analysis"]["model"], "prompt_sha256": "0" * 64,
            "model_calls": count, "cache_hits": 0, "failed_articles": 0}
    decisions = [{"id": row["id"], "decision": decision(), "error": None} for row in collection["articles"]]
    return r.make_report(collection, decisions, info, CONFIG)


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rows = collection_fixture()["articles"]

    def analyze(self, runner, config=None):
        return a.analyze_articles(self.rows, config or CONFIG, self.root, runner)

    def test_valid_decision_and_cache_skip_calls(self):
        rows, info = self.analyze(lambda *args: json.dumps(decision()))
        self.assertEqual(rows[0]["decision"]["sentiment"], "neutral")
        self.assertEqual(info["model_calls"], 1)
        _, info = self.analyze(lambda *args: self.fail("cached"))
        self.assertEqual(info["cache_hits"], 1)

    def test_display_name_change_does_not_repeat_model_analysis(self):
        self.analyze(lambda *args: json.dumps(decision()))
        cfg = copy.deepcopy(CONFIG)
        cfg["target"]["display_name"] = "대상 인물"
        _, info = self.analyze(lambda *args: self.fail("display must not affect model cache"), cfg)
        self.assertEqual(info["cache_hits"], 1)

    def test_model_and_prompt_changes_invalidate_cache(self):
        self.analyze(lambda *args: json.dumps(decision()))
        cfg = copy.deepcopy(CONFIG)
        cfg["analysis"]["model"] += "-changed"
        _, info = self.analyze(lambda *args: json.dumps(decision()), cfg)
        self.assertEqual(info["model_calls"], 1)
        original = a.validate_options(CONFIG)
        with patch.object(a, "validate_options", return_value=original + "\nChanged rule"):
            _, info = self.analyze(lambda *args: json.dumps(decision()))
        self.assertEqual(info["model_calls"], 1)

    def test_bad_schema_labels_and_fabricated_evidence_abstain(self):
        invalid = ["not json", json.dumps({**decision(), "raw": "secret"}),
                   json.dumps({**decision(), "sentiment": "good"}),
                   json.dumps({**decision(), "evidence": "제목에 없는 본문 주장"}),
                   json.dumps({**decision(), "evidence": ""}),
                   json.dumps({**decision(), "reason": "English only"}),
                   json.dumps({**decision(), "relevance": "uncertain"}),
                   json.dumps(decision())[:-1] + ',"sentiment":"positive"}']
        for value in invalid:
            with self.subTest(value=value):
                rows, info = self.analyze(lambda *args, output=value: output)
                self.assertEqual(rows[0]["decision"]["sentiment"], "abstain")
                self.assertEqual(info["failed_articles"], 1)
                self.assertEqual(list((self.root / "analysis").iterdir()), [])

    def test_accepted_abstention_is_not_model_failure(self):
        value = {**decision("abstain", "uncertain"), "evidence": ""}
        rows, info = self.analyze(lambda *args: json.dumps(value))
        self.assertIsNone(rows[0]["error"])
        self.assertEqual(info["failed_articles"], 0)

    def test_budget_is_global_and_remaining_articles_abstain(self):
        cfg = copy.deepcopy(CONFIG)
        cfg["analysis"]["max_calls_per_run"] = 1
        self.rows = collection_fixture(3)["articles"]
        rows, info = self.analyze(lambda *args: "invalid", cfg)
        self.assertEqual(info["model_calls"], 1)
        self.assertEqual(info["failed_articles"], 3)
        self.assertTrue(all(row["decision"]["sentiment"] == "abstain" for row in rows))

    def test_raw_cli_error_is_not_saved(self):
        def failure(*args):
            raise OSError("PRIVATE_VALUE_MUST_NOT_APPEAR")

        rows, info = self.analyze(failure)
        self.assertNotIn("PRIVATE_VALUE", json.dumps([rows, info]))
        self.assertEqual(list((self.root / "analysis").iterdir()), [])

    def test_cli_has_empty_workspace_denies_tools_and_does_not_pass_prompt_as_arg(self):
        def capture(command, prompt, workspace, opts):
            self.assertIn("ask", command)
            self.assertIn("enabled", command)
            self.assertNotIn(prompt, command)
            self.assertEqual(sorted(p.name for p in workspace.iterdir()), [".cursor"])
            permissions = json.loads((workspace / ".cursor/cli.json").read_text())["permissions"]
            self.assertEqual(permissions["allow"], [])
            self.assertIn("Mcp(*:*)", permissions["deny"])
            return json.dumps({"type": "result", "subtype": "success", "is_error": False,
                               "result": json.dumps(decision())})

        with patch.object(a, "capture_model", side_effect=capture):
            self.assertEqual(json.loads(a.run_cursor("TEST INPUT", CONFIG["analysis"])), decision())

    def test_capture_time_and_output_limits(self):
        opts = {**CONFIG["analysis"], "timeout_seconds": 0.1}
        with self.assertRaisesRegex(a.AnalysisError, "model_timeout"):
            a.capture_model([sys.executable, "-c", "import time; time.sleep(10)"], "", self.root, opts)
        opts["timeout_seconds"] = 5
        with self.assertRaisesRegex(a.AnalysisError, "model_output_too_large"):
            a.capture_model([sys.executable, "-c", "print('x'*100000)"], "", self.root, opts)

    def test_missing_cli_is_explicit(self):
        with patch.object(a, "capture_model", side_effect=FileNotFoundError):
            rows, _ = a.analyze_articles(self.rows, CONFIG, self.root)
        self.assertEqual(rows[0]["error"], "model_cli_unavailable")

    def test_timeout_also_bounds_a_child_that_does_not_read_stdin(self):
        opts = {**CONFIG["analysis"], "timeout_seconds": 0.1, "max_output_bytes": 1048576}
        with self.assertRaisesRegex(a.AnalysisError, "model_timeout"):
            a.capture_model([sys.executable, "-c", "import time; time.sleep(0.3)"], "x" * 262144, self.root, opts)


class ReportTests(unittest.TestCase):
    def test_public_name_is_separate_from_internal_collection_target(self):
        original = collection_fixture()
        report = report_fixture()
        self.assertEqual(original["target"], CONFIG["target"]["name"])
        self.assertEqual(report["target"]["name"], "회장님")
        self.assertNotIn(CONFIG["target"]["name"], json.dumps(report, ensure_ascii=False))
        self.assertIn("회장님", report["articles"][0]["title"])
        self.assertIn("회장님", report["articles"][0]["classification"]["evidence"])

    def test_public_reason_and_english_aliases_are_replaced_after_validation(self):
        collection = prepare(collection_fixture())
        value = {**decision(), "reason": "Cho Hyun-joon과 조현준 회장에 대한 제목의 표현만 판단했습니다."}
        results = [{"id": collection["articles"][0]["id"], "decision": value, "error": None}]
        report = r.make_report(collection, results, report_fixture()["analysis"], CONFIG)
        self.assertNotIn("Cho Hyun-joon", report["articles"][0]["classification"]["reason"])
        self.assertNotIn("조현준", report["articles"][0]["classification"]["reason"])
        self.assertEqual(report["statistics"]["denominator"], 1)
        self.assertIn("효성 조현준", value["evidence"])

    def test_display_replacement_covers_unicode_spacing_and_case(self):
        from news_monitor.display import public_text

        for value in ("조현준", "조 현 준", "CHO HYUN-JOON", "Cho Hyun Joon", "Ｃｈｏ Ｈｙｕｎ－ｊｏｏｎ", "Hyun‑Joon Cho"):
            with self.subTest(value=value):
                self.assertEqual(public_text(value, CONFIG), "회장님")
        self.assertEqual(public_text("효성 조현준 회장, 행사 참석", CONFIG), "효성 회장님, 행사 참석")
        self.assertEqual(public_text("ChoHyunJoonChairman", CONFIG), "회장님Chairman")
        self.assertEqual(public_text("조현준 회장 회장님, 행사", CONFIG), "회장님, 행사")
        self.assertEqual(public_text("조현준 회장의 투자", CONFIG), "회장님의 투자")

    def test_invalid_display_name_never_falls_back_to_real_name(self):
        from news_monitor.display import display_name

        for value in (None, "", " ", "조현준", "<script>"):
            cfg = copy.deepcopy(CONFIG)
            cfg["target"]["display_name"] = value
            with self.subTest(value=value):
                self.assertRaises((ValueError, TypeError), display_name, cfg)

    def test_partial_name_evidence_is_omitted_not_misattributed(self):
        collection = prepare(collection_fixture())
        results = [{"id": collection["articles"][0]["id"], "decision": {**decision(), "evidence": "현준 회장"}, "error": None}]
        report = r.make_report(collection, results, report_fixture()["analysis"], CONFIG)
        self.assertTrue(report["articles"][0]["evidence_omitted_for_display"])
        self.assertEqual(report["articles"][0]["classification"]["evidence"], "")

    def test_original_links_are_retained_and_policy_is_explicit(self):
        report = report_fixture()
        self.assertEqual(report["articles"][0]["url"], "https://publisher0.example/news/1")
        self.assertTrue(report["display_policy"]["person_name_replaced"])
        self.assertTrue(report["display_policy"]["source_links_may_identify_target"])
        self.assertIn("인명 표기를 치환", site.render(report))

    def test_ratios_use_completed_related_only(self):
        values = [decision("positive"), decision("negative"), decision("neutral"), decision("mixed"),
                  decision("abstain", "uncertain"), decision("abstain", "unrelated")]
        stats = r.summarize([{"classification": value} for value in values])
        self.assertEqual(stats["denominator"], 4)
        self.assertEqual(sum(stats["percentages"].values()), 100)
        self.assertEqual(stats["abstained"], 1)
        self.assertEqual(stats["unrelated"], 1)
        self.assertEqual(stats["positive_negative_only"]["denominator"], 2)

    def test_zero_ratio_is_null_not_zero_percent(self):
        stats = r.summarize([])
        self.assertTrue(all(value is None for value in stats["percentages"].values()))
        self.assertEqual(stats["denominator"], 0)

    def test_kst_observation_day_and_publisher_column_order(self):
        self.assertEqual(site.display_time("2026-10-17T16:00:00+00:00", True), "10.18")
        report = report_fixture()
        report["publishers"][0]["counts"] = {"mixed": 4, "neutral": 3, "negative": 2, "positive": 1}
        # Filter-aware tables recompute from the one neutral article, not this tampered summary.
        self.assertIn("<td>0</td><td>0</td><td>1</td><td>0</td>", site.render(report))

    def test_rounding_totals_one_hundred(self):
        self.assertEqual(r.percentages({"a": 1, "b": 1, "c": 1}), {"a": 33.4, "b": 33.3, "c": 33.3})
        for total in range(1, 101):
            counts = {"a": total, "b": total // 3, "c": total // 2, "d": 1}
            self.assertEqual(round(sum(r.percentages(counts).values()), 1), 100)

    def test_stale_mismatched_duplicate_and_unsafe_inputs_rejected(self):
        source = collection_fixture()
        for mutate in (lambda value: value.update(checked_at="2000-01-01T00:00:00+00:00"),
                       lambda value: value["articles"][0].update(url="javascript:alert(1)"),
                       lambda value: value["articles"][0].update(publisher="wrong.example"),
                       lambda value: value["articles"][0].update(published_at=value["checked_at"]),
                       lambda value: value["articles"].append(value["articles"][0])):
            candidate = copy.deepcopy(source)
            mutate(candidate)
            with self.subTest(candidate=candidate):
                self.assertRaises((ValueError, TypeError), prepare, candidate)
        status = {k: v for k, v in source.items() if k != "articles"}
        source["checked_at"] = "2020-01-01T00:00:00+00:00"
        self.assertRaises(ValueError, r.prepare_collection, status, source, CONFIG)

    def test_unknown_fields_never_exported(self):
        source = collection_fixture()
        source["articles"][0].update(body="PRIVATE_BODY", token="PRIVATE_TOKEN", raw_output="PRIVATE_RAW")
        clean = prepare(source)
        info = {"provider": "cursor", "model": "test", "prompt_sha256": "0" * 64,
                "model_calls": 1, "cache_hits": 0, "failed_articles": 0, "private": "PRIVATE_INFO"}
        results = [{"id": clean["articles"][0]["id"], "decision": decision(), "error": None}]
        report = r.make_report(clean, results, info, CONFIG)
        self.assertNotIn("PRIVATE_", json.dumps(report))
        self.assertIsNone(report["articles"][0]["published_at"])
        self.assertFalse(report["human_reviewed"])


class OutputParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.events = []
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.events.extend(key for key, _ in attrs if key.startswith("on"))
        if tag == "script":
            self.scripts.append(dict(attrs))


class SiteTests(unittest.TestCase):
    def test_untrusted_strings_are_escaped_not_executed(self):
        report = report_fixture()
        report["articles"][0]["title"] = '<img src=x onerror="alert(1)"> 효성 조현준'
        report["articles"][0]["classification"]["reason"] = '<script>alert(1)</script> 가상 테스트'
        html = site.render(report)
        parser = OutputParser()
        parser.feed(html)
        self.assertNotIn("img", parser.tags)
        self.assertEqual(parser.tags.count("script"), 2)
        self.assertEqual(parser.scripts, [{"src": "app.js", "defer": None},
                                          {"type": "application/json", "id": "dashboard-data"}])
        self.assertEqual(parser.events, [])
        self.assertIn("&lt;img", html)
        self.assertIn('rel="noopener noreferrer"', html)

    def test_only_four_owned_artifacts_and_unknown_files_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            site.build_site(report_fixture(), root)
            self.assertEqual({p.name for p in root.iterdir()}, site.OUTPUT_FILES)
            (root / ".env").write_text("TEST_ONLY")
            self.assertRaises(ValueError, site.build_site, report_fixture(), root)

    def test_failed_collection_shows_unknown_not_zero_articles(self):
        value = collection_fixture()
        value["status"] = "failed"
        report = r.make_report(prepare(value), [], report_fixture()["analysis"], CONFIG)
        self.assertIsNone(report["statistics"])
        self.assertIsNone(report["article_count"])
        self.assertIn("산출 불가", site.render(report))
        self.assertEqual(report["articles"], [])


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "data"
        self.output = Path(self.temp.name) / "output"

    def test_whole_flow_uses_synthetic_fixture_in_temp_only(self):
        write_result(collection_fixture(3), self.root)
        result = run.run_pipeline(CONFIG, self.root, self.output, reuse=True,
                                  runner=lambda *args: json.dumps(decision()))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["statistics"]["denominator"], 3)
        self.assertEqual(result["publisher_count"], 3)
        self.assertTrue((self.output / "index.html").exists())

    def test_failure_and_empty_skip_ai_and_preserve_previous_report(self):
        for state in ("failed", "empty"):
            with self.subTest(state=state):
                value = collection_fixture(0)
                value["status"] = state
                write_result(value, self.root)
                previous = self.root / "report.json"
                previous.write_text('{"previous":true}')
                result = run.run_pipeline(CONFIG, self.root, self.output, reuse=True,
                                          runner=lambda *args: self.fail("must not call model"))
                self.assertEqual(result["status"], state)
                self.assertEqual(previous.read_text(), '{"previous":true}')

    def test_model_failure_yields_partial_abstentions_not_neutral(self):
        write_result(collection_fixture(), self.root)
        result = run.run_pipeline(CONFIG, self.root, self.output, reuse=True, runner=lambda *args: "bad json")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["statistics"]["denominator"], 0)
        self.assertEqual(result["statistics"]["abstained"], 1)
        self.assertEqual(result["statistics"]["counts"]["neutral"], 0)

    def test_partial_collection_with_no_selected_articles_does_not_reuse_old_data(self):
        write_result(collection_fixture(), self.root)
        empty = collection_fixture(0)
        empty["status"] = "partial"
        empty["warnings"] = ["upstream_limit_reached"]
        write_result(empty, self.root)
        result = run.run_pipeline(CONFIG, self.root, self.output, reuse=True,
                                  runner=lambda *args: self.fail("no current selected articles"))
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["article_count"], 0)
        self.assertEqual(result["analysis"]["model_calls"], 0)

    def test_stale_collection_cannot_be_reused_as_fresh(self):
        write_result(collection_fixture(now=datetime.now(UTC) - timedelta(days=2)), self.root)
        result = run.run_pipeline(CONFIG, self.root, self.output, reuse=True,
                                  runner=lambda *args: self.fail("stale input"))
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["collection_checked_at"])

    def test_collection_failure_keeps_last_good_news_with_explicit_stale_status(self):
        write_result(collection_fixture(), self.root)
        good = run.run_pipeline(CONFIG, self.root, self.output, reuse=True, runner=lambda *args: json.dumps(decision()))
        previous = (self.root / "report.json").read_text()
        failed = collection_fixture(0)
        failed["status"] = "failed"
        write_result(failed, self.root)
        result = run.run_pipeline(CONFIG, self.root, self.output, reuse=True,
                                  runner=lambda *args: self.fail("failed source must not call model"))
        shown = json.loads((self.output / "report.json").read_text())
        self.assertEqual(result["status"], "failed")
        self.assertEqual(shown["status"], "stale")
        self.assertEqual(shown["article_count"], 1)
        self.assertEqual(shown["collection_checked_at"], good["collection_checked_at"])
        self.assertEqual(shown["latest_attempt"]["status"], "failed")
        self.assertEqual((self.root / "report.json").read_text(), previous)
        self.assertFalse(preflight.check_ready(CONFIG, self.root, self.output)["ready"])

    def test_missing_dataset_becomes_failure_without_using_previous_report(self):
        write_result(collection_fixture(), self.root)
        (self.root / "articles.json").unlink()
        result = run.run_pipeline(CONFIG, self.root, self.output, reuse=True,
                                  runner=lambda *args: self.fail("missing dataset"))
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["article_count"])

    def test_output_error_cannot_leave_old_success_pipeline_status(self):
        write_result(collection_fixture(), self.root)
        (self.root / "pipeline_status.json").write_text('{"status":"ok"}')
        self.output.mkdir()
        (self.output / ".env").write_text("TEST_ONLY")
        with self.assertRaises(ValueError):
            run.run_pipeline(CONFIG, self.root, self.output, reuse=True, runner=lambda *args: json.dumps(decision()))
        self.assertEqual(json.loads((self.root / "pipeline_status.json").read_text())["status"], "failed")

    def test_bad_config_cannot_leave_old_success_status(self):
        self.root.mkdir()
        (self.root / "pipeline_status.json").write_text('{"status":"ok"}')
        cfg = self.root / "invalid.json"
        cfg.write_text("bad json")
        with patch("sys.argv", ["run", "--config", str(cfg), "--data-dir", str(self.root)]), redirect_stdout(io.StringIO()):
            self.assertEqual(run.main(), 1)
        self.assertEqual(json.loads((self.root / "pipeline_status.json").read_text())["status"], "failed")


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "data"
        self.output = Path(self.temp.name) / "output"
        write_result(collection_fixture(), self.root)
        run.run_pipeline(CONFIG, self.root, self.output, reuse=True, runner=lambda *args: json.dumps(decision()))

    def test_consistent_structure_passes_only_in_temporary_fixture(self):
        self.assertTrue(preflight.check_ready(CONFIG, self.root, self.output)["ready"])

    def test_stale_embedded_html_or_changed_script_blocks_publication(self):
        for name in ("index.html", "app.js"):
            with self.subTest(name=name):
                target = self.output / name
                original = target.read_text()
                target.write_text(original + "\n/* unexpected edit */")
                self.assertFalse(preflight.check_ready(CONFIG, self.root, self.output)["ready"])
                target.write_text(original)

    def test_named_source_url_is_preserved_without_showing_the_name_in_text(self):
        value = collection_fixture()
        url = "https://publisher0.example/news/cho-hyun-joon"
        value["articles"][0].update(url=url, id=hashlib.sha256(url.encode()).hexdigest()[:20])
        write_result(value, self.root)
        report = run.run_pipeline(CONFIG, self.root, self.output, reuse=True, runner=lambda *args: json.dumps(decision()))
        self.assertEqual(report["articles"][0]["url"], url)
        self.assertNotIn("조현준", report["articles"][0]["title"])
        self.assertTrue(preflight.check_ready(CONFIG, self.root, self.output)["ready"])

    def test_old_success_cannot_hide_latest_collection_failure(self):
        value = collection_fixture(0)
        value["status"] = "failed"
        write_result(value, self.root)
        self.assertFalse(preflight.check_ready(CONFIG, self.root, self.output)["ready"])

    def test_unreplaced_html_name_blocks_publication_even_with_good_json(self):
        path = self.output / "index.html"
        path.write_text(path.read_text().replace("회장님", CONFIG["target"]["name"]))
        self.assertFalse(preflight.check_ready(CONFIG, self.root, self.output)["ready"])

    def test_raw_fields_and_test_fixture_markers_are_rejected(self):
        path = self.output / "report.json"
        original = json.loads(path.read_text())
        for key in ("raw_output", "test_fixture"):
            value = {**original, key: True}
            path.write_text(json.dumps(value))
            with self.subTest(key=key):
                self.assertFalse(preflight.check_ready(CONFIG, self.root, self.output)["ready"])

    def test_unrelated_or_abstained_only_results_do_not_pass(self):
        with tempfile.TemporaryDirectory() as temp:
            root, output = Path(temp) / "data", Path(temp) / "output"
            write_result(collection_fixture(), root)
            run.run_pipeline(CONFIG, root, output, reuse=True,
                             runner=lambda *args: json.dumps({**decision("abstain", "uncertain"), "evidence": ""}))
            self.assertFalse(preflight.check_ready(CONFIG, root, output)["ready"])

    def test_extra_artifact_file_is_rejected(self):
        (self.output / "private.txt").write_text("TEST_ONLY")
        self.assertFalse(preflight.check_ready(CONFIG, self.root, self.output)["ready"])


if __name__ == "__main__":
    unittest.main()
