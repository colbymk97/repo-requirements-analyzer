from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from agents import Agent, WebSearchTool, function_tool, set_default_openai_api
from agents.mcp import MCPServerManager, MCPServerStdio, create_static_tool_filter

from .main import (
    ShellCommandLogger,
    _shell_invocation,
    _truncate_text,
    acquire_repo,
    configure_openai_client_from_env,
    require_approval,
    run_with_retries,
)


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


def _safe_decode(data: bytes | None) -> str:
    return (data or b"").decode(errors="replace")


def _extract_patch_paths(patch: str) -> list[str]:
    paths: list[str] = []
    seen: set[str] = set()
    for line in patch.splitlines():
        if line.startswith("+++ b/"):
            p = line[6:].strip()
            if p and p != "/dev/null" and p not in seen:
                seen.add(p)
                paths.append(p)
            continue

        m = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
        if m:
            p = m.group(2).strip()
            if p and p != "/dev/null" and p not in seen:
                seen.add(p)
                paths.append(p)
    return paths


def _extract_first_fenced_block(text: str) -> str | None:
    match = re.search(r"```(?:diff|patch)?\s*\n(.*?)\n```", text, flags=re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def _normalize_patch_text(raw_patch: str) -> tuple[str, list[str]]:
    notes: list[str] = []
    candidate = raw_patch.strip()

    # Sometimes the model sends {"patch":"..."} as a string payload.
    if candidate.startswith("{") and candidate.endswith("}"):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            obj = None
        if isinstance(obj, dict):
            for key in ("patch", "diff", "content"):
                value = obj.get(key)
                if isinstance(value, str) and value.strip():
                    candidate = value.strip()
                    notes.append(f"extracted_json_field:{key}")
                    break

    fenced = _extract_first_fenced_block(candidate)
    if fenced:
        candidate = fenced
        notes.append("stripped_markdown_fence")

    # If the model wrapped the patch in prose, trim to first diff header.
    diff_index = candidate.find("diff --git ")
    if diff_index > 0:
        candidate = candidate[diff_index:]
        notes.append("trimmed_to_diff_header")

    if diff_index < 0:
        has_unified_headers = bool(re.search(r"(?m)^---\s+\S+", candidate)) and bool(
            re.search(r"(?m)^\+\+\+\s+\S+", candidate)
        )
        if has_unified_headers:
            first_header = re.search(r"(?m)^---\s+\S+", candidate)
            if first_header and first_header.start() > 0:
                candidate = candidate[first_header.start() :]
                notes.append("trimmed_to_unified_headers")

    candidate = candidate.replace("\r\n", "\n")
    if candidate and not candidate.endswith("\n"):
        candidate += "\n"

    return candidate, notes


def _looks_like_unified_diff(text: str) -> bool:
    has_diff_header = "diff --git " in text
    has_headers = bool(re.search(r"(?m)^---\s+\S+", text)) and bool(re.search(r"(?m)^\+\+\+\s+\S+", text))
    has_hunk = bool(re.search(r"(?m)^@@\s", text))
    return has_hunk and (has_diff_header or has_headers)


async def _run_exec(
    args: list[str],
    cwd: Path,
    timeout_ms: int,
    stdin_text: str | None = None,
) -> tuple[int | None, str, str, bool, int]:
    started = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    timed_out = False
    timeout_s = max(1, timeout_ms / 1000)
    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(None if stdin_text is None else stdin_text.encode("utf-8")),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        stdout_b, stderr_b = await proc.communicate()

    duration_ms = int((time.perf_counter() - started) * 1000)
    return (
        None if timed_out else proc.returncode,
        _safe_decode(stdout_b),
        _safe_decode(stderr_b),
        timed_out,
        duration_ms,
    )


def build_code_shell_function_tool(cwd: Path, logger: ShellCommandLogger):
    @function_tool(name_override="shell")
    async def shell(command: str | None = None, commands: list[str] | None = None, timeout_ms: int = 120_000) -> str:
        command_list: list[str] = []
        if command and command.strip():
            command_list.append(command.strip())
        if commands:
            command_list.extend([c.strip() for c in commands if c and c.strip()])
        if not command_list:
            return "No command provided."

        unsafe = re.compile(
            r"(^|\s)(rm\s+-rf\s+/|mkfs\b|shutdown\b|reboot\b|dd\s+if=|git\s+reset\s+--hard\b|git\s+clean\s+-fdx\b)"
        )
        for command_text in command_list:
            if unsafe.search(command_text):
                logger.log(
                    source="code_shell_function",
                    cwd=cwd,
                    command=command_text,
                    timeout_ms=timeout_ms,
                    timed_out=False,
                    exit_code=None,
                    duration_ms=0,
                    stdout="",
                    stderr="",
                    blocked=True,
                    block_reason="unsafe_command_pattern",
                )
                return f"Blocked unsafe command: {command_text}"

        await require_approval(command_list)

        output_parts: list[str] = []
        for command_text in command_list:
            output_parts.append(f"$ {command_text}")
            exit_code, stdout, stderr, timed_out, duration_ms = await _run_exec(
                _shell_invocation(command_text),
                cwd=cwd,
                timeout_ms=timeout_ms,
            )
            logger.log(
                source="code_shell_function",
                cwd=cwd,
                command=command_text,
                timeout_ms=timeout_ms,
                timed_out=timed_out,
                exit_code=exit_code,
                duration_ms=duration_ms,
                stdout=stdout,
                stderr=stderr,
            )
            if timed_out:
                output_parts.append(f"Command timed out after {max(1, int(timeout_ms / 1000))}s")
                output_parts.append("[exit_code=124]")
                break

            merged = (stdout + stderr).strip()
            output_parts.append(merged or "(no output)")
            output_parts.append(f"[exit_code={exit_code}]")
        return "\n".join(output_parts)

    return shell


def build_local_mcp_servers(repo_path: Path) -> list[MCPServerStdio]:
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
            "write_file",
            "edit_file",
            "create_directory",
            "move_file",
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


def build_code_agent_prompt(repo_path: Path, task: str, analysis_context: str | None) -> str:
    analysis_block = f"\nAnalysis context from prior run:\n{analysis_context}\n" if analysis_context else ""
    return f"""You are a senior autonomous coding agent.

Goal:
- Complete the requested coding task in this repository with production-grade quality.

Repository:
- Path: {repo_path}

Execution requirements:
- Investigate the repository thoroughly using any available tools.
- Execute real changes on disk: edit, patch, create, move, or delete files as needed to complete the task.
- Use MCP and shell tools whenever they are useful; do not stop at analysis.
- For git MCP tools, call `git_set_working_dir` early with the repository path.
- Prefer concrete execution over planning narratives.
- If a tool call fails, diagnose and retry with a refined approach.
- After changes, run relevant checks/tests and summarize results with concrete file paths.

Requested task:
{task}
{analysis_block}
Deliverables:
1. Implemented code changes
2. Validation steps and results
3. Remaining risks or follow-ups
"""


def build_codex_cli_prompt(repo_path: Path, task: str, analysis_context: str | None) -> str:
    analysis_block = f"\nAnalysis context from prior run:\n{analysis_context}\n" if analysis_context else ""
    return f"""You are a senior autonomous coding agent running in Codex CLI.

Repository:
- Path: {repo_path}

Execution requirements:
- Investigate the repository directly and execute real file operations.
- Read, patch, edit, and create files in this repository until the task is completed.
- Do not ask the user for file contents; inspect and modify files yourself.
- Use shell and git commands when needed to confirm your edits.
- Do not stop at recommendations; perform the implementation.
- After editing, run quick validation checks and summarize exactly which files changed.

Requested task:
{task}
{analysis_block}
Deliverables:
1. Implemented code changes
2. Validation steps and results
3. Remaining risks or follow-ups
"""


def _load_analysis_context(path_raw: str, max_chars: int) -> str | None:
    path_raw = (path_raw or "").strip()
    if not path_raw:
        return None
    path = Path(path_raw).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Analysis report not found: {path}")
    content = path.read_text(encoding="utf-8", errors="replace")
    return _truncate_text(content, max_chars)


async def run_code_agent(args: argparse.Namespace) -> dict[str, Any]:
    workspace_root = Path(args.workspace).expanduser().resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    started_at = datetime.utcnow()

    run_stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    run_dir = workspace_root / f"code-run-{run_stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    repo_path = acquire_repo(args.repo, run_dir)
    command_log_path = run_dir / "commands.jsonl"
    logger = ShellCommandLogger(log_path=command_log_path, max_output_chars=args.command_log_max_output_chars)

    analysis_context = _load_analysis_context(args.analysis_report, max_chars=args.analysis_context_max_chars)

    backend = (getattr(args, "backend", "agents_sdk") or "agents_sdk").strip().lower()
    using_azure = bool(os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip() or os.environ.get("ENDPOINT", "").strip())
    azure_deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "").strip()
    endpoint_used = (
        os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip() or os.environ.get("ENDPOINT", "").strip()
        if using_azure
        else os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
    )
    api_mode = os.environ.get("OPENAI_API_MODE", "").strip().lower() or "responses"

    tools: list[Any] = []
    if backend == "agents_sdk":
        using_azure = configure_openai_client_from_env()
        api_mode = _detect_api_mode(using_azure=using_azure)
        if args.enable_shell_fallback:
            tools.append(build_code_shell_function_tool(repo_path, logger=logger))
        if args.enable_web_search:
            tools.append(WebSearchTool())

    model_name = args.model
    if using_azure and args.model in {"gpt-5.1-codex", "gpt-5.1-codex-mini"} and azure_deployment:
        model_name = azure_deployment

    final_output = ""
    status = "failed"
    error_message = ""
    output_path: Path | None = None
    mcp_server_names: list[str] = []
    try:
        if backend == "codex_cli":
            prompt = build_codex_cli_prompt(repo_path=repo_path, task=args.task, analysis_context=analysis_context)
            last_message_path = run_dir / "codex-last-message.txt"
            codex_stdout_path = run_dir / "codex-cli.stdout.log"
            codex_stderr_path = run_dir / "codex-cli.stderr.log"
            profile = (getattr(args, "codex_profile", "") or "").strip()

            cmd = [
                "codex",
                "exec",
                "--full-auto",
                "--cd",
                str(repo_path),
                "--output-last-message",
                str(last_message_path),
                "--json",
                "-m",
                model_name,
                "-",
            ]
            if profile:
                cmd[2:2] = ["--profile", profile]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(repo_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(prompt.encode("utf-8")),
                    timeout=max(30, int(getattr(args, "codex_timeout_seconds", 600))),
                )
            except asyncio.TimeoutError:
                proc.kill()
                stdout_b, stderr_b = await proc.communicate()
                codex_stdout_path.write_text(_safe_decode(stdout_b), encoding="utf-8")
                codex_stderr_path.write_text(_safe_decode(stderr_b), encoding="utf-8")
                raise RuntimeError(
                    f"codex exec timed out after {int(getattr(args, 'codex_timeout_seconds', 600))}s. "
                    f"See {codex_stdout_path} and {codex_stderr_path}."
                )
            stdout = _safe_decode(stdout_b)
            stderr = _safe_decode(stderr_b)
            codex_stdout_path.write_text(stdout, encoding="utf-8")
            codex_stderr_path.write_text(stderr, encoding="utf-8")

            if proc.returncode != 0:
                raise RuntimeError(
                    f"codex exec failed with exit code {proc.returncode}. "
                    f"See {codex_stdout_path} and {codex_stderr_path}."
                )

            if last_message_path.exists():
                final_output = last_message_path.read_text(encoding="utf-8", errors="replace")
            else:
                final_output = stdout.strip()

            status = "completed"
            mcp_server_names = ["codex_cli"]
            if args.output:
                output_path = Path(args.output).expanduser().resolve()
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(final_output, encoding="utf-8")
                print(f"Saved output to {output_path}")

            return {
                "run_dir": run_dir,
                "repo_path": repo_path,
                "final_output": final_output,
                "output_path": output_path,
            }

        mcp_servers = build_local_mcp_servers(repo_path=repo_path)
        async with MCPServerManager(
            mcp_servers,
            strict=False,
            drop_failed_servers=True,
            connect_in_parallel=True,
        ) as mcp_manager:
            if not mcp_manager.active_servers:
                raise RuntimeError("No MCP servers connected successfully.")
            mcp_server_names = [s.name for s in mcp_manager.active_servers]

            agent = Agent(
                name="Repo Coding Agent (MCP)",
                model=model_name,
                instructions=build_code_agent_prompt(
                    repo_path=repo_path, task=args.task, analysis_context=analysis_context
                ),
                tools=tools,
                mcp_servers=mcp_manager.active_servers,
            )

            result = await run_with_retries(
                agent,
                input_text="Start now. Make the required code changes and then summarize exactly what changed.",
                max_turns=args.max_turns,
                retries=args.retries,
                backoff_seconds=args.retry_backoff_seconds,
            )
            final_output = str(result.final_output)
            status = "completed"

            if args.output:
                output_path = Path(args.output).expanduser().resolve()
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(final_output, encoding="utf-8")
                print(f"Saved output to {output_path}")

            return {
                "run_dir": run_dir,
                "repo_path": repo_path,
                "final_output": final_output,
                "output_path": output_path,
            }
    except Exception as exc:
        error_message = str(exc)
        raise
    finally:
        diagnostics_path = logger.write_summary(run_dir=run_dir)
        summary_path = run_dir / "run-summary.json"
        summary_payload = {
            "run_id": run_dir.name,
            "run_dir": str(run_dir),
            "started_at_utc": started_at.isoformat(timespec="seconds") + "Z",
            "finished_at_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "repo_input": args.repo,
            "repo_path": str(repo_path),
            "analysis_report": args.analysis_report,
            "provider": "azure" if using_azure else "openai",
            "api_mode": api_mode,
            "endpoint_used": endpoint_used,
            "model_requested": args.model,
            "model_used": model_name,
            "backend": backend,
            "mcp_servers_active": mcp_server_names,
            "run_status": status,
            "error_message": error_message,
            "output_path": str(output_path) if output_path else "",
            "command_log_path": str(command_log_path),
            "command_diagnostics_path": str(diagnostics_path),
        }
        summary_path.write_text(json.dumps(summary_payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        print(f"Command log written to {command_log_path}")
        print(f"Command diagnostics written to {diagnostics_path}")
        print(f"Run summary written to {summary_path}")


def parse_args() -> argparse.Namespace:
    default_model = (
        os.environ.get("AZURE_OPENAI_DEPLOYMENT", "").strip()
        or os.environ.get("MODEL", "").strip()
        or "gpt-5.1-codex-mini"
    )
    parser = argparse.ArgumentParser(description="Autonomous coding agent with MCP filesystem and git tools.")
    parser.add_argument("--repo", required=True, help="Git URL or local repository path.")
    parser.add_argument("--task", required=True, help="Coding task to execute.")
    parser.add_argument("--model", default=default_model, help="Model name for the agent.")
    parser.add_argument(
        "--backend",
        default=(os.environ.get("CODE_AGENT_BACKEND", "").strip() or "agents_sdk"),
        choices=["agents_sdk", "codex_cli"],
        help="Execution backend for coding runs.",
    )
    parser.add_argument(
        "--codex-profile",
        default=(os.environ.get("CODEX_PROFILE", "").strip()),
        help="Optional Codex CLI profile name (for example: azure).",
    )
    parser.add_argument(
        "--codex-timeout-seconds",
        type=int,
        default=int(os.environ.get("CODEX_TIMEOUT_SECONDS", "600")),
        help="Timeout for codex exec backend before aborting.",
    )
    parser.add_argument("--workspace", default="./.agent-workspace", help="Directory for run artifacts.")
    parser.add_argument("--analysis-report", default="", help="Optional path to a prior analysis markdown file.")
    parser.add_argument(
        "--analysis-context-max-chars",
        type=int,
        default=20_000,
        help="Max chars loaded from --analysis-report (default: 20000).",
    )
    parser.add_argument("--output", default="", help="Optional path to save final agent output markdown/text.")
    parser.add_argument(
        "--enable-web-search",
        action="store_true",
        help="Enable built-in WebSearchTool for external context.",
    )
    parser.add_argument(
        "--enable-shell-fallback",
        action="store_true",
        help="Enable shell tool in addition to MCP tools (off by default).",
    )
    parser.add_argument("--max-turns", type=int, default=40, help="Maximum agent turns before aborting.")
    parser.add_argument("--retries", type=int, default=2, help="Retries for transient API errors.")
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=2.0,
        help="Initial retry backoff in seconds (default: 2.0).",
    )
    parser.add_argument(
        "--command-log-max-output-chars",
        type=int,
        default=4000,
        help="Max chars stored per stdout/stderr in command logs (default: 4000).",
    )
    return parser.parse_args()


def entrypoint() -> None:
    args = parse_args()
    run_result = asyncio.run(run_code_agent(args))
    if not args.output:
        print(run_result["final_output"])


if __name__ == "__main__":
    entrypoint()
