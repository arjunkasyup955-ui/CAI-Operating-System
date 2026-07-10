# AFOS — AI Founder Operating System

A multi-agent operating system that takes a solo founder from Idea → Research → Decision →
Product → Launch → Marketing → Sales → Revenue. Full architecture blueprint:
`C:\Users\Ashish\.claude\plans\spicy-swimming-quokka.md`.

## Status

**Phase 0 — Core Platform Skeleton.** No domain agents yet. This phase proves the kernel every
later phase builds on: Event Bus, Agent Registry, Tool Registry, Model Router, Memory Gateway
(working + vector tiers), Permission Layer, Approval Framework, Budget Manager, plus a Manager
stub agent running through one real LangGraph human-approval gate.

## Setup

```
.venv\Scripts\activate
pip install -e .
copy .env.example .env   # then fill in OPENAI_API_KEY
```

## Verify Phase 0

```
python scripts\smoke_test_phase0.py
```

## Layout

- `core/` — domain-agnostic kernel (event bus, registries, model router, memory gateway,
  permissions, approval, budget manager). Nothing here knows about ventures, ideas, or marketing.
- `agents/` — one self-contained package per agent (code + manifest + prompts + tests).
- `workflows/` — LangGraph graphs composing agents into supervised pipelines.
- `database/` — SQLite-backed storage for checkpoints, vectors, and the budget ledger.
- `scripts/` — dev/ops scripts, including the Phase 0 smoke test.
