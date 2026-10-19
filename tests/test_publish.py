"""Publish only synthetic validated artifacts to a temporary local Git remote."""

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_pipeline import CONFIG, collection_fixture, decision

from news_monitor import publish, run
from news_monitor.collect import write_result


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.output = self.root / "dist"
        self.checkout = self.root / "publish"
        self.remote = self.root / "remote.git"
        self.data.mkdir()
        subprocess.run(["git", "init", "--bare", "--quiet", str(self.remote)], check=True)
        write_result(collection_fixture(), self.data)
        run.run_pipeline(CONFIG, self.data, self.output, reuse=True, runner=lambda *args: json.dumps(decision()))
        self.options = {"remote": str(self.remote), "branch": "pages-data",
                        "url": "https://example.com/", "author_name": "Synthetic Test", "author_email": "test@example.com"}

    def push(self):
        return publish.publish_site(CONFIG, self.data, self.output, self.checkout, self.options)

    def test_publish_has_only_four_files_and_noop_is_safe(self):
        first = self.push()
        second = self.push()
        self.assertEqual(first["status"], "pushed")
        self.assertEqual(first["revision"], second["revision"])
        names = subprocess.check_output(["git", "--git-dir", str(self.remote), "ls-tree", "--name-only", "pages-data"], text=True).splitlines()
        self.assertEqual(set(names), {"index.html", "report.json", "styles.css", "app.js"})

    def test_failed_gate_never_creates_a_publishing_checkout(self):
        value = json.loads((self.output / "report.json").read_text())
        value["status"] = "failed"
        (self.output / "report.json").write_text(json.dumps(value))
        with self.assertRaises(publish.PublishError):
            self.push()
        self.assertFalse(self.checkout.exists())

    def test_untracked_files_and_symlinks_are_not_overwritten(self):
        self.push()
        extra = self.checkout / "manual.txt"
        extra.write_text("user-owned")
        with self.assertRaises(publish.PublishError):
            self.push()
        self.assertEqual(extra.read_text(), "user-owned")

    def test_retained_only_result_does_not_publish(self):
        value = json.loads((self.output / "report.json").read_text())
        value["warnings"].append("retained_articles_only")
        (self.output / "report.json").write_text(json.dumps(value))
        with self.assertRaises(publish.PublishError):
            self.push()
        self.assertFalse(self.checkout.exists())

    def test_dirty_publishing_checkout_is_preserved(self):
        self.push()
        target = self.checkout / "index.html"
        target.write_text("user edit")
        with self.assertRaises(publish.PublishError):
            self.push()
        self.assertEqual(target.read_text(), "user edit")

    def test_public_verification_checks_every_file_against_pushed_bytes(self):
        result = self.push()
        requested = []

        def opener(request, **kwargs):
            name = request.full_url.split("/")[-1].split("?")[0]
            requested.append(name)
            return io.BytesIO((self.checkout / name).read_bytes())

        verified = publish.verify_pages(self.options, self.checkout, result["revision"], opener=opener)
        self.assertEqual(verified["status"], "published")
        self.assertEqual(set(requested), {"index.html", "styles.css", "app.js", "report.json"})

    def test_http_200_with_old_json_is_not_a_successful_deployment(self):
        result = self.push()
        with self.assertRaisesRegex(publish.PublishError, "timeout"):
            publish.verify_pages(self.options, self.checkout, result["revision"],
                                 opener=lambda *args, **kwargs: io.BytesIO(b"{}"), sleeper=lambda _: None,
                                 clock=iter([0, 0, 0, 11, 11]).__next__, timeout=10)


if __name__ == "__main__":
    unittest.main()
