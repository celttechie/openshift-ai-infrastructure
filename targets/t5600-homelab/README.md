# Homelab Target: Dell Precision T5600 PCIe VFIO GPU Passthrough

This target documents and automates passing through a physical **NVIDIA GeForce RTX 3060** PCIe graphics card from a Dell Precision T5600 KVM host directly into a Single Node OpenShift (SNO) or Kubernetes virtual machine.

---

## 1. Hardware Verification & IOMMU Topology

On the Dell Precision T5600 host:
- **VGA Controller**: `0000:03:00.0` (NVIDIA Corporation GA106 [GeForce RTX 3060 Lite Hash Rate], vendor/device ID `10de:2504`).
- **Audio Device**: `0000:03:00.1` (NVIDIA Corporation GA106 HD Audio, vendor/device ID `10de:228e`).
- **IOMMU Group**: **Group 23** (Completely isolated! No other motherboard peripherals share Group 23).

---

## 2. Host Configuration for VFIO Passthrough

To allow KVM/QEMU to seize exclusive control of the GPU without the host Linux desktop claiming it:

1. **Kernel Parameters (`/etc/default/grub`)**:
   Ensure Intel VT-d and VFIO are enabled:
   ```bash
   GRUB_CMDLINE_LINUX="intel_iommu=on iommu=pt"
   ```
2. **VFIO Driver Binding (`/etc/modprobe.d/vfio.conf`)**:
   Tell the Linux kernel to bind the RTX 3060 to `vfio-pci` on boot:
   ```bash
   options vfio-pci ids=10de:2504,10de:228e
   ```

---

## 3. Libvirt Domain Attachment (`gpu-passthrough.tf`)

When launching or updating the OpenShift SNO VM in OpenTofu, include the PCI hostdev block:

```xml
<hostdev mode='subsystem' type='pci' managed='yes'>
  <source>
    <address domain='0x0000' bus='0x03' slot='0x00' function='0x0'/>
  </source>
  <rom bar='on'/>
</hostdev>
```

Once booted, the SNO node has native, direct access to the NVIDIA RTX 3060's 3,584 CUDA cores and 12GB GDDR6 VRAM with zero hypervisor virtualization overhead!
