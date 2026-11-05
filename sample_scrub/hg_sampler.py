"""Restate a Mercurial repo to the oldest N% of changesets on one branch.

The same contract as ``sampler.py`` (git): the result is a standalone repository whose whole
history is the first N% of one branch, with every later changeset, every other branch's own work,
bookmarks and the link back to the source gone. Mercurial makes this cleaner than Git, because
``hg clone -r <cutoff>`` copies *only* that changeset and its ancestors: nothing has to be deleted
afterwards, and uncommitted work in the source cannot leak because the clone reads history, not the
working copy.

Native on purpose: the sample stays a Mercurial repo (profile it with ``codebase-profiler --hg``).
repo-pii-scrub is git-only, so this path does not scrub.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

_ENV = {"HGPLAIN": "1", "HGENCODING": "utf-8", "HGRCPATH": ""}
# a repo owned by another UID would otherwise have its own hgrc ignored with a "not trusting" warning
_TRUST = ["--config", "trusted.users=*", "--config", "trusted.groups=*"]


class SampleError(RuntimeError):
    pass


def hg(args, repo: Optional[Path] = None, check: bool = True, timeout: int = 1800) -> subprocess.CompletedProcess:
    cmd = ["hg", *_TRUST, *(["-R", str(repo)] if repo else []), *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout,
                           env={**os.environ, **_ENV})
    except FileNotFoundError as exc:
        raise SampleError("`hg` is not installed") from exc
    if check and r.returncode != 0:
        raise SampleError(f"hg {' '.join(args)} failed ({r.returncode}): {(r.stderr or r.stdout).strip()[:300]}")
    return r


def pick_branch(repo: Path, branch: Optional[str]) -> str:
    """The branch to sample: the one asked for, else ``default``, else the checked-out one."""
    names = [ln.split()[0] for ln in hg(["branches", "-T", "{branch}\n"], repo).stdout.splitlines() if ln.strip()]
    if branch:
        if branch not in names:
            raise SampleError(f"branch {branch!r} not found (have: {', '.join(names[:10])})")
        return branch
    if "default" in names:
        return "default"
    current = hg(["branch"], repo).stdout.strip()
    if current:
        return current
    raise SampleError("cannot determine a branch to sample")


def branch_head(repo: Path, branch: str) -> str:
    """Newest open head of the branch (a branch can have several heads)."""
    out = hg(["log", "-r", f"last(sort(branch('{branch}') and head() and not closed(), date))",
              "-T", "{node}"], repo).stdout.strip()
    if not out:  # a branch whose heads are all closed still has a tip
        out = hg(["log", "-r", f"last(branch('{branch}'))", "-T", "{node}"], repo).stdout.strip()
    if not out:
        raise SampleError(f"branch {branch!r} has no changesets")
    return out


def cutoff(repo: Path, head: str, pct: float, min_commits: int):
    """Oldest ``ceil(pct%)`` of the changesets reachable from ``head``, in revision order (parents first)."""
    nodes = hg(["log", "-r", f"sort(ancestors({head}), rev)", "-T", "{node}\n"], repo).stdout.split()
    total = len(nodes)
    if total < min_commits:
        raise SampleError(f"only {total} changesets on the branch, below min-commits {min_commits}")
    idx = min(total, max(1, math.ceil(total * pct / 100)))
    return nodes[idx - 1], total, idx


def sample(src: Path, out_dir: Path, pct: float = 5.0, branch: Optional[str] = None,
           min_commits: int = 20, log: Callable[[str], None] = print) -> dict:
    """Write the sampled repo to ``out_dir`` (replaced if present); return its manifest."""
    src, out_dir = Path(src), Path(out_dir)
    if not (src / ".hg").is_dir():
        raise SampleError(f"not a Mercurial repository: {src}")
    branch = pick_branch(src, branch)
    head = branch_head(src, branch)
    node, total, idx = cutoff(src, head, pct, min_commits)
    log(f"{branch}: {total} changesets, first {pct:g}% => #{idx} ({node[:12]})")

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        # --pull: never hardlink the source's store; -r: only the cutoff and its ancestors are copied
        hg(["clone", "--pull", "--noupdate", "-r", node, str(src), str(out_dir)])
        hg(["update", "-r", node, "--clean"], out_dir)
        # clone records the source path (a local path, or a URL with credentials): remove it, and any
        # bookmark that came along, so nothing points back to the source
        (out_dir / ".hg" / "hgrc").unlink(missing_ok=True)
        for line in hg(["bookmarks", "-T", "{bookmark}\n"], out_dir, check=False).stdout.splitlines():
            if line.strip() and "no bookmarks" not in line:
                hg(["bookmarks", "-d", line.strip()], out_dir, check=False)
        verify = hg(["verify"], out_dir, check=False)
        reachable = len(hg(["log", "-r", "all()", "-T", "x"], out_dir).stdout)
        top = hg(["log", "-r", "tip", "-T", "{node}"], out_dir).stdout.strip()
        branches = hg(["branches", "-c", "-T", "{branch}\n"], out_dir).stdout.split()
    except Exception:
        shutil.rmtree(out_dir, ignore_errors=True)
        raise
    manifest = {
        "vcs": "mercurial", "source": str(src), "branch_sampled": branch, "requested_pct": pct,
        "total_commits": total, "cutoff_commit_index": idx, "cutoff_commit_sha": node,
        "reachable_commits_in_sample": reachable, "branches_in_sample": sorted(set(branches)),
        "tip_is_cutoff": top == node, "verify_clean": verify.returncode == 0,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "not_followed": "subrepos (.hgsub) and largefiles are not part of the sample",
    }
    (out_dir.parent / f"{out_dir.name}.manifest.json").write_text(json.dumps(manifest, indent=2))
    log(f"sample ready: {reachable} changesets -> {out_dir}")
    return manifest
