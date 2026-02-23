# Repo Requirements Analyzer

An OpenAI Agents SDK CLI that can:
- clone a repository (or inspect an existing local path)
- analyze the codebase using the built-in `ShellTool`
- reverse engineer product features and user stories
- pre-scan repositories into a deterministic `scan.json` artifact
- run an autonomous coding agent with MCP filesystem/git tools
- run a fresh-clone secret sanitization workflow (model review + refactor)

## Runtime

- Python 3.10+ (recommended: latest Python 3.12+)
- OpenAI key (`OPENAI_API_KEY`) or Azure OpenAI credentials
- `git` installed
- Shell runtime:
  - macOS/Linux: `/bin/sh`
  - Windows: `pwsh` (preferred) or `powershell`

## Model Requirements

- The model must support tool use/function calling because this agent relies on `ShellTool`.
- The model should support long-context code analysis for medium/large repositories.
- Default `MODEL` is `gpt-5.1-codex` for OpenAI-hosted usage.
- For Azure OpenAI, set `MODEL` to your Azure deployment name (not a raw model family string).
- API mode compatibility:
  - `OPENAI_API_MODE=responses` for Responses API-compatible deployments.
  - `OPENAI_API_MODE=chat_completions` for Chat Completions-compatible deployments.

## Azure Shell Compatibility

- Azure Responses/Chat APIs may reject hosted tool payload fields used by SDK `ShellTool` (for example `environment`).
- This project automatically uses an Azure-compatible function tool named `shell` when Azure credentials are configured.
- Shell capability is preserved; only the transport changes (hosted `ShellTool` on OpenAI, function tool fallback on Azure).

## Install

Quickest path with local venv:

```bash
make setup
```

Configure API key with an env file:

```bash
cp .env.example .env
# edit .env and set OPENAI_API_KEY=...
```

Secret workflow model routing env file:

```bash
cp .env.secret-workflow.example .env.secret-workflow
# edit .env.secret-workflow for:
#   SECRET_REVIEW_MODEL / SECRET_CODE_MODEL
#   SECRET_REVIEW_DEPLOYMENT / SECRET_CODE_DEPLOYMENT
#   SECRET_CODE_BACKEND=codex_cli
#   SECRET_CODEX_PROFILE=azure
```

Bring your own deployment:

```bash
# .env
OPENAI_API_KEY=your_provider_key
OPENAI_BASE_URL=https://your-provider.example.com/v1
# if your deployment only supports chat completions:
OPENAI_API_MODE=chat_completions
```

Azure OpenAI deployment:

```bash
# .env
AZURE_OPENAI_ENDPOINT=https://your-resource-name.openai.azure.com/
AZURE_OPENAI_API_KEY=your_azure_openai_key
AZURE_OPENAI_API_VERSION=2024-10-21
# Use your Azure deployment name as MODEL when running:
# make run REPO=... MODEL=your_deployment_name
```

## Azure Configuration (Step-by-Step)

1. In Azure AI Foundry / Azure OpenAI, deploy a model and copy:
   - Resource endpoint (`https://<resource>.openai.azure.com/`)
   - API key
   - API version (for example `2024-10-21`)
   - Deployment name (this is what you pass as `MODEL`)

2. Create your local env file:

```bash
cp .env.example .env
```

3. Set Azure values in `.env`:

```env
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
AZURE_OPENAI_API_KEY=<your_azure_key>
AZURE_OPENAI_API_VERSION=2024-10-21

# Optional: set a default deployment so you don't pass MODEL every run
AZURE_OPENAI_DEPLOYMENT=<your_deployment_name>

# Optional: override API mode. If omitted, this app defaults to responses for Azure.
# OPENAI_API_MODE=chat_completions
```

If your Azure deployment returns `404 Resource not found` in `responses` mode, set:

```env
OPENAI_API_MODE=chat_completions
```

4. Run with your Azure deployment name as `MODEL`:

```bash
make run REPO=https://github.com/owner/repo.git MODEL=<your_deployment_name> OUTPUT=analysis.md
```

5. If you set `AZURE_OPENAI_DEPLOYMENT` in `.env`, you can also run:

```bash
make run REPO=https://github.com/owner/repo.git MODEL=${AZURE_OPENAI_DEPLOYMENT} OUTPUT=analysis.md
```

Manual path:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip setuptools wheel
pip install -e .
```

## Usage

With `Makefile`:

```bash
make run REPO=https://github.com/owner/repo.git OUTPUT=analysis.md
```

Run the coding agent:

```bash
make code REPO=/path/to/repo TASK="implement pagination in list endpoint"
```

Run the fresh secret sanitization workflow (always clones into a new run folder):

```bash
make secret-workflow REPO=https://github.com/owner/repo.git
```

Use a different workflow env file if needed:

```bash
make secret-workflow REPO=https://github.com/owner/repo.git SECRET_ENV_FILE=./.env.secret-workflow
```

For Azure OpenAI, set deployment names in `.env.secret-workflow`:

```env
SECRET_REVIEW_DEPLOYMENT=<your_5_2_review_deployment>
SECRET_CODE_DEPLOYMENT=<your_5_1_codex_deployment>
```

Optionally pass analysis output as context:

```bash
make code REPO=/path/to/repo TASK="implement top 3 backlog items" ANALYSIS_REPORT=analysis.md
```

Optional vars: `MODEL=...`, `FOCUS="..."`, `ENABLE_WEB_SEARCH=1`, `SHELL_AUTO_APPROVE=0`, `MAX_TURNS=30`, `MIN_STORIES=15`, `MIN_EVIDENCE=25`, `RETRIES=2`, `RETRY_BACKOFF_SECONDS=2.0`.
For Azure, `MODEL` should be your Azure deployment name.
Validation now runs on every report and does not block output writes. If checks fail, the report is still saved with an appended `## Quality Warning` section.

Command logging and diagnostics:

```bash
make run REPO=https://github.com/owner/repo.git OUTPUT=analysis.md \
  COMMAND_LOG_PATH=./data/commands.jsonl \
  COMMAND_LOG_MAX_OUTPUT_CHARS=4000
```

- If `COMMAND_LOG_PATH` is omitted, logs are written per run to `.agent-workspace/run-*/commands.jsonl`.
- Each run also writes `.agent-workspace/run-*/command-diagnostics.json` with command counts (total/failed/timed-out/blocked).
- Validation metadata (`passed`/`warning` + issue list) is stored in SQLite per report and shown in the local web UI.

Direct CLI:

Analyze a remote repository (agent clones it):

```bash
repo-req-analyzer --repo https://github.com/owner/repo.git
```

Analyze a local repository path:

```bash
repo-req-analyzer --repo /path/to/local/repo
```

Coding agent (edits code with MCP filesystem/git tools):

```bash
repo-req-code --repo /path/to/local/repo --task "fix failing auth tests"
```

Coding agent with prior analysis context:

```bash
repo-req-code --repo /path/to/local/repo --task "implement recommendations 1-3" --analysis-report analysis.md
```

Secret sanitization workflow:

```bash
repo-req-secret-workflow --repo https://github.com/owner/repo.git --task "prefer framework-native env loading"
```

Write output to a file:

```bash
repo-req-analyzer --repo https://github.com/owner/repo.git --output analysis.md
```

Enable command audit logging:

```bash
repo-req-analyzer --repo https://github.com/owner/repo.git --output analysis.md \
  --command-log-path ./data/commands.jsonl \
  --command-log-max-output-chars 4000
```

Extra analysis focus:

```bash
repo-req-analyzer --repo https://github.com/owner/repo.git --focus "billing, onboarding, access controls"
```

Enable cookbook-style web research tool in addition to shell:

```bash
repo-req-analyzer --repo https://github.com/owner/repo.git --enable-web-search
```

## Cookbook Alignment

The implementation follows the OpenAI cookbook approach for coding agents:
- custom shell executor for `ShellTool`
- command approval gate (`SHELL_AUTO_APPROVE=1` to auto-approve)
- optional built-in `WebSearchTool`

Autonomous coding agent specifics:
- uses function tools end-to-end (Azure-compatible)
- uses MCP filesystem + git servers as primary tools with optional shell fallback
- supports optional analysis handoff via `--analysis-report` without coupling to analyzer runtime

## Output

Markdown report with:
- repository summary
- personas/actors
- feature inventory
- user stories
- acceptance criteria
- evidence mapping to file paths
- risks/open questions
- next backlog items

Quality gates:
- minimum user stories (`MIN_STORIES`)
- minimum evidence rows/links (`MIN_EVIDENCE`)
- required section coverage

Run artifacts:
- deterministic repository scan per run: `.agent-workspace/run-*/scan.json`
- per-run report file: `.agent-workspace/run-*/report.md` (always written)
- per-run metadata summary: `.agent-workspace/run-*/run-summary.json` (model, endpoint, status, paths)

List most recent runs:

```bash
make recent-runs N=10
```

## Persist Reports In SQLite

You can keep markdown reports and also load structured data (features, stories, recommendations, evidence) into SQLite.

Ingest an existing report:

```bash
make ingest REPORT=analysis-eps-uwaste.md DB=./data/specs.db
```

Or ingest automatically when running analysis:

```bash
make run REPO=https://github.com/owner/repo.git OUTPUT=analysis.md
# direct CLI equivalent:
# repo-req-analyzer --repo ... --output analysis.md --db ./data/specs.db
```

Direct ingest CLI:

```bash
repo-req-ingest --report analysis-eps-uwaste.md --db ./data/specs.db --repo "https://dev.azure.com/upmc/_git/EPS%20-%20UWaste" --model gpt-5.1-codex
```

## Local Web App

Launch a local web frontend over the SQLite data:

```bash
make web DB=./data/specs.db HOST=127.0.0.1 PORT=8000
```

Then open:

```text
http://127.0.0.1:8000
```

Web UI capabilities:
- list all ingested reports
- inspect parsed features/stories/recommendations
- edit story status/notes
- edit recommendation status/notes
- view raw markdown report side-by-side
