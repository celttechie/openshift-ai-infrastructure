# SRE Runbook: Node Drain & GPU Workload Evacuation Procedure

**Document ID**: `RBK-NODE-002`  
**Classification**: Operational SRE Procedure  
**Target Platform**: Red Hat OpenShift Container Platform (ROCP)  
**Scope**: Single Node OpenShift (SNO), GPU Worker Nodes, NVIDIA Accelerated Workloads  

---

## 1. Overview & Purpose

This runbook documents the exact procedure for gracefully cordoning and draining an OpenShift node hosting GPU-accelerated workloads (such as the vLLM inference engine). 

Node evacuation is required prior to:
* Applying immutable RHCOS kernel updates via MachineConfig Operator.
* Performing physical hypervisor or bare-metal host maintenance.
* Upgrading host firmware (BIOS/UEFI, PCIe bifurcation settings, BMC/iDRAC).
* Performing GPU hardware swap or PCIe slot reconfiguration.

---

## 2. Core Architectural Challenges with GPU Drains

A naive execution of `oc adm drain <node>` will fail on GPU nodes due to three distinct Kubernetes safeguards:

```
+-----------------------------------------------------------------------------------+
| Kubernetes Safeguard     | Default Drain Behavior | Required Flag / Remediation   |
+--------------------------+------------------------+-------------------------------+
| DaemonSets (GPU Operator)| Blocks drain           | --ignore-daemonsets           |
| EmptyDir Volume (/dev/shm)| Blocks drain           | --delete-emptydir-data        |
| PodDisruptionBudget (PDB)| Blocks eviction (SNO)  | --disable-eviction or scale 0 |
| In-flight HTTP Requests  | Abrupt termination     | terminationGracePeriodSeconds |
+-----------------------------------------------------------------------------------+
```

1. **GPU Operator DaemonSets**:
   The NVIDIA GPU Operator runs multiple DaemonSets (`nvidia-device-plugin`, `gpu-feature-discovery`, `nvidia-dcgm-exporter`, `nvidia-driver-daemonset`). The drain command rejects evicting DaemonSet-managed pods unless `--ignore-daemonsets` is explicitly supplied.
2. **Shared Memory (`/dev/shm`) EmptyDir**:
   vLLM mounts a high-speed RAM-backed emptyDir (`medium: Memory`) for PyTorch inter-process tensor sharing. Kubernetes protects local ephemeral data from deletion unless `--delete-emptydir-data` is supplied.
3. **PodDisruptionBudget (`minAvailable: 1`)**:
   Our production SRE policy enforces a PDB (`workloads/vllm/06-pdb.yaml`) requiring at least 1 replica available. On a Single Node OpenShift (SNO) cluster, evicting the sole replica violates the PDB. The drain must either use `--disable-eviction=true` (bypassing the Eviction API and issuing direct Pod deletion) or the deployment must be cleanly scaled to zero.
4. **In-Flight Request Preservation**:
   Our deployment manifest specifies `terminationGracePeriodSeconds: 60`. When evicted, vLLM receives `SIGTERM`, closes ingress to new requests, and completes in-flight token generation for up to 60 seconds before Kubelet issues `SIGKILL`.

---

## 3. Pre-Drain Assessment & Inspection

Before initiating a node drain, run the pre-flight inspection checklist:

```bash
# 1. Identify node name
NODE_NAME=$(oc get nodes -o jsonpath='{.items[0].metadata.name}')
echo "Target Node: $NODE_NAME"

# 2. Check active GPU pods
oc get pods -A -o wide | grep -E "vllm|kserve|nvidia"

# 3. Check active PodDisruptionBudgets
oc get pdb -A

# 4. Check active client connections / inference traffic
# Query Prometheus for active request rate:
# sum(rate(vllm:num_requests_running[1m]))
```

---

## 4. Step-by-Step Evacuation Procedure

```
[ Step 1: Cordon Node ] ---> [ Step 2: Graceful Workload Evacuation ]
                                               |
                                               v
[ Step 4: Maintenance / Reboot ] <--- [ Step 3: Verify GPU & Storage Unmount ]
               |
               v
[ Step 5: Uncordon Node ] ------> [ Step 6: Health & Traffic Validation ]
```

### Step 1: Cordon the Node
Mark the node as unschedulable to ensure no new pods land on it during preparation:
```bash
oc adm cordon "$NODE_NAME"
```
Verify status transitions to `Ready,SchedulingDisabled`:
```bash
oc get nodes "$NODE_NAME"
```

### Step 2: Execute Node Drain

#### Option A: Single Node OpenShift (SNO) Maintenance (Recommended)
On SNO, because there are no alternate worker nodes for evicted pods to fail over to, cleanly scaling the inference workload prevents PodDisruptionBudget deadlocks:

```bash
# Gracefully scale vLLM to 0, allowing up to 60s for in-flight requests to complete
oc scale deployment/vllm -n llm-serving --replicas=0

# Wait for pod termination
oc wait --for=delete pod -l app=vllm-inference-engine -n llm-serving --timeout=90s

# Drain the node for remaining non-critical workloads and daemonsets
oc adm drain "$NODE_NAME" \
  --ignore-daemonsets \
  --delete-emptydir-data \
  --force \
  --grace-period=60 \
  --timeout=5m
```

#### Option B: Multi-Worker Cluster Eviction
In multi-worker clusters with available GPU nodes, leverage the Kubernetes Eviction API:
```bash
oc adm drain "$NODE_NAME" \
  --ignore-daemonsets \
  --delete-emptydir-data \
  --force \
  --grace-period=60 \
  --timeout=5m
```
*Note: If the vLLM PDB blocks eviction on SNO, add `--disable-eviction=true` to force pod termination.*

### Step 3: Verify Storage & GPU Unmount
Confirm that persistent volumes and GPU device character handles are released:
```bash
# Check PVC mount status
oc get pv
# Verify no host processes hold /dev/nvidia*
ssh t5600 "fuser /dev/nvidia* /dev/dri/*"
```

---

## 5. Post-Maintenance Restoration & Verification

After host maintenance, kernel updates, or reboot cycles are completed:

### Step 1: Wait for Node `Ready` Status
```bash
oc wait --for=condition=Ready node/"$NODE_NAME" --timeout=300s
```

### Step 2: Uncordon the Node
Re-enable workload scheduling:
```bash
oc adm uncordon "$NODE_NAME"
```

### Step 3: Restore & Verify GPU Workloads
If the deployment was scaled down for SNO maintenance, scale it back up:
```bash
oc scale deployment/vllm -n llm-serving --replicas=1
```

Monitor pod rollout:
```bash
oc get pods -n llm-serving -w
```

### Step 4: Validate Probes & Readiness
Wait for `startupProbe` and `readinessProbe` to succeed:
```bash
oc wait --for=condition=Ready pod -l app=vllm-inference-engine -n llm-serving --timeout=180s
```

### Step 5: Execute End-to-End Inference Verification
Run the automated validation script:
```bash
./scripts/test-vllm-endpoint.sh
```

Ensure generation latency, TTFT, and tokens/sec match baseline benchmarks.
