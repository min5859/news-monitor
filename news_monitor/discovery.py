"""Collect indexed publisher metadata using a bounded, live web-search run."""

import json
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as day_time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from news_monitor.analysis import AnalysisError, strict_json
from news_monitor.codex import execute
from news_monitor.collect import (
    ROOT,
    canonical_url,
    headline_names_target,
    normalized,
    parse_article,
)
from news_monitor.diagnostics import error_code


def story_key(row):
    # Not the date: sources may report the same story a day or an update apart.
    return row["publisher"], normalized(row["title"])


def select_stories(rows, stats, limit):
    """One row per story across subdomain copies, newest publication first, at most limit."""
    stories = {}
    for row in rows:
        key = story_key(row)
        if key in stories:
            stats["duplicates"] += 1
        # Keep the same URL for a story whatever order the sources returned it in.
        if key not in stories or row["url"] < stories[key]["url"]:
            stories[key] = row
    ordered = sorted(stories.values(), key=lambda row: (row["published_at"], row["url"]), reverse=True)
    stats.update(selected=min(len(ordered), limit), limit_excluded=max(0, len(ordered) - limit))
    return ordered[:limit]


def collect_web(config, now, runner=execute):
    opts = config["collection"]
    now = now.astimezone(UTC)
    today = now.astimezone(ZoneInfo("Asia/Seoul")).date()
    first = today - timedelta(days=opts["lookback_days"] - 1)
    start = datetime.combine(first, day_time.min, ZoneInfo("Asia/Seoul")).astimezone(UTC)
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
               "max_articles": opts["max_results_per_query"],
               "require_organization_in_headline": opts.get("require_organization_in_headline", True)}
    template = (ROOT / opts["search_prompt_file"]).read_text()
    groups = opts.get("search_groups", [[config["target"]["name"] + " " + config["target"]["organization"]]])
    result["source_queries"] = {"planned": len(groups), "attempted": 0, "succeeded": 0}
    result["web_search_calls"] = 0
    deadline = time.monotonic() + opts["codex_timeout_seconds"]
    items, verified = [], set()
    # Reserve a fixed share even when a child fails: its actual usage may be unknown.
    for index, group in enumerate(groups):
        allocation = opts["max_search_calls"] // len(groups) + (index < opts["max_search_calls"] % len(groups))
        remaining = deadline - time.monotonic()
        if allocation < 1 or remaining < 1:
            result["errors"].append({"error": "search_budget_exhausted", "group": index + 1})
            break
        # A soft finish target leaves room for output, without raising the event cap.
        finish_after = max(1, allocation - opts["search_finish_reserve_calls"])
        query_calls = min(opts["search_query_calls_per_group"], max(1, finish_after - 1))
        prompt = template + "\n\nINPUT_JSON:\n" + json.dumps(
            {**payload, "query_group": group, "max_web_calls": allocation,
             "finish_after_web_calls": finish_after, "query_call_budget": query_calls,
             "verification_call_budget": finish_after - query_calls,
             "publisher_goal_scope": "whole_collection_not_this_group",
             "already_found_urls": [item["url"] for item in items if isinstance(item, dict) and "url" in item]},
            ensure_ascii=False)
        result["source_queries"]["attempted"] += 1
        attempted_at = time.monotonic()
        try:
            raw, trace = runner(prompt, opts["codex_model"], search=True, schema=ROOT / opts["schema_file"],
                                timeout=remaining / (len(groups) - index), max_bytes=opts["codex_output_bytes"],
                                max_search_calls=allocation)
            document = strict_json(raw)
            if not isinstance(document, dict) or set(document) != {"articles"} or not isinstance(document["articles"], list):
                raise ValueError("invalid discovery schema")
            calls = trace["web_search_calls"]
            if type(calls) is not int or not 1 <= calls <= allocation or (document["articles"] and not trace["sources"]):
                raise ValueError("missing real search provenance")
            if len(document["articles"]) > opts["max_results_per_query"]:
                raise ValueError("too many discovery results")
            if len(document["articles"]) == opts["max_results_per_query"]:
                result["warnings"].append("upstream_limit_reached")
            group_sources = set()
            for url in trace["sources"]:
                try:
                    group_sources.add(canonical_url(url, opts))
                except (ValueError, TypeError):
                    pass
            # Provenance belongs to this group, not a different successful search.
            for item in document["articles"]:
                if isinstance(item, dict) and isinstance(item.get("url"), str):
                    try:
                        if canonical_url(item["url"], opts) not in group_sources:
                            stats["unverified_url"] += 1
                            continue
                    except ValueError:
                        pass  # Count malformed URLs in metadata validation below.
                items.append(item)
            verified.update(group_sources)
            result["web_search_calls"] += calls
            result["source_queries"]["succeeded"] += 1
        except (AnalysisError, OSError, ValueError, TypeError, KeyError) as exc:
            result["errors"].append({"error": "web_discovery_failed", "group": index + 1,
                                     "reason": error_code(exc), "elapsed_seconds": round(time.monotonic() - attempted_at, 3)})
    rows = {}
    stats["fetched"] = len(items) + stats["unverified_url"]
    for item in items:
        try:
            if not isinstance(item, dict) or set(item) != {"title", "url", "published_date", "language"}:
                raise ValueError("unexpected metadata")
            published = date.fromisoformat(item["published_date"])
            url = canonical_url(item["url"], opts)
            article = parse_article({**item, "url": url, "domain": urlsplit(url).hostname,
                                     "seendate": now.strftime("%Y%m%dT%H%M%SZ")}, config)
            article.update(published_at=published.isoformat(), indexed_at=now.isoformat(),
                           collected_at=now.isoformat(), first_seen_at=now.isoformat())
        except (ValueError, KeyError, TypeError):
            stats["invalid_metadata"] += 1
            continue
        if url not in verified:
            stats["unverified_url"] += 1
            continue
        if not first <= published <= today:
            stats["out_of_window"] += 1
            continue
        if not headline_names_target(article["title"], config):
            stats["not_target"] += 1
            continue
        if url in rows:
            stats["duplicates"] += 1
        if url not in rows or article["published_at"] >= rows[url]["published_at"]:
            rows[url] = article
    selected = select_stories(rows.values(), stats, opts["max_articles"])
    result.update(articles=selected, publishers=sorted({row["publisher"] for row in selected}))
    if not result["source_queries"]["succeeded"] or (not selected and (stats["invalid_metadata"] or stats["unverified_url"])):
        result["errors"].append({"error": "no_verified_discovery_metadata"})
    else:
        result["status"] = "ok" if selected else "empty"
        if (result["errors"] or stats["invalid_metadata"] or stats["unverified_url"] or stats["limit_excluded"]
                or "upstream_limit_reached" in result["warnings"]):
            result["status"] = "partial"
    stats.update(newly_found=len(selected), reconfirmed=0, retained=0)
    if len(result["publishers"]) < opts["publisher_goal"]:
        result["warnings"].append("publisher_goal_not_met")
    result["warnings"] = sorted(set(result["warnings"]))
    return result


def merge_previous(result, previous, config):
    """Retain validated in-window metadata, never relabel old observations as new."""
    from news_monitor.report import prepare_collection, timestamp

    if result["status"] == "failed" or not previous:
        return result
    try:
        checked = timestamp(result["checked_at"])
        old_checked = timestamp(previous["checked_at"])
        if old_checked > checked:
            raise ValueError("future previous collection")
        clean = prepare_collection(previous, previous, config, now=old_checked)
        if clean["status"] not in ("ok", "partial"):
            return result
        start = timestamp(result["window_start"]).astimezone(ZoneInfo("Asia/Seoul")).date()
        end = checked.astimezone(ZoneInfo("Asia/Seoul")).date()
        old = {}
        for row in clean["articles"]:
            key = story_key(row)
            # Older saves may hold one story per subdomain; the earliest first sighting wins.
            if (start <= date.fromisoformat(row["published_at"]) <= end
                    and (key not in old or row["first_seen_at"] < old[key]["first_seen_at"])):
                old[key] = row
    except (ValueError, KeyError, TypeError, OverflowError):
        result["warnings"].append("previous_collection_rejected")
        return result
    fresh = {story_key(row): row for row in result["articles"]}
    old_urls = {row["url"]: row for row in old.values()}
    fresh_urls = {row["url"] for row in fresh.values()}
    for key in old.keys() & fresh.keys():
        fresh[key]["first_seen_at"] = old[key]["first_seen_at"]
    for row in fresh.values():
        if row["url"] in old_urls:
            row["first_seen_at"] = old_urls[row["url"]]["first_seen_at"]
    remaining_old = {key: row for key, row in old.items() if row["url"] not in fresh_urls}
    ordered = sorted({**remaining_old, **fresh}.values(), key=lambda row: (row["published_at"], row["url"]), reverse=True)
    selected = ordered[:config["collection"]["max_articles"]]
    excluded = len(ordered) - len(selected)
    result["statistics"]["limit_excluded"] += excluded
    if excluded:
        result["status"] = "partial"
    retained = sum(story_key(row) not in fresh for row in selected)
    reconfirmed = sum(story_key(row) in fresh and (story_key(row) in old or row["url"] in old_urls) for row in selected)
    result["statistics"].update(selected=len(selected), retained=retained, reconfirmed=reconfirmed,
                                newly_found=len(selected) - retained - reconfirmed)
    result.update(articles=selected, publishers=sorted({row["publisher"] for row in selected}))
    result["warnings"] = [value for value in result["warnings"] if value != "publisher_goal_not_met"]
    if len(result["publishers"]) < config["collection"]["publisher_goal"]:
        result["warnings"].append("publisher_goal_not_met")
    if retained and retained == len(selected):
        result["status"] = "partial"
        result["warnings"].append("retained_articles_only")
    return result
