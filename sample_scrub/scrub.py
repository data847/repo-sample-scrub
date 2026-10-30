"""Drive the repo-pii-scrub gates (init -> clone -> sanitize -> brand-map -> package)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable, List, Optional

from . import config


class ScrubError(RuntimeError):
    pass


def _env(skill: Path) -> dict:
    env = dict(os.environ)
    extra = [str(skill / ".tools" / "bin"), str(skill / ".venv" / "bin")]
    env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    return env


def _run(label: str, cmd: List[str], env: dict, log: Callable[[str], None], redact) -> str:
    log(f"== {label}")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    lines = []
    for line in p.stdout:
        lines.append(line)
        log(redact(line.rstrip()))
    if p.wait() != 0:
        raise ScrubError(f"{label} failed (exit {p.returncode})")
    return "".join(lines)


def scrub(sample_dir: Path, slug: str, run_id: str, terms: List[str], fast: bool,
          log: Callable[[str], None] = print, redact=lambda s: s) -> dict:
    """Scrub sample_dir. Returns {run_dir, zip, manifest, verdict} paths (verdict may be None)."""
    skill = config.pii_scrub_dir()
    if skill is None:
        raise ScrubError("repo-pii-scrub not found. Set REPO_PII_SCRUB_DIR or add vendor/repo-pii-scrub.")
    venv_py = skill / ".venv" / "bin" / "python"
    py = str(venv_py if venv_py.exists() else sys.executable)
    orch = str(skill / "scripts" / "orchestrator.py")
    env = _env(skill)
    if not fast and not env.get("REPO_SANITIZER_SALT"):
        raise ScrubError("REPO_SANITIZER_SALT is not set (needed for deterministic pseudonyms)")

    init = [py, orch, "init", "--run-id", run_id, "--repo-slug", slug, "--source", str(sample_dir)]
    if fast:
        init.append("--fast")
    out = _run("init", init, env, log, redact)
    run_dir = json.loads(out[out.index("{"):out.rindex("}") + 1])["run_dir"]

    _run("clone", [py, orch, "clone", "--run-dir", run_dir], env, log, redact)
    _run("sanitize", [py, orch, "sanitize", "--run-dir", run_dir, "--pre-scan"], env, log, redact)
    if not fast:
        bm = [py, orch, "brand-map", "--run-dir", run_dir]
        for t in terms:
            bm += ["--term", t]
        _run("brand-map", bm, env, log, redact)
    _run("package", [py, orch, "package", "--run-dir", run_dir], env, log, redact)

    verdict = None
    verify = skill / "scripts" / "verify_deliverable.py"
    if verify.exists():
        _run("verify", [py, str(verify), "--run-dir", run_dir], env, log, redact)
        verdict = Path(run_dir) / "VERDICT.json"
    else:
        log("verify_deliverable.py not present in this repo-pii-scrub copy; skipping acceptance gate")
    zips = sorted(Path(run_dir).glob("*_sanitized.zip"))
    if not zips:
        raise ScrubError("package produced no *_sanitized.zip")
    return {"run_dir": Path(run_dir), "zip": zips[0], "manifest": Path(run_dir) / "MANIFEST.json",
            "verdict": verdict}
