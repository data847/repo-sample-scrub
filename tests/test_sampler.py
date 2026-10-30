import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from sample_scrub import bridge, sampler


def git(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "A", "GIT_AUTHOR_EMAIL": "a@x.io",
           "GIT_COMMITTER_NAME": "A", "GIT_COMMITTER_EMAIL": "a@x.io"}
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout


def make_repo(path: Path, n: int) -> None:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for i in range(n):
        (path / "f.txt").write_text(f"v{i}\n")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", f"c{i}")
    git(path, "branch", "other")
    git(path, "remote", "add", "origin", "https://example.com/x/y.git")


class SamplerTest(unittest.TestCase):
    def test_keeps_oldest_five_percent_and_drops_the_rest(self):
        with tempfile.TemporaryDirectory() as d:
            src, out = Path(d) / "src", Path(d) / "out" / "sample"
            make_repo(src, 40)
            m = sampler.sample(src, out, pct=5, min_commits=20, log=lambda *_: None)
            self.assertEqual(m["reachable_commits_in_sample"], 2)  # ceil(40*5%)
            self.assertEqual(git(out, "log", "--format=%s").split(), ["c1", "c0"])
            self.assertEqual(git(out, "remote").strip(), "")
            self.assertEqual(git(out, "branch", "--format=%(refname:short)").split(), ["sample5pct"])
            self.assertTrue(m["fsck_clean"])
            self.assertEqual((out / "f.txt").read_text(), "v1\n")
            self.assertFalse((out / "SAMPLE_MANIFEST.json").exists())  # manifest stays outside the repo
            self.assertEqual(git(src, "rev-list", "--count", "HEAD").strip(), "40")  # source untouched

    def test_refuses_short_history(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "src"
            make_repo(src, 5)
            with self.assertRaises(sampler.SampleError):
                sampler.sample(src, Path(d) / "o", min_commits=20, log=lambda *_: None)


class ParseRepoTest(unittest.TestCase):
    def test_forms(self):
        cases = {
            "acme/api": ("github", "acme/api"),
            "bitbucket:ws/repo": ("bitbucket", "ws/repo"),
            "https://bitbucket.org/ws/repo.git": ("bitbucket", "ws/repo"),
            "https://github.com/acme/api": ("github", "acme/api"),
            "git@gitlab.com:grp/sub/proj.git": ("gitlab", "grp/sub/proj"),
            "https://gitlab.example.com/g/p": ("gitlab", "g/p"),
        }
        for text, (platform, name) in cases.items():
            r = bridge.parse_repo(text)
            self.assertEqual((r.platform, r.full_name), (platform, name), text)
        self.assertEqual(bridge.parse_repo("https://gitlab.example.com/g/p").host, "gitlab.example.com")

    def test_bad(self):
        with self.assertRaises(bridge.BridgeError):
            bridge.parse_repo("justaword")


if __name__ == "__main__":
    unittest.main()
