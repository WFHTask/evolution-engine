# Evolution Engine V1

**面向自主代码库的自净化引擎。**

读取一份 `evolution.yml`，观察证据，让 Actor LLM 提出最小化 patch，再由完全独立的 Judge LLM 审核，最终通过 GitHub REST API 自动提 PR——全程无需本地 `git` 或 `gh` 命令行工具。

> 🇺🇸 **[English Documentation → README.md](README.md)**

---

## 目录

- [工作原理](#工作原理)
- [快速上手](#快速上手)
- [配置参考](#配置参考)
  - [mission（任务目标）](#mission任务目标)
  - [principles（审核原则）](#principles审核原则)
  - [resources（资源声明）](#resources资源声明)
  - [evidence\_sources（证据来源）](#evidence_sources证据来源)
  - [hard\_stops（熔断器）](#hard_stops熔断器)
  - [models（LLM 配置）](#modelsllm-配置)
  - [github（PR 目标仓库）](#githubpr-目标仓库)
  - [safety\_mode（安全模式）](#safety_mode安全模式)
- [环境变量](#环境变量)
  - [LLM 服务商配置](#llm-服务商配置)
  - [GitHub Token](#github-token)
- [CLI 命令](#cli-命令)
- [项目结构](#项目结构)
- [运行测试](#运行测试)
- [常见问题](#常见问题)
- [相关文档](#相关文档)

---

## 工作原理

```
┌─────────────┐   证据      ┌───────────┐   patch    ┌──────────┐   裁定       ┌──────────┐
│  Observer   │ ──────────► │   Actor   │ ─────────► │  Judge   │ ───────────► │  Router  │
│ (读文件、   │             │ (LLM #1,  │            │ (LLM #2, │              │          │
│  执行脚本、 │             │  提出统一 │            │  独立审核│      PASS ───► 调 GitHub │
│  请求 URL)  │             │  diff)    │            │  打分)   │              │ API 提 PR│
└─────────────┘             └───────────┘            └──────────┘      FAIL ───► 跳过+记录│
                                                                         │       └──────────┘
                                                                         ▼
                                                                    Hard Stops
                                                               (预算 / 连续失败 /
                                                                每日迭代上限)
```

每次运行执行**一个完整周期**：

1. **Observer** — 读取所有 `evidence_sources`（本地文件、Shell 脚本、远程 URL），组装结构化证据包。
2. **Actor** — 将证据 + mission + principles 发给 LLM #1，模型必须返回一个用 ` ```diff ` 包裹的统一 diff。
3. **Judge** — 在**全新独立会话**中调用 LLM #2（与 Actor 完全隔离，不共享任何上下文）。Judge 只接收：mission、principles、evidence 和 patch 文本，返回结构化 JSON 裁定：`PASS/FAIL`、`overall_score`（0–100）、`confidence`、`top_risks`。
4. **Router** — 裁定为 `PASS` 时：通过 GitHub REST API 创建分支、提交 patch、开 PR。裁定为 `FAIL` 时：记录事件，干净退出。
5. **Hard Stops** — 持久化熔断器，预算超限、连续失败或每日迭代超限时自动暂停引擎。

> **Actor / Judge 隔离是硬性要求。** 两个 LLM 必须使用不同的模型或不同的 API 会话。Judge 永远只看最终 patch，看不到 Actor 的推理过程。这一点在运行时强制校验。

---

## 快速上手

### 1. 安装

```bash
# 克隆并进入目录
git clone <repo-url> && cd evolution-engine

# 创建虚拟环境
python3 -m venv .venv && source .venv/bin/activate

# 安装（含开发依赖）
pip install -e ".[dev]"
```

### 2. 配置环境变量

```bash
# 复制模板
cp examples/.env.example examples/.env

# 编辑 examples/.env，填入真实值：
#   ACTOR_MODEL、ACTOR_API_BASE_URL、ACTOR_API_KEY
#   JUDGE_MODEL、JUDGE_API_BASE_URL、JUDGE_API_KEY
#   GITHUB_REPO（格式：owner/repo）
#   GITHUB_TOKEN（GitHub 个人访问令牌）
```

加载环境变量（`set -a` 确保变量 export 给子进程，Python 才能读到）：

```bash
set -a && source examples/.env && set +a
```

### 3. 校验配置文件

```bash
evolve validate --config examples/evolution.yml
```

退出码为 0 表示 YAML schema 合法。

### 4. 运行一次进化周期

```bash
evolve run --config examples/evolution.yml
```

如果 Judge 通过了 patch，会在 GitHub 上真实创建 PR，并将 PR 链接打印到 stdout。

---

## 配置参考

所有配置集中在一个 `evolution.yml` 文件中。完整示例见 [`examples/evolution.yml`](examples/evolution.yml)。

### `mission`（任务目标）

3–5 句话描述**代码库应该实现什么目标**。Actor 和 Judge 都以此作为最高指令。

```yaml
mission: |
  运营一批 GitHub 账号，持续向真实的优质开源项目贡献有用的 PR。
  账号长期存活并积累真实声誉，避免被平台风控识别。
```

---

### `principles`（审核原则）

Judge 评分每个 patch 时遵循的规则列表，按优先级排序。`priority` 数字越小，优先级越高。Judge 不能为了满足低优先级规则而违反高优先级规则。

```yaml
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
```

---

### `resources`（资源声明）

声明操作资源的路径。路径可以是本地文件路径，也可以用 `env:变量名` 引用环境变量。

```yaml
resources:
  accounts:
    path: "./workspace/resources/accounts.enc.json"
    health_check: "./scripts/check_account_health.sh"
  proxies:
    path: "env:PROXY_PROVIDER_URL"    # 运行时从环境变量读取
    health_check: "./scripts/check_proxy_latency.sh"
  budget:
    daily_usd: 30
    hard_cap_usd: 100
```

> **V1 说明：** `resources` 字段用于 schema 完整性声明。健康检查的主动调用是 V2 功能。

---

### `evidence_sources`（证据来源）

Observer 每个周期读取的数据来源列表，用于构建证据包。支持三种类型：

| 类型 | 示例 | 说明 |
|------|------|------|
| 本地文件 | `"./dashboard/metrics.json"` | 读取并包含文件内容 |
| Shell 脚本 | `"./scripts/status.sh"` | 执行并捕获 stdout |
| 远程 URL | `"https://example.com/api/status"` | HTTP GET 并包含响应体 |

```yaml
evidence_sources:
  - "./dashboard/metrics.json"
  - "./dashboard/config_hint.json"   # 目标文件的当前内容，帮助 Actor 生成精确 diff
  - "./scripts/status.sh"
```

**技巧：** 加入一个 `config_hint.json`，包含你希望 Actor 修改的文件的当前真实内容。这能大幅提升 diff 精度，显著提高 Judge 通过率。

---

### `hard_stops`（熔断器）

持久化熔断器，状态保存在配置文件同级的 `.evolution_state.json` 中。

```yaml
hard_stops:
  budget_hard_cap_usd: 100          # 累计 LLM 花费超限则暂停
  max_consecutive_failures: 5       # 连续 N 次 Judge FAIL 或错误后暂停
  max_iterations_per_day: 20        # 单日运行超过 N 次后暂停
  on_trigger: "halt_and_notify"     # 向 stderr 输出通知并以非零码退出
```

人工排查后解除暂停：

```bash
evolve reset --config examples/evolution.yml
```

`reset` 会同时清除暂停标志**和**连续失败计数器，避免残留计数立即再次触发熔断。

---

### `models`（LLM 配置）

配置 Actor 和 Judge 使用的 LLM。支持两种写法：

**字符串简写**（Anthropic 原生 SDK，key 来自 `ANTHROPIC_API_KEY`）：

```yaml
models:
  actor: "claude-sonnet-4"
  judge: "claude-opus-4"
```

**完整对象形式**（任意 OpenAI 兼容 API）：

```yaml
models:
  actor:
    name: "env:ACTOR_MODEL"              # 运行时从 $ACTOR_MODEL 解析
    api_base_url: "env:ACTOR_API_BASE_URL"
    api_key_env: "ACTOR_API_KEY"         # 持有密钥的环境变量名
  judge:
    name: "env:JUDGE_MODEL"
    api_base_url: "env:JUDGE_API_BASE_URL"
    api_key_env: "JUDGE_API_KEY"
```

`name` 和 `api_base_url` 支持 `env:变量名` 前缀——真实值在调用时才解析，YAML 文件中不含任何密钥。

**常用 `api_base_url`：**

| 服务商 | `api_base_url` |
|--------|---------------|
| OpenAI | `https://api.openai.com/v1` |
| DeepSeek | `https://api.deepseek.com/v1` |
| Groq | `https://api.groq.com/openai/v1` |
| OpenRouter | `https://openrouter.ai/api/v1` |
| Ollama（本地） | `http://localhost:11434/v1` |
| Anthropic 原生 | *（不填 `api_base_url`）* |

> Actor 和 Judge 可以**使用不同的服务商**。建议 Actor 用代码能力强的模型，Judge 用推理能力强的模型。

---

### `github`（PR 目标仓库）

PR 创建的目标仓库。Router 直接调用 GitHub REST API——**无需本地 `git` 或 `gh` 命令行工具**。

```yaml
github:
  repo: "env:GITHUB_REPO"        # "owner/repo" 或 env:变量名
  token_env: "GITHUB_TOKEN"      # 持有 PAT 的环境变量名
  base_branch: "main"            # PR 的目标基础分支
```

GitHub PAT 所需权限：**`repo`**（完整）或至少 **`contents: write`** + **`pull_requests: write`**。

获取 Token：https://github.com/settings/tokens

---

### `safety_mode`（安全模式）

V1 仅支持 `"human_in_the_loop"`。引擎始终创建 PR 供人工审核，不会自动合并。

```yaml
safety_mode: "human_in_the_loop"
```

---

## 环境变量

将 [`examples/.env.example`](examples/.env.example) 复制为 `examples/.env` 并填入真实值：

```bash
cp examples/.env.example examples/.env
# 编辑 examples/.env
set -a && source examples/.env && set +a
```

### LLM 服务商配置

| 变量名 | 说明 |
|--------|------|
| `ACTOR_MODEL` | Actor 使用的模型名（如 `gpt-4o`、`deepseek-chat`） |
| `ACTOR_API_BASE_URL` | Actor 服务商的 API base URL |
| `ACTOR_API_KEY` | Actor 的 API 密钥 |
| `JUDGE_MODEL` | Judge 使用的模型名 |
| `JUDGE_API_BASE_URL` | Judge 服务商的 API base URL |
| `JUDGE_API_KEY` | Judge 的 API 密钥 |
| `ANTHROPIC_API_KEY` | 仅使用 Anthropic 原生 SDK（字符串简写形式）时需要 |

### GitHub Token

| 变量名 | 说明 |
|--------|------|
| `GITHUB_REPO` | 目标仓库，格式 `owner/repo` |
| `GITHUB_TOKEN` | 个人访问令牌，需含 `repo` + `pull_requests` 权限 |

> **安全提示：** 永远不要提交 `examples/.env`。该文件已被 `.gitignore` 排除。只有 `examples/.env.example`（不含真实密钥）应该提交到版本控制。

---

## CLI 命令

```bash
# 校验 evolution.yml schema（退出码 0 表示合法）
evolve validate --config examples/evolution.yml

# 运行一次 Observer → Actor → Judge → Router 周期
evolve run --config examples/evolution.yml

# 开启详细日志模式运行
evolve -v run --config examples/evolution.yml

# 打印 JSONL 格式的进化历史
evolve history --config examples/evolution.yml

# 人工排查后解除熔断暂停
evolve reset --config examples/evolution.yml
```

---

## 项目结构

```
evolution-engine/
├── src/
│   ├── cli.py              # Click CLI 入口（validate / run / history / reset）
│   ├── config_loader.py    # evolution.yml 的 Pydantic schema 校验
│   ├── observer.py         # 证据来源扫描器（文件 / 脚本 / URL）
│   ├── actor.py            # Actor LLM 调用 + 统一 diff 提取
│   ├── judge.py            # 独立 Judge LLM + JSON 裁定解析
│   ├── router.py           # 裁定后分发器（创建 PR 或跳过）
│   ├── github_client.py    # GitHub REST API：blob → tree → commit → branch → PR
│   ├── hard_stops.py       # 持久化熔断器状态机
│   ├── llm.py              # 统一 LLM 调用层（Anthropic 原生 + OpenAI 兼容）
│   └── history.py          # JSONL 历史写入器
├── prompts/
│   ├── actor_system.md     # Actor 系统提示词
│   └── judge_system.md     # Judge 系统提示词
├── tests/
│   └── test_v1.py          # 45 个单元 + 集成测试（Task A 验收套件）
├── examples/
│   ├── evolution.yml                    # 示例配置（ClawOSS 场景）
│   ├── .env.example                     # 环境变量模板（应提交）
│   ├── .env                             # 真实密钥——禁止提交（已 git-ignore）
│   ├── scripts/
│   │   └── status.sh                    # 示例证据脚本
│   └── dashboard/
│       ├── metrics.json                 # 示例指标证据文件
│       └── openclaw_config_hint.json    # 示例配置提示证据文件
├── pyproject.toml
├── .gitignore
├── README.md               # 英文文档
└── README.zh.md            # 本文件（中文）
```

---

## 运行测试

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

所有 45 个 Task A 验收测试应全部通过。无需真实 API 密钥或 GitHub Token——所有 LLM 和 GitHub 调用均已 mock。

运行特定测试组：

```bash
# 仅运行单元测试
python -m pytest tests/ -v -k "TestHardStops or TestConfig or TestRouter"

# 仅运行集成测试
python -m pytest tests/ -v -k "test_full_cycle"
```

---

## 常见问题

**`Error: metrics.json stale: generated_at age Xs > 60s`**

Observer 对 metrics 文件有 60 秒的新鲜度校验。运行前更新文件的 `generated_at` 时间戳，或重新生成该文件。

**`ACTOR_API_KEY is not set`**

用普通 `source .env` 设置的变量不会 export 给子进程，Python 读不到。必须使用：
```bash
set -a && source examples/.env && set +a
```

**`Hard stop triggered: max consecutive failures reached`**

之前的失败运行已在 `.evolution_state.json` 中积累了计数。执行：
```bash
evolve reset --config examples/evolution.yml
```

**`Cannot get branch 'main': Branch not found`**

目标仓库的默认分支不是 `main`。去 GitHub 确认分支名后更新 `evolution.yml`：
```yaml
github:
  base_branch: "v6-release"   # 填写实际的默认分支名
```

**`Cannot access repo 'owner/repo'`**

- 确认 `GITHUB_REPO` 格式为 `owner/repo`（不是完整 URL）。
- 确认 `GITHUB_TOKEN` 拥有 `repo` scope。
- 若仓库为私有，Token 所属账号需有访问权限。

**Judge 裁定总是 `FAIL`**

- 确认 Actor 输出了完整的 diff 块（` ```diff ... ``` `）。
- 添加 `config_hint.json` 证据来源，包含目标文件的当前真实内容，帮助 Actor 生成精确的、上下文匹配的 diff。
- 如果响应被截断，适当增大 Judge 的 `max_tokens`。

---

## 相关文档

| 文件 | 说明 |
|------|------|
| [`PRD_EvolutionEngine.md`](PRD_EvolutionEngine.md) | 产品需求文档 — Task A（本引擎） |
| [`PRD_ClawOSS_Evolvable.md`](PRD_ClawOSS_Evolvable.md) | ClawOSS 可进化改造方案 — Task B |
| [`CONTRACT_metrics_schema.md`](CONTRACT_metrics_schema.md) | 共享数据契约：`metrics.json` schema |
| [`TEST_CASES.md`](TEST_CASES.md) | V1 验收测试用例 |

---

## 许可证

Apache 2.0
