# repo-sample-scrub

Restate a repository to its **first N% of commits** (default 5%), then **redact it
end to end** with the [repo-pii-scrub](https://github.com/data847/repo-pii-scrub)
pipeline. Works with **GitHub, GitLab and Bitbucket**, from a CLI or a local web UI.

```
 GitHub / GitLab / Bitbucket          codebase-profiler            this repo                        repo-pii-scrub
 ───────────────────────────  list ─► (list + clone logic) ─►  oldest N% of one branch  ─►  init → clone → sanitize
                                                               (no later history, no other           → brand-map → package
                                                                branches, no remote)                 → verify_deliverable
                                                                                                  ─► <slug>_sanitized.zip
```

## What you get per repo

`outputs/<job-id>/<slug>/`

| File | Contents |
|------|----------|
| `<slug>_sanitized.zip` | the scrubbed sample repo (full history of the sample) |
| `MANIFEST.json` | scrub manifest, incl. the independent gitleaks result |
| `VERDICT.json` | acceptance gate: head, gitlinks, emails, origin, refs, parity, tip |
| `SAMPLE_MANIFEST.json` | which branch, how many commits, the cutoff SHA |
| `RESULT.json` | one-line summary |

The un-scrubbed intermediate sample is deleted after the run (also on failure).

## Requirements

| Needs | Why |
|-------|-----|
| [`codebase-profiler`](https://github.com/lh2-tech/codebase-profiler) | repo listing, pagination, rate-limit backoff, clone URLs, retrying clone — **reused, not reimplemented**. Found via `CODEBASE_PROFILER_DIR`, `vendor/codebase-profiler` (submodule) or `../codebase-profiler`. Run this tool with that project's Python environment. |
| `repo-pii-scrub` | the scrub gates. Found via `REPO_PII_SCRUB_DIR`, `vendor/repo-pii-scrub` or `../skills/repo-pii-scrub`. Needs its own venv, `repo-sanitizer`, `gitleaks`, `git-filter-repo` (see its README). |
| `git` | |
| `REPO_SANITIZER_SALT` | deterministic pseudonym salt, required in every mode (including `--fast`) |

```bash
git submodule add https://github.com/lh2-tech/codebase-profiler vendor/codebase-profiler
git submodule add https://github.com/data847/repo-pii-scrub     vendor/repo-pii-scrub
```

## Tokens

Credentials come from the environment (or a `.env` in the working directory, or a
profiler-style `--tokens-file`), or can be typed into the UI's **Credentials**
panel. UI-entered credentials live in the server process's memory only: they are
never written to disk, never returned by the API (the page only learns which
platforms have one) and are cleared when the server stops (or with "Clear all").
In every case they are redacted from logs, never written to outputs, and removed
from the retained clone's `origin` URL.

| Platform | Variable | Notes |
|----------|----------|-------|
| GitHub | `GITHUB_TOKEN` (or `GH_TOKEN`) | classic PAT, `repo` + `read:org` |
| GitLab | `GITLAB_TOKEN` | classic PAT, `read_api` + `read_repository`, Reporter+. Fine-grained tokens return an empty list |
| Bitbucket | `BITBUCKET_TOKEN`, `BITBUCKET_EMAIL` | Atlassian scoped API token (`ATAT…`) needs the account email for REST; omit the email for workspace/repository access tokens. git cloning uses `x-token-auth` automatically. **No account-wide discovery: always pass the workspace slug** |

## Usage

```bash
# what's in an org / group / workspace
python -m sample_scrub list github my-org
python -m sample_scrub list bitbucket my-workspace

# one repo, several repos, or a whole org (platform inferred from URLs / prefixes)
python -m sample_scrub run --repo acme/api --term AcmeCorp
python -m sample_scrub run --repo bitbucket:ws/repo --repo https://gitlab.com/grp/proj
python -m sample_scrub run --platform bitbucket --org my-workspace --pct 5

# an existing local clone
python -m sample_scrub run --local /path/to/full/clone

# web UI on http://127.0.0.1:8795
python -m sample_scrub ui
```

Options: `--pct 5`, `--branch main`, `--min-commits 20` (shorter histories are
refused: a percentage slice of them is not meaningful), `--term` (brand/org names
for the brand pass, repeatable), `--host` (self-hosted GitHub/GitLab), `--fast`.

### Fast vs normal mode

| | Normal (default) | `--fast` |
|---|---|---|
| NER (person/org names in free text) | on | off |
| Brand pseudonym pass | yes | no |
| Time | minutes–tens of minutes per repo | seconds |
| Names/brands in code and commit messages | redacted | **left in place** |

Fast mode is for smoke tests. It is **not fit for delivery**: emails, tokens,
endpoints and keys are caught, but a company name in a footer or a person's name
in a string literal survives. Always check a delivery with a normal run.

### The UI

One tab per platform (GitHub, GitLab, Bitbucket, Local). Each has its own credentials
fields (or set them in the environment) and listing inputs: an org / group /
workspace, or a local path (one repo, or a folder to scan). A filter box narrows long
lists, and the selection is shared across tabs so one job can mix platforms. You can
also paste repos. Tick the ones you
want, set the percentage and brand terms, and run. Jobs queue and run one at a
time (the full scrub loads a large NER model). Logs stream live; each finished repo
links its zip, manifest and verdict. The server binds `127.0.0.1` only, rejects
non-loopback `Host` headers and non-JSON POSTs, and serves only an allow-list of
output files.

## Docker

```bash
REPOS_DIR=~/code REPO_SANITIZER_SALT=$(openssl rand -hex 32) docker compose up --build
# -> http://127.0.0.1:8795
```

The image carries `git`, `gitleaks`, codebase-profiler and repo-pii-scrub with its
NER model dependencies. They are built in from the public GitHub repos by default;
to build from local checkouts instead:
`PROFILER_SRC=../codebase-profiler SCRUB_SRC=../skills/repo-pii-scrub docker compose build`.

| Mount / setting | Purpose |
|-----------------|---------|
| `REPOS_DIR` → `/repos` (read-only) | the folder shown on the **Local** tab |
| `./outputs` → `/data/outputs` | deliverables |
| `./clones` → `/data/clones` | clones of remote repos, reused between runs |
| `hf-cache` volume → `/data/hf` | NER model cache (downloaded once, on the first normal-mode run) |
| `PORT` | host port; always published on the host's **loopback only** |

**Local paths inside Docker.** The server only sees what is mounted. On the Local tab,
type the path *as the container sees it* (`/repos/my-repo`), not the host path
(`/Users/you/code/my-repo`). The UI states the mounted folder, and
`SAMPLE_SCRUB_LOCAL_ROOTS` (set to `/repos` in the image) makes the server refuse any
path outside it, including `..` traversal and symlinks that point out of the mount.

**Network exposure.** The container listens on all of its interfaces so the port
mapping works, and the UI has no login. Keep the mapping on `127.0.0.1`
(`-p 127.0.0.1:8795:8795`, as in the compose file) and never publish it to a network.
Loopback hostnames are accepted on any port (so `-p 9000:8795` works); any other
`Host` header is refused unless its hostname is listed in `SAMPLE_SCRUB_ALLOWED_HOSTS`
(DNS-rebinding protection).

Credentials can be passed as environment variables (`GITHUB_TOKEN`, `GITLAB_TOKEN`,
`BITBUCKET_TOKEN`, `BITBUCKET_EMAIL`) or typed into the UI. Neither route writes them to
the container's disk, the mounted volumes or the logs. One difference: a token passed as
an environment variable is visible to anyone who can run `docker inspect` on the
container; one typed into the UI is not (it lives only in the server's memory, and is
gone when the container restarts).

**Persistence.** Deliverables and clones persist on the host mounts. The jobs list and any
credentials typed into the UI live in the server's memory and reset when the container
restarts; finished results remain under `outputs/<job-id>/`.

**Building.** Use `docker compose build`: the Dockerfile takes codebase-profiler and
repo-pii-scrub as named build contexts, so a bare `docker build .` fails by design.

**Acceptance check.** The image builds `repo-pii-scrub` from its public repository,
which does not include `verify_deliverable.py` (the workspace version does, but depends
on the DataLabs layout). In Docker the scrub, gitleaks assertion and zip all run, but
the extra acceptance gate does not: results show "acceptance check not run" instead of
a verdict. Run `verify_deliverable.py` yourself on a delivered zip if you need it.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `SAMPLE_SCRUB_OUTPUTS_DIR` | `./outputs` (`$DATALABS_OUTPUTS_DIR/sample-scrub` in DataLabs) | deliverables |
| `SAMPLE_SCRUB_CLONES_DIR` | `./clones` (`$DATALABS_CLONES_DIR/batches/sample-scrub` in DataLabs) | full clones, reused between runs |
| `CODEBASE_PROFILER_DIR`, `REPO_PII_SCRUB_DIR` | see Requirements | dependency locations |
| `SAMPLE_SCRUB_LOCAL_ROOTS` | unset (unrestricted) | folders the UI may read local repos from (comma/colon separated) |
| `SAMPLE_SCRUB_ALLOWED_HOSTS` | loopback only | extra `Host` hostnames the UI accepts |
| `ui --bind` | `127.0.0.1` | listen address (`0.0.0.0` inside a container only) |

## Notes and limits

- **Phone numbers:** `repo-pii-scrub`'s phone rules match only strict E.164 (`+16502530000`)
  and Russian formats. International numbers written with spaces, dashes or parentheses
  (`+1 650 253 0000`, `(650) 253-0000`) are **not** redacted in either mode, locally or in
  Docker. Check for them before delivering.

- Only **one branch** is sampled (the default unless `--branch`), and tags are dropped.
- `repo-pii-scrub` keeps a Gate-1 copy of the sample under its run directory's `source/`;
  this tool deletes it after every run (success or failure), so no un-scrubbed copy is left
  behind. The scrubbed `clean/` and `sanitized/` directories stay.
- Metadata (PRs, issues) is not part of this flow; `repo-pii-scrub` handles it
  separately via `PII_SCRUB_METADATA_ROOT`.
- Bitbucket support goes through codebase-profiler's Bitbucket listing and clone code.

## Tests

```bash
python -m unittest discover -s tests -t .
```
