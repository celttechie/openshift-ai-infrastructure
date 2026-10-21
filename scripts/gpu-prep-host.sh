#!/usr/bin/env bash
# ==============================================================================
# GPU Host Preparation & Lifecycle Tool (Homelab VFIO Target)
# Manages releasing host GPU, binding to vfio-pci, and libvirt VM attachment
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

REMOTE_HOST="${REMOTE_HOST:-t5600}"
LIBVIRT_URI="${LIBVIRT_URI:-qemu+ssh://bjarrett@${REMOTE_HOST}/system}"
VM_DOMAIN="${VM_DOMAIN:-sno-node}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
HOST_ADMIN_SCRIPT="${REPO_ROOT}/targets/t5600-homelab/gpu-host-admin.py"

usage() {
  cat <<EOF
Usage: $(basename "$0") [COMMAND] [OPTIONS]

Commands:
  --check               (Default) Non-destructively inspect host GPU state and VM attachment.
  --bind-vfio           Release GPU from display manager/desktop and bind to vfio-pci.
  --attach-vm [DOMAIN]  Attach PCIe hostdev to target VM domain (default: ${VM_DOMAIN}).
  --full-prep [DOMAIN]  Run --bind-vfio followed by --attach-vm (complete onboarding).
  --restore-host        Detach GPU from VM and restore to host desktop & nvidia driver.
  --help                Show this help message.

Environment Variables:
  REMOTE_HOST           Target hypervisor host (default: t5600).
  LIBVIRT_URI           Libvirt system URI (default: qemu+ssh://bjarrett@\${REMOTE_HOST}/system).
  VM_DOMAIN             Default target VM domain (default: sno-node).
EOF
  exit 0
}

sync_admin_script() {
  if [[ -f "${HOST_ADMIN_SCRIPT}" ]]; then
    scp -q "${HOST_ADMIN_SCRIPT}" "${REMOTE_HOST}:/home/bjarrett/admin_tools/sb_admin.py"
  fi
}

exec_host_action() {
  local action="$1"
  sync_admin_script
  ssh "${REMOTE_HOST}" "echo '{\"action\": \"${action}\"}' > /home/bjarrett/admin_tools/action.json && sudo /usr/bin/python3 /home/bjarrett/admin_tools/sb_admin.py"
}

check_libvirt_attached() {
  local domain="$1"
  if virsh -c "${LIBVIRT_URI}" dominfo "${domain}" > /dev/null 2>&1; then
    if virsh -c "${LIBVIRT_URI}" dumpxml "${domain}" | grep -q "0x03' slot='0x00' function='0x0'"; then
      return 0
    fi
  fi
  return 1
}

do_check() {
  echo -e "${BLUE}${BOLD}=== GPU Host Preparation & Readiness Check ===${NC}"
  echo -e "Hypervisor Target: ${REMOTE_HOST}"
  echo -e "Target Domain:     ${VM_DOMAIN}\n"

  echo -e "${BOLD}[1/3] Querying Host GPU Driver & Process State...${NC}"
  CHECK_JSON=$(exec_host_action "check")
  
  VGA_DRIVER=$(echo "${CHECK_JSON}" | jq -r '.vga_driver // "unknown"')
  ACTIVE_PIDS=$(echo "${CHECK_JSON}" | jq -r '.active_gpu_pids | length')
  LIGHTDM=$(echo "${CHECK_JSON}" | jq -r '.lightdm_active')
  READY_VFIO=$(echo "${CHECK_JSON}" | jq -r '.ready_for_vfio')

  echo -e "  Current VGA Driver in use: ${BOLD}${VGA_DRIVER}${NC}"
  if [[ "${LIGHTDM}" == "true" ]]; then
    echo -e "  [${YELLOW}WARN${NC}] Desktop display manager (lightdm) is currently ACTIVE."
  else
    echo -e "  [${GREEN}PASS${NC}] Desktop display manager is STOPPED."
  fi

  if [[ "${ACTIVE_PIDS}" -gt 0 ]]; then
    echo -e "  [${YELLOW}WARN${NC}] ${ACTIVE_PIDS} active process(es) holding /dev/nvidia* device handles."
  else
    echo -e "  [${GREEN}PASS${NC}] No active host processes holding GPU handles."
  fi

  if [[ "${READY_VFIO}" == "true" ]]; then
    echo -e "  [${GREEN}PASS${NC}] GPU is cleanly bound to vfio-pci."
  else
    echo -e "  [${YELLOW}WARN${NC}] GPU is NOT bound to vfio-pci (currently '${VGA_DRIVER}')."
  fi

  echo -e "\n${BOLD}[2/3] Checking Libvirt VM Hostdev Attachment...${NC}"
  if check_libvirt_attached "${VM_DOMAIN}"; then
    echo -e "  [${GREEN}PASS${NC}] GPU PCIe hostdev (03:00.0 / 03:00.1) is ATTACHED to '${VM_DOMAIN}'."
  else
    echo -e "  [${YELLOW}INFO${NC}] GPU is NOT currently attached to domain '${VM_DOMAIN}'."
  fi

  echo -e "\n${BOLD}[3/3] Overall Readiness Recommendation:${NC}"
  if [[ "${READY_VFIO}" == "true" ]] && check_libvirt_attached "${VM_DOMAIN}"; then
    echo -e "  [${GREEN}READY${NC}] Host and VM are fully configured for Kubernetes/OpenShift AI workloads!"
  elif [[ "${READY_VFIO}" == "true" ]]; then
    echo -e "  [${YELLOW}ACTION REQUIRED${NC}] Run: $(basename "$0") --attach-vm ${VM_DOMAIN}"
  else
    echo -e "  [${YELLOW}ACTION REQUIRED${NC}] Run: $(basename "$0") --full-prep ${VM_DOMAIN}"
  fi
}

do_bind_vfio() {
  echo -e "${BLUE}${BOLD}=== Transitioning GPU to vfio-pci on ${REMOTE_HOST} ===${NC}"
  exec_host_action "bind_vfio"
  echo -e "[${GREEN}SUCCESS${NC}] GPU is bound to vfio-pci."
}

do_attach_vm() {
  local domain="${1:-${VM_DOMAIN}}"
  echo -e "${BLUE}${BOLD}=== Attaching GPU to VM Domain: ${domain} ===${NC}"

  if check_libvirt_attached "${domain}"; then
    echo -e "  [${GREEN}PASS${NC}] GPU is already attached to '${domain}'."
    return 0
  fi

  # Create device XMLs on hypervisor
  ssh "${REMOTE_HOST}" "cat <<'EOF' > /tmp/gpu_vga.xml
<hostdev mode='subsystem' type='pci' managed='yes'>
  <source>
    <address domain='0x0000' bus='0x03' slot='0x00' function='0x0'/>
  </source>
  <rom bar='on'/>
</hostdev>
EOF
cat <<'EOF' > /tmp/gpu_audio.xml
<hostdev mode='subsystem' type='pci' managed='yes'>
  <source>
    <address domain='0x0000' bus='0x03' slot='0x00' function='0x1'/>
  </source>
</hostdev>
EOF
virsh -c qemu:///system attach-device '${domain}' /tmp/gpu_vga.xml --live --config
virsh -c qemu:///system attach-device '${domain}' /tmp/gpu_audio.xml --live --config
"
  echo -e "[${GREEN}SUCCESS${NC}] Successfully attached GPU (VGA + Audio) to '${domain}'."
}

do_restore_host() {
  local domain="${1:-${VM_DOMAIN}}"
  echo -e "${BLUE}${BOLD}=== Restoring GPU to Host Desktop on ${REMOTE_HOST} ===${NC}"

  # Detach from VM if attached
  if check_libvirt_attached "${domain}"; then
    echo "Detaching PCIe hostdev from '${domain}' in libvirt..."
    ssh "${REMOTE_HOST}" "virsh -c qemu:///system detach-device '${domain}' /tmp/gpu_vga.xml --live --config 2>/dev/null || true
virsh -c qemu:///system detach-device '${domain}' /tmp/gpu_audio.xml --live --config 2>/dev/null || true"
  fi

  exec_host_action "restore_host"
  echo -e "[${GREEN}SUCCESS${NC}] GPU and desktop display services restored to host."
}

CMD="${1:---check}"
shift || true

case "${CMD}" in
  --check)
    do_check
    ;;
  --bind-vfio)
    do_bind_vfio
    ;;
  --attach-vm)
    do_attach_vm "${1:-${VM_DOMAIN}}"
    ;;
  --full-prep)
    do_bind_vfio
    do_attach_vm "${1:-${VM_DOMAIN}}"
    ;;
  --restore-host)
    do_restore_host "${1:-${VM_DOMAIN}}"
    ;;
  --help|-h)
    usage
    ;;
  *)
    echo -e "${RED}Unknown command: ${CMD}${NC}"
    usage
    ;;
esac
