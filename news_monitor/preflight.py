"""Read-only artifact gate. Does not publish, authenticate, or change the host."""

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from news_monitor.analysis import DECISION_KEYS, RELEVANCE, SENTIMENTS, validate_options
from news_monitor.collect import ROOT, canonical_url, load_config
from news_monitor.display import public_text, validate_public_names
from news_monitor.report import ARTICLE_FIELDS, WARNINGS, summarize, timestamp
from news_monitor.site import OUTPUT_FILES

PUBLIC_FIELDS = {
    "schema_version", "sample", "human_reviewed", "status", "target", "display_policy",
    "generated_at", "collection_checked_at", "window_start", "window_end", "analysis_scope",
    "date_basis", "coverage", "selection", "article_count", "publisher_count", "publisher_goal",
    "warnings", "source_attribution", "analysis", "statistics", "publishers", "articles",
}


class DisplayText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        self.parts.extend(value for key, value in attrs if value and key not in ("href", "src"))


def exact_keys(value, keys):
    return isinstance(value, dict) and set(value) == set(keys)


def check_artifact(report, config):
    if not exact_keys(report, PUBLIC_FIELDS):
        raise ValueError("unexpected public report fields")
    if (report["schema_version"] != 1 or report["sample"] is not True or report["human_reviewed"] is not False
            or report["analysis_scope"] != "headline_only"
            or not exact_keys(report["target"], {"name", "organization"})
            or not exact_keys(report["display_policy"], {"person_name_replaced", "source_links_may_identify_target",
                                                       "article_titles_modified", "evidence_may_be_omitted"})):
        raise ValueError("invalid public sample metadata")
    validate_public_names(report, config)
    if not exact_keys(report["source_attribution"], {"name", "url"}):
        raise ValueError("unexpected source fields")
    if not exact_keys(report["analysis"], {"provider", "model", "prompt_sha256", "model_calls", "cache_hits", "failed_articles"}):
        raise ValueError("unexpected analysis fields")
    info = report["analysis"]
    if (info["provider"] != config["analysis"]["provider"] or info["model"] != config["analysis"]["model"]
            or info["prompt_sha256"] != hashlib.sha256(validate_options(config).encode()).hexdigest()
            or not isinstance(report["warnings"], list) or not set(report["warnings"]).issubset(WARNINGS)):
        raise ValueError("outdated analysis or invalid warnings")
    expected = {*ARTICLE_FIELDS, "observation", "classification", "analysis_failed", "evidence_omitted_for_display"}
    seen = set()
    for row in report["articles"]:
        if not exact_keys(row, expected) or not exact_keys(row["classification"], DECISION_KEYS):
            raise ValueError("unexpected article fields")
        url = canonical_url(row["url"], config["collection"])
        if (url != row["url"] or url in seen or row["publisher"] != urlsplit(url).hostname.removeprefix("www.")
                or row["id"] != hashlib.sha256(url.encode()).hexdigest()[:20]):
            raise ValueError("invalid public article URL")
        value = row["classification"]
        if (any(not isinstance(item, str) for item in value.values())
                or value["relevance"] not in RELEVANCE or value["sentiment"] not in (*SENTIMENTS, "abstain")
                or (value["relevance"] != "related" and value["sentiment"] != "abstain")
                or not value["reason"].strip() or (value["evidence"] and value["evidence"] not in row["title"])
                or (value["sentiment"] != "abstain" and not value["evidence"] and row["evidence_omitted_for_display"] is not True)
                or (row["analysis_failed"] and (value["relevance"], value["sentiment"]) != ("uncertain", "abstain"))):
            raise ValueError("invalid public classification")
        seen.add(url)
        observation = row["observation"]
        if (not exact_keys(observation, {"first_seen_at", "last_seen_at", "reused"})
                or type(observation["reused"]) is not bool
                or observation["last_seen_at"] != row["indexed_at"]
                or not timestamp(observation["first_seen_at"]) <= timestamp(observation["last_seen_at"]) <= timestamp(report["collection_checked_at"])):
            raise ValueError("invalid public observation")
    if report["article_count"] != len(report["articles"]):
        raise ValueError("public article count mismatch")
    if report["statistics"] != summarize(report["articles"]):
        raise ValueError("public statistics mismatch")
    domains = sorted({row["publisher"] for row in report["articles"]})
    expected_publishers = []
    for domain in domains:
        rows = [row for row in report["articles"] if row["publisher"] == domain]
        expected_publishers.append({"publisher": domain, "article_count": len(rows), **summarize(rows)})
    if report["publisher_count"] != len(domains) or report["publishers"] != expected_publishers:
        raise ValueError("publisher statistics mismatch")


def check_ready(config, data_dir, output_dir, now=None):
    now = now or datetime.now(UTC)
    checks = []

    def record(name, condition):
        checks.append({"check": name, "ok": bool(condition)})

    try:
        validate_options(config)
        record("only_owned_static_files", {path.name for path in output_dir.iterdir()} == OUTPUT_FILES
               and all(path.is_file() and not path.is_symlink() for path in output_dir.iterdir()))
        with (output_dir / "report.json").open("rb") as stream:
            raw = stream.read(config["collection"]["max_response_bytes"] + 1)
        if len(raw) > config["collection"]["max_response_bytes"]:
            raise ValueError("artifact too large")
        report = json.loads(raw)
        record("successful_nonempty_report", report["status"] in ("ok", "partial")
               and report["article_count"] is not None and report["article_count"] > 0)
        if not checks[-1]["ok"]:
            return {"ready": False, "checks": checks}
        check_artifact(report, config)
        record("public_schema_and_display_name", True)
        parser = DisplayText()
        parser.feed((output_dir / "index.html").read_text())
        record("html_display_name", all(public_text(value, config) == value for value in parser.parts))
        pipeline = json.loads((data_dir / "pipeline_status.json").read_text())
        collection = json.loads((data_dir / "collection_status.json").read_text())
        record("matches_latest_pipeline", pipeline["status"] == report["status"]
               and pipeline["generated_at"] == report["generated_at"]
               and pipeline["collection_checked_at"] == report["collection_checked_at"])
        record("matches_latest_collection", collection["status"] in ("ok", "partial")
               and collection["checked_at"] == report["collection_checked_at"])
        age = now - timestamp(report["collection_checked_at"])
        record("fresh_collection", timedelta(0) <= age <= timedelta(hours=config["analysis"]["max_collection_age_hours"]))
        record("has_completed_related_analysis", report["statistics"]["denominator"] > 0)
        record("all_model_calls_completed", report["analysis"]["failed_articles"] == 0)
        record("has_current_search_results", "retained_articles_only" not in report["warnings"]
               and any(not row["observation"]["reused"] for row in report["articles"]))
    except (OSError, ValueError, KeyError, TypeError, OverflowError):
        record("valid_artifact_and_inputs", False)
    return {"ready": bool(checks) and all(row["ok"] for row in checks), "checks": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    try:
        result = check_ready(load_config(args.config), args.data_dir, args.output_dir)
    except (OSError, ValueError, KeyError, TypeError):
        result = {"ready": False, "checks": [{"check": "valid_config", "ok": False}]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
