#!/usr/bin/env bash
# ==============================================================================
# Kubernetes / OpenShift GPU Diagnostic & Troubleshooting Suite
# Validates PCIe hardware, IOMMU isolation, VFIO passthrough, and K8s GPU discovery
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

REMOTE_HOST="${REMOTE_HOST:-t5600}"

echo -e "${BLUE}${BOLD}================================================================${NC}"
echo -e "${BLUE}${BOLD}      Kubernetes & OpenShift GPU Diagnostic & Health Suite      ${NC}"
echo -e "${BLUE}${BOLD}================================================================${NC}"
echo -e "Hypervisor Target: ${REMOTE_HOST}"
echo ""

ALL_PASS=true

# ------------------------------------------------------------------------------
# 1. Hypervisor PCIe GPU Discovery
# ------------------------------------------------------------------------------
echo -e "${BOLD}[1/5] Checking Hypervisor PCIe GPU Presence...${NC}"

if ssh -q "${REMOTE_HOST}" "lspci | grep -i -E 'nvidia|ga106'" > /dev/null 2>&1; then
  GPU_DEVICE=$(ssh "${REMOTE_HOST}" "lspci | grep -i -E 'nvidia|ga106'" | head -n 1)
  echo -e "  [${GREEN}PASS${NC}] Found physical GPU: ${GPU_DEVICE}"
else
  echo -e "  [${YELLOW}WARN${NC}] No NVIDIA PCIe device detected via lspci on ${REMOTE_HOST}."
  ALL_PASS=false
fi

# ------------------------------------------------------------------------------
# 2. IOMMU Isolation Check
# ------------------------------------------------------------------------------
echo -e "\n${BOLD}[2/5] Checking IOMMU Group Isolation for VFIO Passthrough...${NC}"

IOMMU_OUTPUT=$(ssh "${REMOTE_HOST}" "for d in /sys/kernel/iommu_groups/*/devices/*; do if echo \"\$d\" | grep -q '03:00'; then echo \"\$(basename \"\$d\") in Group \$(echo \"\$d\" | awk -F/ '{print \$(NF-2)}')\"; fi; done" || true)

if [[ -n "${IOMMU_OUTPUT}" ]]; then
  echo -e "  [${GREEN}PASS${NC}] IOMMU grouping detected:"
  while IFS= read -r line; do
    echo -e "         -> ${line}"
  done <<< "${IOMMU_OUTPUT}"
else
  echo -e "  [${YELLOW}WARN${NC}] IOMMU groups not populated. Check if intel_iommu=on / amd_iommu=on is set in kernel cmdline."
fi

# ------------------------------------------------------------------------------
# 3. Host Kernel Driver Status
# ------------------------------------------------------------------------------
echo -e "\n${BOLD}[3/5] Inspecting Active Kernel Driver Binding...${NC}"

DRIVER_IN_USE=$(ssh "${REMOTE_HOST}" "lspci -nnk -s 03:00.0 | grep 'Kernel driver in use' | awk '{print \$5}'" || echo "none")

echo -e "  Current Driver in use: ${BOLD}${DRIVER_IN_USE}${NC}"
VM_DOMAIN="${VM_DOMAIN:-sno-node}"
LIBVIRT_URI="${LIBVIRT_URI:-qemu+ssh://bjarrett@${REMOTE_HOST}/system}"

if [[ "${DRIVER_IN_USE}" == "vfio-pci" ]]; then
  echo -e "  [${GREEN}PASS${NC}] GPU is cleanly bound to vfio-pci (Ready for VM Passthrough)."
  if virsh -c "${LIBVIRT_URI}" dominfo "${VM_DOMAIN}" > /dev/null 2>&1; then
    if virsh -c "${LIBVIRT_URI}" dumpxml "${VM_DOMAIN}" | grep -q "0x03' slot='0x00' function='0x0'"; then
      echo -e "  [${GREEN}PASS${NC}] GPU PCIe hostdev is attached to domain '${VM_DOMAIN}'."
    else
      echo -e "  [${YELLOW}INFO${NC}] GPU not yet attached to '${VM_DOMAIN}'. Run: ./scripts/gpu-prep-host.sh --attach-vm ${VM_DOMAIN}"
    fi
  fi
elif [[ "${DRIVER_IN_USE}" == "nvidia" ]]; then
  echo -e "  [${YELLOW}WARN${NC}] GPU is claimed by host nvidia driver / desktop."
  echo -e "         To free and bind to OpenShift, run: ./scripts/gpu-prep-host.sh --full-prep ${VM_DOMAIN}"
else
  echo -e "  [${YELLOW}WARN${NC}] Driver '${DRIVER_IN_USE}'. Ensure vfio-pci or nvidia driver is loaded."
fi

# ------------------------------------------------------------------------------
# 4. OpenShift / Kubernetes Cluster Connectivity & Node Resource Discovery
# ------------------------------------------------------------------------------
echo -e "\n${BOLD}[4/5] Checking Kubernetes Node Allocatable GPU Resources...${NC}"

BASTION_KEY="/home/bjarrett/Projects/openshift-sno-lab/.keys/id_ed25519_operator"
if [[ -z "${K8S_CMD:-}" ]]; then
  if [[ -f "${BASTION_KEY}" ]] && ssh -q -o BatchMode=yes -o ConnectTimeout=3 "${REMOTE_HOST}" "true" 2>/dev/null; then
    K8S_CMD="ssh -q -i ${BASTION_KEY} -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ProxyJump=${REMOTE_HOST} bjarrett@192.168.150.10 oc"
  elif command -v oc > /dev/null 2>&1; then
    K8S_CMD="oc"
  elif command -v kubectl > /dev/null 2>&1; then
    K8S_CMD="kubectl"
  else
    K8S_CMD=""
  fi
fi

k8s_run() {
  local cmd="$1"
  if [[ "${K8S_CMD}" =~ ^ssh ]]; then
    ${K8S_CMD} "${cmd}"
  else
    ${K8S_CMD} ${cmd}
  fi
}

if [[ -n "${K8S_CMD}" ]] && k8s_run "get nodes" > /dev/null 2>&1; then
  echo -e "  [${GREEN}PASS${NC}] Connected to cluster."
  
  # Check for nvidia.com/gpu in allocatable
  GPU_ALLOCATABLE=$(k8s_run "get nodes -o jsonpath='{.items[*].status.allocatable.nvidia\.com/gpu}'" 2>/dev/null || echo "")
  if [[ -n "${GPU_ALLOCATABLE}" && "${GPU_ALLOCATABLE}" != "0" ]]; then
    echo -e "  [${GREEN}PASS${NC}] Node advertises allocatable GPU: ${BOLD}${GPU_ALLOCATABLE}${NC}"
  else
    echo -e "  [${YELLOW}INFO${NC}] No 'nvidia.com/gpu' resource currently advertised in cluster."
    echo -e "         (Install NVIDIA GPU Operator or configure time-slicing to advertise virtual slices)."
  fi
else
  echo -e "  [${YELLOW}INFO${NC}] Cluster API not reachable from this environment."
fi

# ------------------------------------------------------------------------------
# 5. GPU Operator & Pod Workload Inspection
# ------------------------------------------------------------------------------
echo -e "\n${BOLD}[5/5] Checking Active GPU Pods & Scheduled Allocations...${NC}"

if [[ -n "${K8S_CMD}" ]] && k8s_run "get nodes" > /dev/null 2>&1; then
  GPU_PODS=$(k8s_run "get pods -A -o custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name,GPU:.spec.containers[*].resources.limits.'nvidia\.com/gpu' --no-headers" 2>/dev/null | grep -v '<none>' || true)
  if [[ -n "${GPU_PODS}" ]]; then
    echo -e "  [${GREEN}PASS${NC}] Active GPU Workloads Found:"
    echo "${GPU_PODS}" | awk '{printf "         -> %-20s %-30s (GPU Request: %s)\n", $1, $2, $3}'
  else
    echo -e "  [${BLUE}INFO${NC}] No active pods requesting 'nvidia.com/gpu' at this time."
  fi
fi

echo -e "\n${BLUE}${BOLD}================================================================${NC}"
echo -e "${BLUE}${BOLD}              GPU Diagnostic Inspection Complete                ${NC}"
echo -e "${BLUE}${BOLD}================================================================${NC}"
