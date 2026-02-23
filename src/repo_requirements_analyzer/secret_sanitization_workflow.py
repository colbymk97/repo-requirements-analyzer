from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from agents import Agent, set_default_openai_api
from agents.mcp import MCPServerManager, MCPServerStdio, create_static_tool_filter

from .code_agent import run_code_agent
from .main import configure_openai_client_from_env, run_with_retries


def clone_repo_fresh(repo: str, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    clone_path = target_dir / "repo"
    cmd = ["git", "clone", "--depth", "1", repo, str(clone_path)]
    completed = subprocess.run(cmd, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"Failed to clone repository: {completed.stderr or completed.stdout}")
    return clone_path.resolve()


def _detect_api_mode(using_azure: bool) -> str:
    api_mode = os.environ.get("OPENAI_API_MODE", "").strip().lower()
    if not api_mode and using_azure:
        api_mode = "responses"
    if not api_mode:
        api_mode = "responses"

    if api_mode == "chat_completions":
        set_default_openai_api("chat_completions")
    elif api_mode == "responses":
        set_default_openai_api("responses")
    else:
        raise ValueError("OPENAI_API_MODE must be 'responses' or 'chat_completions'.")
    return api_mode


def build_review_mcp_servers(repo_path: Path) -> list[MCPServerStdio]:
    project_root = Path(__file__).resolve().parents[2]
    fs_server_path = (
        project_root / ".mcp-node" / "node_modules" / "@modelcontextprotocol" / "server-filesystem" / "dist" / "index.js"
    )
    git_server_path = (
        project_root / ".mcp-node" / "node_modules" / "@cyanheads" / "git-mcp-server" / "dist" / "index.js"
    )
    if not fs_server_path.exists() or not git_server_path.exists():
        raise FileNotFoundError(
            "MCP servers are not installed. Install with: cd .mcp-node && npm install @modelcontextprotocol/server-filesystem @cyanheads/git-mcp-server"
        )

    repo_abs = str(repo_path.resolve())
    runtime_path = os.environ.get("PATH", "").strip() or "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    git_env = dict(os.environ)
    git_env.update(
        {
            "MCP_TRANSPORT_TYPE": "stdio",
            "MCP_LOG_LEVEL": "error",
            "NODE_ENV": "production",
            "LOGS_DIR": str((project_root / ".agent-workspace" / "mcp-git-logs").resolve()),
            "GIT_BASE_DIR": repo_abs,
            "PATH": runtime_path,
        }
    )
    fs_tool_filter = create_static_tool_filter(
        allowed_tool_names=[
            "read_text_file",
            "read_file",
            "read_multiple_files",
            "search_files",
            "list_directory",
            "get_file_info",
        ]
    )
    git_tool_filter = create_static_tool_filter(
        allowed_tool_names=[
            "git_set_working_dir",
            "git_status",
            "git_diff",
            "git_show",
            "git_log",
        ]
    )
    return [
        MCPServerStdio(
            name="filesystem",
            params={
                "command": "node",
                "args": [str(fs_server_path), repo_abs],
                "cwd": str(project_root),
            },
            cache_tools_list=True,
            tool_filter=fs_tool_filter,
        ),
        MCPServerStdio(
            name="git",
            params={
                "command": "node",
                "args": [str(git_server_path)],
                "cwd": repo_abs,
                "env": git_env,
            },
            cache_tools_list=True,
            tool_filter=git_tool_filter,
        ),
    ]


def build_review_prompt(repo_path: Path) -> str:
    return f"""You are a security-focused repository review agent.

Repository:
- Path: {repo_path}

Task:
- Investigate the repository for hardcoded secrets, credentials, tokens, and sensitive connection strings.
- Use MCP tools only (filesystem + git). Do not edit files.
- Prioritize high-confidence findings.
- Redact values in any snippet. Never output full secret values.

Execution requirements:
- Call `git_set_working_dir` first.
- Use `search_files`, directory listing, and targeted file reads.
- Focus on config files, DB connectors, auth modules, env examples, and deployment configs.
- Distinguish between real secrets and placeholders.

Output format (strict JSON only):
{{
  "stack_assessment": ["php", "node", "..."],
  "findings": [
    {{
      "path": "relative/path",
      "line": 123,
      "confidence": "high|medium|low",
      "kind": "db_password|api_key|token|connection_string|other",
      "evidence_redacted": "short redacted code line",
      "recommended_env_var": "DB_PASSWORD",
      "recommended_fix": "one sentence"
    }}
  ],
  "summary": "short paragraph",
  "priority_files": ["path1", "path2"]
}}
"""


def _extract_json_from_text(text: str) -> dict[str, Any]:
    content = (text or "").strip()
    if not content:
        return {"stack_assessment": [], "findings": [], "summary": "", "priority_files": []}

    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```json\s*(\{.*?\})\s*```", content, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        try:
            obj = json.loads(fenced.group(1))
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    start = content.find("{")
    end = content.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(content[start : end + 1])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    return {"stack_assessment": [], "findings": [], "summary": content, "priority_files": []}


async def run_model_secret_review(
    repo_path: Path,
    run_dir: Path,
    review_model: str,
    review_deployment: str,
    max_turns: int,
    retries: int,
    retry_backoff_seconds: float,
) -> dict[str, Any]:
    using_azure = configure_openai_client_from_env()
    _detect_api_mode(using_azure=using_azure)

    effective_model = review_deployment.strip() or review_model
    if not effective_model:
        effective_model = "gpt-5.2-chat"

    output_text = ""
    mcp_server_names: list[str] = []
    mcp_servers = build_review_mcp_servers(repo_path=repo_path)
    async with MCPServerManager(
        mcp_servers,
        strict=False,
        drop_failed_servers=True,
        connect_in_parallel=True,
    ) as mcp_manager:
        if not mcp_manager.active_servers:
            raise RuntimeError("No MCP servers connected successfully for review stage.")
        mcp_server_names = [s.name for s in mcp_manager.active_servers]

        review_agent = Agent(
            name="Secret Review Agent (Model-Driven)",
            model=effective_model,
            instructions=build_review_prompt(repo_path=repo_path),
            mcp_servers=mcp_manager.active_servers,
        )
        result = await run_with_retries(
            review_agent,
            input_text="Investigate now. Return strict JSON only.",
            max_turns=max_turns,
            retries=retries,
            backoff_seconds=retry_backoff_seconds,
        )
        output_text = str(result.final_output)

    review_output_path = run_dir / "secret-review-output.md"
    review_output_path.write_text(output_text, encoding="utf-8")

    review_json = _extract_json_from_text(output_text)
    review_json_path = run_dir / "secret-review.json"
    review_json_path.write_text(json.dumps(review_json, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")

    findings = review_json.get("findings", [])
    findings_count = len(findings) if isinstance(findings, list) else 0
    return {
        "review_output_path": review_output_path,
        "review_json_path": review_json_path,
        "review_json": review_json,
        "mcp_servers_active": mcp_server_names,
        "model_used": effective_model,
        "findings_count": findings_count,
    }


def build_secret_refactor_task(review_json: dict[str, Any], extra_task: str) -> str:
    stack = review_json.get("stack_assessment", [])
    stack_text = ", ".join(stack) if isinstance(stack, list) and stack else "unknown stack"
    findings = review_json.get("findings", [])
    priority_lines: list[str] = []
    if isinstance(findings, list):
        for item in findings[:80]:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path", "")).strip()
            line = item.get("line", "")
            env_var = str(item.get("recommended_env_var", "")).strip()
            fix = str(item.get("recommended_fix", "")).strip()
            if path:
                priority_lines.append(f"- `{path}:{line}` -> env `{env_var}` ({fix})")
    findings_block = "\n".join(priority_lines) if priority_lines else "- No structured findings provided by review stage."

    return f"""Sanitize this repository by removing hardcoded secrets and refactoring to runtime environment variables.

Execution style:
- Investigate the codebase and implement the remediation, not just recommendations.
- Use best practices for detected stack: {stack_text}.
- You may edit, patch, and create files needed to complete the remediation.
- Ensure no real secrets remain in committed source files.

Required outcomes:
1. Replace hardcoded credentials/tokens/passwords with environment-variable based runtime configuration.
2. Update all relevant references so the refactor is functionally complete.
3. Add supporting config/docs artifacts when needed (for example `.env.example` with placeholders).
4. Validate through available tests/lints or focused sanity checks.
5. Summarize each changed file and why.

Prioritized review findings:
{findings_block}

Additional user goal:
{extra_task.strip() or "Perform full secret sanitization from scan findings."}
"""


async def run_workflow(args: argparse.Namespace) -> dict[str, Any]:
    workspace_root = Path(args.workspace).expanduser().resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)

    run_stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    run_dir = workspace_root / f"secret-run-{run_stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    repo_path = clone_repo_fresh(args.repo, run_dir)

    review_stage = await run_model_secret_review(
        repo_path=repo_path,
        run_dir=run_dir,
        review_model=args.review_model,
        review_deployment=args.review_deployment,
        max_turns=args.max_turns,
        retries=args.retries,
        retry_backoff_seconds=args.retry_backoff_seconds,
    )
    analysis_context_path = review_stage["review_output_path"]

    code_output_path = run_dir / "secret-sanitization-output.md"
    code_args = argparse.Namespace(
        repo=str(repo_path),
        task=build_secret_refactor_task(review_stage["review_json"], args.task),
        model=args.model,
        backend=args.code_backend,
        codex_profile=args.codex_profile,
        codex_timeout_seconds=args.codex_timeout_seconds,
        workspace=str(workspace_root),
        analysis_report=str(analysis_context_path),
        analysis_context_max_chars=args.analysis_context_max_chars,
        output=str(code_output_path),
        enable_web_search=args.enable_web_search,
        enable_shell_fallback=args.enable_shell_fallback,
        max_turns=args.max_turns,
        retries=args.retries,
        retry_backoff_seconds=args.retry_backoff_seconds,
        command_log_max_output_chars=args.command_log_max_output_chars,
    )
    code_run = await run_code_agent(code_args)

    summary = {
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "repo_input": args.repo,
        "repo_path": str(repo_path),
        "review_output_path": str(review_stage["review_output_path"]),
        "review_json_path": str(review_stage["review_json_path"]),
        "review_findings_count": review_stage["findings_count"],
        "review_model_used": review_stage["model_used"],
        "review_mcp_servers_active": review_stage["mcp_servers_active"],
        "code_agent_run_dir": str(code_run["run_dir"]),
        "code_agent_output_path": str(code_output_path),
    }
    summary_path = run_dir / "workflow-summary.json"
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    return {
        "workflow_run_dir": run_dir,
        "summary_path": summary_path,
        "review_json_path": review_stage["review_json_path"],
        "code_agent_output_path": code_output_path,
        "repo_path": repo_path,
    }


def parse_args() -> argparse.Namespace:
    default_code_model = (
        os.environ.get("SECRET_CODE_DEPLOYMENT", "").strip()
        or os.environ.get("SECRET_CODE_MODEL", "").strip()
        or os.environ.get("AZURE_OPENAI_DEPLOYMENT", "").strip()
        or os.environ.get("MODEL", "").strip()
        or "gpt-5.1-codex-mini"
    )
    default_review_model = os.environ.get("SECRET_REVIEW_MODEL", "").strip() or "gpt-5.2-chat"
    default_review_deployment = os.environ.get("SECRET_REVIEW_DEPLOYMENT", "").strip()
    default_code_backend = (
        os.environ.get("SECRET_CODE_BACKEND", "").strip()
        or os.environ.get("CODE_AGENT_BACKEND", "").strip()
        or "agents_sdk"
    )
    default_codex_profile = os.environ.get("SECRET_CODEX_PROFILE", "").strip() or os.environ.get(
        "CODEX_PROFILE", ""
    ).strip()
    parser = argparse.ArgumentParser(
        description="Fresh-clone secret sanitization workflow (model review + autonomous refactor)."
    )
    parser.add_argument("--repo", required=True, help="Repository URL to clone fresh for this run.")
    parser.add_argument(
        "--task",
        default="",
        help="Optional additional refactor guidance merged into the generated sanitization task.",
    )
    parser.add_argument("--model", default=default_code_model, help="Model/deployment for coding agent.")
    parser.add_argument(
        "--code-backend",
        default=default_code_backend,
        choices=["agents_sdk", "codex_cli"],
        help="Execution backend for coding stage.",
    )
    parser.add_argument(
        "--codex-profile",
        default=default_codex_profile,
        help="Optional Codex CLI profile for code stage (for example: azure).",
    )
    parser.add_argument(
        "--codex-timeout-seconds",
        type=int,
        default=int(os.environ.get("CODEX_TIMEOUT_SECONDS", "600")),
        help="Timeout for codex exec backend before aborting.",
    )
    parser.add_argument("--review-model", default=default_review_model, help="Model for secret review stage.")
    parser.add_argument(
        "--review-deployment",
        default=default_review_deployment,
        help="Optional Azure deployment override for review stage.",
    )
    parser.add_argument("--workspace", default="./.agent-workspace", help="Run artifacts root directory.")
    parser.add_argument(
        "--analysis-context-max-chars",
        type=int,
        default=20_000,
        help="Max chars loaded from generated analysis context.",
    )
    parser.add_argument(
        "--enable-web-search",
        action="store_true",
        help="Enable web search tool during coding run.",
    )
    parser.add_argument(
        "--enable-shell-fallback",
        action="store_true",
        help="Enable shell tool fallback during coding run.",
    )
    parser.add_argument("--max-turns", type=int, default=40)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--retry-backoff-seconds", type=float, default=2.0)
    parser.add_argument(
        "--command-log-max-output-chars",
        type=int,
        default=4000,
    )
    return parser.parse_args()


def entrypoint() -> None:
    args = parse_args()
    result = asyncio.run(run_workflow(args))
    print(f"Workflow run dir: {result['workflow_run_dir']}")
    print(f"Summary: {result['summary_path']}")
    print(f"Review JSON: {result['review_json_path']}")
    print(f"Agent output: {result['code_agent_output_path']}")
    print(f"Cloned repo: {result['repo_path']}")


if __name__ == "__main__":
    entrypoint()
