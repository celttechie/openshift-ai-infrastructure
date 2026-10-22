# SRE Runbook: GPU Hardware Hang & Driver Recovery Procedure

**Document ID**: `RBK-GPU-001`  
**Classification**: Operational SRE Procedure  
**Target Platform**: Red Hat OpenShift Container Platform & Linux Bare-Metal/Hypervisors  
**Scope**: Single Node OpenShift (`sno-node`), Workstation/Server PCIe GPUs (NVIDIA RTX 3060 / Enterprise GPUs)  

---

## 1. Overview & Failure Modes

This runbook defines the standard operating procedure for diagnosing, isolating, and recovering from GPU hardware hangs, CUDA driver crashes, and PCIe bus disconnects without requiring an ungraceful host power cycle.

### Common Alert Triggers & Symptoms
* **Prometheus Alert**: `VLLMEngineDown` or `KubeNodeNotReady` firing.
* **Workload Symptoms**: vLLM pod is stuck in `Terminating`, `CrashLoopBackOff`, or logging `CUDA error: an illegal memory access was encountered`.
* **Host Symptoms**: `nvidia-smi` hangs indefinitely, outputs `ERR!`, or returns:
  ```
  Unable to determine the device handle for GPU 0000:03:00.0: GPU has fallen off the bus.
  ```

---

## 2. NVIDIA Xid Diagnostic Reference

When a hardware or driver fault occurs, the NVIDIA kernel driver logs an **Xid message** to the Linux kernel ring buffer (`dmesg`).

| Xid Code | Description | Severity | Typical Root Cause | Action Required |
| :--- | :--- | :--- | :--- | :--- |
| **Xid 13** | Graphics Engine Exception | High | Invalid CUDA address or invalid instruction | Soft Reset (kill container process) |
| **Xid 31** | GPU memory page fault | High | Out-of-bounds VRAM access or hardware memory error | Soft Reset & container restart |
| **Xid 45** | Preemption Error | Critical | Compute kernel hung in infinite loop exceeding watchdog | Driver reload or PCIe bus reset |
| **Xid 62** | Internal Microcontroller Halt | Critical | Firmware stall in GPU GSP or PMU engine | PCIe Secondary Bus Reset |
| **Xid 79** | GPU has fallen off the bus | Fatal | PCIe link down, power trip, or thermal throttling trip | Hard PCIe Sysfs Reset & Link Re-train |

To inspect active Xid events:
```bash
dmesg -T | grep -i -E "nvrrm|xid" | tail -n 20
```

---

## 3. Diagnostic & Triage Workflow

```
[ Alert Received: VLLMEngineDown or nvidia-smi Hang ]
                         |
                         v
       +------------------------------------+
       | Does 'nvidia-smi' execute cleanly? |
       +-----------------+------------------+
                         |
             +-----------+-----------+
             | YES                   | NO
             v                       v
  [ Soft Triage: Section 4.1 ]   [ Check Kernel dmesg for Xid 79/45 ]
  (Orphaned CUDA processes)                  |
                                             v
                                 [ Hard Reset: Section 4.2 ]
                                 (PCIe bus reset & module reload)
```

---

## 4. Step-by-Step Recovery Procedure

### 4.1 Tier 1: Soft Recovery (Orphaned Process & Device Clean)

Use this tier when `nvidia-smi` is responsive but containers cannot obtain device locks.

1. **Cordon the OpenShift Node**:
   Prevent new workloads from scheduling onto the degraded node:
   ```bash
   oc adm cordon $(oc get nodes -o jsonpath='{.items[0].metadata.name}')
   ```

2. **Identify and Terminate Trapped GPU Processes**:
   Check if terminated container processes continue holding device locks on `/dev/nvidia*`:
   ```bash
   # On the node / hypervisor:
   fuser -v /dev/nvidia* /dev/dri/*
   ```
   If lingering PIDs exist:
   ```bash
   fuser -k -9 /dev/nvidia* /dev/dri/*
   ```

3. **Restart the Workload**:
   Delete the stuck vLLM pod. Because the deployment specifies `strategy: { type: Recreate }`, Kubernetes will wait for process termination before rescheduling:
   ```bash
   oc delete pod -n llm-serving -l app=vllm-inference-engine --force --grace-period=0
   ```

4. **Verify Recovery**:
   Check container logs and startup probes:
   ```bash
   oc logs -n llm-serving -l app=vllm-inference-engine -f
   ```
   If the container completes initialization, proceed to **Section 5 (Verification)** and uncordon. If `nvidia-smi` continues hanging, escalate to **Tier 2**.

---

### 4.2 Tier 2: Hard Recovery (PCIe Bus Reset & Kernel Module Reload)

Use this tier when the GPU has "fallen off the bus" (Xid 79), PCIe link state is down, or `nvidia-smi` hangs in D-state (uninterruptible sleep).

1. **Evacuate the Workload**:
   Scale the inference deployment to zero to prevent the Kubelet from restarting containers during driver reload:
   ```bash
   oc scale deployment/vllm -n llm-serving --replicas=0
   ```

2. **Unbind GPU from Host Drivers**:
   Find the PCI Bus:Device.Function (BDF) (e.g., `0000:03:00.0` for VGA and `0000:03:00.1` for Audio):
   ```bash
   GPU_VGA="0000:03:00.0"
   GPU_AUDIO="0000:03:00.1"

   # Unbind from active driver
   if [ -e "/sys/bus/pci/devices/$GPU_VGA/driver/unbind" ]; then
       echo "$GPU_VGA" > "/sys/bus/pci/devices/$GPU_VGA/driver/unbind"
   fi
   if [ -e "/sys/bus/pci/devices/$GPU_AUDIO/driver/unbind" ]; then
       echo "$GPU_AUDIO" > "/sys/bus/pci/devices/$GPU_AUDIO/driver/unbind"
   fi
   ```

3. **Trigger PCIe Device Removal & Bus Rescan**:
   Forcing device removal and rescan triggers a PCIe link re-training across the Root Complex:
   ```bash
   # Remove device nodes from kernel bus representation
   echo 1 > "/sys/bus/pci/devices/$GPU_VGA/remove"
   echo 1 > "/sys/bus/pci/devices/$GPU_AUDIO/remove"
   sleep 2

   # Trigger PCIe Root Complex bus rescan
   echo 1 > /sys/bus/pci/rescan
   sleep 2
   ```

4. **Verify PCIe Link Status**:
   Confirm that the PCIe link has recovered at negotiated speed and width (e.g., Gen3 x16):
   ```bash
   lspci -vvv -s "$GPU_VGA" | grep -i -E "LnkCap|LnkSta"
   # Output should confirm: LnkSta: Speed 8GT/s, Width x16
   ```

5. **Unload & Reload NVIDIA Kernel Modules**:
   Flush and reload the kernel driver stack:
   ```bash
   # Unload modules in reverse dependency order
   modprobe -r nvidia_uvm nvidia_drm nvidia_modeset nvidia
   sleep 1

   # Re-insert kernel module
   modprobe nvidia
   ```

6. **Restart NVIDIA Device Plugin DaemonSet**:
   Restart the Kubelet device plugin pod to re-register the GPU resource:
   ```bash
   oc delete pod -n nvidia-gpu-operator -l app=nvidia-device-plugin-daemonset
   ```

7. **Restore Workload**:
   ```bash
   oc scale deployment/vllm -n llm-serving --replicas=1
   oc adm uncordon $(oc get nodes -o jsonpath='{.items[0].metadata.name}')
   ```

---

## 5. Verification & Health Validation

Execute the following post-recovery checklist:

1. **Verify Kubelet Extended Resource Allocatability**:
   ```bash
   oc describe node | grep -A 4 "Allocatable:" | grep "nvidia.com/gpu"
   # Expected: nvidia.com/gpu: 1
   ```

2. **Verify GPU Telemetry via nvidia-smi**:
   ```bash
   nvidia-smi --query-gpu=name,driver_version,temperature.gpu,utilization.gpu,memory.used,memory.total --format=csv
   ```

3. **Execute Live Inference Smoke Test**:
   Run the automated benchmark suite:
   ```bash
   ./scripts/test-vllm-endpoint.sh
   ```
   Verify HTTP 200 OK, valid token streaming, and generation throughput (>30 tokens/sec).

4. **Clear SRE Incident Alerts**:
   Confirm in Prometheus / OpenShift Console that `VLLMEngineDown` and `KubeNodeNotReady` have cleared (`ALERTS{alertname="VLLMEngineDown"} == 0`).
