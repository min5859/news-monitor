"""Collect indexed publisher metadata using a bounded, live web-search run."""

import json
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from news_monitor.analysis import AnalysisError, strict_json
from news_monitor.codex import execute
from news_monitor.collect import ROOT, canonical_url, normalized, parse_article


def collect_web(config, now, runner=execute):
    opts = config["collection"]
    now = now.astimezone(UTC)
    today = now.astimezone(ZoneInfo("Asia/Seoul")).date()
    first = today - timedelta(days=opts["lookback_days"] - 1)
    start = datetime.combine(first, time.min, ZoneInfo("Asia/Seoul")).astimezone(UTC)
    stats = dict.fromkeys(("fetched", "invalid_metadata", "out_of_window", "not_target", "duplicates",
                           "limit_excluded", "selected", "unverified_url"), 0)
    result = {
        "schema_version": 1, "status": "failed", "target": config["target"]["name"],
        "checked_at": now.isoformat(), "window_start": start.isoformat(), "window_end": now.isoformat(),
        "date_basis": "publisher_date", "coverage": "search_sample_not_exhaustive",
        "selection": "latest_published_target_in_headline", "statistics": stats,
        "publishers": [], "warnings": [], "errors": [], "articles": [], "retry_after_seconds": None,
        "source_attribution": {"name": opts["attribution_name"], "url": opts["attribution_url"]},
    }
    payload = {"person": config["target"]["name"], "organization": config["target"]["organization"],
               "start_date": first.isoformat(), "end_date": today.isoformat(),
               "publisher_goal": opts["publisher_goal"],
               "max_articles": opts["max_results_per_query"], "max_web_calls": opts["max_search_calls"]}
    prompt = (ROOT / opts["search_prompt_file"]).read_text() + "\n\nINPUT_JSON:\n" + json.dumps(payload, ensure_ascii=False)
    try:
        raw, trace = runner(prompt, opts["codex_model"], search=True, schema=ROOT / opts["schema_file"],
                            timeout=opts["codex_timeout_seconds"], max_bytes=opts["codex_output_bytes"],
                            max_search_calls=opts["max_search_calls"])
        document = strict_json(raw)
        if not isinstance(document, dict) or set(document) != {"articles"} or not isinstance(document["articles"], list):
            raise ValueError("invalid discovery schema")
        if not trace["web_search_calls"] or (document["articles"] and not trace["sources"]):
            raise ValueError("missing real search provenance")
        verified = set()
        for url in trace["sources"]:
            try:
                verified.add(canonical_url(url, opts))
            except (ValueError, TypeError):
                pass
        result["web_search_calls"] = trace["web_search_calls"]
        items = document["articles"]
        if len(items) > opts["max_results_per_query"]:
            raise ValueError("too many discovery results")
    except AnalysisError as exc:
        result["errors"] = [{"error": str(exc)}]
        return result
    except (OSError, ValueError, TypeError, KeyError):
        result["errors"] = [{"error": "web_discovery_failed"}]
        return result
    rows = {}
    stats["fetched"] = len(items)
    for item in items:
        try:
            if not isinstance(item, dict) or set(item) != {"title", "url", "published_date", "language"}:
                raise ValueError("unexpected metadata")
            published = date.fromisoformat(item["published_date"])
            url = canonical_url(item["url"], opts)
            article = parse_article({**item, "url": url, "domain": urlsplit(url).hostname,
                                     "seendate": now.strftime("%Y%m%dT%H%M%SZ")}, config)
            article.update(published_at=published.isoformat(), collected_at=now.isoformat())
        except (ValueError, KeyError, TypeError):
            stats["invalid_metadata"] += 1
            continue
        if url not in verified:
            stats["unverified_url"] += 1
            continue
        if not first <= published <= today:
            stats["out_of_window"] += 1
            continue
        title = normalized(article["title"])
        if any(not any(normalized(alias) in title for alias in config["target"][key])
               for key in ("person_aliases", "organization_aliases")):
            stats["not_target"] += 1
            continue
        if url in rows:
            stats["duplicates"] += 1
        rows[url] = article
    ordered = sorted(rows.values(), key=lambda row: (row["published_at"], row["url"]), reverse=True)
    selected = ordered[:opts["max_articles"]]
    stats.update(selected=len(selected), limit_excluded=len(ordered) - len(selected))
    result.update(articles=selected, publishers=sorted({row["publisher"] for row in selected}))
    if items and stats["invalid_metadata"] + stats["unverified_url"] == len(items):
        result["errors"] = [{"error": "no_verified_discovery_metadata"}]
    else:
        result["status"] = "ok" if selected else "empty"
        if stats["invalid_metadata"] or stats["unverified_url"] or len(items) >= opts["max_results_per_query"]:
            result["status"] = "partial"
    if len(result["publishers"]) < opts["publisher_goal"]:
        result["warnings"].append("publisher_goal_not_met")
    return result
