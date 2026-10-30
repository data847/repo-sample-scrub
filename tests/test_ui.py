import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from sample_scrub import config, ui


class UiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.environ["SAMPLE_SCRUB_OUTPUTS_DIR"] = cls.tmp.name
        for k in ("GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "BITBUCKET_TOKEN"):
            os.environ.pop(k, None)
        cls.mgr = ui.Manager(config.Tokens())
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
        cls.port = cls.srv.server_address[1]
        cls.srv.RequestHandlerClass = ui.make_handler(cls.mgr, cls.port)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        d = Path(cls.tmp.name) / "job-1" / "acme__api"
        d.mkdir(parents=True)
        (d / "acme__api_sanitized.zip").write_bytes(b"PK")
        (d / "secret.txt").write_text("nope")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def req(self, path, body=None, host=None, ctype="application/json"):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                   data=None if body is None else json.dumps(body).encode(),
                                   headers={"Content-Type": ctype})
        if host:
            r.add_header("Host", host)
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_page_and_config(self):
        self.assertEqual(self.req("/")[0], 200)
        code, body = self.req("/api/config")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["tokens"], {"github": False, "gitlab": False, "bitbucket": False})

    def test_rebinding_host_rejected(self):
        self.assertEqual(self.req("/api/config", host="evil.example.com")[0], 403)
        self.assertEqual(self.req("/api/config", host="evil.example.com:8795")[0], 403)
        self.assertEqual(self.req("/api/config", host="127.0.0.1.evil.com")[0], 403)

    def test_loopback_host_on_a_different_port_is_fine(self):
        # docker -p 9000:8795 makes the browser send Host: localhost:9000
        self.assertEqual(self.req("/api/config", host="localhost:9000")[0], 200)

    def test_config_reports_local_roots(self):
        self.assertEqual(json.loads(self.req("/api/config")[1])["local_roots"], [])

    def test_post_needs_json(self):
        self.assertEqual(self.req("/api/jobs", {"repos": ["a/b"]}, ctype="text/plain")[0], 415)

    def test_list_without_token_is_a_clean_error(self):
        code, body = self.req("/api/list", {"platform": "bitbucket", "owner": "ws"})
        self.assertEqual(code, 400)
        self.assertIn("token", json.loads(body)["error"])

    def test_empty_and_bad_pct_jobs_rejected(self):
        self.assertEqual(self.req("/api/jobs", {"repos": []})[0], 400)
        self.assertEqual(self.req("/api/jobs", {"repos": ["a/b"], "pct": 0})[0], 400)

    def test_download_allowlist_and_traversal(self):
        self.assertEqual(self.req("/download/job-1/acme__api/acme__api_sanitized.zip")[0], 200)
        self.assertEqual(self.req("/download/job-1/acme__api/secret.txt")[0], 404)
        self.assertIn(self.req("/download/../acme__api/x_sanitized.zip")[0], (400, 404))
        self.assertIn(self.req("/download/job-1/../job-1/x_sanitized.zip")[0], (400, 404))

    def test_credentials_are_memory_only_and_never_echoed(self):
        secret = "ghp_SUPERSECRETVALUE1234567890"
        try:
            code, body = self.req("/api/credentials", {"github": secret, "bitbucket_email": "a@b.co"})
            self.assertEqual(code, 200)
            self.assertNotIn(secret.encode(), body)
            self.assertEqual(json.loads(body)["tokens"]["github"], True)
            self.assertNotIn(secret.encode(), self.req("/api/config")[1])
            self.assertEqual(self.mgr.tokens.redact(f"x {secret} y"), "x *** y")
            # a blank field must not wipe another platform's credential
            self.req("/api/credentials", {"gitlab": "glpat-abcdefghijk"})
            self.assertTrue(self.mgr.tokens.github and self.mgr.tokens.gitlab)
            # clearing one platform leaves the other alone
            self.req("/api/credentials", {"clear": "github"})
            self.assertFalse(self.mgr.tokens.github)
            self.assertTrue(self.mgr.tokens.gitlab)
        finally:
            self.req("/api/credentials", {"clear": True})
        self.assertEqual(json.loads(self.req("/api/config")[1])["tokens"],
                         {"github": False, "gitlab": False, "bitbucket": False})

    def test_scan_local_path(self):
        import subprocess
        base = Path(self.tmp.name) / "repos"
        for n in ("a", "b"):
            (base / n).mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(base / n)], check=True)
        (base / "notrepo").mkdir()
        code, body = self.req("/api/scan", {"path": str(base)})
        self.assertEqual(code, 200)
        self.assertEqual([r["name"] for r in json.loads(body)], ["a", "b"])
        self.assertTrue(all(r["ref"].startswith("local:/") for r in json.loads(body)))
        one = json.loads(self.req("/api/scan", {"path": str(base / "a")})[1])
        self.assertEqual([r["name"] for r in one], ["a"])
        self.assertEqual(self.req("/api/scan", {"path": "relative/dir"})[0], 400)
        self.assertEqual(self.req("/api/scan", {"path": str(base / "missing")})[0], 400)

    def test_local_repo_validation(self):
        with self.assertRaises(Exception):
            ui.local_repo("relative")
        with self.assertRaises(Exception):
            ui.local_repo(self.tmp.name)  # exists but is not a git repo


if __name__ == "__main__":
    unittest.main()
