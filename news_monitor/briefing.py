"""Bounded headline-only grouping, attributable briefings and deterministic views."""

import copy
import hashlib
import json
import re
import time
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from news_monitor import analysis
from news_monitor.analysis import AnalysisError, strict_json
from news_monitor.codex import execute
from news_monitor.collect import ROOT, atomic_json, plain_title
from news_monitor.diagnostics import NON_RETRYABLE, error_code, record_errors
from news_monitor.display import public_text
from news_monitor.report import summarize, timestamp

KST = ZoneInfo("Asia/Seoul")
GROUP_FIELDS = {"label", "summary", "topic", "article_ids", "evidence"}


def kst_day(value):
    return timestamp(value).astimezone(KST).date().isoformat()


def article_day(row):
    return row["published_at"] or kst_day(row["indexed_at"])


def options(config):
    opts = config["briefing"]
    if (type(opts["enabled"]) is not bool or opts["provider"] not in ("codex", "cursor")
            or not isinstance(opts["model"], str) or not opts["model"].strip()):
        raise ValueError("invalid briefing options")
    for key, low, high in (("attempts", 1, 2), ("timeout_seconds", 1, 180), ("max_output_bytes", 1024, 131072),
                           ("max_label_chars", 10, 200), ("max_summary_chars", 20, 500),
                           ("max_evidence_chars", 10, 300), ("max_group_span_days", 0, 30)):
        if type(opts[key]) is not int or not low <= opts[key] <= high:
            raise ValueError("invalid briefing limit")
    if (not isinstance(opts["period_days"], list) or not opts["period_days"]
            or any(type(value) is not int or not 1 <= value <= 90 for value in opts["period_days"])
            or len(set(opts["period_days"])) != len(opts["period_days"])
            or type(opts["default_period_days"]) is not int or opts["default_period_days"] not in opts["period_days"]
            or not isinstance(opts["topics"], dict) or "other" not in opts["topics"]
            or any(not re.fullmatch(r"[a-z_]+", key) or not isinstance(value, str) or not value.strip()
                   for key, value in opts["topics"].items())):
        raise ValueError("invalid briefing periods or topics")
    for key, folder in (("prompt_file", "prompts"), ("schema_file", "schemas")):
        if not (ROOT / opts[key]).resolve().is_relative_to(ROOT / folder):
            raise ValueError("unsafe briefing resource")
    return opts


def run_cursor(prompt, model, *, timeout, max_bytes, **_):
    """Cursor has no output-schema option; the prompt states the shape and validation enforces it."""
    return analysis.run_cursor(prompt, {"model": model, "timeout_seconds": timeout, "max_output_bytes": max_bytes}), {}


def eligible(report):
    return [row for row in report["articles"] if row["classification"]["relevance"] == "related" and not row["analysis_failed"]]


def numbers(text):
    return set(re.findall(r"\d+(?:[.,]\d+)*", text)) if isinstance(text, str) else set()


def validate_groups(document, rows, config, *, extractive=False):
    opts = options(config)
    if not isinstance(document, dict) or set(document) != {"groups"} or not isinstance(document["groups"], list):
        raise ValueError("invalid briefing envelope")
    by_id = {row["id"]: row for row in rows}
    seen, clean = set(), []
    for group in document["groups"]:
        if not isinstance(group, dict) or set(group) != GROUP_FIELDS:
            raise ValueError("unexpected group fields")
        ids = group["article_ids"]
        if not isinstance(ids, list) or not ids or any(not isinstance(item, str) for item in ids):
            raise ValueError("invalid group identities")
        if len(set(ids)) != len(ids) or seen.intersection(ids):
            raise ValueError("duplicate group articles")
        if not set(ids).issubset(by_id):
            raise ValueError("unknown group articles")
        members = [by_id[item] for item in ids]
        dates = [date.fromisoformat(article_day(row)) for row in members]
        if (max(dates) - min(dates)).days > opts["max_group_span_days"]:
            raise ValueError("unrelated date ranges")
        group = copy.deepcopy(group)
        for key, limit in (("label", opts["max_label_chars"]), ("summary", opts["max_summary_chars"])):
            text = group[key]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("invalid briefing text")
            if len(text) > limit:
                raise ValueError("briefing " + key + " too long")
            if plain_title(text) != text or any(ord(char) < 32 for char in text):
                raise ValueError("briefing text markup")
            if not extractive and not re.search(r"[가-힣]", text):
                raise ValueError("briefing " + key + " not Korean")
            group[key] = public_text(text, config)
        if group["topic"] not in opts["topics"]:
            raise ValueError("unknown topic")
        if not extractive and not numbers(group["label"] + " " + group["summary"]) <= numbers(" ".join(row["title"] for row in members)):
            raise ValueError("unsupported summary number")
        evidence = group["evidence"]
        if not isinstance(evidence, list) or len(evidence) != len(ids):
            raise ValueError("missing headline evidence")
        evidence_ids = set()
        for entry in evidence:
            if not isinstance(entry, dict) or set(entry) != {"article_id", "quote"}:
                raise ValueError("invalid evidence")
            identity, quote = entry["article_id"], entry["quote"]
            if (identity not in ids or identity in evidence_ids or not isinstance(quote, str)
                    or not quote.strip() or len(quote) > opts["max_evidence_chars"] or quote not in by_id[identity]["title"]):
                raise ValueError("fabricated evidence")
            evidence_ids.add(identity)
        seen.update(ids)
        group["article_ids"] = sorted(ids)
        group["evidence"] = sorted(evidence, key=lambda entry: entry["article_id"])
        clean.append(group)
    if seen != set(by_id):
        raise ValueError("missing related articles")
    return sorted(clean, key=lambda group: group["article_ids"])


def split_by_date(ids, days, span):
    """Same-issue headlines further apart than the span become separate groups; the model does not count days."""
    chunks = []
    for identity in sorted(ids, key=lambda item: (days[item], item)):
        if chunks and (days[identity] - days[chunks[-1][0]]).days <= span:
            chunks[-1].append(identity)
        else:
            chunks.append([identity])
    return chunks


def ground_model_groups(document, rows, config):
    """The model selects identities; source evidence is never generated by it."""
    opts = options(config)
    if not isinstance(document, dict) or set(document) != {"groups"} or not isinstance(document["groups"], list):
        raise ValueError("invalid briefing envelope")
    by_id = {row["id"]: row for row in rows}
    days = {identity: date.fromisoformat(article_day(row)) for identity, row in by_id.items()}
    grounded = []
    for group in document["groups"]:
        if not isinstance(group, dict) or set(group) != GROUP_FIELDS - {"evidence"}:
            raise ValueError("unexpected group fields")
        ids = group["article_ids"]
        if not isinstance(ids, list) or not ids or any(not isinstance(identity, str) for identity in ids):
            raise ValueError("invalid group identities")
        if not set(ids).issubset(by_id):
            raise ValueError("unknown group articles")
        chunks = split_by_date(ids, days, opts["max_group_span_days"])
        for chunk in chunks:
            evidence = [{"article_id": identity, "quote": by_id[identity]["title"][:opts["max_evidence_chars"]].rstrip()}
                        for identity in chunk]
            value = {**group, "article_ids": chunk, "evidence": evidence}
            headline, source = by_id[chunk[0]]["title"], numbers(" ".join(by_id[identity]["title"] for identity in chunk))
            # The summary covers the whole group, so each part shows its own headline. Text with a number no member
            # headline has gets a headline too, instead of discarding every issue (the prompt rule alone did not stop it).
            if len(chunks) > 1 or not numbers(group["summary"]) <= source:
                value["summary"] = ("선택된 기사 제목: " + headline)[:opts["max_summary_chars"]].rstrip()
            if not numbers(group["label"]) <= source:
                value["label"] = headline[:opts["max_label_chars"]].rstrip()
            grounded.append(value)
    return validate_groups({"groups": grounded}, rows, config)


def singles(report, config):
    opts = options(config)
    return [{"label": row["title"][:opts["max_label_chars"]].rstrip(), "summary": row["title"][:opts["max_summary_chars"]].rstrip(),
             "topic": "other", "article_ids": [row["id"]],
             "evidence": [{"article_id": row["id"], "quote": row["title"][:opts["max_evidence_chars"]]}]}
            for row in eligible(report)]


def select_groups(groups, rows):
    by_id = {row["id"]: row for row in rows}
    selected = []
    for group in groups:
        ids = [identity for identity in group["article_ids"] if identity in by_id]
        if not ids:
            continue
        value = {**group, "article_ids": ids, "evidence": [entry for entry in group["evidence"] if entry["article_id"] in ids]}
        if len(ids) != len(group["article_ids"]):
            value["summary"] = "선택된 기사 제목: " + by_id[ids[0]]["title"]
        selected.append(value)
    return selected


def period_views(report, groups, config):
    if not report["window_end"] or not report["window_start"]:
        return []
    end = date.fromisoformat(kst_day(report["window_end"]))
    available = date.fromisoformat(kst_day(report["window_start"]))
    views = []
    for days in options(config)["period_days"]:
        start = end - timedelta(days=days - 1)
        rows = [row for row in report["articles"] if start.isoformat() <= article_day(row) <= end.isoformat()]
        daily = []
        for offset in range(days):
            day = start + timedelta(days=offset)
            day_rows = [row for row in rows if article_day(row) == day.isoformat()]
            daily.append({"date": day.isoformat(), "covered": day >= available,
                          "article_count": len(day_rows) if day >= available else None,
                          "statistics": summarize(day_rows) if day >= available else None})
        views.append({"days": days, "start": start.isoformat(), "end": end.isoformat(),
                      "coverage_complete": start >= available, "article_ids": [row["id"] for row in rows],
                      "statistics": summarize(rows), "publisher_count": len({row["publisher"] for row in rows}),
                      "issue_count": len(select_groups(groups, rows)),
                      "newly_seen_count": sum(kst_day(row["observation"]["first_seen_at"]) == end.isoformat() for row in rows),
                      "retained_count": sum(row["observation"]["reused"] for row in rows), "daily": daily})
    return views


def compose(report, config, groups=None, *, status="pending", model_calls=0, cache_hit=False):
    opts = options(config)
    groups = groups if groups is not None else singles(report, config)
    groups = sorted(groups, key=lambda group: group["article_ids"])
    prompt_hash = hashlib.sha256((ROOT / opts["prompt_file"]).read_bytes()).hexdigest()
    result = {"schema_version": 1, "status": status, "provider": opts["provider"], "model": opts["model"],
              "prompt_sha256": prompt_hash, "model_calls": model_calls, "cache_hit": cache_hit,
              "topics": opts["topics"], "default_period_days": opts["default_period_days"],
              "as_of_day": kst_day(report["window_end"] or report["generated_at"]),
              "available_start": kst_day(report["window_start"]) if report["window_start"] else None,
              "groups": groups, "periods": period_views(report, groups, config)}
    return result


def enrich(report, config, data_dir, runner=None, *, refresh=False, max_attempts=None):
    report = copy.deepcopy(report)
    opts = options(config)
    rows = eligible(report)
    status = "disabled" if not opts["enabled"] else "empty" if not rows else "fallback"
    calls, hit, groups = 0, False, None
    errors = []
    attempt_limit = opts["attempts"] if max_attempts is None else min(max_attempts, opts["attempts"])
    if type(attempt_limit) is not int or not 1 <= attempt_limit <= opts["attempts"]:
        raise ValueError("invalid briefing attempt limit")
    if opts["enabled"] and rows:
        payload = {"articles": [{"id": row["id"], "title": row["title"], "date": article_day(row)} for row in rows],
                   "topics": opts["topics"], "max_group_span_days": opts["max_group_span_days"]}
        template = (ROOT / opts["prompt_file"]).read_text()
        fingerprint = {"input": payload, "options": opts, "prompt": template, "version": 1}
        key = hashlib.sha256(json.dumps(fingerprint, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        cache_dir = data_dir / "briefing"
        cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache_file = cache_dir / (key + ".json")
        try:
            if not refresh and cache_file.is_file() and not cache_file.is_symlink() and cache_file.stat().st_size <= opts["max_output_bytes"]:
                groups = validate_groups(strict_json(cache_file.read_text()), rows, config)
                status, hit = "ok", True
        except (OSError, ValueError, TypeError, KeyError) as exc:
            errors.append({"phase": "cache_read", "reason": error_code(exc)})
        if groups is None:
            runner = runner or (execute if opts["provider"] == "codex" else run_cursor)
            for _ in range(attempt_limit):
                calls += 1
                started = time.monotonic()
                phase = "model_call"
                try:
                    raw, _trace = runner(template + "\n\nINPUT_JSON:\n" + json.dumps(payload, ensure_ascii=False),
                                         opts["model"], search=False, schema=ROOT / opts["schema_file"],
                                         timeout=opts["timeout_seconds"], max_bytes=opts["max_output_bytes"])
                    phase = "json_parse"
                    document = strict_json(raw)
                    phase = "validation"
                    groups = ground_model_groups(document, rows, config)
                    phase = "cache_write"
                    atomic_json(cache_file, {"groups": groups})
                    status = "ok"
                    break
                except (AnalysisError, OSError, ValueError, TypeError, KeyError) as exc:
                    groups = None
                    reason = error_code(exc)
                    errors.append({"phase": phase, "attempt": calls, "reason": reason,
                                   "elapsed_seconds": round(time.monotonic() - started, 3)})
                    if reason in NON_RETRYABLE:
                        break
    report["insights"] = compose(report, config, groups, status=status, model_calls=calls, cache_hit=hit)
    if status == "fallback" and report["status"] == "ok":
        report["status"] = "partial"
    atomic_json(data_dir / "briefing_status.json", {
        "checked_at": datetime.now(UTC).isoformat(), "collection_checked_at": report["collection_checked_at"],
        "status": status, "model_calls": calls, "cache_hit": hit, "refresh": refresh,
        "eligible_articles": len(rows), "attempt_limit": attempt_limit, "errors": errors})
    record_errors(data_dir, config, "briefing", report["collection_checked_at"], errors)
    return report


def validate_insights(report, config):
    value = report["insights"]
    statuses = {"ok", "fallback", "disabled", "empty", "pending"}
    if (not isinstance(value, dict) or value.get("status") not in statuses
            or type(value.get("model_calls")) is not int or not 0 <= value["model_calls"] <= options(config)["attempts"]
            or type(value.get("cache_hit")) is not bool):
        raise ValueError("invalid insights metadata")
    status = value["status"]
    calls, hit = value["model_calls"], value["cache_hit"]
    if (status == "ok" and not ((hit and calls == 0) or (not hit and 1 <= calls <= options(config)["attempts"]))
            or status == "fallback" and (hit or not 1 <= calls <= options(config)["attempts"])
            or status in ("empty", "disabled", "pending") and (hit or calls != 0)
            or status == "empty" and eligible(report)):
        raise ValueError("inconsistent insights provenance")
    if (status == "ok" and not options(config)["enabled"]) or (status == "disabled" and options(config)["enabled"]):
        raise ValueError("unexpected insights state")
    groups = validate_groups({"groups": value["groups"]}, eligible(report), config, extractive=status != "ok")
    if status != "ok" and groups != sorted(singles(report, config), key=lambda group: group["article_ids"]):
        raise ValueError("fallback must use source headlines")
    if compose(report, config, groups, status=status, model_calls=value["model_calls"], cache_hit=value["cache_hit"]) != value:
        raise ValueError("insights derived fields mismatch")
