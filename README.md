# Evolution Engine V1

**Self-purification engine for autonomous codebases.**

Read an `evolution.yml`, observe evidence, let an Actor LLM propose a minimal patch, have an independent Judge LLM audit it, and automatically open a GitHub PR — all without local `git` or `gh` CLI.

> 🇨🇳 **[中文文档 → README.zh.md](README.zh.md)**

---

## Table of Contents

- [How It Works](#how-it-works)
- [Quick Start](#quick-start)
- [Configuration Reference](#configuration-reference)
  - [mission](#mission)
  - [principles](#principles)
  - [resources](#resources)
  - [evidence\_sources](#evidence_sources)
  - [hard\_stops](#hard_stops)
  - [models](#models)
  - [github](#github)
  - [safety\_mode](#safety_mode)
- [Environment Variables](#environment-variables)
  - [LLM Providers](#llm-providers)
  - [GitHub Token](#github-token)
- [CLI Commands](#cli-commands)
- [Project Structure](#project-structure)
- [Running Tests](#running-tests)
- [Troubleshooting](#troubleshooting)
- [Documentation](#documentation)

---

## How It Works

```
┌─────────────┐   evidence    ┌───────────┐   patch    ┌──────────┐   verdict   ┌──────────┐
│  Observer   │ ────────────► │   Actor   │ ─────────► │  Judge   │ ──────────► │  Router  │
│ (read files,│               │ (LLM #1,  │            │ (LLM #2, │             │          │
│  run scripts│               │  proposes │            │  audits, │      PASS ──► create PR │
│  fetch URLs)│               │  unified  │            │  scores) │             │ via HTTP  │
└─────────────┘               │  diff)    │            └──────────┘      FAIL ──► skip + log│
                              └───────────┘                               │      └──────────┘
                                                                          ▼
                                                                   Hard Stops
                                                              (budget / failures /
                                                               daily iterations)
```

Each run performs **one full cycle**:

1. **Observer** — reads all `evidence_sources` (local files, shell scripts, remote URLs) and assembles a structured evidence bundle.
2. **Actor** — calls LLM #1 with the evidence + mission + principles. The model must return a single unified diff (fenced in ` ```diff `).
3. **Judge** — calls LLM #2 in a **completely fresh session** (no shared context with Actor). Receives only: mission, principles, evidence, and the patch text. Returns a structured JSON verdict: `PASS/FAIL`, `overall_score` (0–100), `confidence`, `top_risks`.
4. **Router** — on `PASS`: uses the GitHub REST API to create a branch, commit the patch, and open a PR. On `FAIL`: records the event and exits cleanly.
5. **Hard Stops** — a persistent circuit breaker that halts the engine if budget, consecutive failures, or daily iteration limits are exceeded.

> **Actor / Judge isolation is a hard requirement.** The two LLMs must use different models or different API sessions. The Judge never sees the Actor's reasoning — only the final patch. This is enforced at runtime.

---

## Quick Start

### 1. Install

```bash
# Clone and enter the repo
git clone <repo-url> && cd evolution-engine

# Create a virtual environment
python3 -m venv .venv && source .venv/bin/activate

# Install with dev dependencies
pip install -e ".[dev]"
```

### 2. Configure environment variables

```bash
# Copy the template
cp examples/.env.example examples/.env

# Edit examples/.env and fill in real values:
#   ACTOR_MODEL, ACTOR_API_BASE_URL, ACTOR_API_KEY
#   JUDGE_MODEL, JUDGE_API_BASE_URL, JUDGE_API_KEY
#   GITHUB_REPO (e.g. "your-org/your-repo")
#   GITHUB_TOKEN (GitHub Personal Access Token)
```

Load the variables (the `set -a` ensures they are exported to child processes):

```bash
set -a && source examples/.env && set +a
```

### 3. Validate your config

```bash
evolve validate --config examples/evolution.yml
```

A clean exit (code 0) means the YAML schema is valid.

### 4. Run one evolution cycle

```bash
evolve run --config examples/evolution.yml
```

If the Judge passes the patch, a real GitHub PR is created and the URL is printed to stdout.

---

## Configuration Reference

All configuration lives in a single `evolution.yml` file. See [`examples/evolution.yml`](examples/evolution.yml) for a complete working example.

### `mission`

A 3–5 sentence north-star statement that describes **what the codebase should achieve**. Both Actor and Judge receive this as the primary objective.

```yaml
mission: |
  Run a fleet of GitHub accounts that continuously contribute useful PRs
  to high-quality open-source projects. Accounts must remain alive and
  accumulate genuine reputation without triggering platform risk controls.
```

---

### `principles`

An ordered list of rules the Judge uses to score every patch. Lower `priority` number = higher importance. The Judge must never pass a patch that violates a higher-priority rule to satisfy a lower-priority one.

```yaml
principles:
  - priority: 1
    rule: "Accounts must never be banned or shadowbanned"
  - priority: 2
    rule: "Behavior must appear human; no mechanical patterns"
  - priority: 3
    rule: "Every PR must provide genuine value to the target project"
```

---

### `resources`

Declares paths to operational resources. Paths can be local file paths or `env:VAR_NAME` references.

```yaml
resources:
  accounts:
    path: "./workspace/resources/accounts.enc.json"
    health_check: "./scripts/check_account_health.sh"
  proxies:
    path: "env:PROXY_PROVIDER_URL"          # read from environment variable
    health_check: "./scripts/check_proxy_latency.sh"
  budget:
    daily_usd: 30
    hard_cap_usd: 100
```

> **V1 note:** `resources` is declared for schema completeness. Active health-check invocation is a V2 feature.

---

### `evidence_sources`

A list of sources the Observer reads each cycle to build the evidence bundle. Three source types are supported:

| Type | Example | Description |
|------|---------|-------------|
| Local file | `"./dashboard/metrics.json"` | Read and include file content |
| Shell script | `"./scripts/status.sh"` | Execute and capture stdout |
| Remote URL | `"https://example.com/api/status"` | HTTP GET and include response body |

```yaml
evidence_sources:
  - "./dashboard/metrics.json"
  - "./dashboard/config_hint.json"   # current config excerpt to help Actor produce exact diffs
  - "./scripts/status.sh"
```

**Tip:** Include a `config_hint.json` that contains the exact current content of the file you want the Actor to patch. This dramatically improves diff precision and Judge pass rates.

---

### `hard_stops`

A persistent circuit breaker. State is saved to `.evolution_state.json` next to the config file.

```yaml
hard_stops:
  budget_hard_cap_usd: 100          # halt if cumulative LLM spend exceeds this
  max_consecutive_failures: 5       # halt after N consecutive Judge FAILs or errors
  max_iterations_per_day: 20        # halt after N cycles in a single calendar day
  on_trigger: "halt_and_notify"     # write a message to stderr and exit non-zero
```

To clear a halted state after human review:

```bash
evolve reset --config examples/evolution.yml
```

`reset` clears the halted flag **and** resets the consecutive-failure counter so a single stale failure doesn't immediately re-trigger.

---

### `models`

Configures the Actor and Judge LLMs. Two formats are supported:

**String shorthand** (Anthropic native SDK, key from `ANTHROPIC_API_KEY`):

```yaml
models:
  actor: "claude-sonnet-4"
  judge: "claude-opus-4"
```

**Full object form** (any OpenAI-compatible API):

```yaml
models:
  actor:
    name: "env:ACTOR_MODEL"            # resolved from $ACTOR_MODEL at runtime
    api_base_url: "env:ACTOR_API_BASE_URL"
    api_key_env: "ACTOR_API_KEY"       # name of the env var holding the key
  judge:
    name: "env:JUDGE_MODEL"
    api_base_url: "env:JUDGE_API_BASE_URL"
    api_key_env: "JUDGE_API_KEY"
```

`name` and `api_base_url` support the `env:VAR_NAME` prefix — the real value is resolved at call time, keeping the YAML file secret-free.

**Common `api_base_url` values:**

| Provider | `api_base_url` |
|----------|---------------|
| OpenAI | `https://api.openai.com/v1` |
| DeepSeek | `https://api.deepseek.com/v1` |
| Groq | `https://api.groq.com/openai/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Ollama (local) | `http://localhost:11434/v1` |
| Anthropic native | *(omit `api_base_url`)* |

> Actor and Judge can use **different providers**. Recommend a strong coding model for Actor and a strong reasoning model for Judge.

---

### `github`

Target repository for PR creation. The Router calls the GitHub REST API directly — **no local `git` or `gh` CLI required**.

```yaml
github:
  repo: "env:GITHUB_REPO"        # "owner/repo" or env:VAR_NAME
  token_env: "GITHUB_TOKEN"      # env var holding the Personal Access Token
  base_branch: "main"            # branch to create PRs against
```

Required GitHub PAT permissions: **`repo`** (full) or at minimum **`contents: write`** + **`pull_requests: write`**.

Get a token at: https://github.com/settings/tokens

---

### `safety_mode`

V1 only supports `"human_in_the_loop"`. The engine always creates a PR for human review rather than auto-merging.

```yaml
safety_mode: "human_in_the_loop"
```

---

## Environment Variables

Copy [`examples/.env.example`](examples/.env.example) to `examples/.env` and fill in real values.

```bash
cp examples/.env.example examples/.env
# edit examples/.env
set -a && source examples/.env && set +a
```

### LLM Providers

| Variable | Description |
|----------|-------------|
| `ACTOR_MODEL` | Model name for the Actor (e.g. `gpt-4o`, `deepseek-chat`) |
| `ACTOR_API_BASE_URL` | API base URL for Actor's provider |
| `ACTOR_API_KEY` | API key for Actor |
| `JUDGE_MODEL` | Model name for the Judge |
| `JUDGE_API_BASE_URL` | API base URL for Judge's provider |
| `JUDGE_API_KEY` | API key for Judge |
| `ANTHROPIC_API_KEY` | Required only when using Anthropic native SDK (string shorthand form) |

### GitHub Token

| Variable | Description |
|----------|-------------|
| `GITHUB_REPO` | Target repo in `owner/repo` format |
| `GITHUB_TOKEN` | Personal Access Token with `repo` + `pull_requests` permissions |

> **Security:** Never commit `examples/.env`. It is excluded by `.gitignore`. Only `examples/.env.example` (which contains no real secrets) should be committed.

---

## CLI Commands

```bash
# Validate evolution.yml schema (exits 0 if valid)
evolve validate --config examples/evolution.yml

# Run one Observer → Actor → Judge → Router cycle
evolve run --config examples/evolution.yml

# Run with verbose logging
evolve -v run --config examples/evolution.yml

# Print the JSONL evolution history
evolve history --config examples/evolution.yml

# Clear halted state after human review
evolve reset --config examples/evolution.yml
```

---

## Project Structure

```
evolution-engine/
├── src/
│   ├── cli.py              # Click CLI entry point (validate / run / history / reset)
│   ├── config_loader.py    # Pydantic schema validation for evolution.yml
│   ├── observer.py         # Evidence source scanner (files / scripts / URLs)
│   ├── actor.py            # Actor LLM caller + unified diff extractor
│   ├── judge.py            # Independent Judge LLM + JSON verdict parser
│   ├── router.py           # Post-verdict dispatcher (PR creation or skip)
│   ├── github_client.py    # GitHub REST API: blob → tree → commit → branch → PR
│   ├── hard_stops.py       # Persistent circuit breaker state machine
│   ├── llm.py              # Unified LLM caller (Anthropic native + OpenAI-compat)
│   └── history.py          # JSONL history writer
├── prompts/
│   ├── actor_system.md     # Actor system prompt
│   └── judge_system.md     # Judge system prompt
├── tests/
│   └── test_v1.py          # 45 unit + integration tests (Task A acceptance suite)
├── examples/
│   ├── evolution.yml       # Sample config (ClawOSS use-case)
│   ├── .env.example        # Environment variable template (commit this)
│   ├── .env                # Real secrets — DO NOT COMMIT (git-ignored)
│   ├── scripts/
│   │   └── status.sh       # Example evidence script
│   └── dashboard/
│       ├── metrics.json              # Example metrics evidence file
│       └── openclaw_config_hint.json # Example config-hint evidence file
├── pyproject.toml
├── .gitignore
├── README.md               # This file (English)
└── README.zh.md            # Chinese version
```

---

## Running Tests

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

All 45 Task A acceptance tests should pass. No real API keys or GitHub tokens are required — all LLM and GitHub calls are mocked.

To run a specific test group:

```bash
# Unit tests only
python -m pytest tests/ -v -k "TestHardStops or TestConfig or TestRouter"

# Integration test only
python -m pytest tests/ -v -k "test_full_cycle"
```

---

## Troubleshooting

**`Error: metrics.json stale: generated_at age Xs > 60s`**

The Observer enforces a 60-second freshness window on metrics files. Regenerate the file or update its `generated_at` timestamp before running.

**`ACTOR_API_KEY is not set`**

Environment variables set with plain `source .env` are not exported to child processes. Always use:
```bash
set -a && source examples/.env && set +a
```

**`Hard stop triggered: max consecutive failures reached`**

Previous failed runs accumulated in `.evolution_state.json`. Run:
```bash
evolve reset --config examples/evolution.yml
```

**`Cannot get branch 'main': Branch not found`**

The target repository's default branch is not `main`. Check on GitHub and update `base_branch` in `evolution.yml`:
```yaml
github:
  base_branch: "master"   # or whatever the default branch is
```

**`Cannot access repo 'owner/repo'`**

- Verify `GITHUB_REPO` is set to `owner/repo` format (not a full URL).
- Verify `GITHUB_TOKEN` has `repo` scope.
- If the repo is private, the token must belong to an account with access.

**Judge verdict is always `FAIL`**

- Ensure the Actor is producing a fenced diff block (` ```diff ... ``` `).
- Add a `config_hint.json` evidence source with the exact current file content so the Actor can produce a precise, context-matching diff.
- Increase `max_tokens` for the Judge if responses are being truncated.

---

## Documentation

| File | Description |
|------|-------------|
| [`PRD_EvolutionEngine.md`](PRD_EvolutionEngine.md) | Product requirements — Task A (this engine) |
| [`PRD_ClawOSS_Evolvable.md`](PRD_ClawOSS_Evolvable.md) | ClawOSS evolvable transformation — Task B |
| [`CONTRACT_metrics_schema.md`](CONTRACT_metrics_schema.md) | Shared data contract: `metrics.json` schema |
| [`TEST_CASES.md`](TEST_CASES.md) | V1 acceptance test cases |

---

## License

Apache 2.0
