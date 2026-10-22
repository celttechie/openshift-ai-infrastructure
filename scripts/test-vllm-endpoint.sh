#!/usr/bin/env bash
# ==============================================================================
# vLLM Inference Endpoint Test & Performance Benchmark
# Queries the OpenShift Route for the vLLM OpenAI-compatible API
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

REMOTE_HOST="${REMOTE_HOST:-t5600}"
BASTION_IP="${BASTION_IP:-192.168.150.10}"
BASTION_KEY="${BASTION_KEY:-/home/bjarrett/Projects/openshift-sno-lab/.keys/id_ed25519_operator}"

echo -e "${BLUE}${BOLD}================================================================${NC}"
echo -e "${BLUE}${BOLD}        vLLM Inference Endpoint Test & Verification             ${NC}"
echo -e "${BLUE}${BOLD}================================================================${NC}"

# Execute command on OpenShift bastion
oc_run() {
  local cmd="$1"
  ssh -q -i "${BASTION_KEY}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ProxyJump="${REMOTE_HOST}" "bjarrett@${BASTION_IP}" "oc ${cmd}"
}

echo -e "\n${BOLD}[1/4] Discovering vLLM Route...${NC}"
ROUTE_HOST=$(oc_run "get route vllm-route -n llm-serving -o jsonpath='{.spec.host}' 2>/dev/null" || echo "")

if [[ -z "${ROUTE_HOST}" ]]; then
  echo -e "  [${RED}FAIL${NC}] Could not find route 'vllm-route' in namespace 'llm-serving'."
  echo -e "         Ensure the vLLM deployment is applied: oc apply -k workloads/vllm/"
  exit 1
fi

echo -e "  [${GREEN}PASS${NC}] Found Route Host: ${BOLD}${ROUTE_HOST}${NC}"

echo -e "\n${BOLD}[2/4] Checking vLLM Health Status (/health)...${NC}"
HEALTH_STATUS=$(ssh -q -i "${BASTION_KEY}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ProxyJump="${REMOTE_HOST}" "bjarrett@${BASTION_IP}" "curl -k -s -o /dev/null -w '%{http_code}' https://${ROUTE_HOST}/health" || echo "000")

if [[ "${HEALTH_STATUS}" == "200" ]]; then
  echo -e "  [${GREEN}PASS${NC}] Engine Health: ${GREEN}200 OK${NC} (Model loaded and ready for inference)"
else
  echo -e "  [${YELLOW}WAIT${NC}] Engine Health: HTTP ${HEALTH_STATUS}."
  echo -e "         The model weights may still be downloading. Check logs with:"
  echo -e "         oc logs -f deployment/vllm-inference-engine -n llm-serving"
  exit 1
fi

echo -e "\n${BOLD}[3/4] Querying Loaded Models (/v1/models)...${NC}"
MODELS_JSON=$(ssh -q -i "${BASTION_KEY}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ProxyJump="${REMOTE_HOST}" "bjarrett@${BASTION_IP}" "curl -k -s https://${ROUTE_HOST}/v1/models" || echo "{}")
SERVED_MODEL=$(echo "${MODELS_JSON}" | grep -o '"id": *"[^"]*"' | head -n 1 | awk -F'"' '{print $4}' || echo "default")

echo -e "  [${GREEN}PASS${NC}] Active Model: ${BOLD}${SERVED_MODEL}${NC}"

echo -e "\n${BOLD}[4/4] Sending Inference Completion Request (/v1/chat/completions)...${NC}"
PROMPT="${1:-Explain how OpenShift SNO and NVIDIA VFIO GPU passthrough work together in two technical sentences.}"

echo -e "  ${BOLD}User Prompt:${NC} \"${PROMPT}\""
echo ""

START_TIME=$(date +%s%N)
RESPONSE=$(ssh -q -i "${BASTION_KEY}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ProxyJump="${REMOTE_HOST}" "bjarrett@${BASTION_IP}" "curl -k -s https://${ROUTE_HOST}/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    \"model\": \"${SERVED_MODEL}\",
    \"messages\": [{\"role\": \"user\", \"content\": \"${PROMPT}\"}],
    \"max_tokens\": 256,
    \"temperature\": 0.7
  }'")
END_TIME=$(date +%s%N)

DURATION_SEC=$(awk "BEGIN {print (${END_TIME} - ${START_TIME}) / 1000000000}")

CONTENT=$(echo "${RESPONSE}" | jq -r '.choices[0].message.content // empty' 2>/dev/null || echo "")
USAGE_TOKENS=$(echo "${RESPONSE}" | jq -r '.usage.completion_tokens // 0' 2>/dev/null || echo "0")

if [[ -n "${CONTENT}" ]]; then
  echo -e "${GREEN}${BOLD}Response:${NC}"
  echo -e "${CONTENT}"
  echo ""
  echo -e "  [${GREEN}PASS${NC}] Inference Completed in ${BOLD}${DURATION_SEC}s${NC}"
  if [[ "${USAGE_TOKENS}" != "0" && "${USAGE_TOKENS}" != "null" ]]; then
    TPS=$(awk -v t="${USAGE_TOKENS}" -v d="${DURATION_SEC}" 'BEGIN {if (d > 0) printf "%.2f", t / d; else print "0"}')
    echo -e "         Tokens Generated: ${BOLD}${USAGE_TOKENS}${NC} (~${TPS} tokens/sec)"
  fi
else
  echo -e "  [${RED}FAIL${NC}] Did not receive valid completion response. Raw response:"
  echo "${RESPONSE}"
fi

echo -e "\n${BLUE}${BOLD}================================================================${NC}"
echo -e "${BLUE}${BOLD}                 Inference Verification Complete                ${NC}"
echo -e "${BLUE}${BOLD}================================================================${NC}"
