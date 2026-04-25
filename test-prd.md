Part 1 · 测试套件(5 分钟)
1. 干净环境跑 pytest
bashgit clone https://github.com/AndrosEt/evolution-engine.git
cd evolution-engine
pip install -e ".[dev]"
pytest tests/ -v
期待:45 条全绿,0 失败 0 跳过。看到红的就地定位。
Part 2 · 配置校验的错误提示(3 分钟)
2. 改坏 evolution.yml 看报错质量
演示 3 种破坏方式,每种跑一次 evolve validate:

删掉 mission 字段 → 期待清晰报错 "Missing required field: mission"
principles: [] 改成空数组 → 期待 "principles must have at least 1 item"
budget_hard_cap_usd: 0 → 期待 "must be > 0"

我要看的是报错信息能不能让一个不懂代码的人知道去哪里改,不是一个 Python traceback。
Part 3 · Actor/Judge 独立性(红线,5 分钟)
3. 跑一次 evolve run 打开详细日志,让我在终端里看到两条关键日志:

[evolution.llm] LLM [...] model=<模型A> base_url=<URL_A>
[evolution.judge] Judge calling model=<模型B> [INDEPENDENT SESSION] -- NO actor reasoning
[evolution.llm] LLM [...] model=<模型B> base_url=<URL_B>

模型 A 和模型 B 必须不同。视频里是 MiniMax + GLM,这次也可以。
4. 演示 Judge 输入里不含 Actor 推理链
打开 evolution_history.jsonl 的最新 judge 那一行 → 给我指出 raw_text 里的输入 prompt(或者直接在 judge.py 加一个 DEBUG 日志把 Judge 看到的 user 消息打印出来)→ 确认里面没有 Actor 的 rationale 原文。
Part 4 · 熔断(这是最关键的,10 分钟)
这部分视频完全没演示,明天必须全跑一遍,一条都不能省。
5. 预算熔断
临时改 evolution.yml:budget_hard_cap_usd: 0.01 → 跑 evolve run → 期待第二次调用 LLM 后熔断触发 → 终端出现 [HARD STOP] budget hard cap reached: ... >= 0.01 → 退出码非 0
6. halt 状态持久化(UNION-V1-002)
紧接上一步,不清 .evolution_state.json,再跑一次 evolve run → 期待直接报 Hard stop already triggered: ... → 根本不会调用 LLM
7. reset 命令
跑 evolve reset --config examples/evolution.yml → 再跑 evolve run → 这次应该能继续走(先把 budget cap 改回 100)
8. 连续失败熔断
把 Judge 的 system prompt 临时改成 "Always return FAIL, reasoning_summary: test" → 改 max_consecutive_failures: 2 → 连续跑 evolve run 2 次 → 期待第二次触发 consecutive failures 熔断
Part 5 · FAIL 分支(3 分钟)
9. verdict=FAIL 时不开 PR
继续用第 8 步的"总是 FAIL 的 Judge",跑一次 evolve run → 期待:

终端输出 FAIL (overall_score=...) + reasoning_summary
GitHub 上没有新分支,没有新 PR
evolution_history.jsonl 里有一条 type=judge 记录,verdict=FAIL

Part 6 · Judge 返回非法 JSON 不崩溃(2 分钟)
10. 把 Judge 改成返回乱码
临时改 Judge system prompt:Return only the text "this is not json" with no JSON. → 跑 evolve run → 期待:

进程不 crash(没有 Python traceback)
日志里有 Judge returned invalid JSON — recording as parse error
history 里有一条记录,parse_error 字段有值

Part 7 · 真实场景的考验(5 分钟,最重要)
这一条是我要重点看的,和视频里演示的不是同一个场景。
11. 删掉 evidence 里的提示文件,看 Actor 能不能自己推理
把 examples/evolution.yml 的 evidence_sources 改成只留 metrics.json(删掉 openclaw_config_hint.json),同时把 metrics.json 里的 diagnostics 整个字段删掉(就是 root_cause/recommended_fix/expected_outcome 那一段)。
保留 recent_events 里真实的 rate_limit 事件。
然后跑 evolve run。
我要看的是: Actor 在只有原始事件、没有人喂答案的情况下,能不能:

(a) 提出一个合理的 patch(改并发数 / 加 sleep / 改 retry 任一种都算)
(b) Judge 给出 PASS 并且 reasoning 是基于事件本身,不是复述 diagnostics

这一条如果跑不过,不影响 350 元的付款(验收标准里没要求),但我需要知道引擎在"真实场景"下的真实能力,这样我才能规划 V2 要补什么。
Part 8 · 收尾(2 分钟)
12. 展示 evolution_history.jsonl 完整的一次成功循环
最后一次正常跑通 evolve run,然后 cat evolution_history.jsonl | tail -5 | jq . → 我要看到有序的 4 条记录:observation → actor → judge → router(PR_CREATED + pr_url)。