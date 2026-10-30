"""Paths and secrets. Every location is overridable by an environment variable.

Inside the DataLabs workspace the three workspace invariants are honoured
(DATALABS_OUTPUTS_DIR / DATALABS_CLONES_DIR / DATALABS_ENV_FILE); standalone,
everything lands under the current directory.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Dict, Optional

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def load_env() -> None:
    """Load `.env` into os.environ (real environment wins). Never exports
    non-identifier keys, so legacy hyphenated token names are ignored."""
    candidates = [os.environ.get("DATALABS_ENV_FILE"), ".env"]
    for c in candidates:
        if not c:
            continue
        for k, v in parse_env_file(Path(c)).items():
            if _IDENT.match(k):
                os.environ.setdefault(k, v)


def outputs_root() -> Path:
    if os.environ.get("SAMPLE_SCRUB_OUTPUTS_DIR"):
        return Path(os.environ["SAMPLE_SCRUB_OUTPUTS_DIR"]).resolve()
    if os.environ.get("DATALABS_OUTPUTS_DIR"):
        return (Path(os.environ["DATALABS_OUTPUTS_DIR"]) / "sample-scrub").resolve()
    return Path("outputs").resolve()


def clones_root() -> Path:
    if os.environ.get("SAMPLE_SCRUB_CLONES_DIR"):
        return Path(os.environ["SAMPLE_SCRUB_CLONES_DIR"]).resolve()
    if os.environ.get("DATALABS_CLONES_DIR"):
        return (Path(os.environ["DATALABS_CLONES_DIR"]) / "batches" / "sample-scrub").resolve()
    return Path("clones").resolve()


def _first_existing(candidates) -> Optional[Path]:
    for c in candidates:
        if c and Path(c).is_dir():
            return Path(c).resolve()
    return None


def profiler_dir() -> Optional[Path]:
    """codebase-profiler checkout that supplies the repo listing/cloning logic."""
    here = Path(__file__).resolve().parents[1]
    return _first_existing([
        os.environ.get("CODEBASE_PROFILER_DIR"),
        here / "vendor" / "codebase-profiler",
        here.parent / "codebase-profiler",
    ])


def pii_scrub_dir() -> Optional[Path]:
    """repo-pii-scrub checkout that supplies the scrub pipeline."""
    here = Path(__file__).resolve().parents[1]
    return _first_existing([
        os.environ.get("REPO_PII_SCRUB_DIR"),
        here / "vendor" / "repo-pii-scrub",
        here.parent / "skills" / "repo-pii-scrub",
        here.parent / "repo-pii-scrub",
    ])


class Tokens:
    """Platform credentials from the environment (or a profiler-style tokens file).

    GITHUB_TOKEN | GITLAB_TOKEN | BITBUCKET_TOKEN (+ BITBUCKET_EMAIL for Atlassian
    scoped tokens; omit it for workspace/repo access tokens).
    """

    def __init__(self, tokens_file: Optional[str] = None) -> None:
        load_env()
        file_vals = parse_env_file(Path(tokens_file)) if tokens_file else {}
        get = lambda *names: next(  # noqa: E731
            (v for n in names for v in (os.environ.get(n) or file_vals.get(n),) if v), "")
        self.github = get("GITHUB_TOKEN", "GH_TOKEN", "github-data-token")
        self.gitlab = get("GITLAB_TOKEN", "gitlab_token")
        self.bitbucket = get("BITBUCKET_TOKEN", "bitbucket_token")
        self.bitbucket_email = get("BITBUCKET_EMAIL", "bitbucket_email")

    def for_platform(self, platform: str) -> str:
        return {"github": self.github, "gitlab": self.gitlab, "bitbucket": self.bitbucket}[platform]

    def available(self) -> Dict[str, bool]:
        return {"github": bool(self.github), "gitlab": bool(self.gitlab), "bitbucket": bool(self.bitbucket)}

    def redact(self, text: str) -> str:
        for t in (self.github, self.gitlab, self.bitbucket):
            if t:
                text = text.replace(t, "***")
        return text
