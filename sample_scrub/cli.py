"""Command line: list a platform's repos, or sample + scrub one or many."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import bridge, config, pipeline


def _opts(a) -> pipeline.Options:
    return pipeline.Options(pct=a.pct, branch=a.branch, min_commits=a.min_commits,
                            terms=a.term, fast=a.fast)


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--tokens-file", help="profiler-style key=value tokens file (default: environment / .env)")
    p.add_argument("--host", default="", help="self-hosted GitHub/GitLab host")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sample-scrub", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    ls = sub.add_parser("list", help="list the repos of an org / group / workspace")
    ls.add_argument("platform", choices=bridge.PLATFORMS)
    ls.add_argument("owner", help="GitHub org/user, GitLab group, Bitbucket workspace slug")
    _common(ls)

    run = sub.add_parser("run", help="clone -> first N%% of commits -> PII scrub")
    src = run.add_mutually_exclusive_group(required=True)
    src.add_argument("--repo", action="append", default=[],
                     help="owner/repo, platform:owner/repo or URL (repeatable)")
    src.add_argument("--org", help="every repo of this org / group / workspace (needs --platform)")
    src.add_argument("--local", help="path to an existing full clone")
    run.add_argument("--platform", choices=bridge.PLATFORMS, default="github")
    run.add_argument("--pct", type=float, default=5.0, help="%% of commits to keep (default 5)")
    run.add_argument("--branch")
    run.add_argument("--min-commits", type=int, default=20)
    run.add_argument("--term", action="append", default=[], help="brand/org term for the brand pass (repeatable)")
    run.add_argument("--fast", action="store_true",
                     help="regex/secrets only: skips NER + brand pass, leaves names in free text")
    run.add_argument("--job-id", default=None)
    _common(run)

    ui = sub.add_parser("ui", help="start the local web UI")
    ui.add_argument("--port", type=int, default=8795)
    ui.add_argument("--tokens-file")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    a = build_parser().parse_args(argv)
    if a.cmd == "ui":
        from . import ui
        return ui.serve(a.port, a.tokens_file)

    tokens = config.Tokens(a.tokens_file)
    try:
        if a.cmd == "list":
            for r in bridge.list_repos(a.platform, a.owner, tokens, a.host):
                print(f"{r.full_name}\t{r.default_branch}")
            return 0

        job = a.job_id or time.strftime("job-%Y%m%d-%H%M%S")
        log = lambda m: print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)  # noqa: E731
        if a.local:
            targets = [(None, Path(a.local))]
        elif a.org:
            targets = [(r, None) for r in bridge.list_repos(a.platform, a.org, tokens, a.host)]
        else:
            targets = [(bridge.parse_repo(r, a.platform), None) for r in a.repo]
        failures = 0
        for ref, local in targets:
            try:
                pipeline.run_repo(ref, local, job, _opts(a), tokens, log)
            except Exception as exc:  # one bad repo must not sink the batch
                failures += 1
                log(f"FAILED {ref.full_name if ref else local}: {tokens.redact(str(exc))}")
        print(f"\n{len(targets) - failures}/{len(targets)} ok -> {config.outputs_root() / job}")
        return 1 if failures else 0
    except bridge.BridgeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
