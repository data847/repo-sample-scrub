import importlib.util
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("make_scrub_src", Path(__file__).parents[1] / "tools" / "make_scrub_src.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def fake_skill(root: Path) -> Path:
    s = root / "skill"
    (s / "scripts").mkdir(parents=True)
    (s / "rules" / "regex").mkdir(parents=True)
    (s / ".venv" / "bin").mkdir(parents=True)
    (s / ".tools").mkdir()
    for n in list(mod.SCRIPTS) + ["acme_customer_batch.py", "orchestrator.py.pre-normalize.bak", "clone_misc.sh"]:
        (s / "scripts" / n).write_text("x = 1\n")
    (s / "scripts" / "workspace_paths.py").write_text("from datalabs_paths import out  # workspace-only\n")
    (s / "scripts" / "__pycache__").mkdir()
    (s / "scripts" / "__pycache__" / "a.pyc").write_text("x")
    (s / "rules" / "regex" / "p.yaml").write_text("a: 1\n")
    (s / "requirements.txt").write_text("repo-sanitizer>=0.1.0\ngit-filter-repo>=2.45.0\n")
    (s / ".salt").write_text("SUPERSECRETSALT")
    (s / ".venv" / "bin" / "python").write_text("x")
    return s


class MakeScrubSrcTest(unittest.TestCase):
    def build(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        skill = fake_skill(Path(d.name))
        out = Path(d.name) / "out"
        self.assertEqual(mod.main([str(skill), "--out", str(out)]), 0)
        return out

    def test_allow_list_only(self):
        out = self.build()
        shipped = sorted(p.name for p in (out / "scripts").iterdir())
        self.assertEqual(shipped, sorted(set(mod.SCRIPTS) | {"workspace_paths.py"}))
        self.assertTrue((out / "rules" / "regex" / "p.yaml").is_file())

    def test_no_secrets_customer_scripts_venv_or_backups(self):
        out = self.build()
        names = {p.name for p in out.rglob("*")}
        for bad in (".salt", ".venv", ".tools", "acme_customer_batch.py", "clone_misc.sh", "a.pyc", "__pycache__"):
            self.assertNotIn(bad, names)
        self.assertFalse(any(".bak" in n or ".pre-" in n for n in names))
        self.assertNotIn("SUPERSECRETSALT", "".join(p.read_text() for p in out.rglob("*") if p.is_file()))

    def test_shim_is_layout_independent(self):
        out = self.build()
        shim = (out / "scripts" / "workspace_paths.py").read_text()
        self.assertNotIn("datalabs_paths", shim)
        ns = {"__file__": str(out / "scripts" / "workspace_paths.py")}
        exec(compile(shim, "shim", "exec"), ns)
        self.assertEqual(ns["out"]("run1").resolve(), (out / "outputs" / "run1").resolve())

    def test_repo_sanitizer_line_dropped_other_requirements_kept(self):
        out = self.build()
        req = (out / "requirements.txt").read_text()
        self.assertNotIn("repo-sanitizer>=", req)
        self.assertIn("git-filter-repo>=2.45.0", req)

    def test_rejects_a_non_checkout(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(mod.main([d, "--out", str(Path(d) / "o")]), 1)


if __name__ == "__main__":
    unittest.main()
