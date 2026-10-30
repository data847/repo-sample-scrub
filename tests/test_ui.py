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


if __name__ == "__main__":
    unittest.main()
