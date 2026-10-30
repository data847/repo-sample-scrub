"""Repo discovery and cloning, delegated to codebase-profiler.

The listing, pagination, rate-limit backoff, clone URLs and retrying clone all
live in codebase-profiler (`extract_org_raw_data`, `clone_all_repos`); this
module imports them rather than re-implementing "how to reach a repo".
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit

from . import config

PLATFORMS = ("github", "gitlab", "bitbucket")


class BridgeError(RuntimeError):
    pass


@dataclass(frozen=True)
class RepoRef:
    platform: str          # github | gitlab | bitbucket
    full_name: str         # owner/repo (gitlab: group/sub/project)
    host: str = ""         # self-hosted github/gitlab host; "" = public default
    default_branch: str = ""

    @property
    def slug(self) -> str:
        return re.sub(r"[^\w.\-]+", "_", self.full_name.replace("/", "__"))

    @property
    def bare_url(self) -> str:
        host = self.host or {"github": "github.com", "gitlab": "gitlab.com", "bitbucket": "bitbucket.org"}[self.platform]
        return f"https://{host}/{self.full_name}.git"


def _profiler():
    d = config.profiler_dir()
    if d is None:
        raise BridgeError(
            "codebase-profiler not found. Set CODEBASE_PROFILER_DIR, or add it as "
            "vendor/codebase-profiler (git submodule add <url> vendor/codebase-profiler).")
    if str(d) not in sys.path:
        sys.path.insert(0, str(d))
    try:
        import extract_org_raw_data as eor  # noqa: WPS433
        import clone_all_repos as car       # noqa: WPS433
    except Exception as exc:  # pragma: no cover - environment dependent
        raise BridgeError(f"could not import codebase-profiler from {d}: {exc}") from exc
    return eor, car


def parse_repo(text: str, default_platform: str = "github") -> RepoRef:
    """`owner/repo`, `platform:owner/repo`, or an https/ssh URL -> RepoRef."""
    t = text.strip()
    m = re.match(r"^(github|gitlab|bitbucket):(.+)$", t)
    if m:
        return RepoRef(m.group(1), m.group(2).strip("/").removesuffix(".git"))
    m = re.match(r"^git@([^:]+):(.+)$", t)
    if m:
        host, path = m.group(1), m.group(2)
    elif "://" in t:
        u = urlsplit(t)
        host, path = u.hostname or "", u.path
    else:
        if t.count("/") < 1:
            raise BridgeError(f"cannot parse repo {text!r}; use owner/repo or a URL")
        return RepoRef(default_platform, t.strip("/").removesuffix(".git"))
    path = path.strip("/").removesuffix(".git")
    if host == "bitbucket.org":
        return RepoRef("bitbucket", path)
    if host == "github.com":
        return RepoRef("github", path)
    if host == "gitlab.com":
        return RepoRef("gitlab", path)
    platform = "gitlab" if "gitlab" in host else "github"
    return RepoRef(platform, path, host=host)


def list_repos(platform: str, owner: str, tokens: config.Tokens, host: str = "") -> List[RepoRef]:
    """All repos of a GitHub org/user, GitLab group or Bitbucket workspace."""
    if platform not in PLATFORMS:
        raise BridgeError(f"unknown platform {platform!r}")
    token = tokens.for_platform(platform)
    if not token:
        raise BridgeError(f"no {platform} token set (see README: Tokens)")
    eor, car = _profiler()
    try:
        if platform == "github":
            objs = car.list_repos_with_retry(eor.list_github_repo_objects, token, owner, host or "github.com")
            return [RepoRef("github", o["full_name"], host if host != "github.com" else "",
                            o.get("default_branch") or "") for o in objs]
        if platform == "gitlab":
            objs = car.list_repos_with_retry(eor.list_gitlab_project_objects, token, owner, host or "gitlab.com")
            return [RepoRef("gitlab", o.get("full_name") or o.get("path_with_namespace", ""),
                            host if host != "gitlab.com" else "", o.get("default_branch") or "")
                    for o in objs]
        objs = car.list_repos_with_retry(eor.list_bitbucket_repo_objects, token, owner, tokens.bitbucket_email)
        return [RepoRef("bitbucket", o["full_name"], "", (o.get("mainbranch") or {}).get("name", ""))
                for o in objs]
    except RuntimeError as exc:
        raise BridgeError(tokens.redact(str(exc))) from None


def _auth_url(ref: RepoRef, token: str) -> str:
    eor, _ = _profiler()
    if ref.platform == "github":
        return eor.github_clone_url(ref.full_name, token, ref.host or "github.com")
    if ref.platform == "gitlab":
        return eor.gitlab_clone_url(ref.full_name, token, ref.host or "gitlab.com")
    return eor.bitbucket_clone_url(ref.full_name, token)


def clone_path(ref: RepoRef) -> Path:
    return config.clones_root() / ref.platform / ref.slug


def ensure_clone(ref: RepoRef, tokens: config.Tokens, log=print) -> Path:
    """Full clone into the clone store (reused if already present). The token is
    scrubbed from the retained clone's origin URL."""
    dest = clone_path(ref)
    if (dest / ".git").is_dir():
        log(f"reusing existing clone {dest}")
        return dest
    token = tokens.for_platform(ref.platform)
    if not token:
        raise BridgeError(f"no {ref.platform} token set; cannot clone {ref.full_name}")
    eor, _ = _profiler()
    log(f"cloning {ref.platform}:{ref.full_name}")
    try:
        eor.clone_repo(_auth_url(ref, token), dest)
    except Exception as exc:
        raise BridgeError(tokens.redact(str(exc))) from None
    subprocess.run(["git", "-C", str(dest), "remote", "set-url", "origin", ref.bare_url],
                   capture_output=True, check=False)
    return dest
