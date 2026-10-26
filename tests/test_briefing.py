"""Synthetic headline grouping and period checks; no model or network calls."""

import copy
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from test_pipeline import CONFIG, collection_fixture, decision, report_fixture

from news_monitor import analysis, briefing, preflight, run, site
from news_monitor.collect import ROOT, write_result
from news_monitor.report import timestamp


class BriefingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = copy.deepcopy(CONFIG)
        self.cfg["briefing"]["enabled"] = True
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.report = report_fixture(3)
        self.report["analysis"]["prompt_sha256"] = hashlib.sha256(analysis.validate_options(self.cfg).encode()).hexdigest()
        titles = ["가상 효성 회장님 초고압차단기 생산 확대", "가상 효성 회장님 초고압차단기 공장 준공", "가상 효성 회장님 계열사 지분 매입"]
        for row, title in zip(self.report["articles"], titles, strict=True):
            row["title"] = title
            row["classification"]["evidence"] = "효성 회장님"
        self.ids = [row["id"] for row in self.report["articles"]]

    def grouped(self):
        return {"groups": [
            {"label": "초고압차단기 생산 확대", "summary": "제목들은 회장님과 초고압차단기 생산 확대·공장 준공 소식을 연결합니다.",
             "topic": "management", "article_ids": self.ids[:2]},
            {"label": "계열사 지분 매입", "summary": "제목은 회장님의 계열사 지분 매입 소식을 전합니다.", "topic": "governance",
             "article_ids": self.ids[2:]},
        ]}

    def enrich(self, response=None, runner=None):
        return briefing.enrich(self.report, self.cfg, self.data,
                               runner=runner or (lambda *args, **kwargs: (json.dumps(response or self.grouped()), {})))

    def test_cursor_provider_runs_cursor_and_records_it(self):
        self.cfg["briefing"]["provider"] = "cursor"
        envelope = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                               "result": json.dumps(self.grouped())})
        with patch.object(analysis, "capture_model", return_value=envelope) as capture:
            result = briefing.enrich(self.report, self.cfg, self.data)
        self.assertEqual(capture.call_args.args[0][0], "agent")
        self.assertEqual((result["insights"]["status"], result["insights"]["provider"]), ("ok", "cursor"))
        briefing.validate_insights(result, self.cfg)
        self.cfg["briefing"]["provider"] = "other"
        with self.assertRaises(ValueError):
            briefing.options(self.cfg)

    def test_group_stats_are_computed_and_cache_avoids_second_call(self):
        result = self.enrich()
        self.assertEqual(result["insights"]["status"], "ok")
        self.assertEqual(len(result["insights"]["groups"]), 2)
        again = self.enrich(runner=lambda *args, **kwargs: self.fail("cached"))
        self.assertTrue(again["insights"]["cache_hit"])
        self.assertEqual(again["insights"]["model_calls"], 0)

    def test_model_selects_ids_and_code_copies_source_headlines_as_evidence(self):
        document = self.grouped()
        for group in document["groups"]:
            group.pop("evidence", None)
        result = self.enrich(document)
        self.assertEqual(result["insights"]["status"], "ok")
        titles = {row["id"]: row["title"] for row in self.report["articles"]}
        for group in result["insights"]["groups"]:
            for entry in group["evidence"]:
                self.assertEqual(entry["quote"], titles[entry["article_id"]][:self.cfg["briefing"]["max_evidence_chars"]])

    def test_fabricated_public_evidence_remains_rejected(self):
        result = self.enrich()
        result["insights"]["groups"][0]["evidence"][0]["quote"] = "제목에 없는 문구"
        with self.assertRaises(ValueError):
            preflight.check_artifact(result, self.cfg)

    def test_invalid_ids_missing_members_and_fabricated_evidence_fall_back(self):
        for kind in ("duplicate", "missing", "unknown", "evidence", "raw"):
            document = self.grouped()
            if kind == "duplicate":
                document["groups"][1]["article_ids"] = self.ids[:1]
            elif kind == "missing":
                document["groups"].pop()
            elif kind == "unknown":
                document["groups"][0]["article_ids"].append("not-an-article")
            elif kind == "evidence":
                document["groups"][0]["evidence"] = [{"article_id": self.ids[0], "quote": "제목에 없는 주장"}]
            else:
                document["raw"] = "MUST_NOT_PERSIST"
            with self.subTest(kind=kind):
                result = self.enrich(document)
                self.assertEqual(result["insights"]["status"], "fallback")
                self.assertEqual(len(result["insights"]["groups"]), 3)
                self.assertEqual(list((self.data / "briefing").glob("*.json")), [])

    def test_unrelated_articles_are_not_in_issues(self):
        self.report["articles"][2]["classification"].update(relevance="uncertain", sentiment="abstain")
        document = self.grouped()
        document["groups"].pop()
        result = self.enrich(document)
        self.assertEqual(len(result["insights"]["groups"]), 1)
        self.assertNotIn(self.ids[2], result["insights"]["groups"][0]["article_ids"])

    def test_partial_period_uses_selected_headline_not_full_group_summary(self):
        result = self.enrich()
        rows = self.report["articles"][:1]
        groups = briefing.select_groups(result["insights"]["groups"], rows)
        self.assertEqual(len(groups), 1)
        self.assertNotEqual(groups[0]["summary"], self.grouped()["groups"][0]["summary"])
        self.assertIn(rows[0]["title"], groups[0]["summary"])

    def test_dates_outside_real_collection_window_are_not_zero_news(self):
        result = self.enrich()
        window = next(row for row in result["insights"]["periods"] if row["days"] == 30)
        self.assertFalse(window["coverage_complete"])
        self.assertIsNone(window["daily"][0]["article_count"])
        self.assertEqual(window["statistics"]["denominator"], 3)

    def test_existing_observation_times_are_unchanged(self):
        observations = copy.deepcopy([row["observation"] for row in self.report["articles"]])
        result = self.enrich()
        self.assertEqual([row["observation"] for row in result["articles"]], observations)

    def test_bad_public_group_and_tampered_period_statistics_fail_gate(self):
        result = self.enrich()
        preflight.check_artifact(result, self.cfg)
        result["insights"]["periods"][0]["statistics"]["denominator"] = 999
        with self.assertRaises(ValueError):
            preflight.check_artifact(result, self.cfg)

    def test_kst_first_seen_day_and_article_publication_day_are_distinct(self):
        self.assertEqual(briefing.kst_day("2026-10-18T16:00:00+00:00"), "2026-10-19")
        result = self.enrich()
        expected = timestamp(self.report["window_end"]).astimezone(briefing.KST).date().isoformat()
        self.assertEqual(result["insights"]["as_of_day"], expected)

    def test_pipeline_uses_readonly_grouping_and_records_it_separately(self):
        write_result(collection_fixture(3), self.data)

        def runner(prompt, model, **kwargs):
            self.assertFalse(kwargs["search"])
            payload = json.loads(prompt.split("INPUT_JSON:\n")[1])
            self.assertNotIn("조현준", prompt)
            self.assertNotIn("https://", prompt)
            self.assertTrue(str(kwargs["schema"]).endswith("schemas/briefing.json"))
            document = {"groups": [{"label": "가상 제목 보도", "summary": "제목은 회장님 관련 소식을 전합니다.",
                                     "topic": "other", "article_ids": [row["id"]]}
                                    for row in payload["articles"]]}
            return json.dumps(document), {}

        output = self.data / "dist"
        result = run.run_pipeline(self.cfg, self.data, output, reuse=True,
                                  runner=lambda *args: json.dumps(decision()), briefing_runner=runner)
        self.assertEqual(result["insights"]["status"], "ok")
        self.assertTrue(preflight.check_ready(self.cfg, self.data, output)["ready"])
        status = json.loads((self.data / "pipeline_status.json").read_text())
        self.assertEqual(status["briefing"]["model_calls"], 1)

    def test_raw_model_failure_is_not_saved_and_fallback_is_explicit(self):
        def failure(*args, **kwargs):
            raise analysis.AnalysisError("PRIVATE_TOKEN_MUST_NOT_LEAK")

        result = self.enrich(runner=failure)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["insights"]["status"], "fallback")
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(result))
        preflight.check_artifact(result, self.cfg)

    def test_unknown_topic_falls_back(self):
        document = self.grouped()
        document["groups"][0]["topic"] = "invented_topic"
        self.assertEqual(self.enrich(document)["insights"]["status"], "fallback")

    def test_same_issue_beyond_date_span_is_split_not_merged(self):
        self.report["articles"][0]["published_at"] = "2026-01-01"
        self.report["articles"][1]["published_at"] = "2026-02-01"
        self.report["articles"][1]["title"] = "가상 효성 회장님 초고압차단기 공장 2곳 준공"
        document = self.grouped()
        document["groups"][0]["summary"] = "제목들은 생산 확대와 공장 2곳 준공 소식을 연결합니다."
        result = self.enrich(document)
        self.assertEqual(result["insights"]["status"], "ok")
        groups = result["insights"]["groups"]
        self.assertEqual(sorted(group["article_ids"] for group in groups), sorted([[identity] for identity in self.ids]))
        titles = {row["id"]: row["title"] for row in self.report["articles"]}
        split = [group for group in groups if group["label"] == "초고압차단기 생산 확대"]
        self.assertEqual([group["summary"] for group in split],
                         ["선택된 기사 제목: " + titles[group["article_ids"][0]] for group in split])
        preflight.check_artifact(result, self.cfg)
        merged = copy.deepcopy(result)
        first, second = (group for group in merged["insights"]["groups"] if group["label"] == "초고압차단기 생산 확대")
        first["article_ids"] = sorted(first["article_ids"] + second["article_ids"])
        first["evidence"] = sorted(first["evidence"] + second["evidence"], key=lambda entry: entry["article_id"])
        merged["insights"]["groups"].remove(second)
        with self.assertRaises(ValueError):
            preflight.check_artifact(merged, self.cfg)
        days = {"a": date(2026, 1, 1), "b": date(2026, 1, 6), "c": date(2026, 1, 10), "d": date(2026, 1, 1)}
        self.assertEqual(briefing.split_by_date(["c", "b", "a", "d"], days, 7), [["a", "d", "b"], ["c"]])

    def test_number_missing_from_headlines_replaces_only_that_issue_text(self):
        # OCI 10/26 12:00: the model added "100" to one single-headline issue in 3/3 calls despite the prompt rule.
        document = self.grouped()
        document["groups"][1]["label"] = "계열사 지분 100% 매입"
        document["groups"][1]["summary"] = "제목은 회장님이 계열사 지분 100%를 매입했다고 전합니다."
        result = self.enrich(document)
        self.assertEqual(result["insights"]["status"], "ok")
        kept, replaced = sorted(result["insights"]["groups"], key=lambda group: group["article_ids"] != self.ids[:2])
        self.assertEqual(kept["summary"], self.grouped()["groups"][0]["summary"])
        title = self.report["articles"][2]["title"]
        self.assertEqual((replaced["label"], replaced["summary"]), (title, "선택된 기사 제목: " + title))
        self.assertNotIn("100", json.dumps(result["insights"]["groups"], ensure_ascii=False))
        preflight.check_artifact(result, self.cfg)

    def test_no_related_articles_skips_model_instead_of_making_up_a_brief(self):
        for row in self.report["articles"]:
            row["classification"].update(relevance="uncertain", sentiment="abstain")
        result = self.enrich(runner=lambda *args, **kwargs: self.fail("no input"))
        self.assertEqual(result["insights"]["status"], "empty")
        self.assertEqual(result["insights"]["groups"], [])

    def test_script_data_cannot_close_its_inert_container(self):
        self.report["articles"][0]["title"] = "가상 </script><img src=x onerror=alert(1)>"
        data = site.dashboard_content(self.report)["dashboard_data"]
        self.assertNotIn("<", data)
        self.assertNotIn("</script>", data)
        self.assertEqual(json.loads(data)["articles"][0]["title"], self.report["articles"][0]["title"])

    @unittest.skipUnless(shutil.which("node"), "Node unavailable; run JS tests separately on Mac")
    def test_browser_period_aggregation_matches_python(self):
        result = self.enrich()
        payload = site.dashboard_content(result)["dashboard_data"]
        javascript = "const {buildView}=require('./web/app.js');let input='';process.stdin.on('data',x=>input+=x);process.stdin.on('end',()=>console.log(JSON.stringify([1,7,30].map(days=>buildView(JSON.parse(input),{days})))));"
        response = subprocess.run(["node", "-e", javascript], input=payload, text=True, capture_output=True, check=True, cwd=ROOT)
        actual = json.loads(response.stdout)
        for js, py in zip(actual, result["insights"]["periods"], strict=True):
            for key in ("statistics", "publisher_count", "newly_seen_count", "retained_count", "coverage_complete", "daily"):
                self.assertEqual(js[key], py[key])


if __name__ == "__main__":
    unittest.main()
