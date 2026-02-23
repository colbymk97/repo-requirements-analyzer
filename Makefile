PYTHON ?= python3.12
VENV_DIR ?= .venv
VENV_PYTHON := $(VENV_DIR)/bin/python
VENV_PIP := $(VENV_DIR)/bin/pip
AGENT := $(VENV_DIR)/bin/repo-req-analyzer
CODE_AGENT := $(VENV_DIR)/bin/repo-req-code
SECRET_WORKFLOW := $(VENV_DIR)/bin/repo-req-secret-workflow

# Load local environment variables when present (e.g. OPENAI_API_KEY).
-include .env
export OPENAI_API_KEY
OPENAI_BASE_URL ?= https://api.openai.com/v1
export OPENAI_BASE_URL
export OPENAI_API_MODE
export AZURE_OPENAI_ENDPOINT
export ENDPOINT
export AZURE_OPENAI_API_KEY
export AZURE_OPENAI_API_VERSION
export AZURE_OPENAI_DEPLOYMENT

# Backward-compatible alias: allow ENDPOINT in .env to behave like AZURE_OPENAI_ENDPOINT.
AZURE_OPENAI_ENDPOINT ?= $(ENDPOINT)

REPO ?=
MODEL ?= gpt-5.1-codex
CODE_MODEL ?= $(if $(AZURE_OPENAI_DEPLOYMENT),$(AZURE_OPENAI_DEPLOYMENT),gpt-5.1-codex-mini)
SECRET_ENV_FILE ?= ./.env.secret-workflow
SECRET_REVIEW_MODEL ?= gpt-5.2-chat
SECRET_CODE_MODEL ?= $(CODE_MODEL)
SECRET_REVIEW_DEPLOYMENT ?=
SECRET_CODE_DEPLOYMENT ?=
SECRET_CODE_BACKEND ?= agents_sdk
SECRET_CODEX_PROFILE ?=
CODEX_TIMEOUT_SECONDS ?= 600
export SECRET_REVIEW_MODEL
export SECRET_CODE_MODEL
export SECRET_REVIEW_DEPLOYMENT
export SECRET_CODE_DEPLOYMENT
export SECRET_CODE_BACKEND
export SECRET_CODEX_PROFILE
WORKSPACE ?= ./.agent-workspace
OUTPUT ?=
TASK ?=
ANALYSIS_REPORT ?=
FOCUS ?=
ENABLE_WEB_SEARCH ?= 0
SHELL_AUTO_APPROVE ?= 1
MAX_TURNS ?= 30
MIN_STORIES ?= 15
MIN_EVIDENCE ?= 25
RETRIES ?= 2
RETRY_BACKOFF_SECONDS ?= 2.0
SKIP_VALIDATION ?= 0
COMMAND_LOG_PATH ?=
COMMAND_LOG_MAX_OUTPUT_CHARS ?= 4000
DB ?= ./data/specs.db
REPORT ?=
HOST ?= 127.0.0.1
PORT ?= 8000
N ?= 10

.PHONY: help setup run code secret-workflow ingest web recent-runs clean

help:
	@echo "Targets:"
	@echo "  make setup                         Create venv and install project in editable mode"
	@echo "  make run REPO=<url-or-path>        Run analyzer from the venv"
	@echo "  make code REPO=<url-or-path> TASK=<text>  Run coding agent from the venv"
	@echo "  make secret-workflow REPO=<git-url> Clone fresh + model review + sanitize via code agent"
	@echo "  make ingest REPORT=<file>          Ingest markdown report into SQLite"
	@echo "  make web                           Run local web UI for stored reports"
	@echo "  make recent-runs [N=10]            List top N most recent run work folders"
	@echo "  make clean                         Remove venv and agent workspace"
	@echo ""
	@echo "Optional run vars:"
	@echo "  MODEL=<model>                      Default: $(MODEL)"
	@echo "  WORKSPACE=<path>                   Default: $(WORKSPACE)"
	@echo "  OUTPUT=<file>                      Write report to file"
	@echo "  TASK=<text>                        Required for make code"
	@echo "  CODE_MODEL=<model-or-deployment>   Default: $(CODE_MODEL)"
	@echo "  SECRET_ENV_FILE=<path>             Default: $(SECRET_ENV_FILE)"
	@echo "  SECRET_REVIEW_MODEL=<model>        Default: $(SECRET_REVIEW_MODEL)"
	@echo "  SECRET_CODE_MODEL=<model>          Default: $(SECRET_CODE_MODEL)"
	@echo "  SECRET_REVIEW_DEPLOYMENT=<name>    Azure deployment for 5.2 review model"
	@echo "  SECRET_CODE_DEPLOYMENT=<name>      Azure deployment for 5.1 code model"
	@echo "  SECRET_CODE_BACKEND=<backend>      agents_sdk or codex_cli (default: $(SECRET_CODE_BACKEND))"
	@echo "  SECRET_CODEX_PROFILE=<profile>     Codex CLI profile (example: azure)"
	@echo "  CODEX_TIMEOUT_SECONDS=<sec>        Codex CLI timeout (default: $(CODEX_TIMEOUT_SECONDS))"
	@echo "  ANALYSIS_REPORT=<file>             Optional analysis markdown context for make code"
	@echo "  FOCUS=<text>                       Extra analysis focus"
	@echo "  ENABLE_WEB_SEARCH=1                Add WebSearchTool"
	@echo "  SHELL_AUTO_APPROVE=0               Prompt before shell commands"
	@echo "  MAX_TURNS=30                       Maximum agent turns"
	@echo "  MIN_STORIES=15                     Minimum user stories required"
	@echo "  MIN_EVIDENCE=25                    Minimum evidence rows/links required"
	@echo "  RETRIES=2                          Retries for transient API errors"
	@echo "  RETRY_BACKOFF_SECONDS=2.0          Initial retry backoff seconds"
	@echo "  SKIP_VALIDATION=1                  Deprecated (validation is warning-only and always runs)"
	@echo "  COMMAND_LOG_PATH=<file>            Optional JSONL command audit log path"
	@echo "  COMMAND_LOG_MAX_OUTPUT_CHARS=4000  Max chars per stdout/stderr in logs"
	@echo "  OPENAI_API_KEY                     Loaded from .env when present"
	@echo "  OPENAI_BASE_URL                    Optional custom OpenAI-compatible base URL"
	@echo "  OPENAI_API_MODE                    responses (default) or chat_completions"
	@echo "  AZURE_OPENAI_ENDPOINT              Azure OpenAI resource endpoint"
	@echo "  AZURE_OPENAI_API_KEY               Azure OpenAI key"
	@echo "  AZURE_OPENAI_API_VERSION           Azure API version (required for Azure)"
	@echo "  AZURE_OPENAI_DEPLOYMENT            Optional default deployment name"
	@echo "  DB=./data/specs.db                 SQLite path for ingest/web"

setup:
	$(PYTHON) -m venv $(VENV_DIR)
	$(VENV_PYTHON) -m pip install -U pip setuptools wheel
	$(VENV_PIP) install -e .

run:
	@if [ -z "$(REPO)" ]; then echo "REPO is required. Example: make run REPO=https://github.com/owner/repo.git"; exit 1; fi
	@if [ -z "$$OPENAI_API_KEY" ] && [ -z "$$AZURE_OPENAI_API_KEY" ]; then echo "Set OPENAI_API_KEY or AZURE_OPENAI_API_KEY."; exit 1; fi
	SHELL_AUTO_APPROVE=$(SHELL_AUTO_APPROVE) $(AGENT) --repo "$(REPO)" --model "$(MODEL)" --workspace "$(WORKSPACE)" --max-turns "$(MAX_TURNS)" --min-stories "$(MIN_STORIES)" --min-evidence "$(MIN_EVIDENCE)" --retries "$(RETRIES)" --retry-backoff-seconds "$(RETRY_BACKOFF_SECONDS)" --command-log-max-output-chars "$(COMMAND_LOG_MAX_OUTPUT_CHARS)" $(if $(filter 1,$(SKIP_VALIDATION)),--skip-validation,) $(if $(DB),--db "$(DB)",) $(if $(OUTPUT),--output "$(OUTPUT)",) $(if $(FOCUS),--focus "$(FOCUS)",) $(if $(COMMAND_LOG_PATH),--command-log-path "$(COMMAND_LOG_PATH)",) $(if $(filter 1,$(ENABLE_WEB_SEARCH)),--enable-web-search,)

code:
	@if [ -z "$(REPO)" ]; then echo "REPO is required. Example: make code REPO=/path/to/repo TASK='add endpoint'"; exit 1; fi
	@if [ -z "$(TASK)" ]; then echo "TASK is required. Example: make code REPO=/path/to/repo TASK='fix failing tests'"; exit 1; fi
	@if [ -z "$$OPENAI_API_KEY" ] && [ -z "$$AZURE_OPENAI_API_KEY" ]; then echo "Set OPENAI_API_KEY or AZURE_OPENAI_API_KEY."; exit 1; fi
	SHELL_AUTO_APPROVE=$(SHELL_AUTO_APPROVE) $(CODE_AGENT) --repo "$(REPO)" --task "$(TASK)" --model "$(CODE_MODEL)" --workspace "$(WORKSPACE)" --max-turns "$(MAX_TURNS)" --retries "$(RETRIES)" --retry-backoff-seconds "$(RETRY_BACKOFF_SECONDS)" --command-log-max-output-chars "$(COMMAND_LOG_MAX_OUTPUT_CHARS)" $(if $(OUTPUT),--output "$(OUTPUT)",) $(if $(ANALYSIS_REPORT),--analysis-report "$(ANALYSIS_REPORT)",) $(if $(filter 1,$(ENABLE_WEB_SEARCH)),--enable-web-search,)

secret-workflow:
	@if [ -z "$(REPO)" ]; then echo "REPO is required. Example: make secret-workflow REPO=https://github.com/owner/repo.git"; exit 1; fi
	@set -a; \
		if [ -f "$(SECRET_ENV_FILE)" ]; then . "$(SECRET_ENV_FILE)"; fi; \
		set +a; \
		if [ -z "$$OPENAI_API_KEY" ] && [ -z "$$AZURE_OPENAI_API_KEY" ]; then echo "Set OPENAI_API_KEY or AZURE_OPENAI_API_KEY (or configure $(SECRET_ENV_FILE))."; exit 1; fi; \
		CODE_MODEL_RESOLVED="$${SECRET_CODE_DEPLOYMENT:-$${SECRET_CODE_MODEL:-$(SECRET_CODE_MODEL)}}"; \
		REVIEW_MODEL_RESOLVED="$${SECRET_REVIEW_MODEL:-$(SECRET_REVIEW_MODEL)}"; \
		REVIEW_DEPLOYMENT_RESOLVED="$${SECRET_REVIEW_DEPLOYMENT:-}"; \
		CODE_BACKEND_RESOLVED="$${SECRET_CODE_BACKEND:-$(SECRET_CODE_BACKEND)}"; \
		CODEX_PROFILE_RESOLVED="$${SECRET_CODEX_PROFILE:-$(SECRET_CODEX_PROFILE)}"; \
		echo "Using secret workflow env: $(SECRET_ENV_FILE)"; \
		echo "Review model: $$REVIEW_MODEL_RESOLVED"; \
		[ -n "$$REVIEW_DEPLOYMENT_RESOLVED" ] && echo "Review deployment: $$REVIEW_DEPLOYMENT_RESOLVED" || true; \
		echo "Code model: $$CODE_MODEL_RESOLVED"; \
		echo "Code backend: $$CODE_BACKEND_RESOLVED"; \
		[ -n "$$CODEX_PROFILE_RESOLVED" ] && echo "Codex profile: $$CODEX_PROFILE_RESOLVED" || true; \
		SECRET_REVIEW_MODEL="$$REVIEW_MODEL_RESOLVED" SECRET_REVIEW_DEPLOYMENT="$$REVIEW_DEPLOYMENT_RESOLVED" AZURE_OPENAI_DEPLOYMENT="$$CODE_MODEL_RESOLVED" SHELL_AUTO_APPROVE=$(SHELL_AUTO_APPROVE) $(SECRET_WORKFLOW) --repo "$(REPO)" --task "$(TASK)" --model "$$CODE_MODEL_RESOLVED" --code-backend "$$CODE_BACKEND_RESOLVED" --codex-profile "$$CODEX_PROFILE_RESOLVED" --codex-timeout-seconds "$(CODEX_TIMEOUT_SECONDS)" --workspace "$(WORKSPACE)" --max-turns "$(MAX_TURNS)" --retries "$(RETRIES)" --retry-backoff-seconds "$(RETRY_BACKOFF_SECONDS)" --command-log-max-output-chars "$(COMMAND_LOG_MAX_OUTPUT_CHARS)" $(if $(filter 1,$(ENABLE_WEB_SEARCH)),--enable-web-search,)

ingest:
	@if [ -z "$(REPORT)" ]; then echo "REPORT is required. Example: make ingest REPORT=analysis.md"; exit 1; fi
	$(VENV_DIR)/bin/repo-req-ingest --report "$(REPORT)" --db "$(DB)"

web:
	$(VENV_DIR)/bin/repo-req-web --db "$(DB)" --host "$(HOST)" --port "$(PORT)"

recent-runs:
	@echo "Workspace: $(WORKSPACE)"
	@ls -1dt "$(WORKSPACE)"/run-* 2>/dev/null | head -n "$(N)" | while read -r run; do \
		echo "- $$(basename "$$run")"; \
		if [ -f "$$run/run-summary.json" ]; then \
			PY_BIN="$(VENV_PYTHON)"; [ -x "$$PY_BIN" ] || PY_BIN=python3; \
			"$$PY_BIN" -c "import json,sys; s=json.load(open(sys.argv[1], encoding='utf-8')); print('  finished: ' + (s.get('finished_at_utc') or '')); print('  status: ' + (s.get('run_status') or '')); m=s.get('model_used') or ''; e=s.get('endpoint_used') or ''; r=s.get('report_path') or ''; print('  model: ' + m) if m else None; print('  endpoint: ' + e) if e else None; print('  report: ' + r) if r else None" "$$run/run-summary.json"; \
		fi; \
	done

clean:
	@echo "Removing $(VENV_DIR) and .agent-workspace"
	$(PYTHON) -c "import shutil; shutil.rmtree('$(VENV_DIR)', ignore_errors=True); shutil.rmtree('.agent-workspace', ignore_errors=True)"
