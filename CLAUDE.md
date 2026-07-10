# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

This repository is an early-stage scaffold for "CAI-Operating-System." At present it contains only
the top-level directory layout below and a Python virtual environment (`.venv/`, not committed) with
dependencies installed — no application code, config files (`requirements.txt`/`pyproject.toml`),
tests, or documentation have been added yet. Treat the directory names as the intended architecture,
not as proof that any implementation exists: check whether a directory actually has files in it before
assuming behavior.

## Directory layout (intended architecture)

- `agents/` — agent definitions/implementations
- `apps/` — application entry points
- `database/` — database schema/access code (`.gitignore` excludes `*.db` files)
- `docker/` — containerization configs
- `docs/` — project documentation
- `prompts/` — prompt templates
- `scripts/` — utility/dev scripts
- `tests/` — test suite
- `tools/` — tool implementations (likely for agent tool-calling)
- `workflows/` — orchestration/workflow definitions (likely LangGraph graphs)

All of these directories are currently empty.

## Stack

The `.venv` has the following key packages installed, indicating the planned stack:

- **langchain**, **langchain-core**, **langgraph**, **langgraph-checkpoint**, **langgraph-prebuilt**,
  **langgraph-sdk**, **langsmith** — agent orchestration is built on LangChain/LangGraph, with LangSmith
  for tracing/observability.
- **openai** — OpenAI API client.
- **pydantic** — data validation/schemas.
- **python-dotenv** — environment variable loading from `.env` (git-ignored).
- The `.gitignore` also references Ollama (`.ollama/`) as a likely local-model runtime target.

There is no `requirements.txt` or `pyproject.toml` yet — when adding one, pin it to the versions
already resolved in `.venv/Lib/site-packages` (check via `.venv/Scripts/python.exe -m pip freeze`)
unless the user asks to upgrade.

## Commands

No build, lint, or test tooling is configured yet. Once a `requirements.txt`/`pyproject.toml` and test
suite are added, update this section with the actual commands (e.g. `pytest`, `ruff`) rather than
assuming standard defaults.

To activate the existing virtual environment on Windows:

```
.venv\Scripts\activate
```
