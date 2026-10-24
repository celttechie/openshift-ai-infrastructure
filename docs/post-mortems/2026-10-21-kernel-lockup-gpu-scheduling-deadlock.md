# Incident Post-Mortem 001: Host Kernel Mutex Deadlock & GPU Scheduling Rollout Lockup

**Incident ID**: `INC-20261021-01`  
**Date**: October 21, 2026  
**Severity**: Severity 1 (Critical Hypervisor Kernel Freeze & Workload Scheduling Deadlock)  
**Status**: Resolved & Mitigated  
**Affected Systems**: Bare-Metal Dell Precision T5600 Hypervisor, Single Node OpenShift (`sno-node`), `llm-serving/vllm` Workload  
**Authors**: Platform Infrastructure & Systems SRE Engineering Team  

---

## 1. Executive Summary

On October 21, 2026, during PCIe GPU lifecycle preparation and the initial deployment of the vLLM inference engine serving `Qwen/Qwen2.5-3B-Instruct` on a Single Node OpenShift (SNO) cluster hosted on a bare-metal Dell Precision T5600 workstation, the physical hypervisor suffered an operating system lockup requiring a manual hardware power cycle, followed by an automated Kubernetes deployment scheduling deadlock upon cluster recovery.

The primary hypervisor lockup was caused by an AB-BA kernel mutex deadlock between Linux virtual console binding (`/sys/class/vtconsole/vtcon*/bind`) and NVIDIA DRM driver initialization (`modprobe nvidia-drm`) during automated host GPU release and rebinding routines. This trapped critical administrative processes in uninterruptible sleep (`TASK_UNINTERRUPTIBLE` / `D` state) behind `console_lock` (`down_console_sem`). While the network interface continued responding to ICMP echo requests (pings), `systemd` (PID 1) worker threads were blocked, causing remote SSH authentication, command execution, and software reboot requests to hang indefinitely. Concurrently during model bring-up, unconstrained PyTorch Inductor JIT compilation attempted to vectorize kernels without AVX2 instruction support on the dual Intel Xeon E5-2670 v1 processors, spawning parallel OpenMP worker threads across all 32 logical cores simultaneously with GPU CUDA graph warmup routines.

Following an out-of-band manual power cycle and filesystem recovery, a secondary scheduling deadlock occurred: the Kubernetes default `RollingUpdate` deployment strategy attempted to schedule a replacement vLLM pod while the existing pod retained the single physical GPU allocation (`nvidia.com/gpu: 1`), causing an indefinite deployment rollout stall.

Both failure modes were permanently resolved through systems hardening and declarative workload configuration:
1. **Hypervisor Driver Lifecycle**: Sanitized [`gpu-host-admin.py`](../../targets/t5600-homelab/gpu-host-admin.py) to eliminate hazardous virtual terminal console unbinding during GPU lifecycle management, preventing kernel console mutex contention.
2. **Eager Mode Execution**: Enforced eager execution (`--enforce-eager`) in the vLLM container arguments, completely bypassing PyTorch Inductor JIT compilation on legacy non-AVX2 CPUs.
3. **Declarative Recreate Strategy**: Configured deployment rollout strategy to `Recreate` (`strategy: { type: Recreate }`), guaranteeing GPU lock release before scheduling replacement pods on Single Node OpenShift.

Automated policy guardrail unit tests were added to CI to prevent configuration regressions. Total outage duration was 42 minutes with zero persistent data loss.

---

## 2. Impact Assessment

| Dimension | Metrics / Observations |
| :--- | :--- |
| **User / Client Impact** | Complete loss of OpenAI-compatible inference API endpoint (`vllm.apps.sno.homelab.local`). HTTP 503 / connection refused. |
| **Incident Duration** | **42 minutes** total (16:38 CDT to 17:20 CDT). |
| **Cluster Impact** | Single Node OpenShift control plane and worker components became unreachable due to hypervisor kernel freeze. Required RHCOS journal replay and etcd single-node recovery upon restart. |
| **Physical Hardware** | Dell Precision T5600 hypervisor froze in ring-0 kernel deadlock. System remained electrically powered (fans spinning, status LEDs illuminated), but kernel was unresponsive; required out-of-band manual power cycle. |
| **Data Integrity** | Zero data corruption. Hugging Face model cache on local NVMe PV remained consistent; etcd single-member WAL recovered cleanly. |

---

## 3. Incident Timeline (October 21, 2026)

| Time (CDT) | Event Description |
| :--- | :--- |
| **16:05:00** | SRE executed automated host GPU lifecycle script (`./scripts/gpu-prep-host.sh --restore-host sno-node`) to transition NVIDIA RTX 3060 from `vfio-pci` back to host drivers. |
| **16:06:15** | Helper script `gpu-host-admin.py` began unbinding VT consoles (`/sys/class/vtconsole/vtcon*/bind`) while invoking `/sbin/modprobe nvidia-drm`. |
| **16:07:00** | **Hypervisor Lockup**: Writing to `vtconsole` raced with DRM framebuffer initialization, triggering an AB-BA mutex deadlock on kernel `console_lock`. Processes trapped in `D` state. |
| **16:07:30** | SRE noted terminal command execution stalled. ICMP pings to `192.168.9.110` responded in <0.4ms, but new SSH sessions hung at connection phase. |
| **16:08:15** | Attempted software recovery: `sudo reboot` and `sudo reboot -f` executed via existing background SSH channels timed out (`Call to Reboot failed: Connection timed out`). |
| **16:09:30** | Kernel inspection via lingering diagnostic task showed PID `1261949` (`modprobe nvidia-drm`) and PID `1264552` (`sb_admin.py`) blocked in `wchan: down_console_sem`. `systemd` PID 1 unable to process RPCs. |
| **16:15:00** | Physical inspection of bare-metal Dell Precision T5600 workstation: motherboard powered, power LED solid white, cooling fans running normally, but host completely unresponsive to console input. |
| **16:19:10** | Manual hardware power cycle performed by physical operator (front panel power button held to force hard shutdown, then powered on). |
| **16:22:30** | Hypervisor booted into Ubuntu Linux kernel; KVM/libvirt initialized and automatically restored OpenShift Single Node OpenShift (`sno-node`) virtual machine. |
| **16:26:00** | SNO RHCOS booted cleanly; single-node etcd verified quorum; OpenShift ClusterVersion and core ClusterOperators returned to `Available=True`. |
| **16:30:00** | Workload deployment applied (`workloads/vllm/03-deployment.yaml`). Container pulled image and initialized `Qwen/Qwen2.5-3B-Instruct`. |
| **16:32:15** | Model weights loaded into GPU VRAM (RTX 3060, 12GB). Upstream PyTorch Inductor initiated default C++ JIT kernel compilation and CUDA graph capture warmup. |
| **16:33:00** | **Compute Saturation & Host Starvation**: Inductor detected absence of AVX2 on dual Xeon E5-2670 v1 CPUs and launched unvectorized OpenMP compilation across all 32 logical threads simultaneously. Load average spiked >40; CUDA graph warmup concurrently executed rapid forward passes. |
| **16:35:00** | Attempted rolling update of deployment manifest to throttle threads. **Scheduling Deadlock**: Kubernetes `RollingUpdate` created replacement pod `vllm-inference-engine-2`, which failed to schedule (`0/1 nodes available: Insufficient nvidia.com/gpu: 1`) because original pod held the single physical device lock. |
| **16:38:00** | SRE diagnosed twin failure modes: kernel console deadlock risk during host prep, thread exhaustion from PyTorch Inductor JIT on non-AVX2 host CPUs, and K8s single-GPU rolling update deadlock. |
| **16:44:00** | Devised permanent engineering mitigations: remove `vtconsole` manipulation from `gpu-host-admin.py`, inject `--enforce-eager` to bypass Inductor JIT, and set deployment strategy to `Recreate`. |
| **16:48:30** | Manifests and scripts updated, committed, and applied to cluster. |
| **16:50:15** | vLLM pod deployed with `--enforce-eager` and `strategy: Recreate`. Model initialized cleanly within 14 seconds: CPU utilization peaked at 12% on a single thread; GPU memory settled at 85% with zero host latency. |
| **16:52:00** | Automated validation script (`scripts/test-vllm-endpoint.sh`) executed. OpenAI-compatible endpoint returned TTFT: 48ms, generation rate: 38.4 tokens/sec. |
| **16:56:00** | Incident formally declared resolved. |

---

## 4. Technical Root Cause Analysis (RCA)

### 4.1 Hypervisor Kernel Deadlock (`console_lock` Mutex Inversion)

Dynamic PCIe passthrough lifecycle management on a Linux hypervisor requires unbinding GPU hardware from host drivers (`nvidia`, `nvidia_drm`) and binding to `vfio-pci` (and the reverse upon host restoration).

In initial versions of [`gpu-host-admin.py`](../../targets/t5600-homelab/gpu-host-admin.py), the script attempted to detach active framebuffers and virtual terminals prior to unbinding:
```python
# Problematic implementation in initial host prep script:
for vt in glob.glob("/sys/class/vtconsole/vtcon*/bind"):
    with open(vt, "w") as f:
        f.write("0\n")
```

This routine introduced a catastrophic race condition:
1. Writing `0` to `/sys/class/vtconsole/vtcon*/bind` acquires the Linux kernel virtual console lock (`console_lock()`) to unbind the fbcon driver.
2. Simultaneously, driver reload routines (`modprobe nvidia-drm` or `systemctl stop lightdm`) acquire driver-internal mutexes (such as the DRM master mutex) before requesting console operations via `down_console_sem()`.
3. When both operations executed concurrently during rapid lifecycle automation, an **AB-BA mutex deadlock** occurred between the DRM subsystem and the console subsystem.
4. Both processes entered `TASK_UNINTERRUPTIBLE` (`D` state). In Linux, tasks in `D` state cannot be interrupted, preempted, or terminated by POSIX signals (`kill -9` has zero effect).
5. Because `systemd` (PID 1) relies on console worker threads to service terminal signals and system state transitions, PID 1 became blocked behind the lock. Network interface hardware interrupts (bottom-halves) continued processing ICMP echo replies, but user-space process creation, SSH multiplexing, and D-Bus reboot RPCs were permanently deadlocked.
6. The only recovery mechanism for an in-kernel mutex deadlock in `D` state is a hardware power cycle.

```
+-------------------------------------------------------------------------------+
| Process 1: gpu-host-admin.py (PID 1264552)                                    |
|   Holds: console_lock (via vtconsole/bind write)                              |
|   Waiting for: DRM subsystem resource lock                                    |
|                                     ^                                         |
|                                     | MUTEX DEADLOCK IN D-STATE               |
|                                     v                                         |
| Process 2: modprobe nvidia-drm (PID 1261949)                                  |
|   Holds: DRM driver mutex                                                     |
|   Waiting for: down_console_sem() [console_lock]                              |
+-------------------------------------------------------------------------------+
                                      |
                                      v
              systemd (PID 1) worker blocked on console_sem
              --> ICMP Ping: ALIVE (kernel network IRQs run)
              --> SSH / D-Bus / sudo reboot: FROZEN (PID 1 hung)
```

### 4.2 Hardware Microarchitecture & Instruction Set Limitations

The Dell Precision T5600 workstation is powered by dual **Intel Xeon E5-2670 v1** processors (Sandy Bridge-EP architecture, 8 physical cores / 16 threads per socket = 16 cores / 32 logical threads total).

Key microarchitectural characteristics:
* Instruction set support: MMX, SSE, SSE2, SSE3, SSSE3, SSE4.1, SSE4.2, AVX 1.0 (256-bit floating point).
* **Missing instruction sets**: **AVX2** (256-bit integer SIMD, FMA3) and **AVX-512**.

```
+--------------------------------------------------------------------------------+
| Host CPU Topology: Dual Intel Xeon E5-2670 v1 (Sandy Bridge-EP)                |
|                                                                                |
|  Socket 0: 8 Cores / 16 Threads (115W TDP)  <--- QPI Link ---> Socket 1: 8 Cores / 16 Threads (115W TDP)
|  Supported: SSE4.2, AVX 1.0                                Supported: SSE4.2, AVX 1.0
|  UNSUPPORTED: AVX2, FMA3, AVX-512                          UNSUPPORTED: AVX2, FMA3, AVX-512
+--------------------------------------------------------------------------------+
```

### 4.3 PyTorch Inductor Thread Exhaustion

Upstream vLLM (v0.6.x+) defaults to PyTorch Inductor graph compilation (`torch.compile`) to generate optimized Triton and C++ kernels for attention heads and tensor manipulation.

During container initialization:
1. PyTorch Inductor inspects the host CPU instruction set.
2. Detecting the absence of AVX2, Inductor's C++ code generator falls back to unvectorized scalar execution loops and emits parallel C++ OpenMP compilation tasks.
3. Because container CPU limits were not artificially throttled, Inductor allocated worker threads across all available system threads (`torch.get_num_threads()` = 32).
4. All 32 CPU threads immediately hit 100% compute load compiling unvectorized kernels, driving system load averages past 40 and starving Kubelet and OS worker threads.
5. Concurrently, vLLM initiated CUDA graph capture on the NVIDIA RTX 3060, executing rapid warmup forward passes across batch sizes 1 through 16. This combined CPU compute spike and GPU warmup induced severe host resource starvation.

### 4.4 Single Node OpenShift Single-GPU Scheduling Deadlock

When the cluster recovered and the deployment was updated, a secondary orchestration failure occurred:
* The default Kubernetes deployment update strategy is `RollingUpdate` with `maxSurge: 25%` and `maxUnavailable: 25%`.
* On a Single Node OpenShift cluster with a single physical GPU:
  1. The deployment controller created `vllm-inference-engine-2` and attempted to schedule it while `vllm-inference-engine-1` was still terminating.
  2. The Kubelet device plugin reported `Allocatable: nvidia.com/gpu: 1` and `Allocated: nvidia.com/gpu: 1`.
  3. `vllm-inference-engine-2` could not be scheduled: `0/1 nodes available: Insufficient nvidia.com/gpu`.
  4. `vllm-inference-engine-1` could not be fully deleted until the deployment rollout progressed.
  5. The deployment remained permanently stalled in `Pending` state.

---

## 5. The 5 Whys Analysis

1. **Why was the OpenShift cluster unresponsive and inaccessible?**  
   The bare-metal Dell Precision T5600 hypervisor suffered a complete ring-0 kernel lockup that prevented SSH authentication and systemd command execution.
2. **Why did the hypervisor kernel lock up?**  
   Processes executing dynamic GPU driver release became trapped in `TASK_UNINTERRUPTIBLE` (`D` state) due to an AB-BA deadlock on `console_lock`.
3. **Why did `console_lock` deadlock?**  
   The host prep helper script wrote to `/sys/class/vtconsole/vtcon*/bind` concurrently while `modprobe nvidia-drm` was initializing DRM framebuffers.
4. **Why did host recovery encounter severe compute starvation during workload bring-up?**  
   PyTorch Inductor launched parallel OpenMP compilation across all 32 CPU cores because eager execution was not enforced and the dual Xeon E5-2670 v1 CPUs lack AVX2 vector extensions.
5. **Why did the vLLM deployment stall indefinitely upon restart?**  
   Kubernetes' default `RollingUpdate` strategy attempted to schedule a second pod before the existing pod surrendered the single exclusive `nvidia.com/gpu: 1` allocation.

---

## 6. Permanent Engineering Mitigations

### 6.1 Mitigation 1: Sanitize Host Driver Lifecycle Automation

Refactored [`targets/t5600-homelab/gpu-host-admin.py`](../../targets/t5600-homelab/gpu-host-admin.py) to completely eliminate hazardous manipulation of `/sys/class/vtconsole/vtcon*/bind`.
* GPU release now strictly decouples display managers (`systemctl stop lightdm`), terminates active GPU processes (`fuser -k -9`), and unbinds PCIe devices directly via `/sys/bus/pci/devices/.../driver/unbind` without touching kernel virtual consoles.
* This completely eliminates the race condition on `down_console_sem`.

### 6.2 Mitigation 2: Enforce Eager Execution Mode (`--enforce-eager`)

By passing `--enforce-eager` to the vLLM container command line, PyTorch Inductor JIT compilation and CUDA graph capture are completely bypassed:
* **Mechanism**: PyTorch executes operations eagerly via standard cuBLAS and pre-compiled PyTorch CUDA kernels without Inductor graph compilation.
* **CPU Impact**: CPU utilization during initialization drops from 100% across 32 threads (3200% aggregate) to **<12%** on a single thread.
* **Startup Performance**: Model initialization time decreased from multi-minute compute hangs to **14 seconds** with absolute runtime stability.

Manifest declaration in `workloads/vllm/03-deployment.yaml`:
```yaml
      containers:
      - name: vllm
        image: vllm/vllm-openai:v0.6.3.post1
        args:
        - "--model"
        - "/mnt/models/hub/models--Qwen--Qwen2.5-3B-Instruct/snapshots/d906f36c54e0a7aa01e233d42fe830e051772f96"
        - "--served-model-name"
        - "Qwen/Qwen2.5-3B-Instruct"
        - "--port"
        - "8000"
        - "--enforce-eager"          # Critical: bypasses Inductor JIT & eliminates thread exhaustion
        - "--gpu-memory-utilization"
        - "0.85"
        - "--max-model-len"
        - "4096"
```

### 6.3 Mitigation 3: Declarative `Recreate` Deployment Strategy

To prevent GPU resource allocation deadlocks on Single Node OpenShift, the deployment strategy was explicitly configured to `Recreate`:
```yaml
  strategy:
    type: Recreate                   # Guarantees old pod termination and GPU release before new pod starts
```
* **Mechanism**: When an update is deployed, the Kubernetes deployment controller terminates the running pod, waits for Kubelet to release the `nvidia.com/gpu` allocation, and only then creates and schedules the replacement pod.
* **Result**: Zero scheduling deadlocks during rolling deployments.

### 6.4 Mitigation 4: Automated Policy Guardrail Tests

To prevent regressions in future pull requests or manifest edits, automated pytest guardrails were added to `tests/test_manifest_policies.py`:

```python
def test_vllm_deployment_eager_mode_guardrail():
    manifest = load_yaml(VLLM_DEPLOYMENT)
    containers = manifest["spec"]["template"]["spec"]["containers"]
    vllm_ctr = next((c for c in containers if c["name"] == "vllm"), None)
    args = vllm_ctr.get("args", [])
    assert "--enforce-eager" in args, (
        "CRITICAL: vLLM deployment must include '--enforce-eager' to prevent CPU "
        "exhaustion on legacy Xeon v1/v2 homelab hypervisors."
    )

def test_vllm_deployment_recreate_strategy():
    manifest = load_yaml(VLLM_DEPLOYMENT)
    strategy = manifest.get("spec", {}).get("strategy", {})
    assert strategy.get("type") == "Recreate", (
        "vLLM deployment strategy must be 'Recreate' to prevent multi-pod GPU contention "
        "on Single Node OpenShift during rollouts."
    )
```

### 6.5 Mitigation 5: Prometheus SRE Alerting Guardrails

Configured Prometheus alerting rules in `workloads/vllm/monitoring/prometheus-rules.yaml`:
* `VLLMEngineDown`: Fires within 1 minute if the inference engine drops offline.
* `VLLMQueueSaturation`: Fires if request queuing spikes, indicating compute backpressure.
* `VLLMKVCacheCritical`: Fires if GPU VRAM utilization exceeds 95%, warning of imminent OOM crashes.

---

## 7. Action Items & Verification Status

| Action Item | Category | Owner | Status | PR / Milestone |
| :--- | :--- | :--- | :--- | :--- |
| **AI-001**: Remove hazardous `vtconsole` unbinding in `gpu-host-admin.py` | Hypervisor Automation | Systems SRE | **Complete** | PR #2 |
| **AI-002**: Add `--enforce-eager` flag to vLLM deployment manifest | Workload Configuration | Systems SRE | **Complete** | PR #3 |
| **AI-003**: Configure `strategy: { type: Recreate }` in vLLM deployment | Workload Configuration | Systems SRE | **Complete** | PR #3 |
| **AI-004**: Add CI unit tests verifying eager mode and recreate strategy | Test & Quality Guardrails | Platform Engineering | **Complete** | PR #3 |
| **AI-005**: Implement ServiceMonitor & PrometheusRule alerting suite | Observability & SRE | Systems SRE | **Complete** | PR #5 |
| **AI-006**: Propagate `--enforce-eager` into OpenShift AI KServe `ServingRuntime` | Workload Configuration | Systems SRE | **Complete** | PR #6 |
| **AI-007**: Publish Day-2 GPU recovery runbook for PCIe bus resets | Documentation & Runbooks | Systems SRE | **Complete** | Milestone 3 |

---

## 8. Lessons Learned

### What Went Well
* **SRE Diagnostic Isolation**: Lingering background process telemetry and `wchan` inspection pinpointed the kernel `down_console_sem` contention rather than misdiagnosing the freeze as generic hardware failure.
* **State Preservation**: Model weights were mounted from a local NVMe persistent volume, avoiding re-downloading model weights after host recovery.
* **Rapid Workload Tuning**: Identifying the microarchitectural mismatch (AVX2 absence) allowed immediate mitigation via `--enforce-eager` without hardware replacement.

### What Went Wrong
* **Unsafe Sysfs Unbinding**: Blindly unbinding `/sys/class/vtconsole` without accounting for DRM framebuffer driver locking orders in kernel space introduced fatal ring-0 deadlocks.
* **Assumed Modern Instruction Sets**: Modern containerized ML frameworks (PyTorch, vLLM) default to assuming modern datacenter CPUs (AVX2/AVX-512). Running them on legacy server hardware requires explicit runtime guardrails.
* **Default Kubernetes Rollout Strategy**: Relying on default `RollingUpdate` for exclusive, un-shareable hardware resources (`nvidia.com/gpu: 1`) on Single Node OpenShift inevitably leads to scheduling deadlock.
