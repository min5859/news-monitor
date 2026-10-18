"""Collect → validate → classify/abstain → aggregate → build a local sample."""

import argparse
import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from news_monitor.analysis import analyze_articles, validate_options
from news_monitor.collect import (
    ROOT,
    _run_collection,
    atomic_json,
    collection_lock,
    load_config,
)
from news_monitor.report import make_report, prepare_collection
from news_monitor.site import build_site


def process_inputs(config, data_dir, reuse, runner):
    template = validate_options(config)
    now = datetime.now(UTC)
    try:
        if reuse:
            status = json.loads((data_dir / "collection_status.json").read_text())
        else:
            status = _run_collection(config, data_dir)
        now = datetime.now(UTC)
        dataset = None
        if status["status"] in ("ok", "partial") and status["statistics"]["selected"] != 0:
            dataset = json.loads((data_dir / "articles.json").read_text())
        collection = prepare_collection(status, dataset, config, now)
    except (OSError, ValueError, TypeError, KeyError):
        collection = {"status": "failed", "checked_at": None, "articles": [],
                      "window_start": None, "window_end": None, "warnings": []}
    info = {"provider": config["analysis"]["provider"], "model": config["analysis"]["model"],
            "prompt_sha256": hashlib.sha256(template.encode()).hexdigest(),
            "model_calls": 0, "cache_hits": 0, "failed_articles": 0}
    if collection["articles"]:
        decisions, info = analyze_articles(collection["articles"], config, data_dir, runner)
    else:
        decisions = []
    return make_report(collection, decisions, info, config, datetime.now(UTC))


def run_pipeline(config, data_dir, output_dir, reuse=False, runner=None):
    with collection_lock(data_dir):
        status_file = data_dir / "pipeline_status.json"
        atomic_json(status_file, {"status": "running", "started_at": datetime.now(UTC).isoformat()})
        try:
            report = process_inputs(config, data_dir, reuse, runner)
            display = report
            if not report["articles"] and (data_dir / "report.json").exists():
                from news_monitor.preflight import check_artifact

                try:
                    previous = json.loads((data_dir / "report.json").read_text())
                    check_artifact(previous, config)
                    if previous["articles"] and previous["status"] in ("ok", "partial"):
                        display = copy.deepcopy(previous)
                        display.update(status="stale", generated_at=report["generated_at"],
                                       latest_attempt={"status": report["status"], "checked_at": report["generated_at"]})
                except (OSError, ValueError, KeyError, TypeError):
                    pass  # Do not display unknown, unsafe or incompatible cached data.
            build_site(display, output_dir)
            if report["status"] != "failed" and report["articles"]:
                atomic_json(data_dir / "report.json", report)
            state = {key: report[key] for key in (
                "status", "generated_at", "collection_checked_at", "analysis", "article_count")}
            state.update(display_status=display["status"], display_article_count=display["article_count"])
            atomic_json(status_file, state)
            return report
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            atomic_json(status_file, {"status": "failed", "checked_at": datetime.now(UTC).isoformat(),
                                      "error": type(exc).__name__})
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--reuse-collection", action="store_true", help="do not request API; still enforce matching/freshness")
    args = parser.parse_args()
    try:
        report = run_pipeline(load_config(args.config), args.data_dir, args.output_dir, args.reuse_collection)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        if not isinstance(exc, BlockingIOError):
            try:
                with collection_lock(args.data_dir):
                    atomic_json(args.data_dir / "pipeline_status.json", {
                        "status": "failed", "checked_at": datetime.now(UTC).isoformat(),
                        "error": type(exc).__name__})
            except OSError:
                pass  # Never clobber an active run or hide the original failure.
        print(json.dumps({"status": "failed", "error": type(exc).__name__}))
        return 1
    print(json.dumps({key: report[key] for key in ("status", "article_count", "publisher_count", "analysis")}, ensure_ascii=False))
    return {"ok": 0, "failed": 1}.get(report["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
