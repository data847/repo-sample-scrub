"""Restate a Perforce depot path to its oldest N% of submitted changelists.

Perforce has no "clone up to a revision", so the sample is *built*: a fresh local ``p4d`` is created
and the first ``ceil(N%)`` changelists that touch the path are replayed into it one by one (sync the
source to ``@change``, reconcile, submit), then the original user, date and description are restored
on each new changelist. The result is a self-contained depot holding exactly that history and
nothing else: no later changelist, no other path, no link to the source server.

What is preserved: file content at every replayed changelist (verified against the source by file
digest at the cutoff), changelist order, authors, dates, descriptions. What is not: original changelist
numbers (new ones are issued; ``changes`` in the manifest maps old -> new), integration history
(a branch becomes plain adds/edits), shelves, jobs, labels, and per-file type/permission overrides
beyond what ``p4 reconcile`` detects.

Native on purpose: the sample stays a Perforce depot (profile it with ``codebase-profiler --p4-port``
after starting ``p4d -r <out>/p4root``). repo-pii-scrub is git-only, so this path does not scrub.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import config


class SampleError(RuntimeError):
    pass


def _p4m():
    d = config.profiler_dir()
    if d is None:
        raise SampleError("codebase-profiler not found (set CODEBASE_PROFILER_DIR); it provides the p4 helpers")
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    try:
        import p4_metrics
    except Exception as exc:  # pragma: no cover - environment dependent
        raise SampleError(f"could not import p4_metrics from {d}: {exc}") from exc
    return p4_metrics


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def cutoff_index(total: int, pct: float, min_commits: int) -> int:
    if total < min_commits:
        raise SampleError(f"only {total} changelists on the path, below min-commits {min_commits}")
    return min(total, max(1, math.ceil(total * pct / 100)))


def _client_spec(name: str, user: str, root: Path, scope: str) -> str:
    # clobber: every step moves files from one revision to the next, so overwriting must be allowed
    return (f"Client: {name}\nOwner: {user}\nRoot: {root}\n"
            "Options: allwrite clobber nocompress unlocked nomodtime normdir\n"
            "SubmitOptions: leaveunchanged\nLineEnd: local\n"
            f"View:\n\t\"{scope}\" \"//{name}/...\"\n")


def _fmt_time(epoch: int) -> str:
    return time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(epoch))


def sample(conn, depot_path: str, out_dir: Path, pct: float = 5.0, min_commits: int = 20,
           log: Callable[[str], None] = print, fingerprint: str = "") -> dict:
    """Build the sampled depot in ``out_dir`` (replaced if present); return its manifest.

    ``conn`` is a ``p4_metrics.P4Conn`` for the source server (credentials stay in it, never on a
    command line)."""
    p4 = _p4m()
    if shutil.which("p4d") is None:
        raise SampleError("`p4d` is not installed (the sample is built in a local Perforce server)")
    p4.connect(conn, fingerprint=fingerprint)
    scope = p4.normalize_depot_path(depot_path)
    prefix = p4.path_prefix(scope)
    depot_name = prefix[2:].split("/", 1)[0]

    changes = list(reversed(list(p4.iter_changes(conn, scope))))  # oldest first
    total = len(changes)
    idx = cutoff_index(total, pct, min_commits)
    chosen = changes[:idx]
    cutoff_change = chosen[-1]["change"]
    log(f"{scope}: {total} changelists, first {pct:g}% => #{idx} (@{cutoff_change})")

    out_dir = Path(out_dir)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    root = out_dir / "p4root"
    root.mkdir(parents=True)
    work = out_dir.parent / f".{out_dir.name}.work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    port = f"127.0.0.1:{_free_port()}"
    tgt = p4.P4Conn(port, "sampler")
    src_client, tgt_client = "sample-src", "sample-tgt"
    started = False
    try:
        subprocess.run(["p4d", "-r", str(root), "-p", port, "-d", "-L", str(out_dir / "p4d.log")], check=True,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        started = True
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", int(port.split(":")[1])), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        p4.connect(tgt)
        if depot_name != "depot":
            spec = p4.run_p4_text(tgt, "depot", "-o", depot_name)
            p4.run_p4_text(tgt, "depot", "-i", stdin=spec)
        p4.run_p4_text(conn, "client", "-i", stdin=_client_spec(src_client, conn.user, work, scope))
        p4.run_p4_text(tgt, "client", "-i", stdin=_client_spec(tgt_client, "sampler", work, scope))

        directory = p4.user_directory(conn)
        made_users = {"sampler"}
        mapping, skipped, attribution_failures = [], [], []
        for n, c in enumerate(chosen, 1):
            p4.run_p4_text(conn, "sync", "-q", f"{scope}@{c['change']}", client=src_client, timeout=3600)
            p4.run_p4_text(tgt, "reconcile", "-a", "-e", "-d", f"//{tgt_client}/...", client=tgt_client,
                           timeout=3600, check=False)
            desc = c["desc"] or "<no description>"
            proc = subprocess.run(["p4", "-c", tgt_client, "submit", "-d", desc], capture_output=True, text=True,
                                  env=tgt.env(), errors="replace")
            # "Change 7 submitted." or, when the number was taken meanwhile, "Change 7 renamed change 9 and submitted."
            m = re.search(r"^Change (\d+)(?: renamed change (\d+))? and? ?submitted|^Change (\d+) submitted",
                          proc.stdout, re.M)
            new = (m.group(2) or m.group(1) or m.group(3)) if m else ""
            if proc.returncode != 0 or not new:
                # a changelist whose only effect was outside this path's content (e.g. an integrate that changed nothing)
                skipped.append(c["change"])
                log(f"  @{c['change']}: nothing to submit ({(proc.stdout + proc.stderr).strip()[:80]})")
                continue
            user = c["user"] or "unknown"
            if user not in made_users:
                full, email = directory.get(user, (user, ""))
                spec = f"User: {user}\nEmail: {email or user + '@sample.invalid'}\nFullName: {full}\n"
                p4.run_p4_text(tgt, "user", "-f", "-i", stdin=spec, check=False)
                made_users.add(user)
            body = p4.run_p4_text(tgt, "change", "-f", "-o", new, client=tgt_client)
            lines = []
            for ln in body.splitlines():
                if ln.startswith("User:"):
                    ln = f"User:\t{user}"
                elif ln.startswith("Date:"):
                    ln = f"Date:\t{_fmt_time(c['time'])}"
                lines.append(ln)
            try:
                p4.run_p4_text(tgt, "change", "-f", "-i", stdin="\n".join(lines) + "\n", client=tgt_client)
            except p4.P4Error as exc:  # the content is replayed; only attribution failed, so say so
                log(f"  @{c['change']}: could not restore user/date on new change {new}: {exc}")
                attribution_failures.append(c["change"])
            mapping.append([c["change"], int(new)])
            if n % 25 == 0 or n == len(chosen):
                log(f"  replayed {n}/{len(chosen)}")

        parity = _parity(p4, conn, tgt, scope, prefix, cutoff_change)
        replayed = len(p4.all_change_ids(tgt, f"//{depot_name}/..."))
        p4.run_p4_text(tgt, "admin", "stop", check=False)
        started = False
        time.sleep(0.5)
        subprocess.run(["p4d", "-r", str(root), "-jc"], check=False, capture_output=True)
    finally:
        if started:
            p4.run_p4_text(tgt, "admin", "stop", check=False)
        p4.run_p4_text(conn, "client", "-d", "-f", src_client, check=False)
        shutil.rmtree(work, ignore_errors=True)

    (out_dir / "RESTORE.txt").write_text(
        "A Perforce depot holding the sampled history.\n"
        f"Serve it:  p4d -r {root.name} -p 127.0.0.1:1666 -d   (user: sampler, no password)\n"
        "or restore the checkpoint in this folder into a new server with:  p4d -r NEWROOT -jr checkpoint.1\n")
    manifest = {
        "vcs": "perforce", "source_depot_path": scope, "requested_pct": pct, "total_commits": total,
        "cutoff_commit_index": idx, "cutoff_commit_sha": str(cutoff_change),
        "reachable_commits_in_sample": replayed, "skipped_empty_changelists": skipped,
        "attribution_not_restored": attribution_failures,
        "parity": parity, "changes": mapping,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "not_preserved": "original changelist numbers, integration history, shelves, jobs, labels, file-type overrides",
    }
    (out_dir.parent / f"{out_dir.name}.manifest.json").write_text(json.dumps(manifest, indent=2))
    log(f"sample ready: {replayed} changelists -> {out_dir}")
    return manifest


def _digests(p4, conn, spec: str, prefix: str, client: str = "") -> dict:
    out = {}
    for r in p4.iter_marshal(conn, "fstat", "-Ol", "-T", "depotFile,digest,headAction", spec, client=client):
        if r.get("headAction", "").endswith("delete") or r.get("headAction") == "purge":
            continue
        out[r.get("depotFile", "")[len(prefix):]] = r.get("digest", "")
    return out


def _parity(p4, src, tgt, scope: str, prefix: str, cutoff_change: int) -> dict:
    """File-by-file check that the sample's head equals the source at the cutoff changelist."""
    want = _digests(p4, src, f"{scope}@{cutoff_change}", prefix)
    got = _digests(p4, tgt, f"{scope}", prefix)
    diff = sorted(set(want) ^ set(got) | {k for k in want if k in got and want[k] != got[k]})
    return {"files_at_cutoff": len(want), "files_in_sample": len(got), "mismatches": len(diff),
            "mismatch_examples": diff[:10], "ok": not diff}
