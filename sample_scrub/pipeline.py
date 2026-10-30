"""One repo, end to end: clone -> first N% of commits -> PII scrub -> deliverable."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from . import bridge, config, sampler, scrub


@dataclass
class Options:
    pct: float = 5.0
    branch: Optional[str] = None
    min_commits: int = 20
    terms: List[str] = field(default_factory=list)
    fast: bool = False


def run_repo(ref: Optional[bridge.RepoRef], local_path: Optional[Path], job_id: str,
             opts: Options, tokens: config.Tokens, log: Callable[[str], None] = print) -> dict:
    """Process one repo (remote `ref`, or an existing `local_path` clone)."""
    redact = tokens.redact
    slug = ref.slug if ref else Path(local_path).name
    run_id = f"{job_id}-{slug}"[:120]
    dest = config.outputs_root() / job_id / slug
    dest.mkdir(parents=True, exist_ok=True)
    started = time.time()

    src = Path(local_path) if local_path else bridge.ensure_clone(ref, tokens, log)
    sample_dir = dest / "sample"
    sample_manifest = sample_dir.parent / f"{sample_dir.name}.manifest.json"
    ok = False
    try:
        manifest = sampler.sample(src, sample_dir, opts.pct, opts.branch, opts.min_commits, log)
        res = scrub.scrub(sample_dir, slug, run_id, opts.terms, opts.fast, log, redact)
        ok = True
    finally:
        # the un-scrubbed intermediate must never be left in outputs, even on failure
        shutil.rmtree(sample_dir, ignore_errors=True)
        if not ok:
            sample_manifest.unlink(missing_ok=True)
            try:
                dest.rmdir()  # nothing worth keeping from a run that produced nothing
            except OSError:
                pass
    for name, path in (("zip", res["zip"]), ("MANIFEST.json", res["manifest"]),
                       ("VERDICT.json", res["verdict"])):
        if path and Path(path).exists():
            shutil.copy2(path, dest / (Path(path).name if name == "zip" else name))
    shutil.move(str(sample_manifest), dest / "SAMPLE_MANIFEST.json")

    verdict = None
    if (dest / "VERDICT.json").exists():
        verdict = json.loads((dest / "VERDICT.json").read_text()).get("verdict")
    summary = {"slug": slug, "zip": f"{slug}/{res['zip'].name}", "verdict": verdict,
               "fast": opts.fast, "commits_sampled": manifest["reachable_commits_in_sample"],
               "commits_total": manifest["total_commits"], "seconds": round(time.time() - started, 1)}
    (dest / "RESULT.json").write_text(json.dumps(summary, indent=2))
    log(f"done {slug}: {summary}")
    return summary
