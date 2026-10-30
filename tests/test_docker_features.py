import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sample_scrub import bridge, cli, config, scrub, ui


def init_repo(p: Path) -> None:
    p.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(p)], check=True)


class HostAllowedTest(unittest.TestCase):
    def test_loopback_any_port(self):
        for h in ("127.0.0.1:8795", "localhost:9000", "localhost", "[::1]:8795", "LOCALHOST:1"):
            self.assertTrue(ui.host_allowed(h), h)

    def test_other_hosts_refused(self):
        for h in ("evil.example.com", "evil.example.com:8795", "127.0.0.1.evil.com:8795", "", "[bad"):
            self.assertFalse(ui.host_allowed(h), h)

    def test_extra_hosts_from_env(self):
        with mock.patch.dict(os.environ, {"SAMPLE_SCRUB_ALLOWED_HOSTS": "scrub.internal, Other.Host"}):
            self.assertTrue(ui.host_allowed("scrub.internal:8795"))
            self.assertTrue(ui.host_allowed("other.host"))
            self.assertFalse(ui.host_allowed("evil.example.com"))


class LocalRootsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "mounted"
        self.outside = self.base / "outside"
        init_repo(self.root / "a")
        init_repo(self.root / "b")
        init_repo(self.outside / "secret")
        self.env = mock.patch.dict(os.environ, {"SAMPLE_SCRUB_LOCAL_ROOTS": str(self.root)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_roots_parsed(self):
        self.assertEqual(config.local_roots(), [self.root])
        with mock.patch.dict(os.environ, {"SAMPLE_SCRUB_LOCAL_ROOTS": f"{self.root}:{self.outside}"}):
            self.assertEqual(config.local_roots(), [self.root, self.outside])

    def test_unrestricted_when_unset(self):
        with mock.patch.dict(os.environ, {"SAMPLE_SCRUB_LOCAL_ROOTS": ""}):
            self.assertEqual(config.local_roots(), [])
            self.assertEqual(len(ui.scan_local(str(self.outside))), 1)

    def test_scan_inside_root(self):
        self.assertEqual([r["name"] for r in ui.scan_local(str(self.root))], ["a", "b"])
        self.assertEqual([r["name"] for r in ui.scan_local(str(self.root / "a"))], ["a"])

    def test_scan_outside_root_refused_with_helpful_message(self):
        with self.assertRaises(bridge.BridgeError) as cm:
            ui.scan_local(str(self.outside))
        self.assertIn("outside the allowed", str(cm.exception))
        self.assertIn(str(self.root), str(cm.exception))
        self.assertIn("Docker", str(cm.exception))

    def test_dotdot_traversal_refused(self):
        with self.assertRaises(bridge.BridgeError):
            ui.scan_local(str(self.root / ".." / "outside"))
        with self.assertRaises(bridge.BridgeError):
            ui.local_repo(str(self.root / ".." / "outside" / "secret"))

    def test_symlink_escape_refused(self):
        (self.root / "link").symlink_to(self.outside / "secret")
        # listing the root must not surface the symlinked repo ...
        self.assertEqual([r["name"] for r in ui.scan_local(str(self.root))], ["a", "b"])
        # ... and addressing it directly resolves outside the root and is refused
        with self.assertRaises(bridge.BridgeError):
            ui.local_repo(str(self.root / "link"))

    def test_sibling_prefix_is_not_inside(self):
        init_repo(self.base / "mounted-evil" / "x")  # shares a string prefix with the root
        with self.assertRaises(bridge.BridgeError):
            ui.local_repo(str(self.base / "mounted-evil" / "x"))

    def test_local_repo_inside_root_ok(self):
        self.assertEqual(ui.local_repo(str(self.root / "a")), self.root / "a")


class SubmitValidationTest(unittest.TestCase):
    def test_bad_local_path_is_rejected_at_submit(self):
        import json, threading, urllib.request, urllib.error
        from http.server import ThreadingHTTPServer
        with tempfile.TemporaryDirectory() as d:
            root = Path(d).resolve() / "m"
            init_repo(root / "ok")
            with mock.patch.dict(os.environ, {"SAMPLE_SCRUB_LOCAL_ROOTS": str(root)}):
                mgr = ui.Manager(config.Tokens())
                srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
                srv.RequestHandlerClass = ui.make_handler(mgr, srv.server_address[1])
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                try:
                    def post(repos):
                        r = urllib.request.Request(
                            f"http://127.0.0.1:{srv.server_address[1]}/api/jobs",
                            data=json.dumps({"repos": repos, "fast": True, "pct": 5}).encode(),
                            headers={"Content-Type": "application/json"})
                        try:
                            return urllib.request.urlopen(r).status
                        except urllib.error.HTTPError as e:
                            return e.code
                    self.assertEqual(post(["local:/etc"]), 400)
                    self.assertEqual(post([f"local:{root}/missing"]), 400)
                    self.assertEqual(len(mgr.jobs), 0)           # nothing was queued
                finally:
                    srv.shutdown()


class ScrubCleanupTest(unittest.TestCase):
    def test_drops_source_dir_and_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as d:
            run = Path(d) / "run"
            (run / "source").mkdir(parents=True)
            (run / "source" / "f").write_text("x")
            (run / "clean").mkdir()
            scrub.drop_unscrubbed_source(run)
            self.assertFalse((run / "source").exists())
            self.assertTrue((run / "clean").exists())

            target = Path(d) / "sample"
            target.mkdir()
            (target / "keep").write_text("x")
            (run / "source").symlink_to(target)  # --fast mode symlinks the input
            scrub.drop_unscrubbed_source(run)
            self.assertFalse((run / "source").exists())
            self.assertTrue((target / "keep").exists())


class FailedRunCleanupTest(unittest.TestCase):
    def test_failed_run_leaves_no_sample_or_manifest(self):
        from sample_scrub import pipeline, sampler
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.dict(os.environ, {"SAMPLE_SCRUB_OUTPUTS_DIR": d}):
            src = Path(d) / "src"
            init_repo(src)

            def fake_sample(src_, out_dir, *a, **k):
                out_dir.mkdir(parents=True)
                (out_dir / "unscrubbed.txt").write_text("PII")
                (out_dir.parent / f"{out_dir.name}.manifest.json").write_text("{}")
                return {"reachable_commits_in_sample": 1, "total_commits": 2}

            with mock.patch.object(sampler, "sample", fake_sample), \
                 mock.patch.object(scrub, "scrub", side_effect=scrub.ScrubError("boom")):
                with self.assertRaises(scrub.ScrubError):
                    pipeline.run_repo(None, src, "jobx", pipeline.Options(), config.Tokens(), lambda *_: None)
            self.assertEqual(list(Path(d, "jobx").rglob("*")), [])  # nothing left, not even the dir shell


class SaltTest(unittest.TestCase):
    def test_salt_required_even_in_fast_mode(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(config, "pii_scrub_dir", return_value=Path(d)), \
                 mock.patch.dict(os.environ, {"REPO_SANITIZER_SALT": ""}):
                for fast in (True, False):
                    with self.assertRaises(scrub.ScrubError) as cm:
                        scrub.scrub(Path(d), "s", "r", [], fast, log=lambda *_: None)
                    self.assertIn("REPO_SANITIZER_SALT", str(cm.exception))


class CliTest(unittest.TestCase):
    def test_bind_flag(self):
        a = cli.build_parser().parse_args(["ui", "--bind", "0.0.0.0", "--port", "9"])
        self.assertEqual((a.bind, a.port), ("0.0.0.0", 9))
        self.assertEqual(cli.build_parser().parse_args(["ui"]).bind, "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
