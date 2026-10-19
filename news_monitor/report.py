"""Check collection freshness and deterministically export public fields only."""

import hashlib
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from news_monitor.analysis import SENTIMENTS, validate_decision
from news_monitor.collect import canonical_url, normalized, plain_title
from news_monitor.display import display_name, public_text, validate_public_names

ARTICLE_FIELDS = ("id", "title", "url", "publisher", "language", "indexed_at", "published_at", "analysis_scope", "source")
WARNINGS = {"publisher_goal_not_met", "upstream_limit_reached", "remaining_queries_skipped_rate_limit",
            "remaining_queries_skipped_retry_after", "retained_articles_only", "previous_collection_rejected"}


def timestamp(value):
    if not isinstance(value, str):
        raise TypeError("invalid timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(UTC)


def prepare_collection(status, dataset, config, now=None):
    now = now or datetime.now(UTC)
    if not isinstance(status, dict) or status.get("status") not in ("ok", "partial", "empty", "failed"):
        raise ValueError("invalid collection status")
    checked = timestamp(status["checked_at"])
    if not timedelta(0) <= now - checked <= timedelta(hours=config["analysis"]["max_collection_age_hours"]):
        raise ValueError("collection is stale or from the future")
    state = status["status"]
    publication_dates = config["collection"]["provider"] == "codex_web_search"
    clean = {"status": state, "checked_at": checked.isoformat(), "articles": [],
             "window_start": None, "window_end": None,
             "date_basis": "publisher_date" if publication_dates else "gdelt_observation_not_publication",
             "selection": "latest_published_target_in_headline" if publication_dates else "latest_observed_target_in_headline",
             "warnings": sorted(set(status.get("warnings", [])) & WARNINGS)}
    if state == "failed":
        return clean  # Old articles.json is deliberately not examined or reused.
    if status["target"] != config["target"]["name"]:
        raise ValueError("collection target mismatch")
    start, end = timestamp(status["window_start"]), timestamp(status["window_end"])
    if start > end or end != checked:
        raise ValueError("invalid collection window")
    clean.update(window_start=start.isoformat(), window_end=end.isoformat())
    selected = status["statistics"]["selected"]
    if type(selected) is not int or not 0 <= selected <= config["collection"]["max_articles"]:
        raise ValueError("invalid selected count")
    if state == "empty" and selected != 0:
        raise ValueError("invalid empty collection")
    if state in ("empty", "partial") and selected == 0:
        return clean
    if not isinstance(dataset, dict):
        raise TypeError("missing articles dataset")
    for field in ("checked_at", "status", "target", "window_start", "window_end", "date_basis", "selection"):
        if dataset.get(field) != status.get(field):
            raise ValueError("articles and status mismatch")
    rows = dataset.get("articles")
    if (not isinstance(rows, list) or not rows or len(rows) > config["collection"]["max_articles"]
            or type(status["statistics"]["selected"]) is not int or status["statistics"]["selected"] != len(rows)):
        raise ValueError("invalid article count")
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("invalid article object")
        article = {field: row[field] for field in ARTICLE_FIELDS}
        title = plain_title(article["title"])
        url = canonical_url(article["url"], config["collection"])
        if not title or len(title) > config["collection"]["max_title_chars"] or title != article["title"]:
            raise ValueError("invalid article title")
        alias_groups = [config["target"]["person_aliases"]]
        if not publication_dates or config["collection"].get("require_organization_in_headline", True):
            alias_groups.append(config["target"]["organization_aliases"])
        for aliases in alias_groups:
            if not any(normalized(alias) in normalized(title) for alias in aliases):
                raise ValueError("headline does not match configured target")
        if article["url"] != url or article["id"] != hashlib.sha256(url.encode()).hexdigest()[:20] or url in seen:
            raise ValueError("invalid article identity")
        if article["publisher"] != urlsplit(url).hostname.removeprefix("www."):
            raise ValueError("invalid article publisher")
        if not isinstance(article["language"], str) or len(article["language"]) > 80:
            raise ValueError("invalid article language")
        if publication_dates:
            published = date.fromisoformat(article["published_at"])
            if (published.isoformat() != article["published_at"]
                    or not start.astimezone(ZoneInfo("Asia/Seoul")).date() <= published <= end.astimezone(ZoneInfo("Asia/Seoul")).date()):
                raise ValueError("invalid publisher date")
        elif article["published_at"] is not None:
            raise ValueError("GDELT observation is not publication time")
        observed = timestamp(article["indexed_at"])
        collected = timestamp(row["collected_at"])
        first_seen = timestamp(row.get("first_seen_at", article["indexed_at"]))
        if (not start <= observed <= collected <= checked or not first_seen <= observed
                or (not publication_dates and collected != checked)
                or article["analysis_scope"] != "headline_only"
                or article["source"] != config["collection"]["provider"]):
            raise ValueError("invalid article provenance")
        article.update(first_seen_at=first_seen.isoformat(), collected_at=collected.isoformat())
        seen.add(url)
        clean["articles"].append(article)
    return clean


def percentages(counts):
    """Largest-remainder rounding at 0.1%; deterministic ties follow key order."""
    total = sum(counts.values())
    if not total:
        return {key: None for key in counts}
    units = {key: count * 1000 // total for key, count in counts.items()}
    order = sorted(counts, key=lambda key: counts[key] * 1000 % total, reverse=True)
    for key in order[:1000 - sum(units.values())]:
        units[key] += 1
    return {key: value / 10 for key, value in units.items()}


def summarize(rows):
    counts = dict.fromkeys(SENTIMENTS, 0)
    abstained = unrelated = 0
    for row in rows:
        decision = row["classification"]
        if decision["relevance"] == "unrelated":
            unrelated += 1
        elif decision["sentiment"] == "abstain":
            abstained += 1
        else:
            counts[decision["sentiment"]] += 1
    return {"counts": counts, "denominator": sum(counts.values()), "percentages": percentages(counts),
            "abstained": abstained, "unrelated": unrelated,
            "positive_negative_only": {"denominator": counts["positive"] + counts["negative"],
                                       "percentages": percentages({key: counts[key] for key in ("positive", "negative")})}}


def make_report(collection, decisions, info, config, now=None):
    now = now or datetime.now(UTC)
    rows = collection["articles"]
    if len(rows) != len(decisions):
        raise ValueError("analysis count mismatch")
    articles = []
    for article, decision in zip(rows, decisions, strict=True):
        if article["id"] != decision["id"]:
            raise ValueError("analysis identity mismatch")
        clean = {field: article[field] for field in ARTICLE_FIELDS}
        clean["observation"] = {"first_seen_at": article.get("first_seen_at", article["indexed_at"]),
                                "last_seen_at": article["indexed_at"],
                                "reused": article.get("collected_at", collection["checked_at"]) != collection["checked_at"]}
        clean["classification"] = validate_decision(decision["decision"], article["title"], config["analysis"])
        clean["analysis_failed"] = decision["error"] is not None
        if clean["analysis_failed"] and (clean["classification"]["relevance"] != "uncertain"
                                         or clean["classification"]["sentiment"] != "abstain"):
            raise ValueError("failed analysis must abstain")
        # Decisions are checked against the original headline before display-only edits.
        clean["title"] = public_text(clean["title"], config)
        for field in ("reason", "evidence"):
            clean["classification"][field] = public_text(clean["classification"][field], config)
        clean["evidence_omitted_for_display"] = bool(clean["classification"]["evidence"]
                                                   and clean["classification"]["evidence"] not in clean["title"])
        if clean["evidence_omitted_for_display"]:
            clean["classification"]["evidence"] = ""
        articles.append(clean)
    failed = collection["status"] == "failed"
    stats = None if failed else summarize(articles)
    publishers = sorted({row["publisher"] for row in articles})
    state = collection["status"]
    if state == "ok" and any(row["analysis_failed"] for row in articles):
        state = "partial"
    source = config["collection"]
    report = {
        "schema_version": 2, "sample": True, "human_reviewed": False, "status": state,
        "target": {"name": display_name(config), "organization": config["target"]["organization"]},
        "display_policy": {"person_name_replaced": True, "source_links_may_identify_target": True,
                           "article_titles_modified": True, "evidence_may_be_omitted": True},
        "generated_at": now.isoformat(), "collection_checked_at": collection["checked_at"],
        "window_start": collection["window_start"], "window_end": collection["window_end"],
        "analysis_scope": "headline_only", "date_basis": collection.get("date_basis", "publisher_date" if source["provider"] == "codex_web_search" else "gdelt_observation_not_publication"),
        "coverage": "search_sample_not_exhaustive", "selection": collection.get("selection", "latest_published_target_in_headline" if source["provider"] == "codex_web_search" else "latest_observed_target_in_headline"),
        "article_count": None if failed else len(articles), "publisher_count": None if failed else len(publishers),
        "publisher_goal": source["publisher_goal"], "warnings": collection["warnings"],
        "source_attribution": {"name": source["attribution_name"], "url": canonical_url(source["attribution_url"], source)},
        "analysis": {key: info[key] for key in ("provider", "model", "prompt_sha256", "model_calls", "cache_hits", "failed_articles")},
        "statistics": stats,
        "publishers": [{"publisher": publisher, "article_count": sum(row["publisher"] == publisher for row in articles),
                        **summarize([row for row in articles if row["publisher"] == publisher])} for publisher in publishers],
        "articles": articles,
    }
    validate_public_names(report, config)
    from news_monitor.briefing import compose

    report["insights"] = compose(report, config, status="pending" if config["briefing"]["enabled"] else "disabled")
    return report
