#!/usr/bin/env bash
set -euo pipefail

# A-V1-IT-001: Set up a temporary git repo with an embedded bug
# and a metrics.json reflecting the symptoms.
#
# Usage:
#   bash tests/setup_it001.sh /tmp/evo-test
#   cd /tmp/evo-test
#   evolve run --config evolution.yml

DEST="${1:?Usage: $0 <target-dir>}"

rm -rf "$DEST"
mkdir -p "$DEST"
cd "$DEST"

git init
git checkout -b main

# ── config/tunable.yml with a deliberately bad value ──
mkdir -p config
cat > config/tunable.yml << 'YAML'
schema_version: "1.0.0"

timing:
  api_call_interval_sec: 1         # BUG: way too fast, causes rate limiting
  account_switch_interval_sec: 120
  pr_submit_wait_sec: 5

retry:
  max_retries: 3
  backoff_base_sec: 2
  timeout_sec: 30

concurrency:
  max_parallel_accounts: 3
  max_parallel_tasks_per_acct: 1

rate_limits:
  max_prs_per_account_per_day: 2
  max_api_calls_per_account_per_day: 100
  max_prs_per_repo_per_day: 1

selection:
  min_repo_stars: 100
  min_repo_activity_days: 30
  max_existing_prs_by_us: 2

behavior:
  sleep_jitter_min_sec: 10
  sleep_jitter_max_sec: 60
YAML

# ── dashboard/metrics.json showing rate_limited symptoms ──
NOW=$(python3 -c "from datetime import datetime,timezone; print(datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))")

mkdir -p dashboard
cat > dashboard/metrics.json << JSON
{
  "schema_version": "1.0.0",
  "generated_at": "${NOW}",
  "accounts": [
    {
      "id": "acc_01",
      "status": "rate_limited",
      "status_detail": "github_secondary_rate_limit",
      "created_at": "2026-04-01T00:00:00Z",
      "last_active_at": "${NOW}",
      "pr_count": 5,
      "merge_count": 2,
      "proxy_id": "proxy_01"
    },
    {
      "id": "acc_02",
      "status": "rate_limited",
      "status_detail": "github_secondary_rate_limit",
      "created_at": "2026-04-05T00:00:00Z",
      "last_active_at": "${NOW}",
      "pr_count": 3,
      "merge_count": 1,
      "proxy_id": "proxy_02"
    }
  ],
  "prs": [
    {
      "id": "pr_001",
      "account_id": "acc_01",
      "repo": "example/demo",
      "state": "open",
      "created_at": "2026-04-23T09:00:00Z",
      "merged_at": null,
      "reactions": 0,
      "comments_count": 0,
      "negative_signals": []
    }
  ],
  "resources": {
    "accounts_total": 2,
    "accounts_alive": 0,
    "accounts_rate_limited": 2,
    "accounts_banned": 0,
    "proxies_total": 2,
    "proxies_healthy": 2
  },
  "budget": {
    "daily_used_usd": 4.23,
    "daily_cap_usd": 30.00,
    "cumulative_used_usd": 12.45,
    "hard_cap_usd": 100.00,
    "reset_at": "2026-04-24T00:00:00Z"
  },
  "recent_events": [
    {
      "ts": "${NOW}",
      "type": "account_rate_limited",
      "severity": "warning",
      "account_id": "acc_01",
      "details": { "endpoint": "/repos", "retry_after_sec": 60 }
    },
    {
      "ts": "${NOW}",
      "type": "account_rate_limited",
      "severity": "warning",
      "account_id": "acc_02",
      "details": { "endpoint": "/issues", "retry_after_sec": 120 }
    },
    {
      "ts": "${NOW}",
      "type": "account_rate_limited",
      "severity": "warning",
      "account_id": "acc_01",
      "details": { "endpoint": "/pulls", "retry_after_sec": 90 }
    }
  ]
}
JSON

# ── scripts/status.sh ──
mkdir -p scripts
cat > scripts/status.sh << 'SH'
#!/bin/bash
echo '{"accounts_alive":0,"accounts_rate_limited":2,"proxies_healthy":2,"budget_remaining_usd":87.55}'
SH
chmod +x scripts/status.sh

# ── Copy prompts from the engine repo ──
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cp -r "$SCRIPT_DIR/prompts" .

# ── evolution.yml (pointing at local evidence) ──
cat > evolution.yml << 'YAML'
mission: |
  运营一批 GitHub 账号，持续向真实的优质开源项目贡献有用的 PR。
  账号长期存活并积累真实声誉，避免被平台风控识别。

principles:
  - priority: 1
    rule: "账号绝不能被封禁或 Shadowban"
  - priority: 2
    rule: "行为必须拟人化，杜绝机械化特征"
  - priority: 3
    rule: "PR 必须对目标项目有实质价值，禁止水 PR"
  - priority: 4
    rule: "PR 被维护者真心合并或获得正面 review"
  - priority: 5
    rule: "Token 与基础设施成本与产出价值匹配"

resources:
  budget:
    daily_usd: 30
    hard_cap_usd: 100

evidence_sources:
  - "./dashboard/metrics.json"
  - "./scripts/status.sh"

hard_stops:
  budget_hard_cap_usd: 100
  max_consecutive_failures: 5
  max_iterations_per_day: 20
  on_trigger: "halt_and_notify"

models:
  actor: "claude-sonnet-4-20250514"
  judge: "claude-sonnet-4-20250514"

safety_mode: "human_in_the_loop"
YAML

# ── Git initial commit ──
git add -A
git commit -m "initial: repo with rate-limit bug for A-V1-IT-001"

echo ""
echo "=== Setup complete: $DEST ==="
echo ""
echo "Next steps:"
echo "  cd $DEST"
echo "  export ANTHROPIC_API_KEY='sk-ant-...'"
echo "  evolve run --config evolution.yml"
echo ""
