"""Validate headline-only model decisions and cache only accepted results."""

import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
from pathlib import Path

from news_monitor.collect import ROOT, atomic_json

SENTIMENTS = ("positive", "negative", "neutral", "mixed")
RELEVANCE = ("related", "uncertain", "unrelated")
DECISION_KEYS = {"relevance", "sentiment", "reason", "evidence"}


class AnalysisError(Exception):
    pass


def strict_json(text):
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    return json.loads(text, object_pairs_hook=unique_object)


def validate_options(config):
    opts = config["analysis"]
    if opts["provider"] != "cursor" or not isinstance(opts["model"], str) or not opts["model"].strip():
        raise ValueError("invalid analysis provider or model")
    bounds = {"attempts": (1, 3), "timeout_seconds": (1, 300),
              "max_output_bytes": (1024, 1048576), "max_calls_per_run": (1, 100),
              "max_reason_chars": (10, 1000), "max_evidence_chars": (1, 1000),
              "max_collection_age_hours": (1, 168)}
    for key, (low, high) in bounds.items():
        if type(opts[key]) is not int or not low <= opts[key] <= high:
            raise ValueError(f"invalid analysis {key}")
    prompt_path = (ROOT / opts["prompt_file"]).resolve()
    if not prompt_path.is_relative_to(ROOT / "prompts"):
        raise ValueError("prompt must be in project prompts directory")
    return prompt_path.read_text(encoding="utf-8")


def validate_decision(value, title, opts):
    if not isinstance(value, dict) or set(value) != DECISION_KEYS:
        raise ValueError("invalid model schema")
    if any(not isinstance(v, str) for v in value.values()):
        raise TypeError("model fields must be strings")
    if value["relevance"] not in RELEVANCE or value["sentiment"] not in (*SENTIMENTS, "abstain"):
        raise ValueError("invalid model label")
    if value["relevance"] != "related" and value["sentiment"] != "abstain":
        raise ValueError("non-related decision must abstain")
    reason, evidence = value["reason"], value["evidence"]
    if not reason.strip() or len(reason) > opts["max_reason_chars"] or not re.search("[가-힣]", reason):
        raise ValueError("invalid Korean reason")
    if len(evidence) > opts["max_evidence_chars"] or (evidence and evidence not in title):
        raise ValueError("evidence must occur in headline")
    if value["sentiment"] != "abstain" and not evidence.strip():
        raise ValueError("classified result needs evidence")
    return {key: value[key] for key in sorted(DECISION_KEYS)}


def capture_model(command, prompt, workspace, opts):
    """Bound combined stdout/stderr, wall time and the whole child process group."""
    chunks = []
    size = 0
    deadline = time.monotonic() + opts["timeout_seconds"]
    pending = memoryview(prompt.encode())
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, cwd=workspace, start_new_session=True) as process:
        try:
            with selectors.DefaultSelector() as streams:
                if pending:
                    os.set_blocking(process.stdin.fileno(), False)
                    streams.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    process.stdin.close()
                streams.register(process.stdout, selectors.EVENT_READ, "stdout")
                streams.register(process.stderr, selectors.EVENT_READ, "stderr")
                while streams.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AnalysisError("model_timeout")
                    for key, _ in streams.select(min(remaining, 0.2)):
                        if key.data == "stdin":
                            sent = os.write(key.fileobj.fileno(), pending[:4096])
                            pending = pending[sent:]
                            if not pending:
                                streams.unregister(key.fileobj)
                                key.fileobj.close()
                            continue
                        block = os.read(key.fileobj.fileno(), 8192)
                        if not block:
                            streams.unregister(key.fileobj)
                            continue
                        size += len(block)
                        if size > opts["max_output_bytes"]:
                            raise AnalysisError("model_output_too_large")
                        if key.data == "stdout":
                            chunks.append(block)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AnalysisError("model_timeout")
            try:
                code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise AnalysisError("model_timeout") from None
            if code:
                raise AnalysisError("model_cli_failed")
            return b"".join(chunks).decode("utf-8")
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                if process.poll() is None:
                    process.kill()
            process.wait()


def run_cursor(prompt, opts):
    with tempfile.TemporaryDirectory(prefix="news-monitor-model-") as temp:
        workspace = Path(temp)
        (workspace / ".cursor").mkdir()
        atomic_json(workspace / ".cursor" / "cli.json", {"permissions": {"allow": [], "deny": [
            "Shell(*)", "Read(**)", "Read(/**)", "Write(**)", "Write(/**)",
            "WebFetch(*)", "Mcp(*:*)",
        ]}})
        command = ["agent", "-p", "--mode", "ask", "--trust", "--sandbox", "enabled",
                   "--output-format", "json", "--model", opts["model"]]
        try:
            envelope = strict_json(capture_model(command, prompt, workspace, opts))
        except FileNotFoundError:
            raise AnalysisError("model_cli_unavailable") from None
        if (not isinstance(envelope, dict) or envelope.get("type") != "result"
                or envelope.get("subtype") != "success" or envelope.get("is_error") is not False
                or not isinstance(envelope.get("result"), str)):
            raise AnalysisError("model_invalid_envelope")
        return envelope["result"]


def abstention():
    return {"relevance": "uncertain", "sentiment": "abstain", "evidence": "",
            "reason": "AI 응답 실패 또는 검증 미통과로 판단을 보류했습니다."}


def analyze_articles(articles, config, data_dir, runner=run_cursor):
    template = validate_options(config)
    opts = config["analysis"]
    prompt_hash = hashlib.sha256(template.encode()).hexdigest()
    results = []
    calls = failures = cache_hits = 0
    cache_dir = data_dir / "analysis"
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for article in articles:
        payload = {"target": {"name": config["target"]["name"], "organization": config["target"]["organization"]},
                   "title": article["title"], "analysis_scope": "headline_only"}
        fingerprint = {"input": payload, "provider": opts["provider"], "model": opts["model"],
                       "prompt_sha256": prompt_hash, "schema_version": 1}
        key = hashlib.sha256(json.dumps(fingerprint, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        cache_file = cache_dir / f"{key}.json"
        decision = None
        error = None
        if cache_file.exists():
            try:
                decision = validate_decision(json.loads(cache_file.read_text()), article["title"], opts)
                cache_hits += 1
            except (OSError, ValueError, TypeError):
                pass
        if decision is None:
            prompt = template + "\n\nINPUT_JSON:\n" + json.dumps(payload, ensure_ascii=False)
            for _ in range(opts["attempts"]):
                if calls >= opts["max_calls_per_run"]:
                    error = "model_call_budget_exhausted"
                    break
                calls += 1
                try:
                    raw = runner(prompt, opts)
                    if not isinstance(raw, str) or len(raw.encode()) > opts["max_output_bytes"]:
                        raise ValueError("invalid model output")
                    candidate = validate_decision(strict_json(raw), article["title"], opts)
                except AnalysisError as exc:
                    error = str(exc)
                    continue
                except (OSError, ValueError, TypeError):
                    error = "model_response_invalid"
                    continue
                atomic_json(cache_file, candidate)
                decision = candidate
                error = None
                break
        if decision is None:
            failures += 1
            decision = abstention()
        results.append({"id": article["id"], "decision": decision, "error": error})
    return results, {"provider": opts["provider"], "model": opts["model"], "prompt_sha256": prompt_hash,
                     "model_calls": calls, "cache_hits": cache_hits, "failed_articles": failures}
