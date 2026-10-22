# SRE Runbook: NVIDIA Driver & Operator Upgrade Procedure

**Document ID**: `RBK-DRV-003`  
**Classification**: Operational SRE Procedure  
**Target Platform**: Red Hat OpenShift Container Platform (ROCP)  
**Scope**: NVIDIA GPU Operator, Containerized Kernel Drivers, ClusterPolicy Lifecycle  

---

## 1. Overview & Architecture

This runbook outlines the standard procedure for performing controlled, non-disruptive version upgrades of the **NVIDIA GPU Operator** and its underlying containerized GPU drivers on Red Hat OpenShift.

### Architectural Context
In Red Hat Enterprise Linux CoreOS (RHCOS), traditional package managers (`apt`, `dnf`, `yum`) do not exist on worker nodes. Instead:
1. The **Node Feature Discovery (NFD)** Operator discovers host PCIe IDs and kernel versions.
2. The **NVIDIA GPU Operator** reconciles a single cluster-wide Custom Resource called `ClusterPolicy` (`nvidia.com/v1`).
3. The Operator spawns the **NVIDIA Driver DaemonSet**, which pulls the OpenShift **Driver Toolkit** image, compiles or loads the target NVIDIA kernel modules (`nvidia.ko`, `nvidia-modeset.ko`, `nvidia-uvm.ko`) into the RHCOS kernel at runtime, and exposes character devices to CRI-O.

```
+-----------------------------------------------------------------------------------+
| OpenShift Driver Lifecycle Architecture                                           |
|                                                                                   |
|  [ ClusterPolicy CR ] ──> [ GPU Operator Controller ]                             |
|                                     │                                             |
|                                     ▼                                             |
|                     [ NVIDIA Driver DaemonSet Pod ]                               |
|                     ┌──────────────────────────────────────────────┐              |
|                     │ 1. Queries running RHCOS kernel (uname -r)   │              |
|                     │ 2. Pulls matching Driver Toolkit headers     │              |
|                     │ 3. Compiles / Inserts nvidia.ko & nvidia-uvm │              |
|                     │ 4. Mounts /dev/nvidia* into host devtmpfs    │              |
|                     └──────────────────────────────────────────────┘              |
|                                     │                                             |
|                                     ▼                                             |
|                     [ Kubelet Device Plugin DaemonSet ]                           |
|                     (Re-discovers GPU and advertises nvidia.com/gpu)              |
+-----------------------------------------------------------------------------------+
```

---

## 2. Pre-Upgrade Compatibility Matrix & Validation

Before upgrading, verify cross-compatibility between the 4 architectural layers:

| Layer | Verification Command | Supported / Required Baseline |
| :--- | :--- | :--- |
| **OpenShift Core** | `oc get clusterversion` | OCP 4.14+ (RHCOS 9.2+) |
| **RHCOS Kernel** | `oc debug node/<name> -- uname -r` | `5.14.0-xxx.el9_x.x86_64` |
| **NVIDIA Driver** | `nvidia-smi --query-gpu=driver_version --format=csv` | Target Branch (e.g., 550.x or 560.x) |
| **CUDA Runtime** | PyTorch / vLLM Image Requirement | CUDA 12.1 through 12.6 |

### Pre-Flight Safety Checks
1. **Back up existing ClusterPolicy**:
   ```bash
   oc get clusterpolicy gpu-cluster-policy -o yaml > /tmp/clusterpolicy-backup-$(date +%F).yaml
   ```
2. **Ensure Hugging Face Model Cache is Preserved**:
   Verify the local PV storage claim in `llm-serving` is unbound from pod state:
   ```bash
   oc get pvc -n llm-serving
   ```

---

## 3. Step-by-Step Upgrade Procedure

### Step 1: Schedule Maintenance Window & Cordon Node
Cordon the target GPU node to prevent application rescheduling during the kernel module reload:
```bash
NODE_NAME=$(oc get nodes -o jsonpath='{.items[0].metadata.name}')
oc adm cordon "$NODE_NAME"
```

### Step 2: Gracefully Evacuate Active Inference Pods
Scale the vLLM deployment to zero to allow active HTTP requests to drain within the 60-second grace window:
```bash
oc scale deployment/vllm -n llm-serving --replicas=0
oc wait --for=delete pod -l app=vllm-inference-engine -n llm-serving --timeout=90s
```

### Step 3: Upgrade Operator Subscription (OLM)
If upgrading the operator version itself, approve the InstallPlan or update the `Subscription` channel:
```bash
oc patch subscription nvidia-gpu-operator -n nvidia-gpu-operator --type=merge -p '
spec:
  channel: "v24.6"
'
```
Wait for the Operator controller to reach `Running` state:
```bash
oc rollout status deployment/gpu-operator -n nvidia-gpu-operator --timeout=180s
```

### Step 4: Update Driver Version in `ClusterPolicy`
Patch the `ClusterPolicy` Custom Resource to target the desired NVIDIA driver release:
```bash
oc patch clusterpolicy gpu-cluster-policy --type=merge -p '
spec:
  driver:
    enabled: true
    version: "550.54.15"
'
```

### Step 5: Monitor Driver DaemonSet Compilation & Rollout
Track the containerized driver build and module insertion process:
```bash
oc get pods -n nvidia-gpu-operator -l app=nvidia-driver-daemonset -w
```

Inspect driver compilation logs to confirm clean module insertion:
```bash
DRIVER_POD=$(oc get pods -n nvidia-gpu-operator -l app=nvidia-driver-daemonset -o jsonpath='{.items[0].metadata.name}')
oc logs "$DRIVER_POD" -n nvidia-gpu-operator -c nvidia-driver --tail=100
```
Expected success log lines:
```
Loading NVIDIA drivers...
Loaded nvidia
Loaded nvidia-modeset
Loaded nvidia-uvm
Driver installation complete.
```

### Step 6: Verify Device Plugin & Feature Discovery
Ensure the Kubelet device plugin daemonset successfully reconnects to the newly loaded driver:
```bash
oc rollout status daemonset/nvidia-device-plugin-daemonset -n nvidia-gpu-operator --timeout=120s
```

Confirm that the node advertises `nvidia.com/gpu: 1`:
```bash
oc describe node "$NODE_NAME" | grep -A 4 "Capacity:" | grep "nvidia.com/gpu"
```

---

## 4. Workload Restoration & Validation

### Step 1: Uncordon Node
```bash
oc adm uncordon "$NODE_NAME"
```

### Step 2: Restore vLLM Inference Deployment
```bash
oc scale deployment/vllm -n llm-serving --replicas=1
```

### Step 3: Validate Pod Startup & Health Probes
```bash
oc wait --for=condition=Ready pod -l app=vllm-inference-engine -n llm-serving --timeout=180s
```

### Step 4: Execute Live Inference Smoke Test
Run the automated test script to confirm inference generation throughput and end-to-end functionality:
```bash
./scripts/test-vllm-endpoint.sh
```

---

## 5. Rollback Procedure (Emergency Recovery)

If driver compilation fails, or if kernel panics / Xid faults occur after upgrade:

1. **Re-apply the Backup ClusterPolicy**:
   ```bash
   oc apply -f /tmp/clusterpolicy-backup-*.yaml
   ```
2. **Force-Restart Driver DaemonSet**:
   ```bash
   oc delete pod -n nvidia-gpu-operator -l app=nvidia-driver-daemonset
   ```
3. **Verify Previous Driver Restored**:
   ```bash
   oc exec -it -n nvidia-gpu-operator $(oc get pod -n nvidia-gpu-operator -l app=nvidia-device-plugin-daemonset -o jsonpath='{.items[0].metadata.name}') -- nvidia-smi
   ```
4. **Restore Workloads and Uncordon**:
   ```bash
   oc scale deployment/vllm -n llm-serving --replicas=1
   oc adm uncordon "$NODE_NAME"
   ```
