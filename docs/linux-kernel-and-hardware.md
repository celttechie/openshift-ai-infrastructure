# Linux Systems Engineering: Hardware, Kernel & Virtualization Guide

A comprehensive systems engineering guide covering low-level Linux kernel architecture, PCIe hardware passthrough, IOMMU grouping, VFIO dynamic driver overrides, cgroups v2 resource accounting, and NUMA/Hugepages optimization for AI inference workloads on Red Hat OpenShift and bare-metal hypervisors.

---

## 1. PCIe Architecture, IOMMU & Virtualization

### 1.1 The PCIe Hierarchy & Access Control Services (ACS)

In modern enterprise server and workstation platforms, the Peripheral Component Interconnect Express (PCIe) bus is a packetized point-to-point interconnect structured around a **Root Complex**:

```
+-----------------------------------------------------------------------------------+
| Host CPU / Chipset                                                                |
|                                                                                   |
|  +-----------------------------------------------------------------------------+  |
|  | Root Complex (Integrated Memory Controller + Host Bridges)                  |  |
|  +-------+-----------------------------+-------------------------------+-------+  |
+----------|-----------------------------|-------------------------------|----------+
           |                             |                               |
    [ PCIe Lane x16 ]             [ PCIe Lane x8 ]                [ PCIe Lane x4 ]
           |                             |                               |
+----------v----------+           +------v-------+                +------v-------+
| GPU Endpoint        |           | NVMe SSD     |                | NIC (10GbE)  |
| (VGA + Audio)       |           +--------------+                +--------------+
+---------------------+
```

When passing physical PCIe hardware (such as an NVIDIA RTX 3060) into a virtual machine (such as Single Node OpenShift on KVM/QEMU), direct DMA (Direct Memory Access) and interrupts must be isolated from the host OS. 

Two critical hardware and firmware features govern this:
1. **Intel VT-d / AMD-Vi (IOMMU - Input-Output Memory Management Unit)**:
   - Translates device virtual addresses to host physical addresses for device DMA, mirroring what the CPU MMU does for user-space processes.
   - Restricts device memory access strictly to pages allocated to that device's domain.
2. **Access Control Services (ACS)**:
   - Prevents PCIe endpoints from performing peer-to-peer (P2P) DMA transactions directly between each other without routing through the Root Complex and IOMMU.
   - Without ACS support in PCIe root ports or downstream switches, the Linux kernel cannot guarantee that device A won't snoop or corrupt device B's memory transactions.

### 1.2 IOMMU Grouping Mechanics

The Linux kernel groups PCIe devices into **IOMMU Groups** based on hardware ACS capabilities and bus topology:
* **The Rule of Isolation**: All devices residing within the same IOMMU group **must** be assigned to the same security domain. You cannot leave one function of an IOMMU group bound to the host while assigning another function to a VM or container.
* **Multi-Function Devices**: GPUs invariably contain multiple PCIe functions:
  - Function `0` (`03:00.0`): VGA Compatible Controller (Graphics / CUDA Compute Engine)
  - Function `1` (`03:00.1`): Audio Controller (HDMI/DisplayPort audio)

To inspect IOMMU groups on a Linux host:
```bash
#!/usr/bin/env bash
for g in $(find /sys/kernel/iommu_groups/* -maxdepth 0 -type d | sort -V); do
    echo "=== IOMMU Group ${g##*/} ==="
    for d in $(find "$g/devices" -type l); do
        echo -n "  "
        lspci -nns "${d##*/}"
    done
done
```

Sample output from the Dell Precision T5600:
```
=== IOMMU Group 27 ===
  0000:03:00.0 VGA compatible controller [0300]: NVIDIA Corporation GA106 [GeForce RTX 3060 Lite Hash Rate] [10de:2504] (rev a1)
  0000:03:00.1 Audio device [0403]: NVIDIA Corporation GA106 High Definition Audio Controller [10de:228e] (rev a1)
```
Because `03:00.0` and `03:00.1` share IOMMU Group 27, **both devices must be uncoupled from host drivers and bound to VFIO simultaneously**.

### 1.3 Kernel Arguments for IOMMU Passthrough

To enable hardware IOMMU translation in passthrough mode, the Linux kernel command line requires:
```bash
intel_iommu=on iommu=pt
```
* `intel_iommu=on`: Activates the Intel VT-d driver in the Linux kernel during bootstrap.
* `iommu=pt` (Passthrough): Tells the kernel to use a 1:1 direct identity mapping for native host devices, activating dynamic IOMMU page table translation **only** for devices assigned to VFIO guests. This eliminates unnecessary DMA translation overhead and maximizes PCIe bandwidth for host storage and networking.

---

## 2. VFIO Subsystem & Dynamic Sysfs Driver Overrides

### 2.1 The Virtual Function I/O (VFIO) Framework

The `vfio-pci` kernel driver provides a secure, user-space driver framework for exposing raw PCIe devices to user-space applications (primarily QEMU/KVM and DPDK).

Instead of permanently blacklisting host GPU drivers (`nouveau` or `nvidia`) via `modprobe.d/blacklist.conf`—which prevents the host from utilizing the GPU when OpenShift is powered off—the modern systems engineering approach uses **Dynamic Sysfs Binding and Driver Overrides**.

### 2.2 Dynamic Driver Override Workflow

The Linux PCI bus subsystem provides runtime hooks under `/sys/bus/pci/` to re-bind devices without a reboot:

```
+-----------------------------------------------------------------------------------+
| Runtime Driver State Machine                                                      |
|                                                                                   |
|  [ Bound to nvidia ] ---> Write BDF to 'unbind' ---> [ Unbound Device ]           |
|                                                               |                   |
|                                                  Write 'vfio-pci' to              |
|                                                  'driver_override'                |
|                                                               |                   |
|  [ Bound to vfio-pci ] <-- Write BDF to 'bind' <----+---------v                   |
+-----------------------------------------------------------------------------------+
```

#### Step 1: Release Active Host Consumers
Before unbinding, all processes holding open file descriptors to `/dev/nvidia*` or `/dev/dri/*` must be terminated, and display managers stopped:
```bash
systemctl stop lightdm.service nxserver.service
fuser -k -9 /dev/nvidia* /dev/dri/*
```

#### Step 2: Unbind Host Drivers
Write the device Bus:Device.Function (BDF) string to the driver's `unbind` sysfs attribute:
```bash
echo "0000:03:00.0" > /sys/bus/pci/devices/0000:03:00.0/driver/unbind
echo "0000:03:00.1" > /sys/bus/pci/devices/0000:03:00.1/driver/unbind
```

#### Step 3: Set Driver Override
Setting `driver_override` forces the Linux PCI core to ignore default `modalias` matches and bind exclusively to the specified driver:
```bash
modprobe vfio-pci
echo "vfio-pci" > /sys/bus/pci/devices/0000:03:00.0/driver_override
echo "vfio-pci" > /sys/bus/pci/devices/0000:03:00.1/driver_override
```

#### Step 4: Bind to VFIO
```bash
echo "0000:03:00.0" > /sys/bus/pci/drivers/vfio-pci/bind
echo "0000:03:00.1" > /sys/bus/pci/drivers/vfio-pci/bind
```

Verification:
```bash
lspci -nnk -s 03:00.0
# Output:
# 03:00.0 VGA compatible controller [0300]: NVIDIA Corporation GA106...
#         Subsystem: ZOTAC International (MCO) Ltd. Device [19da:6431]
#         Kernel driver in use: vfio-pci
#         Kernel modules: nvidiafb, nouveau, nvidia_drm, nvidia
```

This exact sequence is encapsulated in our test-covered automation utility [`targets/t5600-homelab/gpu-host-admin.py`](file:///home/bjarrett/Projects/openshift-ai-infrastructure/targets/t5600-homelab/gpu-host-admin.py).

---

## 3. Container Device Interface (CDI) & cgroups v2 GPU Accounting

### 3.1 Cgroups v1 vs Cgroups v2 Device Control

In legacy cgroups v1, container runtimes controlled access to character device nodes through the `devices` subsystem (`devices.allow` and `devices.deny`).

In **cgroups v2** (the default in Red Hat Enterprise Linux 9 and Red Hat OpenShift 4.14+):
* File-based `devices.allow` files are replaced by **eBPF programs** of type `BPF_PROG_TYPE_CGROUP_DEVICE`.
* When a container executes `open()`, `mknod()`, or `stat()` on `/dev/nvidia0`, the Linux kernel triggers the attached eBPF program, which inspects the device major/minor numbers and permission bits (`rwm`) in sub-microsecond time.

### 3.2 Device Injection: CRI-O, CDI & NVIDIA Container Toolkit

```
+-----------------------------------------------------------------------------------+
| OpenShift Worker Pod Lifecycle                                                    |
|                                                                                   |
|  1. Pod Requests: limits: { nvidia.com/gpu: 1 }                                   |
|  2. Kubelet Device Plugin registers device ID (e.g. GPU-UUID-10de)                |
|  3. CRI-O reads CDI (Container Device Interface) spec                             |
|  4. Low-level runtime attaches eBPF cgroup filter permitting:                    |
|     - /dev/nvidia0       (Major 195, Minor 0)   [rwm]                             |
|     - /dev/nvidiactl     (Major 195, Minor 255) [rwm]                             |
|     - /dev/nvidia-uvm    (Major 243, Minor 0)   [rwm]                             |
|     - /dev/nvidia-uvm-tools                     [rwm]                             |
|  5. NVIDIA Container Hook mounts user-space CUDA libraries into /usr/lib64        |
+-----------------------------------------------------------------------------------+
```

### 3.3 Host RAM vs GPU VRAM Accounting

A frequent point of confusion in Kubernetes SRE is the distinction between Host RAM limits and GPU VRAM:
* **Host RAM (`memory.max`, `memory.current`)**:
  - The Linux kernel cgroup memory controller tracks anonymous memory, page cache, swap, and kernel memory buffers on the host.
  - If a container exceeds `limits.memory: 16Gi`, the host kernel OOM killer terminates the process (`OOMKilled: 137`).
* **GPU VRAM (GDDR6 Dedicated Memory)**:
  - The Linux kernel memory controller **has no direct visibility into physical GPU VRAM**.
  - Allocations via `cudaMalloc()` occur entirely within the GPU memory controller and NVIDIA proprietary driver structures.
  - If a container attempts to allocate 14GB on a 12GB RTX 3060, the host kernel does **not** OOM kill the pod; instead, the CUDA driver returns `CUDA_ERROR_OUT_OF_MEMORY` (exit code `torch.cuda.OutOfMemoryError`).
  - **Engineering Solution**: Applications must self-govern VRAM allocations. In vLLM, `--gpu-memory-utilization 0.85` strictly limits pre-allocated KV-cache memory to 85% of physical VRAM (10.2GB on RTX 3060), reserving 1.8GB for CUDA context and active activations.

---

## 4. NUMA Architecture & Hugepages Optimization

### 4.1 Non-Uniform Memory Access (NUMA) Topology

Multi-socket enterprise systems (such as the dual Intel Xeon E5-2670 v1 on the Dell Precision T5600) divide system resources into **NUMA nodes**:

```
+-----------------------------+               +-----------------------------+
| NUMA Node 0                 |               | NUMA Node 1                 |
|                             |               |                             |
|  [ CPU Socket 0 (16 vCPU) ] | <--- QPI ---> |  [ CPU Socket 1 (16 vCPU) ] |
|  [ Local DDR3 RAM (32GB)  ] |  (QuickPath)  |  [ Local DDR3 RAM (32GB)  ] |
|                             |               |                             |
|  [ PCIe Slot 2 (x16) ]      |               |  [ PCIe Slot 4 (x16) ]      |
|    └─> NVIDIA RTX 3060      |               |    └─> SAS HBA Controller   |
+-----------------------------+               +-----------------------------+
```

* **Local Memory Access**: When CPU Socket 0 accesses RAM attached to Socket 0, latency is ~60ns.
* **Remote Memory Access**: When CPU Socket 1 accesses RAM attached to Socket 0, requests must traverse the Intel QuickPath Interconnect (QPI), increasing latency to ~130ns and consuming QPI bus bandwidth.
* **PCIe Bus NUMA Affinity**:
  To check which NUMA node owns the physical GPU PCIe slot:
  ```bash
  cat /sys/bus/pci/devices/0000:03:00.0/numa_node
  # Output: 0
  ```
  Running inference workloads on CPU cores belonging to NUMA Node 0 avoids cross-socket QPI traversal when staging model tensors into GPU memory.

### 4.2 Linux Hugepages for Large Language Models

Standard Linux memory management divides RAM into **4KB pages**. For a 16GB working set, the CPU page table requires:
$$\frac{16 \times 1024 \times 1024 \text{ KB}}{4 \text{ KB}} = 4{,}194{,}304 \text{ page table entries}$$

Traversing millions of page entries leads to frequent **TLB (Translation Lookaside Buffer) misses**, stalling the CPU memory pipelines during tensor transfers.

By allocating **1GB Hugepages**:
$$\frac{16 \text{ GB}}{1 \text{ GB}} = 16 \text{ page table entries}$$

* **Advantages for LLMs**:
  1. Reduces TLB misses to near zero during large continuous tensor DMA transfers.
  2. Eliminates virtual memory fragmentation in shared memory (`/dev/shm`).
  3. Guarantees memory pages are pinned in physical RAM and cannot be swapped out by the kernel.

Kernel boot configuration for 1GB hugepages:
```bash
default_hugepagesz=1G hugepagesz=1G hugepages=4
```
This reserves 4GB of contiguous host memory exclusively for high-throughput tensor operations.
