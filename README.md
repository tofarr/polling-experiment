# polling-experiment

A scratch space for experimental Python scripts, managed with [uv](https://docs.astral.sh/uv/).

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) (0.4+)
- Python 3.13 (uv will install it automatically if missing; pinned via `.python-version`)

## Setup

Clone the repo and sync the environment:

```bash
uv sync
```

This creates a `.venv/` and installs everything pinned in `uv.lock`.

## Running scripts

Experimental scripts live in [`scripts/`](./scripts). Run them with `uv run` so
they execute inside the project's virtualenv:

```bash
uv run python scripts/hello.py
```

## Adding dependencies

```bash
# Runtime dependency
uv add httpx

# Dev-only dependency
uv add --dev pytest ruff
```

Both `pyproject.toml` and `uv.lock` will be updated; commit both.

## Layout

```
.
├── pyproject.toml      # Project metadata & dependencies
├── uv.lock             # Fully-resolved lockfile (commit this)
├── .python-version     # Pinned Python version for uv
├── scripts/            # Experimental scripts
│   ├── hello.py
│   └── poll_github_events.py
└── README.md
```

## Scripts

### `scripts/poll_github_events.py`

Polls one or more GitHub repositories for new events. Uses
`last_event_id` for "what's new?" filtering and `ETag` /
`If-None-Match` so idle polls cost no rate limit. State (per-repo
`last_event_id`, `etag`, `last_polled_at`, `last_status`) is persisted
to a JSON file via atomic write.

Configuration is entirely via environment variables:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GITHUB_REPOS` | ✓ | — | Comma-separated `owner/name` list. |
| `GITHUB_TOKEN` |  | unset | PAT or fine-grained token. Strongly recommended (60 req/hr → 5000 req/hr). |
| `STATE_FILE` |  | `./state.json` | Where to persist polling state. |
| `POLL_INTERVAL_SECONDS` |  | `60` | Lower bound on sleep in `--loop` mode; `X-Poll-Interval` is also honored. |
| `EVENT_TYPES` |  | all | Comma-separated allow-list (e.g. `PushEvent,PullRequestEvent`). |
| `MAX_PAGES` |  | `1` | Pages of 30 events to fetch per repo per poll (max 10 = 300 events). |
| `OUTPUT_FILE` |  | unset | If set, new events are appended as NDJSON in addition to stdout. |
| `LOG_LEVEL` |  | `INFO` | Standard Python log level. |

**One-shot:**

```bash
GITHUB_REPOS="octocat/Hello-World,github/docs" \
GITHUB_TOKEN=ghp_... \
uv run python scripts/poll_github_events.py
```

**Loop (daemon):**

```bash
GITHUB_REPOS="octocat/Hello-World" \
GITHUB_TOKEN=ghp_... \
uv run python scripts/poll_github_events.py --loop
```

**First-run behavior:** when a repo has no prior state, the script
records the current max event id as the baseline and emits **no**
historical events. New events appear on subsequent polls. To force a
backfill, delete the repo's entry from `state.json` and set
`MAX_PAGES=10`, then manually edit `last_event_id` to a lower value (or
remove it and accept the baseline behavior on the *next* run).

**Output:** each new event is printed as one compact JSON object per
line (NDJSON) to stdout, oldest-first within a poll.
