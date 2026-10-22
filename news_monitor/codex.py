"""Read-only Codex adapter with no local commands, apps, plugins or subagents."""

import codecs
import json
import tempfile
from pathlib import Path

from news_monitor.diagnostics import model_failure


def execute(prompt, model, *, search=False, schema=None, timeout=90, max_bytes=65536, max_search_calls=0):
    # Import at call time: analysis owns the bounded CLI transport and uses this adapter.
    from news_monitor.analysis import AnalysisError, capture_model

    search_mode = "live" if search else "disabled"
    command = ["codex", "exec", "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
               "--ephemeral", "--sandbox", "read-only", "--disable", "shell_tool",
               "--disable", "multi_agent", "--disable", "apps", "--disable", "plugins",
               "--disable", "remote_plugin", "--disable", "image_generation",
               "-c", f'web_search="{search_mode}"',
               "-c", "project_doc_max_bytes=0", "-c", 'approval_policy="never"',
               "-c", 'model_reasoning_effort="low"', "--model", model, "--json"]
    if schema is not None:
        command += ["--output-schema", str(schema)]
    command += ["-"]
    sources, search_calls = {}, 0
    pending = ""
    decoder = codecs.getincrementaldecoder("utf-8")()

    def observe(block):
        nonlocal pending, search_calls
        pending += decoder.decode(block)
        lines = pending.split("\n")
        pending = lines.pop()
        for line in lines:
            if not line.strip():
                continue
            event = json.loads(line)
            item = event.get("item", {})
            kind = item.get("type")
            if event.get("type") in ("turn.failed", "error") or kind == "error":
                error = event.get("error") or item.get("error") or (item if kind == "error" else event)
                message = " ".join(str(error.get(key, "")) for key in ("code", "type", "message")) if isinstance(error, dict) else error
                raise AnalysisError(model_failure(message, default="codex_turn_failed"))
            if kind and kind not in ("agent_message", "reasoning", "web_search"):
                raise AnalysisError("codex_unexpected_tool")
            if kind == "web_search":
                if not search:
                    raise AnalysisError("codex_unexpected_search")
                if event["type"] == "item.started":
                    search_calls += 1
                    if search_calls > max_search_calls:
                        raise AnalysisError("codex_search_budget_exhausted")
                for result in item.get("results", []):
                    if isinstance(result, dict) and isinstance(result.get("url"), str):
                        sources[result["url"]] = result.get("title", "")

    with tempfile.TemporaryDirectory(prefix="news-monitor-codex-") as temp:
        raw = capture_model(command, prompt, Path(temp), {"timeout_seconds": timeout,
                                                        "max_output_bytes": max_bytes}, observer=observe)
    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
    messages = [event["item"]["text"] for event in events if event.get("type") == "item.completed"
                and event.get("item", {}).get("type") == "agent_message"]
    if not messages or not any(event.get("type") == "turn.completed" for event in events):
        raise AnalysisError("codex_incomplete_result")
    return messages[-1], {"sources": sources, "web_search_calls": search_calls}
