from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agents import (
    Agent,
    Runner,
    ShellCallOutcome,
    ShellCommandOutput,
    ShellCommandRequest,
    ShellResult,
    ShellTool,
    WebSearchTool,
    function_tool,
    set_default_openai_client,
    set_default_openai_api,
)
from openai import APIConnectionError, AsyncAzureOpenAI, RateLimitError

from .ingest import ingest_report_to_db
from .quality import ValidationResult, append_quality_warning, validate_report
from .scan import build_scan, write_scan


@dataclass
class AnalysisRunResult:
    report: str
    run_dir: Path
    report_path: Path
    output_copy_path: Path | None
    validation_result: ValidationResult | None


def _is_windows() -> bool:
    return os.name == "nt"


def _shell_invocation(command: str) -> list[str]:
    if _is_windows():
        if shutil.which("pwsh"):
            return ["pwsh", "-NoProfile", "-Command", command]
        return ["powershell", "-NoProfile", "-Command", command]
    return ["/bin/sh", "-lc", command]


def _truncate_text(text: str, max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n... [truncated {len(text) - max_chars} chars]"


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=True) + "\n")


class ShellCommandLogger:
    def __init__(self, log_path: Path, max_output_chars: int = 4000):
        self.log_path = log_path
        self.max_output_chars = max_output_chars
        self.events: list[dict[str, Any]] = []

    def log(
        self,
        *,
        source: str,
        cwd: Path,
        command: str,
        timeout_ms: int,
        timed_out: bool,
        exit_code: int | None,
        duration_ms: int,
        stdout: str,
        stderr: str,
        blocked: bool = False,
        block_reason: str = "",
    ) -> None:
        event = {
            "timestamp_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "source": source,
            "cwd": str(cwd),
            "command": command,
            "timeout_ms": timeout_ms,
            "timed_out": timed_out,
            "exit_code": exit_code,
            "duration_ms": duration_ms,
            "blocked": blocked,
            "block_reason": block_reason,
            "stdout": _truncate_text(stdout, self.max_output_chars),
            "stderr": _truncate_text(stderr, self.max_output_chars),
        }
        self.events.append(event)
        _append_jsonl(self.log_path, event)

    def write_summary(self, run_dir: Path) -> Path:
        summary_path = run_dir / "command-diagnostics.json"
        total = len(self.events)
        timed_out = sum(1 for e in self.events if e.get("timed_out"))
        blocked = sum(1 for e in self.events if e.get("blocked"))
        failures = sum(
            1
            for e in self.events
            if not e.get("blocked")
            and not e.get("timed_out")
            and isinstance(e.get("exit_code"), int)
            and e.get("exit_code") != 0
        )
        summary = {
            "generated_at_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "log_path": str(self.log_path),
            "total_commands": total,
            "failed_commands": failures,
            "timed_out_commands": timed_out,
            "blocked_commands": blocked,
        }
        summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        return summary_path


async def require_approval(commands: Sequence[str]) -> None:
    if os.environ.get("SHELL_AUTO_APPROVE") == "1":
        return

    print("Shell command approval required:")
    for command in commands:
        print(f"  {command}")

    response = input("Proceed? [y/N] ").strip().lower()
    if response not in {"y", "yes"}:
        raise RuntimeError("Shell command execution rejected by user.")


class ShellExecutor:
    """Cookbook-style shell executor for Agents SDK ShellTool."""

    def __init__(self, cwd: Path, logger: ShellCommandLogger, default_timeout_ms: int = 120_000):
        self.cwd = cwd
        self.logger = logger
        self.default_timeout_ms = default_timeout_ms

    async def __call__(self, request: ShellCommandRequest) -> ShellResult:
        action = request.data.action
        await require_approval(action.commands)

        outputs: list[ShellCommandOutput] = []
        for command in action.commands:
            timeout_ms = action.timeout_ms or self.default_timeout_ms
            timeout_s = max(1, timeout_ms / 1000)
            started = time.perf_counter()
            proc = await asyncio.create_subprocess_exec(
                *_shell_invocation(command),
                cwd=str(self.cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            timed_out = False
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            except asyncio.TimeoutError:
                timed_out = True
                proc.kill()
                stdout_bytes, stderr_bytes = await proc.communicate()

            stdout = (stdout_bytes or b"").decode(errors="replace")
            stderr = (stderr_bytes or b"").decode(errors="replace")

            if timed_out:
                outcome = ShellCallOutcome(type="timeout", exit_code=None)
                if not stderr.strip():
                    stderr = f"Command timed out after {int(timeout_s)}s"
                exit_code = None
            else:
                exit_code = getattr(proc, "returncode", None)
                outcome = ShellCallOutcome(type="exit", exit_code=exit_code)

            duration_ms = int((time.perf_counter() - started) * 1000)
            self.logger.log(
                source="shell_tool",
                cwd=self.cwd,
                command=command,
                timeout_ms=timeout_ms,
                timed_out=timed_out,
                exit_code=exit_code,
                duration_ms=duration_ms,
                stdout=stdout,
                stderr=stderr,
            )

            outputs.append(
                ShellCommandOutput(
                    command=command,
                    stdout=stdout,
                    stderr=stderr,
                    outcome=outcome,
                )
            )
            if timed_out:
                break

        return ShellResult(output=outputs, provider_data={"working_directory": str(self.cwd)})


def build_agent_prompt(repo_path: Path, scan_path: Path, focus: str | None, min_stories: int, min_evidence: int) -> str:
    extra_focus = focus.strip() if focus else ""
    return f"""You are a senior product analyst and software reverse engineer.

Goal:
- Reverse engineer the codebase into product requirements.
- Produce a practical feature map and user stories that a product/engineering team can use immediately.

Execution constraints:
- Repository is already available locally at: {repo_path}
- You MUST begin by reading the deterministic scan artifact: {scan_path}
- You MUST use shell commands via tool for targeted verification only.
- Use one complete shell command per tool call.
- Keep commands read-only.
- Prefer fast commands: pwd, ls, rg --files, rg pattern, sed -n for targeted snippets.
- Do not fabricate details not grounded in evidence.

Analysis focus:
- Identify user-facing features, internal/admin features, integrations, workflows.
- Infer personas/actors and convert behavior into user stories.
{f"- Additional focus from user: {extra_focus}" if extra_focus else ""}

Output requirements (Markdown):
1. Repository Summary
2. Inferred Personas/Actors
3. Feature Inventory (grouped by domain)
4. User Stories (format: As a <persona>, I want <goal>, so that <value>)
5. Acceptance Criteria (for top-priority stories)
6. Evidence Table (story/feature -> file paths)
7. Gaps, Risks, and Open Questions
8. Suggested Next 10 Product Backlog Items

Quality gates:
- At least {min_stories} user stories.
- At least {min_evidence} evidence links/rows with concrete file paths.
- Every story must be grounded in repository evidence.
- Label assumptions explicitly.
"""


def configure_openai_client_from_env() -> bool:
    azure_endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip() or os.environ.get("ENDPOINT", "").strip()
    if not azure_endpoint:
        return False

    api_key = os.environ.get("AZURE_OPENAI_API_KEY", "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise ValueError("Azure endpoint is set, but no key found. Set AZURE_OPENAI_API_KEY or OPENAI_API_KEY.")

    api_version = os.environ.get("AZURE_OPENAI_API_VERSION", "").strip()
    if not api_version:
        raise ValueError("AZURE_OPENAI_API_VERSION is required when using Azure OpenAI.")

    azure_deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "").strip() or None

    parsed = urlparse(azure_endpoint)
    if parsed.scheme and parsed.netloc and parsed.path:
        path = parsed.path.rstrip("/")
        for suffix in (
            "/openai/responses",
            "/openai/chat/completions",
            "/openai/v1/responses",
            "/openai/v1/chat/completions",
        ):
            if path.endswith(suffix):
                azure_endpoint = f"{parsed.scheme}://{parsed.netloc}"
                break

    azure_client = AsyncAzureOpenAI(
        azure_endpoint=azure_endpoint,
        api_version=api_version,
        api_key=api_key,
        azure_deployment=azure_deployment,
    )
    set_default_openai_client(azure_client, use_for_tracing=False)
    return True


def build_chat_shell_function_tool(cwd: Path, logger: ShellCommandLogger):
    @function_tool(name_override="shell")
    async def shell(command: str | None = None, commands: list[str] | None = None, timeout_ms: int = 120_000) -> str:
        command_list: list[str] = []
        if command and command.strip():
            command_list.append(command.strip())
        if commands:
            command_list.extend([c.strip() for c in commands if c and c.strip()])
        if not command_list:
            return "No command provided."

        disallowed = re.compile(r"^\s*(rm|mv|dd|mkfs|shutdown|reboot|git\s+reset|git\s+clean)\b")
        for command_text in command_list:
            if disallowed.search(command_text):
                logger.log(
                    source="azure_shell_function",
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
        timeout_s = max(1, timeout_ms / 1000)
        for command_text in command_list:
            output_parts.append(f"$ {command_text}")
            started = time.perf_counter()
            proc = await asyncio.create_subprocess_exec(
                *_shell_invocation(command_text),
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
                stdout = (stdout_b or b"").decode(errors="replace")
                stderr = (stderr_b or b"").decode(errors="replace")
                duration_ms = int((time.perf_counter() - started) * 1000)
                logger.log(
                    source="azure_shell_function",
                    cwd=cwd,
                    command=command_text,
                    timeout_ms=timeout_ms,
                    timed_out=False,
                    exit_code=proc.returncode,
                    duration_ms=duration_ms,
                    stdout=stdout,
                    stderr=stderr,
                )
                output_parts.append((stdout + stderr).strip() or "(no output)")
                output_parts.append(f"[exit_code={proc.returncode}]")
            except asyncio.TimeoutError:
                proc.kill()
                stdout_b, stderr_b = await proc.communicate()
                stdout = (stdout_b or b"").decode(errors="replace")
                stderr = (stderr_b or b"").decode(errors="replace")
                duration_ms = int((time.perf_counter() - started) * 1000)
                logger.log(
                    source="azure_shell_function",
                    cwd=cwd,
                    command=command_text,
                    timeout_ms=timeout_ms,
                    timed_out=True,
                    exit_code=None,
                    duration_ms=duration_ms,
                    stdout=stdout,
                    stderr=stderr or f"Command timed out after {int(timeout_s)}s",
                )
                output_parts.append(f"Command timed out after {int(timeout_s)}s")
                output_parts.append("[exit_code=124]")
                break
        return "\n".join(output_parts)

    return shell


def acquire_repo(repo: str, run_dir: Path) -> Path:
    repo_path = Path(repo).expanduser()
    if repo_path.exists():
        return repo_path.resolve()

    target = run_dir / "repo"
    cmd = ["git", "clone", "--depth", "1", repo, str(target)]
    import subprocess

    completed = subprocess.run(cmd, text=True, capture_output=True)
    if completed.returncode != 0:
        raise RuntimeError(f"Failed to clone repository: {completed.stderr or completed.stdout}")
    return target.resolve()


async def run_with_retries(agent: Agent, input_text: str, max_turns: int, retries: int, backoff_seconds: float):
    attempt = 0
    while True:
        try:
            return await Runner.run(agent, input=input_text, max_turns=max_turns)
        except (RateLimitError, APIConnectionError) as exc:
            attempt += 1
            if attempt > retries:
                raise
            sleep_s = backoff_seconds * (2 ** (attempt - 1))
            print(f"Retrying after transient API error ({exc.__class__.__name__}) in {sleep_s:.1f}s...")
            time.sleep(sleep_s)


async def run_analysis(args: argparse.Namespace) -> AnalysisRunResult:
    if args.skip_validation:
        print("Note: --skip-validation is deprecated; validation still runs in warning-only mode.")

    workspace_root = Path(args.workspace).expanduser().resolve()
    workspace_root.mkdir(parents=True, exist_ok=True)
    started_at = datetime.utcnow()

    run_stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    run_dir = workspace_root / f"run-{run_stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    repo_path = acquire_repo(args.repo, run_dir)

    log_path_raw = (args.command_log_path or "").strip()
    if log_path_raw:
        command_log_path = Path(log_path_raw).expanduser().resolve()
    else:
        command_log_path = run_dir / "commands.jsonl"
    logger = ShellCommandLogger(log_path=command_log_path, max_output_chars=args.command_log_max_output_chars)

    scan_path = run_dir / "scan.json"
    scan = build_scan(repo_path)
    write_scan(scan, scan_path)

    using_azure = configure_openai_client_from_env()
    azure_deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "").strip()
    endpoint_used = (
        os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip() or os.environ.get("ENDPOINT", "").strip()
        if using_azure
        else os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").strip()
    )
    if using_azure and endpoint_used:
        parsed = urlparse(endpoint_used)
        if parsed.scheme and parsed.netloc and parsed.path:
            path = parsed.path.rstrip("/")
            for suffix in (
                "/openai/responses",
                "/openai/chat/completions",
                "/openai/v1/responses",
                "/openai/v1/chat/completions",
            ):
                if path.endswith(suffix):
                    endpoint_used = f"{parsed.scheme}://{parsed.netloc}"
                    break

    api_mode = os.environ.get("OPENAI_API_MODE", "").strip().lower()
    if not api_mode and using_azure:
        api_mode = "responses"
    if not api_mode:
        api_mode = "responses"

    if api_mode == "chat_completions":
        set_default_openai_api("chat_completions")
    elif api_mode == "responses":
        set_default_openai_api("responses")
    elif api_mode:
        raise ValueError("OPENAI_API_MODE must be 'responses' or 'chat_completions'.")

    if api_mode == "chat_completions" or using_azure:
        tools = [build_chat_shell_function_tool(repo_path, logger=logger)]
    else:
        tools = [ShellTool(executor=ShellExecutor(cwd=repo_path, logger=logger))]

    if args.enable_web_search:
        tools.append(WebSearchTool())

    model_name = args.model
    if using_azure and args.model in {"gpt-5.1-codex", "gpt-5.1-codex-mini"} and azure_deployment:
        model_name = azure_deployment

    agent = Agent(
        name="Repo Requirements Analyzer",
        model=model_name,
        instructions=build_agent_prompt(
            repo_path=repo_path,
            scan_path=scan_path,
            focus=args.focus,
            min_stories=args.min_stories,
            min_evidence=args.min_evidence,
        ),
        tools=tools,
    )

    report = ""
    report_path = run_dir / "report.md"
    output_copy_path: Path | None = None
    validation_result = None
    run_status = "failed"
    error_message = ""
    try:
        result = await run_with_retries(
            agent,
            input_text="Analyze the target repository now and produce the full markdown report.",
            max_turns=args.max_turns,
            retries=args.retries,
            backoff_seconds=args.retry_backoff_seconds,
        )
        report = str(result.final_output)

        validation_result = validate_report(report, args.min_stories, args.min_evidence)
        if validation_result.passed:
            print("Quality validation passed.")
            run_status = "completed"
        else:
            print("Quality validation warnings:")
            for err in validation_result.errors:
                print(f" - {err}")
            report = append_quality_warning(report, validation_result)
            run_status = "completed_with_warnings"

        report_path.write_text(report, encoding="utf-8")
        print(f"Saved run report to {report_path}")

        if args.output:
            output_copy_path = Path(args.output).expanduser().resolve()
            output_copy_path.parent.mkdir(parents=True, exist_ok=True)
            output_copy_path.write_text(report, encoding="utf-8")
            print(f"Saved output copy to {output_copy_path}")

        return AnalysisRunResult(
            report=report,
            run_dir=run_dir,
            report_path=report_path,
            output_copy_path=output_copy_path,
            validation_result=validation_result,
        )
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
            "scan_path": str(scan_path),
            "report_path": str(report_path) if report_path.exists() else "",
            "output_copy_path": str(output_copy_path) if output_copy_path else "",
            "provider": "azure" if using_azure else "openai",
            "api_mode": api_mode,
            "endpoint_used": endpoint_used,
            "model_requested": args.model,
            "model_used": model_name,
            "azure_deployment": azure_deployment,
            "run_status": run_status,
            "error_message": error_message,
            "validation": {
                "passed": bool(validation_result.passed) if validation_result else None,
                "story_count": validation_result.story_count if validation_result else None,
                "min_stories": validation_result.min_stories if validation_result else None,
                "evidence_count": validation_result.evidence_count if validation_result else None,
                "min_evidence": validation_result.min_evidence if validation_result else None,
                "errors": validation_result.errors if validation_result else [],
            },
            "command_log_path": str(command_log_path),
            "command_diagnostics_path": str(diagnostics_path),
        }
        summary_path.write_text(json.dumps(summary_payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
        print(f"Command log written to {command_log_path}")
        print(f"Command diagnostics written to {diagnostics_path}")
        print(f"Run summary written to {summary_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reverse engineer features and user stories from a codebase using OpenAI Agents SDK.",
    )
    parser.add_argument("--repo", required=True, help="Git URL or local repository path.")
    parser.add_argument("--model", default="gpt-5.1-codex", help="Model name for the agent.")
    parser.add_argument(
        "--workspace",
        default="./.agent-workspace",
        help="Directory where runs and cloned repositories are stored.",
    )
    parser.add_argument(
        "--focus",
        default="",
        help="Optional extra analysis focus areas (comma-separated or sentence).",
    )
    parser.add_argument("--output", default="", help="Optional output markdown file path.")
    parser.add_argument(
        "--enable-web-search",
        action="store_true",
        help="Enable built-in WebSearchTool for external context.",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=30,
        help="Maximum agent turns before aborting (default: 30).",
    )
    parser.add_argument(
        "--min-stories",
        type=int,
        default=15,
        help="Minimum required user stories in final report (default: 15).",
    )
    parser.add_argument(
        "--min-evidence",
        type=int,
        default=25,
        help="Minimum required evidence links/rows in final report (default: 25).",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=2,
        help="Retries for transient API errors like rate limits (default: 2).",
    )
    parser.add_argument(
        "--retry-backoff-seconds",
        type=float,
        default=2.0,
        help="Initial backoff in seconds for retries (default: 2.0).",
    )
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="Deprecated: validation always runs and no longer blocks report writes.",
    )
    parser.add_argument(
        "--db",
        default="",
        help="Optional SQLite path for auto-ingesting report data (for web app/querying).",
    )
    parser.add_argument(
        "--command-log-path",
        default="",
        help="Optional path for JSONL shell command audit log. Defaults to <run_dir>/commands.jsonl.",
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
    run_result = asyncio.run(run_analysis(args))
    report = run_result.report
    validation_result = run_result.validation_result

    if not args.output:
        print(report)

    if args.db:
        report_id = ingest_report_to_db(
            report_markdown=report,
            db_path=Path(args.db),
            repo=args.repo,
            model=args.model,
            report_path=run_result.report_path,
            validation_result=validation_result,
            min_stories=args.min_stories,
            min_evidence=args.min_evidence,
        )
        print(f"Ingested report_id={report_id} into {Path(args.db).expanduser().resolve()}")


if __name__ == "__main__":
    entrypoint()
