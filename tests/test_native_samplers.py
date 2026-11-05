"""Mercurial and Perforce samplers: oldest N% of one branch / path, nothing else."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from sample_scrub import config, hg_sampler, p4_sampler

HAVE_HG = shutil.which("hg") is not None
HAVE_P4 = shutil.which("p4") is not None and shutil.which("p4d") is not None


def hg(repo: Path, *args: str, user: str = "Ana Muller <ana@example.com>") -> str:
    env = {**os.environ, "HGPLAIN": "1", "HGUSER": user, "HGRCPATH": ""}
    return subprocess.run(["hg", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout


@unittest.skipUnless(HAVE_HG, "hg not installed")
class HgSamplerTest(unittest.TestCase):
    def build(self, root: Path, n: int = 38) -> Path:
        repo = root / "src"
        repo.mkdir()
        hg(repo, "init")
        for i in range(n):
            (repo / "f.txt").write_text(f"v{i}\n")
            if i == 0:
                hg(repo, "add")
            hg(repo, "commit", "-m", f"c{i}", "-d", f"2020-01-{i % 28 + 1:02d} 10:00 +0000")
            if i == 9:
                hg(repo, "bookmark", "keepme")
        hg(repo, "update", "-r", "5")
        hg(repo, "branch", "feat")
        (repo / "side.txt").write_text("side\n")
        hg(repo, "add")
        hg(repo, "commit", "-m", "side work")
        hg(repo, "update", "default")
        (repo / "dirty.txt").write_text("uncommitted\n")  # must never reach the sample
        hg(repo, "paths", "--help")  # no-op, keeps repo busy-free
        with (repo / ".hg" / "hgrc").open("w") as fh:
            fh.write("[paths]\ndefault = https://user:secret@example.com/x/y\n")
        return repo

    def test_oldest_five_percent_of_default_only(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = self.build(root)
            out = root / "out" / "sample"
            m = hg_sampler.sample(src, out, pct=5, min_commits=20, log=lambda *_: None)
            self.assertEqual((m["total_commits"], m["cutoff_commit_index"]), (38, 2))  # ceil(38 * 5%)
            self.assertEqual(m["reachable_commits_in_sample"], 2)
            self.assertEqual(hg(out, "log", "-T", "{desc} ").split(), ["c1", "c0"])
            self.assertEqual(m["branches_in_sample"], ["default"])  # the side branch is gone
            self.assertTrue(m["verify_clean"] and m["tip_is_cutoff"])
            self.assertEqual((out / "f.txt").read_text(), "v1\n")
            self.assertFalse((out / "dirty.txt").exists())  # uncommitted work in the source never leaks
            self.assertFalse((out / "side.txt").exists())
            self.assertFalse((out / ".hg" / "hgrc").exists())  # no link (or credentials) back to the source
            self.assertNotIn("secret", "".join(p.read_text(errors="ignore") for p in (out / ".hg").glob("*") if p.is_file()))
            self.assertIn("no bookmarks", hg(out, "bookmarks"))  # 'keepme' was dropped
            self.assertIn("keepme", hg(src, "bookmarks"))  # source untouched
            self.assertEqual(len(hg(src, "log", "-r", "all()", "-T", "x")), 39)  # 38 on default + 1 on the side branch
            self.assertTrue((out.parent / "sample.manifest.json").is_file())  # manifest stays outside the repo

    def test_refuses_short_history_and_unknown_branch(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            src = self.build(Path(d), n=10)
            with self.assertRaises(hg_sampler.SampleError):
                hg_sampler.sample(src, Path(d) / "o", log=lambda *_: None)
            with self.assertRaises(hg_sampler.SampleError):
                hg_sampler.sample(src, Path(d) / "o", branch="nope", min_commits=1, log=lambda *_: None)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def p4env(port: str, user: str, client: str, root: Path) -> dict:
    return {**os.environ, "P4PORT": port, "P4USER": user, "P4CLIENT": client, "P4CONFIG": "",
            "P4ENVIRO": os.devnull, "P4TICKETS": str(root / "tickets")}


class P4Fixture:
    """A throw-away source Perforce server with 25 changelists on //depot/proj (shared with the UI tests)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls._tmp.name)
        (cls.root / "srv").mkdir()
        cls.p4port = f"127.0.0.1:{_free_port()}"
        subprocess.run(["p4d", "-r", str(cls.root / "srv"), "-p", cls.p4port, "-d", "-L", str(cls.root / "log")],
                       check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                with socket.create_connection(cls.p4port.split(":"), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        cls.ws = {}
        for u in ("alice", "bob"):
            ws = cls.root / f"ws_{u}"
            ws.mkdir()
            cls.ws[u] = ws
            spec = cls.p4(u, "client", "-o")
            spec = "\n".join(f"Root:\t{ws}" if ln.startswith("Root:") else
                             "Options:\tallwrite clobber compress unlocked nomodtime normdir" if ln.startswith("Options:")
                             else ln for ln in spec.splitlines()) + "\n"
            cls.p4(u, "client", "-i", stdin=spec)
        cls._history()

    @classmethod
    def p4(cls, user, *args, stdin=None, cwd=None, check=True):
        env = p4env(cls.p4port, user, f"ws_{user}", cls.root)
        if cwd:
            env["PWD"] = str(cwd)
        r = subprocess.run(["p4", *args], input=stdin, capture_output=True, text=True, cwd=cwd, env=env)
        if check and r.returncode:
            raise RuntimeError(f"p4 {args}: {r.stdout}{r.stderr}")
        return r.stdout

    @classmethod
    def _history(cls) -> None:
        for i in range(25):
            user = "alice" if i % 2 == 0 else "bob"
            ws = cls.ws[user]
            cls.p4(user, "sync", "-q", "//depot/...", cwd=ws)
            (ws / "proj").mkdir(exist_ok=True)
            (ws / "other").mkdir(exist_ok=True)
            f = ws / "proj" / "main.txt"
            existed = f.exists()
            f.write_text("".join(f"line {j}\n" for j in range(i + 1)))
            cls.p4(user, "edit" if existed else "add", "proj/main.txt", cwd=ws)
            if i == 6:
                (ws / "proj" / "extra.txt").write_text("extra\n")
                cls.p4(user, "add", "proj/extra.txt", cwd=ws)
            if i == 9:
                cls.p4(user, "delete", "proj/extra.txt", cwd=ws)
            if i == 0 or i == 4:  # a change that also touches another path: only the proj part belongs to the sample
                x = ws / "other" / "x.txt"
                existed_x = x.exists()
                x.write_text(f"outside {i}\n")
                cls.p4(user, "edit" if existed_x else "add", "other/x.txt", cwd=ws)
            cls.p4(user, "submit", "-d", f"change number {i}", cwd=ws)

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(["pkill", "-f", f"p4d -r {cls.root}"], capture_output=True)
        time.sleep(0.5)
        shutil.rmtree(cls.root, ignore_errors=True)
        cls._tmp.cleanup()

@unittest.skipUnless(HAVE_P4, "p4 / p4d not installed")
class P4SamplerTest(P4Fixture, unittest.TestCase):
    def conn(self):
        p4 = p4_sampler._p4m()
        return p4.P4Conn(self.p4port, "alice")

    def test_oldest_twenty_percent_of_a_depot_path(self) -> None:
        p4 = p4_sampler._p4m()
        out = self.root / "out" / "sample"
        m = p4_sampler.sample(self.conn(), "//depot/proj", out, pct=20, min_commits=10, log=lambda *_: None)
        self.assertEqual((m["skipped_empty_changelists"], m["attribution_not_restored"]), ([], []))
        self.assertEqual((m["total_commits"], m["cutoff_commit_index"]), (25, 5))  # ceil(25 * 20%)
        self.assertEqual(m["reachable_commits_in_sample"], 5)
        self.assertTrue(m["parity"]["ok"], m["parity"])
        self.assertEqual(m["parity"]["files_at_cutoff"], m["parity"]["files_in_sample"])
        # serve the sample and read it back through the same p4 helpers the profiler uses
        port = f"127.0.0.1:{_free_port()}"
        subprocess.run(["p4d", "-r", str(out / "p4root"), "-p", port, "-d", "-L", str(self.root / "s.log")], check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        try:
            c = p4.P4Conn(port, "sampler")
            p4.connect(c)
            got = sorted(p4.iter_changes(c, "//depot/proj"), key=lambda x: x["change"])
            self.assertEqual([x["desc"] for x in got], [f"change number {i}" for i in range(5)])
            self.assertEqual([x["user"] for x in got], ["alice", "bob", "alice", "bob", "alice"])  # authors preserved
            src = sorted(p4.iter_changes(self.conn(), "//depot/proj"), key=lambda x: x["change"])[:5]
            self.assertEqual([x["time"] for x in got], [x["time"] for x in src])  # dates preserved
            self.assertEqual(len(list(p4.iter_changes(c, "//depot/other"))), 0)  # nothing outside the path
        finally:
            p4.run_p4_text(p4.P4Conn(port, "sampler"), "admin", "stop", check=False)
        # source untouched, later history absent from the sample
        self.assertEqual(len(list(p4.iter_changes(self.conn(), "//depot/proj"))), 25)
        self.assertTrue((out.parent / "sample.manifest.json").is_file())
        self.assertEqual(sorted(p.name for p in out.iterdir()).count("RESTORE.txt"), 1)

    def test_refuses_a_short_history(self) -> None:
        with self.assertRaises(p4_sampler.SampleError):
            p4_sampler.sample(self.conn(), "//depot/proj", self.root / "o2", pct=5, min_commits=100, log=lambda *_: None)


if __name__ == "__main__":
    unittest.main()
