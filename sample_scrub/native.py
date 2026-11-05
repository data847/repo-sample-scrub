"""Mercurial / Perforce jobs for the queue and the UI: sample -> zip -> manifest, no scrub.

Same shape as ``pipeline.run_repo`` (one folder per repo under outputs/<job>/<slug>/, a RESULT.json,
the un-zipped sample deleted afterwards), but the deliverable is the native sample zipped as
``<slug>_sample.zip`` and the verdict is the sampler's own integrity check instead of the scrub gates.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path
from typing import Callable

from . import config, hg_sampler, p4_sampler
from .pipeline import Options

VCS = ("hg", "p4")


def slug_for(vcs: str, target: str) -> str:
    base = Path(target).name if vcs == "hg" else target.strip("/").replace("/...", "").replace("/", "__")
    return re.sub(r"[^\w.\-]+", "_", base).strip("_") or "sample"


def p4_conn(tokens: config.Tokens):
    if not (tokens.p4_port and tokens.p4_user):
        raise p4_sampler.SampleError("Perforce server and user are not set (Perforce tab -> Save)")
    return p4_sampler._p4m().P4Conn(tokens.p4_port, tokens.p4_user, tokens.p4_secret)


def run_native(vcs: str, target: str, job_id: str, opts: Options, tokens: config.Tokens,
               log: Callable[[str], None] = print) -> dict:
    """``target`` is an absolute repo path (hg) or a depot path (p4). Returns the RESULT summary."""
    if vcs not in VCS:
        raise ValueError(f"unknown vcs {vcs!r}")
    slug = slug_for(vcs, target)
    dest = config.outputs_root() / job_id / slug
    dest.mkdir(parents=True, exist_ok=True)
    started = time.time()
    sample_dir = dest / "sample"
    manifest_file = sample_dir.parent / f"{sample_dir.name}.manifest.json"
    ok = False
    try:
        if vcs == "hg":
            manifest = hg_sampler.sample(Path(target), sample_dir, opts.pct, opts.branch, opts.min_commits, log)
            verdict = "verify clean" if manifest["verify_clean"] else "hg verify FAILED"
        else:
            manifest = p4_sampler.sample(p4_conn(tokens), target, sample_dir, opts.pct, opts.min_commits, log,
                                         tokens.p4_fingerprint)
            par = manifest["parity"]
            verdict = "parity ok" if par["ok"] else f"parity: {par['mismatches']} file mismatches"
        shutil.make_archive(str(dest / f"{slug}_sample"), "zip", root_dir=sample_dir)
        shutil.move(str(manifest_file), dest / "SAMPLE_MANIFEST.json")
        ok = True
    finally:
        shutil.rmtree(sample_dir, ignore_errors=True)  # the zip is the deliverable
        if not ok:
            manifest_file.unlink(missing_ok=True)
            try:
                dest.rmdir()
            except OSError:
                pass
    summary = {"slug": slug, "native": vcs, "zip": f"{slug}/{slug}_sample.zip", "verdict": verdict,
               "fast": False, "commits_sampled": manifest["reachable_commits_in_sample"],
               "commits_total": manifest["total_commits"], "seconds": round(time.time() - started, 1)}
    (dest / "RESULT.json").write_text(json.dumps(summary, indent=2))
    log(f"done {slug}: {summary}")
    return summary
