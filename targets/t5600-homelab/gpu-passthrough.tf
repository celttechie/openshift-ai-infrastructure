# ==============================================================================
# Homelab GPU Passthrough Module (Dell Precision T5600 -> OpenShift SNO)
# Declares PCIe host device passthrough for NVIDIA GeForce RTX 3060 (03:00.0)
# ==============================================================================

# Libvirt PCI Host Device Attachment Snippet
# Injects the physical PCIe GPU into the libvirt domain XML
locals {
  gpu_pci_bus      = "0x03"
  gpu_pci_slot     = "0x00"
  gpu_pci_function = "0x0"
  audio_function   = "0x1"

  # XML block to append to libvirt_domain
  gpu_passthrough_xml = <<-EOT
    <hostdev mode='subsystem' type='pci' managed='yes'>
      <source>
        <address domain='0x0000' bus='${local.gpu_pci_bus}' slot='${local.gpu_pci_slot}' function='${local.gpu_pci_function}'/>
      </source>
      <rom bar='on'/>
    </hostdev>
    <hostdev mode='subsystem' type='pci' managed='yes'>
      <source>
        <address domain='0x0000' bus='${local.gpu_pci_bus}' slot='${local.gpu_pci_slot}' function='${local.audio_function}'/>
      </source>
    </hostdev>
  EOT
}

output "gpu_passthrough_xml" {
  description = "Libvirt XML snippet to attach NVIDIA RTX 3060 to guest domain"
  value       = local.gpu_passthrough_xml
}
