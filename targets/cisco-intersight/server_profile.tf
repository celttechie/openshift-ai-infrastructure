# ==============================================================================
# Cisco Intersight Server Profile - UCS AI Node
# Represents a bare-metal Cisco UCS X-Series Blade / C-Series Rack Server
# ==============================================================================

resource "intersight_server_profile" "ai_worker_node" {
  name        = "${var.cluster_name}-ai-worker-01"
  description = "Cisco UCS Server Profile for OpenShift / Kubernetes AI Worker Node with NVIDIA GPUs"
  target_platform = "FIAttached"

  organization {
    object_type = "organization.Organization"
    moid        = data.intersight_organization_organization.org.results[0].moid
  }

  policy_bucket {
    moid        = intersight_bios_policy.ai_node_bios.moid
    object_type = "bios.Policy"
  }
}
