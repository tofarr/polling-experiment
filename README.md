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
│   └── hello.py
└── README.md
```
