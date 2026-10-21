# ==============================================================================
# Cisco Intersight Policies - AI Compute Tuning
# Models low-latency BIOS and hardware policies for UCS AI Workload Nodes
# ==============================================================================

# 1. BIOS Policy optimized for GPU Compute, NUMA locality, and SR-IOV
resource "intersight_bios_policy" "ai_node_bios" {
  name        = "${var.cluster_name}-ai-bios-policy"
  description = "BIOS policy tuned for Kubernetes AI nodes, PCIe Gen5 bifurcation, and NUMA optimization"

  organization {
    object_type = "organization.Organization"
    moid        = data.intersight_organization_organization.org.results[0].moid
  }

  # Enterprise AI Tuning: Disable processor C-States, optimize memory frequency
  processor_cstate = "disabled"
  sr_iov           = "enabled"
  intel_virtualization_technology = "enabled"
}
