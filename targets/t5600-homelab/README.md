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

## 3. Automated Host Lifecycle Tool (`scripts/gpu-prep-host.sh`)

Instead of manually stopping systemd display services and unbinding kernel drivers, use the automated host lifecycle utility:

```bash
# 1. Non-destructively inspect host driver state, GPU processes, and VM attachment
./scripts/gpu-prep-host.sh --check

# 2. Automated complete onboarding (releases host display, binds to vfio-pci, and attaches to VM)
./scripts/gpu-prep-host.sh --full-prep sno-node

# 3. Restore GPU to host desktop & NoMachine when lab testing is finished
./scripts/gpu-prep-host.sh --restore-host sno-node
```

---

## 4. Libvirt Domain Attachment (`gpu-passthrough.tf`)

When launching or updating the OpenShift SNO VM in OpenTofu or libvirt, the PCI hostdev blocks attach the card:

```xml
<hostdev mode='subsystem' type='pci' managed='yes'>
  <source>
    <address domain='0x0000' bus='0x03' slot='0x00' function='0x0'/>
  </source>
  <rom bar='on'/>
</hostdev>
<hostdev mode='subsystem' type='pci' managed='yes'>
  <source>
    <address domain='0x0000' bus='0x03' slot='0x00' function='0x1'/>
  </source>
</hostdev>
```

Once booted or attached live, the SNO node has native, direct access to the NVIDIA RTX 3060's 3,584 CUDA cores and 12GB GDDR6 VRAM with zero hypervisor virtualization overhead!
