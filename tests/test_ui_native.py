"""UI: Mercurial and Perforce tabs, end to end through the HTTP API (real hg / p4d)."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path

from sample_scrub import config, ui
from tests.test_native_samplers import HAVE_HG, HAVE_P4, HgSamplerTest, P4Fixture


class UiNativeBase(unittest.TestCase):
    @classmethod
    def start_ui(cls) -> None:
        cls.outputs = tempfile.TemporaryDirectory()
        os.environ["SAMPLE_SCRUB_OUTPUTS_DIR"] = cls.outputs.name
        for k in ("GITHUB_TOKEN", "GH_TOKEN", "GITLAB_TOKEN", "BITBUCKET_TOKEN", "P4PORT", "P4USER", "P4_PASSWORD",
                  "P4_TICKET", "SAMPLE_SCRUB_LOCAL_ROOTS"):
            os.environ.pop(k, None)
        cls.mgr = ui.Manager(config.Tokens())
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), None)
        cls.port = cls.srv.server_address[1]
        cls.srv.RequestHandlerClass = ui.make_handler(cls.mgr, cls.port)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def stop_ui(cls) -> None:
        cls.srv.shutdown()
        cls.outputs.cleanup()

    def req(self, path, body=None):
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                   data=None if body is None else json.dumps(body).encode(),
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def jreq(self, path, body=None):
        code, data = self.req(path, body)
        return code, json.loads(data)

    def wait(self, job_id: str, timeout: int = 240) -> dict:
        end = time.time() + timeout
        while time.time() < end:
            _, j = self.jreq(f"/api/jobs/{job_id}")
            if j["status"] in ("done", "failed", "partial"):
                return j
            time.sleep(0.5)
        self.fail("job did not finish")


class PageTest(UiNativeBase):
    @classmethod
    def setUpClass(cls):
        cls.start_ui()

    @classmethod
    def tearDownClass(cls):
        cls.stop_ui()

    def test_page_has_both_tabs_and_says_they_are_not_scrubbed(self):
        code, body = self.req("/")
        html = body.decode()
        self.assertEqual(code, 200)
        for needle in ("['hg','Mercurial']", "['p4','Perforce']", 'data-p="hg"', 'data-p="p4"', "not</b> redacted"):
            self.assertIn(needle, html)

    def test_config_reports_tools_and_never_the_secret(self):
        self.jreq("/api/credentials", {"p4_port": "ssl:p4.example.com:1666", "p4_user": "alice",
                                       "p4_secret": "hunter2-hunter2", "p4_fingerprint": "AA:BB"})
        code, raw = self.req("/api/config")
        self.assertNotIn(b"hunter2", raw)
        self.assertNotIn(b"AA:BB", raw)
        p4 = json.loads(raw)["p4"]
        self.assertEqual((p4["port"], p4["user"], p4["secret"], p4["ready"]), ("ssl:p4.example.com:1666", "alice", True, True))
        self.assertEqual(self.mgr.tokens.redact("x hunter2-hunter2 y"), "x *** y")
        _, state = self.jreq("/api/credentials", {"clear": "p4"})
        self.assertEqual((state["p4"]["ready"], state["p4"]["secret"]), (False, False))

    def test_jobs_are_validated_before_queueing(self):
        self.jreq("/api/credentials", {"clear": "p4"})
        code, err = self.jreq("/api/jobs", {"repos": ["hg:/no/such/repo"]})
        self.assertEqual(code, 400)
        code, err = self.jreq("/api/jobs", {"repos": ["p4:depot/not-a-depot-path"]})
        self.assertEqual(code, 400)
        self.assertIn("//", err["error"])
        if shutil.which("p4") and shutil.which("p4d"):
            code, err = self.jreq("/api/jobs", {"repos": ["p4://depot/x"]})
            self.assertEqual(code, 400)
            self.assertIn("Perforce server and user", err["error"])
        code, err = self.jreq("/api/scan", {"path": "/tmp", "vcs": "svn"})
        self.assertEqual(code, 400)

    def test_listing_p4_without_credentials_is_a_clear_error(self):
        self.jreq("/api/credentials", {"clear": "p4"})
        code, err = self.jreq("/api/list", {"platform": "p4"})
        self.assertEqual(code, 400)
        self.assertIn("not set", err["error"])


@unittest.skipUnless(HAVE_HG, "hg not installed")
class HgUiTest(UiNativeBase):
    @classmethod
    def setUpClass(cls):
        cls.start_ui()
        cls.work = tempfile.TemporaryDirectory()
        cls.repo = HgSamplerTest().build(Path(cls.work.name))

    @classmethod
    def tearDownClass(cls):
        cls.stop_ui()
        cls.work.cleanup()

    def test_scan_lists_only_mercurial_repos_and_a_job_produces_a_zip(self):
        # a git-looking sibling must not be listed by the Mercurial scan
        (Path(self.work.name) / "gitish" / ".git").mkdir(parents=True)
        code, items = self.jreq("/api/scan", {"path": self.work.name, "vcs": "hg"})
        self.assertEqual([i["ref"] for i in items], [f"hg:{self.repo.resolve()}"])
        code, job = self.jreq("/api/jobs", {"repos": [items[0]["ref"]], "pct": 5, "min_commits": 20})
        self.assertEqual(code, 200)
        self.assertEqual(job["native"], 1)
        done = self.wait(job["id"])
        res = done["results"][items[0]["ref"]]
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["native"], res["commits_sampled"], res["commits_total"], res["verdict"]),
                         ("hg", 2, 38, "verify clean"))
        base = f"/download/{job['id']}/{res['slug']}/"
        code, z = self.req(base + res["zip"].split("/")[-1])
        self.assertEqual(code, 200)
        names = zipfile.ZipFile(io.BytesIO(z)).namelist()
        self.assertIn("f.txt", names)
        self.assertNotIn("dirty.txt", names)  # uncommitted work never reaches the zip
        self.assertFalse(any(n.endswith("hgrc") for n in names))  # no link back to the source
        code, m = self.jreq(base + "SAMPLE_MANIFEST.json")
        self.assertEqual((m["vcs"], m["reachable_commits_in_sample"]), ("mercurial", 2))
        out = Path(self.outputs.name) / job["id"] / res["slug"]
        self.assertFalse((out / "sample").exists())  # the unzipped sample is not left behind

    def test_pasted_path_and_a_bad_branch_fail_cleanly(self):
        code, job = self.jreq("/api/jobs", {"repos": [f"hg:{self.repo.resolve()}"], "branch": "nope", "min_commits": 1})
        done = self.wait(job["id"])
        self.assertEqual(done["status"], "failed")
        self.assertIn("nope", done["results"][f"hg:{self.repo.resolve()}"]["error"])


@unittest.skipUnless(HAVE_P4, "p4 / p4d not installed")
class P4UiTest(P4Fixture, UiNativeBase):
    @classmethod
    def setUpClass(cls):
        cls.start_ui()
        P4Fixture.setUpClass.__func__(cls)

    def test_list_depots_and_run_a_job(self):
        code, st = self.jreq("/api/credentials", {"p4_port": self.p4port, "p4_user": "alice"})
        self.assertTrue(st["p4"]["ready"])
        code, items = self.jreq("/api/list", {"platform": "p4"})
        self.assertEqual(code, 200, items)
        refs = [i["ref"] for i in items]
        self.assertIn("p4://depot/proj", refs)
        self.assertIn("p4://depot/other", refs)
        code, job = self.jreq("/api/jobs", {"repos": ["p4://depot/proj"], "pct": 20, "min_commits": 10})
        self.assertEqual(code, 200, job)
        done = self.wait(job["id"])
        res = done["results"]["p4://depot/proj"]
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["native"], res["commits_sampled"], res["commits_total"], res["verdict"]),
                         ("p4", 5, 25, "parity ok"))
        base = f"/download/{job['id']}/{res['slug']}/"
        code, z = self.req(base + res["zip"].split("/")[-1])
        self.assertEqual(code, 200)
        names = zipfile.ZipFile(io.BytesIO(z)).namelist()
        self.assertTrue(any(n.startswith("p4root/") for n in names))
        self.assertIn("RESTORE.txt", names)
        code, m = self.jreq(base + "SAMPLE_MANIFEST.json")
        self.assertEqual((m["vcs"], m["parity"]["ok"], m["reachable_commits_in_sample"]), ("perforce", True, 5))
        self.assertEqual(m["attribution_not_restored"], [])


if __name__ == "__main__":
    unittest.main()
