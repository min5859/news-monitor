"""Classify failures into fixed codes. Never return arbitrary exception/output text."""

import json
import re
from datetime import UTC, datetime

VALIDATION_CODES = {
    "invalid briefing envelope": "response_schema_invalid",
    "unexpected group fields": "group_fields_invalid",
    "invalid group identities": "group_article_ids_invalid",
    "duplicate group articles": "group_article_ids_duplicate",
    "unknown group articles": "group_article_ids_unknown",
    "unrelated date ranges": "group_date_span_exceeded",
    "invalid briefing text": "group_text_invalid",
    "briefing label too long": "group_label_too_long",
    "briefing summary too long": "group_summary_too_long",
    "briefing label not Korean": "group_label_not_korean",
    "briefing summary not Korean": "group_summary_not_korean",
    "briefing text markup": "group_text_markup_invalid",
    "unknown topic": "group_topic_invalid",
    "unsupported summary number": "group_number_not_grounded",
    "missing headline evidence": "group_evidence_missing",
    "invalid evidence": "group_evidence_invalid",
    "fabricated evidence": "group_evidence_not_grounded",
    "missing related articles": "group_articles_missing",
    "invalid discovery schema": "response_schema_invalid",
    "missing real search provenance": "search_provenance_missing",
    "too many discovery results": "search_result_limit_exceeded",
    "duplicate JSON key": "response_duplicate_key",
}
MODEL_CODES = {
    "model_timeout", "model_output_too_large", "model_cli_failed", "model_cli_unavailable",
    "codex_turn_failed", "codex_unexpected_tool", "codex_unexpected_search",
    "codex_search_budget_exhausted", "codex_incomplete_result", "model_usage_limit",
    "model_rate_limited", "model_authentication_failed", "model_service_unavailable",
    "model_schema_rejected", "model_unavailable", "classification_cache_required",
}
NON_RETRYABLE = {"model_usage_limit", "model_rate_limited", "model_authentication_failed",
                 "model_schema_rejected", "model_unavailable", "model_cli_unavailable"}


def model_failure(text, default="model_cli_failed"):
    text = text.casefold() if isinstance(text, str) else ""
    patterns = (
        ("model_usage_limit", r"usage_limit|usage limit|insufficient_quota|quota exceeded|you.ve hit your.*limit"),
        ("model_rate_limited", r"rate_limit|rate limit|\b429\b|too many requests"),
        ("model_authentication_failed", (r"\b401\b|unauthorized|unauthenticated|invalid_api_key|authentication failed|token.*expired"
                                         r"|authentication required|authentication is invalid|not logged in|log in again")),
        ("model_schema_rejected", r"invalid_json_schema|invalid schema|unsupported.*schema"),
        ("model_unavailable", r"model_not_found|model.*not supported|unsupported model"),
        ("model_service_unavailable", r"\b50[0234]\b|service unavailable|server overloaded"),
    )
    return next((code for code, pattern in patterns if re.search(pattern, text)), default)


def error_code(exc):
    if isinstance(exc, json.JSONDecodeError):
        return "response_json_invalid"
    if isinstance(exc, PermissionError):
        return "filesystem_permission_denied"
    if isinstance(exc, FileNotFoundError):
        return "file_or_cli_missing"
    if isinstance(exc, OSError):
        return "filesystem_or_process_error"
    message = str(exc)
    if message in MODEL_CODES:
        return message
    if message in VALIDATION_CODES:
        return VALIDATION_CODES[message]
    if isinstance(exc, (TypeError, KeyError, ValueError)):
        return "response_validation_failed"
    return "unexpected_error"


def record_errors(data_dir, config, stage, collection_checked_at, errors):
    """Keep a small private error history; a later success must not erase failures."""
    from news_monitor.collect import atomic_json

    if not errors:
        return
    limit = config["diagnostics"]["max_events"]
    if type(limit) is not int or not 1 <= limit <= 1000 or stage not in ("discovery", "briefing"):
        raise ValueError("invalid diagnostics configuration")
    known = MODEL_CODES | set(VALIDATION_CODES.values()) | {
        "response_json_invalid", "filesystem_permission_denied", "file_or_cli_missing",
        "filesystem_or_process_error", "response_validation_failed", "unexpected_error"}
    path = data_dir / "diagnostics.json"
    history = []
    if path.exists():
        with path.open("rb") as stream:
            raw = stream.read(config["collection"]["max_response_bytes"] + 1)
        if len(raw) > config["collection"]["max_response_bytes"]:
            raise ValueError("diagnostics history too large")
        history = json.loads(raw)
        if not isinstance(history, list):
            raise ValueError("invalid diagnostics history")
    for error in errors:
        if error.get("reason") not in known:
            raise ValueError("unsafe diagnostics reason")
        event = {"at": datetime.now(UTC).isoformat(), "stage": stage,
                 "collection_checked_at": collection_checked_at, "reason": error["reason"]}
        # Values originate from fixed phases/counters, never provider/model prose.
        for key in ("phase", "attempt", "group", "elapsed_seconds"):
            if key in error:
                event[key] = error[key]
        history.append(event)
    atomic_json(path, history[-limit:])
