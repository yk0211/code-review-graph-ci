#!/usr/bin/env python3
"""CI AI Review runner with code-review-graph context.

流程：
1. 用 code-review-graph 构建/更新代码结构图谱。
2. 用 code-review-graph detect-changes 获取变更影响、风险函数、测试缺口等上下文。
3. 把图谱上下文 + 关键 diff 发给 Codex provider（NEW API 等 OpenAI 兼容中转）生成中文审查意见。
4. 通过 Lark 机器人通知。

环境变量：
  CODEX_API_KEY        - Codex provider 的 API Key（缺省回退 OPENAI_API_KEY）
                         NEW API 填平台令牌（sk- 开头），不是上游厂商的 key
  CODEX_BASE_URL       - Codex provider 的 OpenAI 兼容根地址，默认 https://api.openai.com/v1
                         （缺省回退 OPENAI_BASE_URL）
                         NEW API 填 https://<你的NEW API域名>/v1
  CODEX_API_MODE       - auto（默认）/ chat / responses
                         auto 会先试 /v1/responses，失败再试 /v1/chat/completions
  CODEX_MODEL          - 模型名，默认 gpt-5-codex
                         NEW API 要填平台「模型列表」里存在的名字（别名也算），
                         可用 `curl $CODEX_BASE_URL/models -H "Authorization: Bearer $CODEX_API_KEY"` 确认
  CODEX_MAX_TOKENS     - 最大输出 token，默认 8192
  CODEX_TOKEN_FIELD    - token 上限字段名：max_tokens / max_completion_tokens，默认 max_tokens
  CODEX_REASONING_EFFORT - 可选，推理强度（low/medium/high），未设置则不下发
  CODEX_TEMPERATURE    - 可选，未设置则不下发（部分推理模型不接受该参数）
  CODEX_TIMEOUT        - 请求超时秒数，默认 600
  MAX_PROMPT_CHARS     - 发给模型的总字符上限，默认 600000
  MAX_CONTEXT_CHARS    - 图谱上下文字符上限，默认 100000（超出截断，给 diff 留预算）
  LARK_WEBHOOK_URL     - Lark 群机器人 webhook
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import requests

CODEX_MODEL = os.environ.get("CODEX_MODEL", "gpt-5-codex")
CODEX_BASE_URL = (
    os.environ.get("CODEX_BASE_URL")
    or os.environ.get("OPENAI_BASE_URL")
    or "https://api.openai.com/v1"
).rstrip("/")
CODEX_API_KEY = os.environ.get("CODEX_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
CODEX_API_MODE = os.environ.get("CODEX_API_MODE", "auto").strip().lower()
CODEX_CHAT_URL = f"{CODEX_BASE_URL}/chat/completions"
CODEX_RESPONSES_URL = f"{CODEX_BASE_URL}/responses"
CODEX_TIMEOUT = int(os.environ.get("CODEX_TIMEOUT", "600"))
CODEX_MAX_TOKENS = int(os.environ.get("CODEX_MAX_TOKENS", "8192"))
CODEX_TOKEN_FIELD = os.environ.get("CODEX_TOKEN_FIELD", "max_tokens")
CODEX_REASONING_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "")
CODEX_TEMPERATURE = os.environ.get("CODEX_TEMPERATURE", "")
LARK_WEBHOOK_URL = os.environ.get("LARK_WEBHOOK_URL", "")
LARK_MAX_CHARS = 20_000
MAX_PROMPT_CHARS = int(os.environ.get("MAX_PROMPT_CHARS", "600000"))
# 图谱 JSON 往往很大（数百个函数），先按上限截断，剩余预算留给 diff
MAX_CONTEXT_CHARS = int(os.environ.get("MAX_CONTEXT_CHARS", "100000"))
ZERO_SHA = "0" * 40


class _CodexCallError(Exception):
    """Codex provider 调用失败（可切换到另一个端点重试）。"""


EMPTY_TREE_SHA = subprocess.run(
    ["git", "hash-object", "-t", "tree", "--stdin"],
    input="", capture_output=True, text=True, encoding="utf-8",
).stdout.strip()

EXCLUDE_SPECS = [
    ":(exclude)*.pb.cc",
    ":(exclude)*.pb.h",
    ":(exclude)*.proto",
]


def run_git(args: list[str]) -> str:
    result = subprocess.run(
        ["git"] + args, capture_output=True, text=True, encoding="utf-8"
    )
    return result.stdout.strip()


def run_cmd(args: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", cwd=cwd
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
    return result


def detect_default_branch() -> str:
    ref = run_git(["symbolic-ref", "refs/remotes/origin/HEAD"])
    if ref:
        return ref.removeprefix("refs/remotes/origin/")
    for name in ("main", "master"):
        if run_git(["rev-parse", "--verify", f"origin/{name}"]):
            return name
    return ""


def resolve_base_sha(local_sha: str, remote_sha: str) -> str:
    """返回 code-review-graph 可用的 base ref。"""
    if remote_sha != ZERO_SHA:
        return remote_sha

    base_branch = (
        os.environ.get("CI_DEFAULT_BRANCH", "")
        or os.environ.get("GITHUB_BASE_REF", "")
        or detect_default_branch()
    )
    if base_branch:
        merge_base = run_git(["merge-base", local_sha, f"origin/{base_branch}"])
        if merge_base:
            return merge_base
    return EMPTY_TREE_SHA


def get_diff(local_sha: str, base_sha: str) -> str:
    if base_sha == EMPTY_TREE_SHA:
        return run_git(["diff", EMPTY_TREE_SHA, local_sha, "--"] + EXCLUDE_SPECS)
    return run_git(["diff", f"{base_sha}..{local_sha}", "--"] + EXCLUDE_SPECS)


def get_commit_log(local_sha: str, base_sha: str) -> str:
    repo_root = run_git(["rev-parse", "--show-toplevel"])
    # CI runner 的工作目录通常是 /build，basename 不能当项目名
    project = (
        os.environ.get("CI_PROJECT_NAME", "")
        or os.environ.get("GITHUB_REPOSITORY", "")
        or os.environ.get("JOB_NAME", "")
        or os.path.basename(repo_root)
    )
    branch = (
        os.environ.get("CI_COMMIT_REF_NAME", "")
        or os.environ.get("GITHUB_REF_NAME", "")
        or os.environ.get("GIT_BRANCH", "")
        or os.environ.get("BRANCH_NAME", "")
        or run_git(["branch", "--show-current"])
    )
    header = f"项目: {project}\n分支: {branch}\n"
    commit_header = "详情:\n提交     作者     描述\n"
    fmt = "%h       %an     %s"
    if base_sha == EMPTY_TREE_SHA:
        log = run_git(["log", f"--format={fmt}", local_sha, "--not", "--remotes"])
    else:
        log = run_git(["log", f"--format={fmt}", f"{base_sha}..{local_sha}"])
    return header + commit_header + log


def ensure_crg() -> None:
    """确保 code-review-graph CLI 可用。"""
    result = subprocess.run(
        ["code-review-graph", "--version"],
        capture_output=True, text=True, encoding="utf-8"
    )
    if result.returncode != 0:
        print("[CRG] code-review-graph not found, installing ...")
        run_cmd([sys.executable, "-m", "pip", "install", "code-review-graph>=2.3.6"])


def build_graph(repo_root: str, base_sha: str) -> None:
    """基于 base..HEAD 增量更新图谱；失败则全量 build。"""
    print(f"[CRG] Updating graph against base {base_sha[:8]} ...")
    try:
        run_cmd(["code-review-graph", "update", "--base", base_sha], cwd=repo_root)
    except RuntimeError as exc:
        print(f"[CRG] update failed, falling back to build: {exc}")
        run_cmd(["code-review-graph", "build"], cwd=repo_root)


def get_graph_context(repo_root: str, base_sha: str) -> str:
    """获取 code-review-graph 变更影响分析文本。"""
    print(f"[CRG] Analyzing changes against base {base_sha[:8]} ...")
    result = run_cmd(
        ["code-review-graph", "detect-changes", "--base", base_sha],
        cwd=repo_root, check=False,
    )
    if result.returncode != 0:
        print(f"[CRG] detect-changes failed: {result.stderr}")
        return ""
    return result.stdout


def build_prompt(context: str, commit_log: str, diff: str) -> tuple[str, str]:
    """返回 (system_prompt, user_content)。"""
    # 图谱上下文先截到固定上限，剩余预算再给 diff，避免总量失控
    if len(context) > MAX_CONTEXT_CHARS:
        context = context[:MAX_CONTEXT_CHARS] + "\n... (图谱上下文已截断)"
    header = (
        f"提交记录：\n{commit_log}\n\n"
        f"代码图谱分析（变更影响、风险函数、测试缺口）：\n```\n{context}\n```\n\n"
        f"关键代码差异：\n```diff\n"
    )
    footer = "\n```"
    budget = max(MAX_PROMPT_CHARS - len(header) - len(footer), 0)
    if len(diff) > budget:
        diff = diff[:budget] + "\n\n... (diff 已截断，超过字符限制)"
    combined = header + diff + footer

    system_prompt = (
        "你是一个资深代码审查助手。请基于下方的代码图谱分析和关键代码差异，"
        "用中文简要审查本次变更，重点指出：1) 潜在 Bug 或逻辑错误 2) 安全风险 "
        "3) 性能问题 4) 可读性/可维护性改进建议。如果没有发现问题，也要说明。"
        "回复请简洁有条理，最大 2048 字符。"
    )
    return system_prompt, combined


def _post(url: str, payload: dict) -> dict:
    headers = {
        "Authorization": f"Bearer {CODEX_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=CODEX_TIMEOUT)
    except requests.RequestException as e:
        raise _CodexCallError(f"请求异常：{e}") from e

    if resp.status_code == 200:
        try:
            return resp.json()
        except ValueError as e:
            raise _CodexCallError(f"响应不是合法 JSON：{e}") from e
    raise _CodexCallError(f"HTTP {resp.status_code}：{resp.text[:300]}")


def _request_chat(system_prompt: str, user_content: str) -> str:
    """OpenAI 兼容 /v1/chat/completions（NEW API 默认走这个）。"""
    payload = {
        "model": CODEX_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        CODEX_TOKEN_FIELD: CODEX_MAX_TOKENS,
    }
    # 可选参数：不设置就不下发，避免部分 provider 报 unsupported parameter
    if CODEX_REASONING_EFFORT:
        payload["reasoning_effort"] = CODEX_REASONING_EFFORT
    if CODEX_TEMPERATURE:
        try:
            payload["temperature"] = float(CODEX_TEMPERATURE)
        except ValueError:
            print(f"[AI Review] 忽略非法 CODEX_TEMPERATURE: {CODEX_TEMPERATURE}")

    try:
        data = _post(CODEX_CHAT_URL, payload)
    except _CodexCallError as e:
        # 推理模型（gpt-5-codex 等）只接受 max_completion_tokens，传 max_tokens 会 400，自动回退重试一次
        if CODEX_TOKEN_FIELD == "max_tokens" and "max_tokens" in str(e):
            print("[AI Review] retry with max_completion_tokens ...")
            payload.pop("max_tokens", None)
            payload["max_completion_tokens"] = CODEX_MAX_TOKENS
            data = _post(CODEX_CHAT_URL, payload)
        else:
            raise

    choices = data.get("choices") or []
    if not choices:
        raise _CodexCallError(f"缺少 choices 字段：{json.dumps(data)[:300]}")

    message = choices[0].get("message") or {}
    content = message.get("content") or message.get("reasoning_content") or ""
    if isinstance(content, list):
        content = "".join(
            part.get("text", "") for part in content if isinstance(part, dict)
        )
    content = content.strip()
    if not content:
        raise _CodexCallError("返回空内容")
    return content


def _request_responses(system_prompt: str, user_content: str) -> str:
    """Codex 原生 /v1/responses（部分 NEW API 的 codex 通道只暴露这个）。"""
    payload = {
        "model": CODEX_MODEL,
        "instructions": system_prompt,
        "input": user_content,
        "max_output_tokens": CODEX_MAX_TOKENS,
        "store": False,
    }
    if CODEX_REASONING_EFFORT:
        payload["reasoning"] = {"effort": CODEX_REASONING_EFFORT}

    data = _post(CODEX_RESPONSES_URL, payload)

    status = data.get("status")
    if status in ("failed", "incomplete") and data.get("error"):
        raise _CodexCallError(f"status={status}：{json.dumps(data)[:300]}")

    text = data.get("output_text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    parts: list[str] = []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("text"):
                parts.append(part["text"])

    content = "".join(parts).strip()
    if not content:
        raise _CodexCallError(
            f"解析不到输出：{json.dumps(data)[:300]}"
        )
    if status == "incomplete":
        content += "\n\n（提示：模型输出被 max_output_tokens 截断）"
    return content


def call_codex(context: str, commit_log: str, diff: str) -> str:
    if not CODEX_API_KEY:
        return "**错误**：未设置 CODEX_API_KEY 环境变量"

    system_prompt, user_content = build_prompt(context, commit_log, diff)

    modes = {"chat": _request_chat, "responses": _request_responses}
    if CODEX_API_MODE in modes:
        order = [CODEX_API_MODE]
    else:
        # auto：先试 codex 原生 responses，再退到 chat/completions
        order = ["responses", "chat"]

    errors: list[str] = []
    for mode in order:
        try:
            print(f"[AI Review] try {mode} endpoint ...")
            result = modes[mode](system_prompt, user_content)
            print(f"[AI Review] {mode} endpoint OK ({len(result)} chars)")
            return result
        except _CodexCallError as e:
            print(f"[AI Review] {mode} endpoint failed: {e}")
            errors.append(f"- **{mode}**：{e}")
        except (ValueError, KeyError, IndexError, TypeError) as e:
            errors.append(f"- **{mode}** 响应解析失败：{e}")

    return "**Codex 调用失败**（已尝试的模式）：\n" + "\n".join(errors)


def send_to_lark(text: str, commit_log: str) -> None:
    if not LARK_WEBHOOK_URL:
        print("[AI Review] LARK_WEBHOOK_URL not set, skipping Lark notification.")
        return
    msg = f"{commit_log}\n\n{text}"
    if len(msg) > LARK_MAX_CHARS:
        msg = msg[:LARK_MAX_CHARS] + "\n... (内容过长已截断)"
    payload = {
        "msg_type": "text",
        "content": {"text": msg},
    }
    try:
        resp = requests.post(LARK_WEBHOOK_URL, json=payload, timeout=10)
        if resp.status_code != 200 or resp.json().get("code"):
            print(
                f"[AI Review] Lark 返回异常：HTTP {resp.status_code} {resp.text[:300]}"
            )
    except requests.RequestException as e:
        print(f"[AI Review] Failed to send to Lark: {e}")


def main() -> None:
    if len(sys.argv) < 3 or len(sys.argv) % 2 != 1:
        print(
            "Usage: review.py <local1> <remote1> [<local2> <remote2> ...]",
            file=sys.stderr,
        )
        sys.exit(0)

    if not CODEX_API_KEY:
        print(
            "[AI Review] ERROR: CODEX_API_KEY (or OPENAI_API_KEY) is not set.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not LARK_WEBHOOK_URL:
        print("[AI Review] ERROR: LARK_WEBHOOK_URL is not set.", file=sys.stderr)
        sys.exit(1)

    repo_root = run_git(["rev-parse", "--show-toplevel"])
    ensure_crg()

    all_contexts: list[str] = []
    all_diffs: list[str] = []
    all_logs: list[str] = []

    for i in range(1, len(sys.argv), 2):
        local_sha = sys.argv[i]
        remote_sha = sys.argv[i + 1]
        base_sha = resolve_base_sha(local_sha, remote_sha)

        print(f"[AI Review] Range {base_sha[:8]}..{local_sha[:8]}")
        build_graph(repo_root, base_sha)
        context = get_graph_context(repo_root, base_sha)
        diff = get_diff(local_sha, base_sha)
        commit_log = get_commit_log(local_sha, base_sha)

        if diff.strip() or context.strip():
            all_contexts.append(context)
            all_diffs.append(diff)
            all_logs.append(commit_log)

    if not all_diffs and not all_contexts:
        print("[AI Review] No diff or graph context found, skipping.")
        sys.exit(0)

    combined_context = "\n\n---\n\n".join(all_contexts)
    combined_diff = "\n\n---\n\n".join(all_diffs)
    combined_log = "\n".join(all_logs)

    print(
        f"[AI Review] Aggregated {len(all_contexts)} range(s). "
        f"Calling Codex provider {CODEX_BASE_URL} model={CODEX_MODEL} mode={CODEX_API_MODE} ..."
    )
    review_result = call_codex(combined_context, combined_log, combined_diff)

    print("[AI Review] Sending result to Lark ...")
    send_to_lark(review_result, combined_log)
    print("[AI Review] Done.")

    sys.exit(0)


if __name__ == "__main__":
    main()
