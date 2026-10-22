# Kubernetes & OpenShift GPU Infrastructure Guide

A technical and architectural reference guide covering how GPU scheduling, resource allocation, and troubleshooting work in Kubernetes and Red Hat OpenShift. This document details the fundamental mechanics of the NVIDIA GPU Operator, hardware allocation models (exclusive vs. time-slicing vs. MIG), node protection strategies, common failure modes, and enterprise hardware alignment.

---

## 1. The Core Problem: Why GPUs Are Different in Kubernetes

By default, the Kubernetes Control Plane only measures **CPU** and **Memory (RAM)**. When you submit a Pod, the scheduler calculates:
$$\text{Node Free CPU} \ge \text{Pod Requested CPU} \quad \text{AND} \quad \text{Node Free Memory} \ge \text{Pod Requested Memory}$$

A physical GPU (e.g., NVIDIA RTX 3060, A100, H100) is a PCIe peripheral attached to the motherboard. Kubernetes has zero native awareness of PCIe bus devices until an **Extended Resource Plugin** is introduced.

---

## 2. The Solution: The NVIDIA GPU Operator Stack

Instead of manually compiling Linux kernel drivers and installing CUDA toolkits on every Kubernetes worker node, the **NVIDIA GPU Operator** automates the entire lifecycle inside containerized daemonsets.

```
+-----------------------------------------------------------------------+
| Kubernetes Worker Node                                                |
|                                                                       |
|  +------------------+  +--------------------+  +-------------------+  |
|  | GPU Feature      |  | Kubelet Device     |  | DCGM Exporter     |  |
|  | Discovery (GFD)  |  | Plugin             |  | (Prometheus GPU   |  |
|  | (Labels Node)    |  | (Advertises GPUs)  |  |  Metrics)         |  |
|  +--------+---------+  +---------+----------+  +---------+---------+  |
|           |                      |                       |            |
|  +--------v----------------------v-----------------------v---------+  |
|  | NVIDIA Container Toolkit & Container Device Interface (CDI)      |  |
|  +-------------------------------+---------------------------------+  |
|                                  |                                    |
|  +-------------------------------v---------------------------------+  |
|  | Host OS / Kernel Driver (or Containerized Driver Daemonset)       |  |
|  +-------------------------------+---------------------------------+  |
|                                  |                                    |
|  +-------------------------------v---------------------------------+  |
|  | Physical Hardware: PCIe GPU (RTX 3060 / A100 / H100 / DGX)      |  |
|  +-----------------------------------------------------------------+  |
+-----------------------------------------------------------------------+
```

### Component Breakdown

1. **NVIDIA Kubelet Device Plugin**:
   - Scans `/dev/nvidia*` devices on the host.
   - Tells the Kubelet: *"This node has 1 GPU available (`nvidia.com/gpu: 1`)"*.
2. **GPU Feature Discovery (GFD)**:
   - Queries GPU capabilities via NVML.
   - Automatically labels the node:
     - `nvidia.com/gpu.family: ampere`
     - `nvidia.com/gpu.product: NVIDIA-GeForce-RTX-3060`
     - `nvidia.com/gpu.count: "1"`
3. **Container Device Interface (CDI) / NVIDIA Container Toolkit**:
   - Injects the GPU device nodes (`/dev/nvidia0`, `/dev/nvidiactl`, `/dev/nvidia-uvm`) into the container's Linux cgroups when CRI-O or containerd starts the container.
4. **DCGM Exporter (Data Center GPU Manager)**:
   - Queries telemetry every 10 seconds: VRAM used/free, GPU compute utilization %, wattage, temperature, and PCIe bandwidth.
   - Emits standard Prometheus metrics for Grafana dashboards.

---

## 3. GPU Allocation Strategies: Whole vs. Time-Slicing vs. MIG

| Strategy | Hardware Supported | Isolation Level | Best Used For |
| :--- | :--- | :--- | :--- |
| **Exclusive (Whole GPU)** | All NVIDIA GPUs | Complete | Heavy model training, Large LLMs (70B+), low-latency production serving |
| **Time-Slicing (Sharing)** | All NVIDIA GPUs (Consumer & Enterprise) | Software-level (no VRAM isolation) | Dev/test environments, homelabs, lightweight inference (multiple models sharing 1 GPU) |
| **Multi-Instance GPU (MIG)** | Enterprise Only (A100, A30, H100, B200) | Full Hardware Isolation (Dedicated compute cores, memory, and cache) | Multi-tenant enterprise clusters (e.g. enterprise rack clusters / NVIDIA DGX SuperPODs) |

### A. Exclusive Allocation Example
A pod requests a whole GPU. Kubernetes ensures no other container can touch it:
```yaml
apiVersion: v1
kind: Pod
metadata:
  name: dedicated-gpu-workload
spec:
  containers:
  - name: cuda-runner
    image: nvcr.io/nvidia/k8s/cuda-sample:vectoradd-cuda11.7.1-ubuntu20.04
    resources:
      limits:
        nvidia.com/gpu: 1  # Kubernetes allocates the entire GPU
```

### B. Time-Slicing Configuration (Sharing 1 GPU into 4 Virtual Slots)
In a homelab or dev cluster, you can configure the GPU Operator with a ConfigMap:
```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: time-slicing-config
  namespace: nvidia-gpu-operator
data:
  any: |-
    version: v1
    sharing:
      timeSlicing:
        resources:
          - name: nvidia.com/gpu
            replicas: 4   # Exposes 4 virtual GPUs to Kubernetes from 1 physical card
```
Now, four different pods can each specify `limits: { nvidia.com/gpu: 1 }` and run concurrently!

---

## 4. Node Protection: Taints, Tolerations & Affinity

Because GPU nodes are specialized and often expensive, you must prevent standard workloads (like NGINX ingress or microservices) from landing on them.

1. **The Taint on the GPU Node**:
   ```bash
   oc adm taint nodes <gpu-node> nvidia.com/gpu=present:NoSchedule
   ```
2. **The Toleration in the AI Pod**:
   Any pod that legitimately needs the GPU must include:
   ```yaml
   tolerations:
   - key: "nvidia.com/gpu"
     operator: "Exists"
     effect: "NoSchedule"
   nodeSelector:
     nvidia.com/gpu.present: "true"
   ```

---

## 5. Top 5 Real-World GPU Troubleshooting Scenarios

### Scenario 1: Pod stuck in `Pending`
- **Symptom**: `oc get pods` shows `Pending`.
- **Root Cause**: `oc describe pod <name>` displays `0/X nodes available: Insufficient nvidia.com/gpu`.
- **Resolution**:
  1. Run `oc describe node <gpu-node> | grep -A 5 "Allocatable:"`. Check if `nvidia.com/gpu` is `0` or missing.
  2. If missing, check if the NVIDIA Device Plugin daemonset is running: `oc get ds -n nvidia-gpu-operator`.
  3. If already allocated, check which pod is holding the GPU: `oc get pods -A -o custom-columns=NAME:.metadata.name,GPU:.spec.containers[*].resources.requests."nvidia\.com/gpu"`.

### Scenario 2: Container crashes with `CUDA driver version is insufficient for CUDA runtime version`
- **Root Cause**: The container was built with a newer CUDA library (e.g. CUDA 12.6) than the NVIDIA driver version installed on the host (e.g. driver 470 supporting only up to CUDA 11.4).
- **Resolution**: Run `nvidia-smi` on the host to check driver version. Upgrade the host driver or pull a container image built for your driver branch.

### Scenario 3: Container crashes with `CUDA Out of Memory (OOM)`
- **Symptom**: Pod status `Error` or `CrashLoopBackOff`. Logs show `torch.cuda.OutOfMemoryError: CUDA out of memory`.
- **Resolution**:
  1. The model weights plus KV cache exceeded the physical VRAM (e.g., trying to run an 8B model with 16-bit precision requiring 16GB VRAM on a 12GB RTX 3060).
  2. Apply 4-bit or 8-bit quantization (AWQ / GPTQ / GGUF), or reduce `max_model_len` / context length.

### Scenario 4: `Failed to initialize NVML: Driver/library version mismatch`
- **Root Cause**: An `apt upgrade` updated the user-space NVIDIA libraries while the running Linux kernel is still using the older kernel module in memory without a reboot.
- **Resolution**: Unload and reload kernel modules (`rmmod nvidia_drm nvidia_modeset nvidia && modprobe nvidia`) or reboot the node.

### Scenario 5: Container cannot see the GPU (`nvidia-smi: command not found` or `/dev/nvidia0: no such file`)
- **Root Cause**: The container runtime (CRI-O) did not execute the NVIDIA Container Toolkit hook because the pod was missing the GPU resource limit or CDI configuration.
- **Resolution**: Verify `resources.limits: { nvidia.com/gpu: 1 }` is declared in the pod spec. In Kubernetes, the runtime hook only activates when the resource is explicitly requested!

---

## 6. Enterprise Hardware Alignment: Bare-Metal Server Profiles as Code

When deploying GPU infrastructure in enterprise datacenters:
- **Software-Defined Bare Metal** abstracts physical rack servers and modular compute nodes into declarative **Server Profiles**.
- Instead of manually setting BIOS flags or wiring cables:
  1. A **GPU / PCIe Policy** maps PCIe slots, bifurcation, and power envelopes to target compute nodes.
  2. A **BIOS Policy** enables IOMMU, SR-IOV, NUMA node interleaving, and maximum performance P-states.
  3. A **Network Policy** configures high-speed virtual interface cards with RoCEv2 (RDMA) for inter-node tensor communication.
- Once the physical node is provisioned, Kubernetes/OpenShift boots up and the NVIDIA GPU Operator takes over at the container orchestration layer.
