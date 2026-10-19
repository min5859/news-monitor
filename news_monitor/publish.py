"""Gate and push four static files. A successful push is not a Pages build result."""

import argparse
import json
import os
import re
import shlex
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from news_monitor.collect import ROOT, atomic_json, collection_lock, load_config
from news_monitor.preflight import check_ready
from news_monitor.site import OUTPUT_FILES, atomic_text


class PublishError(Exception):
    pass


def publish_site(config, data_dir, output_dir, checkout, options):
    with collection_lock(data_dir):
        state_file = data_dir / "publication_status.json"
        state = {"status": "running", "checked_at": datetime.now(UTC).isoformat()}
        atomic_json(state_file, state)
        try:
            if not check_ready(config, data_dir, output_dir)["ready"]:
                raise PublishError("artifact_gate_failed")
            environment = os.environ.copy()
            for key in list(environment):
                if key.startswith("GIT_"):
                    environment.pop(key)
            environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                               GIT_TERMINAL_PROMPT="0")
            if "identity_file" in options:
                environment["GIT_SSH_COMMAND"] = shlex.join([
                    "ssh", "-i", options["identity_file"], "-o", "IdentitiesOnly=yes",
                    "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                    "-o", "UserKnownHostsFile=" + options["known_hosts_file"]])

            def git(*arguments, allowed=(0,)):
                result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(checkout), *arguments],
                                        env=environment, capture_output=True, text=True, timeout=90, check=False)
                if result.returncode not in allowed:
                    raise PublishError("git_" + arguments[0] + "_failed")
                return result

            if checkout.is_symlink():
                raise PublishError("unsafe_checkout")
            checkout.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not (checkout / ".git").exists():
                if any(checkout.iterdir()):
                    raise PublishError("nonempty_checkout")
                git("init", "--quiet", "--initial-branch", options["branch"])
                git("remote", "add", "origin", options["remote"])
            if ((checkout / ".git").is_symlink() or not (checkout / ".git").is_dir()
                    or git("remote", "get-url", "origin").stdout.strip() != options["remote"]
                    or git("branch", "--show-current").stdout.strip() != options["branch"]):
                raise PublishError("unexpected_checkout")
            if git("status", "--porcelain").stdout:
                raise PublishError("dirty_checkout")
            ref = "refs/heads/" + options["branch"]
            remote_head = git("ls-remote", "--heads", "origin", ref).stdout.strip()
            if remote_head:
                git("fetch", "--quiet", "origin", ref)
                head = git("rev-parse", "--verify", "HEAD", allowed=(0, 128))
                if head.returncode == 128:
                    git("checkout", "-B", options["branch"], "FETCH_HEAD")
                else:
                    git("merge", "--ff-only", "FETCH_HEAD")
            # No source tree, workflow, logs or unknown files may reach this branch.
            actual = {path.name for path in checkout.iterdir() if path.name != ".git"}
            if (not actual.issubset(OUTPUT_FILES)
                    or any(path.is_symlink() or not path.is_file() for path in checkout.iterdir() if path.name != ".git")):
                raise PublishError("unexpected_published_files")
            tracked = set(git("ls-files").stdout.splitlines())
            if not tracked.issubset(OUTPUT_FILES):
                raise PublishError("unexpected_tracked_files")
            for name in sorted(OUTPUT_FILES):
                atomic_text(checkout / name, (output_dir / name).read_text())
            git("add", "--", *sorted(OUTPUT_FILES))
            changed = git("diff", "--cached", "--quiet", allowed=(0, 1)).returncode == 1
            if changed:
                git("-c", "user.name=" + options["author_name"], "-c", "user.email=" + options["author_email"],
                    "commit", "--quiet", "-m", "pages: update verified news sample")
            revision = git("rev-parse", "HEAD").stdout.strip()
            git("push", "origin", "HEAD:" + ref)
            state.update(status="pushed", revision=revision, url=options["url"],
                         collection_checked_at=json.loads((output_dir / "report.json").read_text())["collection_checked_at"])
            atomic_json(state_file, state)
            return state
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError, PublishError) as exc:
            state.update(status="failed", error=str(exc) if isinstance(exc, PublishError) else type(exc).__name__)
            atomic_json(state_file, state)
            raise PublishError(state["error"]) from None


def load_publish_options(path):
    options = json.loads(path.read_text())
    if (set(options) != {"remote", "branch", "url", "author_name", "author_email", "identity_file", "known_hosts_file"}
            or any(not isinstance(value, str) or not value or "\n" in value for value in options.values())
            or not re.fullmatch(r"git@github\.com:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git", options["remote"])
            or not re.fullmatch(r"[A-Za-z0-9_-]+", options["branch"])
            or not re.fullmatch(r"https://[A-Za-z0-9-]+\.github\.io/[A-Za-z0-9_-]+/", options["url"])
            or any(not Path(options[key]).is_absolute() or not Path(options[key]).is_file()
                   for key in ("identity_file", "known_hosts_file"))):
        raise ValueError("invalid publishing configuration")
    return options


def verify_pages(options, checkout, revision, *, timeout=180, opener=urlopen, sleeper=time.sleep, clock=time.monotonic):
    """Verify the pushed static bytes, not merely an HTTP 200 from an older site."""
    expected = {name: (checkout / name).read_bytes() for name in OUTPUT_FILES}
    deadline = clock() + timeout
    while clock() < deadline:
        try:
            for name in sorted(OUTPUT_FILES, key=lambda name: name != "report.json"):
                request = Request(options["url"] + name + "?revision=" + revision,
                                  headers={"User-Agent": "news-monitor-publisher", "Cache-Control": "no-cache"})
                with opener(request, timeout=min(10, max(0.1, deadline - clock()))) as response:
                    raw = response.read(len(expected[name]) + 1)
                if raw != expected[name]:
                    raise ValueError("old or changed Pages artifact")
            return {"status": "published", "revision": revision, "url": options["url"],
                    "verified_at": datetime.now(UTC).isoformat()}
        except (OSError, URLError, ValueError):
            remaining = deadline - clock()
            if remaining > 0:
                sleeper(min(10, remaining))
    raise PublishError("pages_verification_timeout")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--publish-config", type=Path, required=True)
    parser.add_argument("--push-only", action="store_true", help="bootstrap before configuring Pages; does not verify deployment")
    args = parser.parse_args()
    try:
        options = load_publish_options(args.publish_config)
        result = publish_site(load_config(args.config), args.data_dir, args.output_dir, args.checkout, options)
        print(json.dumps(result), flush=True)
        if not args.push_only:
            try:
                result.update(verify_pages(options, args.checkout, result["revision"]))
            except PublishError:
                result.update(status="verification_failed", error="pages_verification_timeout")
                atomic_json(args.data_dir / "publication_status.json", result)
                raise
            atomic_json(args.data_dir / "publication_status.json", result)
    except (OSError, ValueError, KeyError, TypeError, PublishError):
        print(json.dumps({"status": "failed", "error": "publication_failed"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
