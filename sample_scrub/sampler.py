"""Restate a repo to the oldest N% of commits on one branch.

Works on a disposable copy of an existing clone: everything after the cutoff,
every other branch, the reflog and the origin remote are dropped, so the result
is a small standalone repo.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


class SampleError(RuntimeError):
    pass


def git(args, cwd: Path, check: bool = True, timeout: int = 900) -> subprocess.CompletedProcess:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise SampleError(f"git {' '.join(args)} failed ({r.returncode}): {r.stderr.strip()}")
    return r


def is_partial(repo: Path) -> bool:
    cfg = repo / ".git" / "config"
    return cfg.is_file() and "promisor = true" in cfg.read_text(errors="ignore")


def default_branch(repo: Path) -> str:
    for ref in (["symbolic-ref", "--short", "-q", "HEAD"],):
        out = git(ref, repo, check=False).stdout.strip()
        if out:
            return out
    out = git(["symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD"], repo, check=False).stdout.strip()
    if out:
        return out.split("/", 1)[-1]
    raise SampleError("cannot determine a branch to sample (detached HEAD, no origin/HEAD)")


def cutoff(repo: Path, ref: str, pct: float, min_commits: int):
    shas = git(["rev-list", "--reverse", ref], repo).stdout.split()
    total = len(shas)
    if total < min_commits:
        raise SampleError(f"only {total} commits on {ref!r}, below min-commits {min_commits}")
    idx = min(total, max(1, math.ceil(total * pct / 100)))
    return shas[idx - 1], total, idx


def sample(src: Path, out_dir: Path, pct: float = 5.0, branch: Optional[str] = None,
           min_commits: int = 20, log=print) -> dict:
    """Write the sampled repo to out_dir (replaced if present); return its manifest."""
    src, out_dir = Path(src), Path(out_dir)
    if not (src / ".git").exists():
        raise SampleError(f"not a git repository: {src}")
    if is_partial(src):
        raise SampleError(f"{src} is a partial (blobless) clone; re-clone it in full")
    work = out_dir.parent / f".{out_dir.name}.work"
    for p in (work, out_dir):
        if p.exists():
            shutil.rmtree(p)
    work.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copytree(src, work, symlinks=True)
        git(["reset", "--hard", "--quiet"], work)
        git(["clean", "-fdx", "--quiet"], work)
        # a fresh clone has only the default branch checked out; resolve against
        # remote-tracking refs too so --branch can name any remote branch
        branch = branch or default_branch(work)
        ref = branch if git(["rev-parse", "--verify", "-q", branch], work, check=False).returncode == 0 \
            else f"origin/{branch}"
        sha, total, idx = cutoff(work, ref, pct, min_commits)
        log(f"{branch}: {total} commits, first {pct:g}% => #{idx} ({sha[:10]})")

        name = f"sample{pct:g}pct".replace(".", "_")
        git(["checkout", "--quiet", "--force", sha], work)
        git(["checkout", "--quiet", "-b", name], work)
        for b in git(["for-each-ref", "--format=%(refname:short)", "refs/heads"], work).stdout.split():
            if b != name:
                git(["branch", "-D", b], work, check=False)
        git(["remote", "remove", "origin"], work, check=False)
        for r in git(["for-each-ref", "--format=%(refname)", "refs/remotes", "refs/tags"], work,
                     check=False).stdout.split():
            git(["update-ref", "-d", r], work, check=False)
        git(["reflog", "expire", "--expire=now", "--all"], work)
        git(["gc", "--prune=now", "--quiet"], work)

        fsck = git(["fsck", "--full"], work, check=False)
        reachable = int(git(["rev-list", "--count", "HEAD"], work).stdout.strip())
        shutil.move(str(work), str(out_dir))
        manifest = {
            "source": str(src), "branch_sampled": branch, "sample_branch_name": name,
            "requested_pct": pct, "total_commits": total, "cutoff_commit_index": idx,
            "cutoff_commit_sha": sha, "reachable_commits_in_sample": reachable,
            "fsck_clean": fsck.returncode == 0 and not fsck.stdout.strip(),
            "generated_utc": datetime.now(timezone.utc).isoformat(),
        }
        # kept next to the repo, not inside it: the scrubber must not see it
        (out_dir.parent / f"{out_dir.name}.manifest.json").write_text(json.dumps(manifest, indent=2))
        log(f"sample ready: {reachable} commits -> {out_dir}")
        return manifest
    finally:
        shutil.rmtree(work, ignore_errors=True)
