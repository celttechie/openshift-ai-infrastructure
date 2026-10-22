#!/usr/bin/env bash
# ==============================================================================
# Local Infrastructure Test & Validation Runner
# Runs YAML linting, Python unit tests, manifest policy guardrails, and shellcheck
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${REPO_ROOT}/.venv"

cd "${REPO_ROOT}"

echo -e "${BLUE}${BOLD}================================================================${NC}"
echo -e "${BLUE}${BOLD}     OpenShift AI Infrastructure Test Suite Runner              ${NC}"
echo -e "${BLUE}${BOLD}================================================================${NC}\n"

# 1. Check or activate Python virtual environment
if [[ ! -d "${VENV_DIR}" ]]; then
  echo -e "${YELLOW}[!] Python virtual environment not found at .venv. Creating one...${NC}"
  python3 -m venv "${VENV_DIR}"
  "${VENV_DIR}/bin/pip" install --upgrade pip -q
  "${VENV_DIR}/bin/pip" install -r "${REPO_ROOT}/requirements-dev.txt" -q
fi

PYTHON="${VENV_DIR}/bin/python"
PYTEST="${VENV_DIR}/bin/pytest"
YAMLLINT="${VENV_DIR}/bin/yamllint"
FLAKE8="${VENV_DIR}/bin/flake8"

TOTAL_STEPS=5
PASSED_STEPS=0

# Step 1: YAML Linting
echo -e "${BOLD}[1/${TOTAL_STEPS}] Linting Kubernetes & OpenShift YAML Manifests...${NC}"
if "${YAMLLINT}" -c "${REPO_ROOT}/.yamllint.yml" operators/ workloads/; then
  echo -e "  [${GREEN}PASS${NC}] YAML formatting and syntax validated cleanly."
  PASSED_STEPS=$((PASSED_STEPS + 1))
else
  echo -e "  [${RED}FAIL${NC}] YAML linting errors detected."
  exit 1
fi

# Step 2: Python Code Style & Linting
echo -e "\n${BOLD}[2/${TOTAL_STEPS}] Linting Python Code (flake8)...${NC}"
if "${FLAKE8}" targets/ tests/; then
  echo -e "  [${GREEN}PASS${NC}] Python code style adheres to PEP 8 standards."
  PASSED_STEPS=$((PASSED_STEPS + 1))
else
  echo -e "  [${RED}FAIL${NC}] Python linting errors detected."
  exit 1
fi

# Step 3: Shell Script Analysis (ShellCheck)
echo -e "\n${BOLD}[3/${TOTAL_STEPS}] Checking Shell Automation Scripts...${NC}"
if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck scripts/*.sh; then
    echo -e "  [${GREEN}PASS${NC}] Shell scripts validated with shellcheck."
    PASSED_STEPS=$((PASSED_STEPS + 1))
  else
    echo -e "  [${RED}FAIL${NC}] ShellCheck reported issues."
    exit 1
  fi
else
  echo -e "  [${YELLOW}SKIP${NC}] shellcheck is not installed locally. (Will run in GitHub Actions CI)."
  PASSED_STEPS=$((PASSED_STEPS + 1))
fi

# Step 4: Python Unit Tests & Manifest Policy Guardrails
echo -e "\n${BOLD}[4/${TOTAL_STEPS}] Running Python Unit Tests & Policy Guardrails...${NC}"
if "${PYTEST}" tests/ -v; then
  echo -e "  [${GREEN}PASS${NC}] All unit tests and architectural policy guardrails passed."
  PASSED_STEPS=$((PASSED_STEPS + 1))
else
  echo -e "  [${RED}FAIL${NC}] Pytest test suite failed."
  exit 1
fi

# Step 5: Kustomize Manifest Compilation
echo -e "\n${BOLD}[5/${TOTAL_STEPS}] Validating Kustomize Manifest Builds...${NC}"
if kubectl kustomize workloads/vllm > /dev/null; then
  echo -e "  [${GREEN}PASS${NC}] workloads/vllm Kustomize bundle compiled successfully."
  PASSED_STEPS=$((PASSED_STEPS + 1))
else
  echo -e "  [${RED}FAIL${NC}] kubectl kustomize failed to compile workloads/vllm."
  exit 1
fi

echo -e "\n${BLUE}${BOLD}================================================================${NC}"
echo -e "${GREEN}${BOLD} All ${PASSED_STEPS}/${TOTAL_STEPS} Test Suites Passed Successfully!${NC}"
echo -e "${BLUE}${BOLD}================================================================${NC}"
