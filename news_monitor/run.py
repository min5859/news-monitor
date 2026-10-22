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


def process_inputs(config, data_dir, reuse, runner, briefing_runner=None, *, briefing_only=False,
                   refresh_briefing=False, briefing_attempts=None):
    from news_monitor.briefing import options

    options(config)  # Validate new settings before spending collection/model budget.
    template = validate_options(config)
    now = datetime.now(UTC)
    try:
        if reuse or briefing_only:
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
        decisions, info = analyze_articles(collection["articles"], config, data_dir, runner, cache_only=briefing_only)
    else:
        decisions = []
    from news_monitor.briefing import enrich

    return enrich(make_report(collection, decisions, info, config, datetime.now(UTC)), config, data_dir, briefing_runner,
                  refresh=refresh_briefing, max_attempts=briefing_attempts)


def run_pipeline(config, data_dir, output_dir, reuse=False, runner=None, briefing_runner=None, *,
                 briefing_only=False, refresh_briefing=False, briefing_attempts=None):
    with collection_lock(data_dir):
        status_file = data_dir / "pipeline_status.json"
        operation = "briefing_only" if briefing_only else "reuse_collection" if reuse else "pipeline"
        atomic_json(status_file, {"status": "running", "operation": operation, "started_at": datetime.now(UTC).isoformat()})
        try:
            report = process_inputs(config, data_dir, reuse, runner, briefing_runner, briefing_only=briefing_only,
                                    refresh_briefing=refresh_briefing, briefing_attempts=briefing_attempts)
            display = report
            if not report["articles"] and (data_dir / "report.json").exists():
                from news_monitor.preflight import check_artifact

                try:
                    previous = json.loads((data_dir / "report.json").read_text())
                    check_artifact(previous, config, allow_legacy=True)
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
            state.update(operation=operation, display_status=display["status"], display_article_count=display["article_count"])
            state["briefing"] = {key: report["insights"][key] for key in ("status", "model_calls", "cache_hit")}
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
    parser.add_argument("--briefing-only", action="store_true", help="reuse collection and require cached classifications; no discovery/classification calls")
    parser.add_argument("--refresh-briefing", action="store_true", help="bypass briefing cache once without deleting it")
    parser.add_argument("--briefing-attempts", type=int, choices=(1, 2), help="cap this replay within the configured attempt budget")
    args = parser.parse_args()
    if (args.refresh_briefing or args.briefing_attempts is not None) and not args.briefing_only:
        parser.error("briefing overrides require --briefing-only")
    try:
        report = run_pipeline(load_config(args.config), args.data_dir, args.output_dir, args.reuse_collection,
                              briefing_only=args.briefing_only, refresh_briefing=args.refresh_briefing,
                              briefing_attempts=args.briefing_attempts)
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
    summary = {key: report[key] for key in ("status", "article_count", "publisher_count", "analysis")}
    summary["briefing"] = {key: report["insights"][key] for key in ("status", "model_calls", "cache_hit")}
    print(json.dumps(summary, ensure_ascii=False))
    return {"ok": 0, "failed": 1}.get(report["status"], 2)


if __name__ == "__main__":
    raise SystemExit(main())
