#!/usr/bin/env python3
"""test_prd_11.py — test-prd.md Part 7, 第 11 条

真实场景考验：删掉 diagnostics 提示后，Actor 在只有原始事件的情况下
能否自主推理出合理 patch，且 Judge 基于事件本身给出 PASS。

用法（在 evolution-engine 根目录下，已激活 venv）：
    python tests/test_prd_11.py
    python tests/test_prd_11.py --verbose        # 打印完整 history
    python tests/test_prd_11.py --keep-tmp       # 运行后保留临时目录方便检查
    python tests/test_prd_11.py --config /path/to/other/evolution.yml

前置条件：
    - examples/.env 已填入 ACTOR_* / JUDGE_* / GITHUB_* 环境变量
    - pip install -e ".[dev]" 已执行（venv 中有 evolve 命令）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ── 路径常量 ─────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent          # evolution-engine/
EXAMPLES_DIR = REPO_ROOT / "examples"
BASE_CONFIG = EXAMPLES_DIR / "evolution.yml"
BASE_METRICS = EXAMPLES_DIR / "dashboard" / "metrics.json"
PROMPTS_DIR = REPO_ROOT / "prompts"

# ── 判断标准 ──────────────────────────────────────────────────────────────────
# (a) patch 或 rationale 里出现以下任意关键词视为"合理 patch"
PATCH_KEYWORDS = [
    "concurrent", "concurren",   # maxConcurrent / concurrency
    "max_workers", "max_parallel",
    "sleep", "jitter",
    "retry", "backoff",
    "rate_limit", "ratelimit",
    "interval", "throttl",
    "429",
]

# (b) reasoning_summary 里若逐字出现以下片段，则认为是在"照抄 diagnostics"
DIAGNOSTICS_VERBATIM = [
    "hard-coded fallback concurrency",
    "unconstrained subagent spawning",
    "Create config/tunable.yml with conservative",
    "concurrency.max_parallel_accounts=2",
    "rate_limits.max_prs_per_account_per_day=1",
    "behavior.sleep_jitter_min_sec=30",
]


# ── 工具函数 ──────────────────────────────────────────────────────────────────

def load_dotenv(path: Path) -> None:
    """从 .env 文件加载环境变量（覆盖已存在的，确保 .env 为准）。"""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key:
            os.environ[key] = val


def strip_diagnostics(metrics: dict) -> dict:
    """返回去掉顶层 diagnostics 字段的新 metrics dict（不修改原始）。"""
    import copy
    m = copy.deepcopy(metrics)
    m.pop("diagnostics", None)
    # 同时清除 recent_events 每条 details 里的预设提示（若存在）
    for evt in m.get("recent_events", []):
        d = evt.get("details", {})
        for hint_key in ("root_cause", "recommended_fix", "expected_outcome",
                         "diagnostic_hint", "fix_hint"):
            d.pop(hint_key, None)
    return m


def find_evolve_bin() -> Path:
    """返回当前 venv 中的 evolve 可执行文件路径。"""
    # sys.executable → .venv/bin/python  →  .venv/bin/evolve
    venv_bin = Path(sys.executable).parent
    for name in ("evolve", "evolve.exe"):
        p = venv_bin / name
        if p.exists():
            return p
    # 后备：尝试 PATH
    found = shutil.which("evolve")
    if found:
        return Path(found)
    raise FileNotFoundError(
        "找不到 evolve 命令。请先运行：pip install -e '.[dev]'"
    )


def check_patch_reasonable(patch: str, rationale: str) -> tuple[bool, str]:
    text = (patch + "\n" + rationale).lower()
    for kw in PATCH_KEYWORDS:
        if kw.lower() in text:
            return True, kw
    return False, ""


def check_reasoning_independent(reasoning: str) -> tuple[bool, str]:
    lower = reasoning.lower()
    for frag in DIAGNOSTICS_VERBATIM:
        if frag.lower() in lower:
            return False, frag
    return True, ""


def _ok(label: str, detail: str = "") -> None:
    print(f"  ✅  {label}" + (f"  ({detail})" if detail else ""))


def _fail(label: str, detail: str = "") -> None:
    print(f"  ❌  {label}" + (f"\n      {detail}" if detail else ""))


# ── 主逻辑 ────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="test-prd.md Item 11 — 真实 LLM 场景测试"
    )
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="打印完整 stdout / stderr 及 history")
    parser.add_argument("--keep-tmp", action="store_true",
                        help="运行完毕保留临时目录（方便检查 history）")
    parser.add_argument("--config", default=str(BASE_CONFIG),
                        help="基础 evolution.yml（默认 examples/evolution.yml）")
    args = parser.parse_args()

    base_cfg_path = Path(args.config)
    dotenv_path = base_cfg_path.parent / ".env"

    # 1. 加载 .env
    load_dotenv(dotenv_path)
    print(f"[setup] .env loaded from: {dotenv_path}")
    print(f"[setup] ACTOR_MODEL={os.environ.get('ACTOR_MODEL', '(unset)')}")
    print(f"[setup] JUDGE_MODEL={os.environ.get('JUDGE_MODEL', '(unset)')}")

    # 2. 确认 evolve 命令可用
    try:
        evolve_bin = find_evolve_bin()
        print(f"[setup] evolve: {evolve_bin}")
    except FileNotFoundError as e:
        print(f"[ERROR] {e}")
        return 1

    # 3. 建立临时工作目录
    tmp = Path(tempfile.mkdtemp(prefix="evo_test11_"))
    print(f"[setup] tmp workspace: {tmp}")

    try:
        # --- prompts/ ---
        shutil.copytree(str(PROMPTS_DIR), str(tmp / "prompts"))

        # --- dashboard/metrics.json（去掉 diagnostics）---
        (tmp / "dashboard").mkdir()
        raw_metrics = json.loads(BASE_METRICS.read_text(encoding="utf-8"))
        clean_metrics = strip_diagnostics(raw_metrics)
        (tmp / "dashboard" / "metrics.json").write_text(
            json.dumps(clean_metrics, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print("[setup] metrics.json: diagnostics 字段已移除")
        print(f"        保留 recent_events: {len(clean_metrics.get('recent_events', []))} 条")

        # --- evolution.yml（只留 metrics.json 作为 evidence_source）---
        import yaml
        raw_cfg = yaml.safe_load(base_cfg_path.read_text(encoding="utf-8"))
        raw_cfg["evidence_sources"] = ["./dashboard/metrics.json"]
        # 给测试留一点预算空间，避免误触发熔断
        raw_cfg.setdefault("hard_stops", {})
        raw_cfg["hard_stops"]["budget_hard_cap_usd"] = max(
            float(raw_cfg["hard_stops"].get("budget_hard_cap_usd", 5)), 2.0
        )
        raw_cfg["hard_stops"]["max_iterations_per_day"] = max(
            int(raw_cfg["hard_stops"].get("max_iterations_per_day", 20)), 5
        )
        cfg_path = tmp / "evolution.yml"
        cfg_path.write_text(yaml.dump(raw_cfg, allow_unicode=True), encoding="utf-8")
        print("[setup] evolution.yml: evidence_sources = [./dashboard/metrics.json]")

        # 4. 运行 evolve run
        print()
        print("[run] evolve run --config tmp/evolution.yml  (真实 LLM 调用，请稍候…)")
        print()
        proc = subprocess.run(
            [str(evolve_bin), "run", "--config", str(cfg_path)],
            capture_output=True,
            text=True,
            env=os.environ,
        )

        if args.verbose:
            print("── STDOUT ──")
            print(proc.stdout or "(empty)")
            print("── STDERR ──")
            print(proc.stderr or "(empty)")
            print()

        # 5. 解析 history
        history_path = tmp / "evolution_history.jsonl"
        if not history_path.exists():
            print("[ERROR] evolution_history.jsonl 不存在，引擎可能崩溃")
            if not args.verbose:
                print("STDERR:", proc.stderr[-1500:] if proc.stderr else "(empty)")
            return 1

        events = [
            json.loads(line)
            for line in history_path.read_text(encoding="utf-8").strip().splitlines()
            if line.strip()
        ]
        by_type = {e["type"]: e["payload"] for e in events}

        print("─" * 60)
        print("结果检查")
        print("─" * 60)

        all_pass = True

        # ── Check (a): Actor 提出合理 patch ──────────────────────────────────
        actor_payload = by_type.get("actor")
        if actor_payload is None:
            _fail("(a) actor 记录不存在")
            all_pass = False
        else:
            patch = actor_payload.get("patch", "")
            rationale = actor_payload.get("rationale", "")
            ok, kw = check_patch_reasonable(patch, rationale)
            if ok:
                _ok(f"(a) Actor 提出了合理 patch", f"命中关键词: {kw!r}")
            else:
                _fail("(a) Actor 未提出预期 patch（无并发/sleep/retry 相关修改）",
                      f"patch preview: {patch[:200]!r}")
                all_pass = False

            if args.verbose:
                print()
                print("    [Actor patch]")
                print(patch[:800] or "(empty)")
                print("    [Actor rationale]")
                print(rationale[:400] or "(empty)")
                print()

        # ── Check (b1): Judge 给出 PASS ──────────────────────────────────────
        judge_payload = by_type.get("judge")
        if judge_payload is None:
            # 区分：API 连接失败 vs 引擎逻辑未调用 Judge
            judge_err = by_type.get("judge_error")
            if judge_err:
                err_msg = judge_err.get("error", "")
                if "APIConnectionError" in err_msg or "Connection error" in err_msg:
                    _fail("(b) Judge API 连接失败（网络/端点问题，非引擎逻辑缺陷）",
                          err_msg)
                    print("      ⚠️  重新运行或切换 JUDGE_MODEL 端点后再验证第 11 条")
                else:
                    _fail("(b) Judge 调用出错", err_msg)
            else:
                _fail("(b) judge 记录不存在（引擎可能在 Judge 之前就已退出）")
            all_pass = False
        else:
            parse_error = judge_payload.get("parse_error")
            if parse_error:
                _fail("(b1) Judge JSON 解析失败", parse_error)
                all_pass = False
            else:
                verdict_obj = judge_payload.get("verdict", {})
                verdict = verdict_obj.get("verdict")
                score = verdict_obj.get("overall_score")
                confidence = verdict_obj.get("confidence")
                reasoning = verdict_obj.get("reasoning_summary", "")

                if verdict == "PASS":
                    _ok(f"(b1) Judge verdict = PASS",
                        f"score={score}, confidence={confidence}")
                else:
                    _fail(f"(b1) Judge verdict = {verdict}（期望 PASS）",
                          f"reasoning: {reasoning[:200]}")
                    all_pass = False

                # ── Check (b2): reasoning 基于事件本身，非照抄 diagnostics ──
                indep, bad = check_reasoning_independent(reasoning)
                if indep:
                    _ok("(b2) reasoning_summary 基于事件推理（未照抄 diagnostics）")
                else:
                    _fail("(b2) reasoning_summary 疑似直接复述 diagnostics 文本",
                          f"匹配片段: {bad!r}")
                    # 仅警告，不算失败（diagnostics 已删，若还能匹配说明 LLM 自行推理出了相同结论）
                    print("        ⚠️  注意：diagnostics 字段已删除，若 LLM 推理结论与之相同属正常")

                if args.verbose:
                    print()
                    print("    [Judge reasoning_summary]")
                    print(reasoning)
                    print()

        # ── 汇总 ─────────────────────────────────────────────────────────────
        print()
        print("─" * 60)
        if all_pass:
            print("✅  test-prd.md Item 11  PASSED")
        else:
            print("❌  test-prd.md Item 11  FAILED")
        print("─" * 60)

        if args.verbose or not all_pass:
            print()
            print("── 完整 history ──")
            for e in events:
                print(json.dumps(e, ensure_ascii=False, indent=2))

        if args.keep_tmp:
            print(f"\n[info] 临时目录保留于: {tmp}")
        return 0 if all_pass else 1

    finally:
        if not args.keep_tmp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
