# Incident Post-Mortem 001: Hardware Power-Trip & GPU Scheduling Deadlock

**Incident ID**: `INC-20261021-01`  
**Date**: October 21, 2026  
**Severity**: Severity 1 (Critical Cluster Outage / Hardware Trip)  
**Status**: Resolved & Mitigated  
**Affected Systems**: Single Node OpenShift (`sno-node`), Bare-Metal Dell Precision T5600 Hypervisor, `llm-serving/vllm` Service  
**Authors**: Platform Infrastructure & Systems SRE Engineering Team  

---

## 1. Executive Summary

On October 21, 2026, during the initial deployment of the vLLM inference engine serving `Qwen/Qwen2.5-3B-Instruct` on a Single Node OpenShift (SNO) cluster hosted on bare-metal Dell Precision T5600 hardware, the physical host experienced a sudden, ungraceful power loss (instant black-out). 

The incident was caused by an Over-Current Protection (OCP) trip within the host Power Supply Unit (PSU). The power trip was triggered by an instantaneous multi-rail power spike: PyTorch Inductor launched parallel JIT compilation across all 32 logical CPU cores to compensate for the absence of AVX2 instructions on the dual Intel Xeon E5-2670 v1 processors, occurring simultaneously with peak transient inrush current during NVIDIA RTX 3060 CUDA graph capture.

Following host reboot and filesystem recovery, a secondary scheduling failure occurred: the Kubernetes default `RollingUpdate` deployment strategy attempted to schedule a replacement vLLM pod while the previous pod held the single physical GPU allocation (`nvidia.com/gpu: 1`), causing an indefinite deployment rollout deadlock.

Both failure modes were permanently resolved through declarative workload configuration:
1. Enforcing eager execution (`--enforce-eager`), completely bypassing PyTorch Inductor JIT compilation and CUDA graph capture.
2. Setting deployment strategy to `Recreate` (`strategy: { type: Recreate }`), guaranteeing GPU lock release before scheduling replacement pods.

Automated policy guardrail unit tests were added to CI to prevent configuration regressions. Total downtime was 42 minutes with zero data loss.

---

## 2. Impact Assessment

| Dimension | Metrics / Observations |
| :--- | :--- |
| **User / Client Impact** | Complete loss of OpenAI-compatible inference API endpoint (`vllm.apps.sno.homelab.local`). HTTP 503 / connection refused. |
| **Incident Duration** | **42 minutes** total (18:14 UTC to 18:56 UTC). |
| **Cluster Impact** | Single Node OpenShift control plane and worker components abruptly unpowered. Required RHCOS journal replay and etcd single-node recovery. |
| **Physical Hardware** | Dell Precision T5600 shut down via PSU hardware protection (crowbar circuit). No hardware damage sustained. |
| **Data Integrity** | Zero data corruption. Hugging Face model cache on local NVMe PV remained consistent; etcd single-member WAL recovered cleanly. |

---

## 3. Incident Timeline (October 21, 2026)

| Time (UTC) | Event Description |
| :--- | :--- |
| **18:12:00** | Initial deployment of `workloads/vllm/03-deployment.yaml` applied to OpenShift cluster (`oc apply -f workloads/vllm/`). |
| **18:13:10** | Container image `vllm/vllm-openai:v0.6.3.post1` pulled from local registry. Pod transitioned to `Running` and began initialization. |
| **18:13:45** | vLLM engine started model weight loading into GPU VRAM (12GB GDDR6 on RTX 3060). |
| **18:14:02** | Model weights loaded. Engine initialized PyTorch Inductor kernel compilation and CUDA graph capture warmup routines. |
| **18:14:08** | **Hardware Trip**: Dell Precision T5600 abruptly lost power. Front panel diagnostic LEDs unlit, power button LED flashing amber, fans halted instantly. |
| **18:14:30** | SRE on-call noted loss of SSH connectivity to hypervisor `t5600` (`192.168.1.100`) and SNO node (`192.168.150.50`). |
| **18:17:00** | Physical inspection of host machine. Power supply required full AC line disconnect (power cord unplugged for 15s to reset PSU crowbar latch). |
| **18:18:15** | AC reconnected; host powered on. Linux kernel booted into Ubuntu hypervisor; KVM/QEMU restored SNO VM with PCIe passthrough. |
| **18:22:40** | SNO RHCOS booted; etcd quorum verified; core OpenShift cluster operators returned to `Available=True`. |
| **18:25:00** | SNO Kubelet attempted to restart the vLLM pod. SRE cordoned the node to halt automated container restart loops pending investigation. |
| **18:31:00** | Host kernel logs (`journalctl -b -1`, `/var/log/syslog`) inspected. No kernel panics, MCEs (Machine Check Exceptions), or PCIe AER errors logged before outage—confirming an external electrical/PSU trip. |
| **18:38:00** | Root cause reproduced in controlled test harness with clamped current meters and CPU/GPU telemetry: Inductor multi-thread JIT compilation saturated dual Xeon E5-2670 v1 CPUs to 100% across 32 threads while GPU drew 170W+ during CUDA graph capture. |
| **18:44:00** | Engineering mitigation devised: apply `--enforce-eager` to container args; adjust deployment strategy to `Recreate`. |
| **18:48:30** | Updated manifest committed and applied (`workloads/vllm/03-deployment.yaml`). |
| **18:50:15** | Pod restarted with `--enforce-eager`. Model initialized within 14 seconds: CPU utilization peaked at 12%, GPU power peaked at 52W. Zero power spikes. |
| **18:52:00** | Automated benchmark script (`scripts/test-vllm-endpoint.sh`) executed. TTFT: 48ms, generation: 38.4 tokens/sec. |
| **18:56:00** | Incident formally declared resolved. |

---

## 4. Technical Root Cause Analysis (RCA)

### 4.1 Hardware Microarchitecture & Instruction Set Limitations

The Dell Precision T5600 workstation is powered by dual **Intel Xeon E5-2670 v1** processors (Sandy Bridge-EP architecture, 8 physical cores / 16 threads per socket = 16 cores / 32 logical threads total). 

Key architectural characteristics:
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

### 4.2 PyTorch Inductor JIT Compilation Mechanics

Upstream vLLM (starting in v0.6.x) defaults to PyTorch 2.4+ Inductor graph compilation (`torch.compile`) to generate optimized Triton and C++ kernels for attention heads and tensor manipulations.

During initialization:
1. PyTorch Inductor inspects the host CPU capabilities.
2. Detecting the absence of AVX2, Inductor's C++ code generator falls back to unvectorized scalar execution loops and emits parallel C++ OpenMP compilation tasks.
3. Because container CPU limits were not artificially throttled, Inductor dynamically allocated worker threads across all available system threads (`torch.get_num_threads()` = 32).
4. All 32 CPU threads immediately hit 100% compute load compiling C++ code. The dual 115W TDP processors spiked beyond their thermal design envelope, drawing over 260W combined from the motherboard CPU VRMs.

### 4.3 Concurrent GPU CUDA Graph Capture

Simultaneously, vLLM initiated CUDA graph capture on the PCIe-passthrough NVIDIA RTX 3060:
1. To avoid kernel launch overhead during real-time inference, vLLM traces and records CUDA execution graphs for batch sizes 1 through 16.
2. Tracing involves executing synthetic forward passes on the GPU in rapid succession.
3. The GPU transitioned from P8 idle clock state (210 MHz core / 405 MHz memory, ~12W) to P0 maximum boost clock state (1882 MHz core / 7500 MHz memory, ~170W).
4. The rapid ramp-up generated an inductive transient inrush spike (`di/dt`) across the 12V PCIe auxiliary power rails.

### 4.4 Electrical Dynamics: PSU Over-Current Protection (OCP)

The Dell Precision T5600 employs an OEM multi-rail power distribution board delivering +12V power across dedicated channels:
* **Rail 12VA**: Motherboard and Socket 0 CPU VRM (~18A limit).
* **Rail 12VB**: Socket 1 CPU VRM (~18A limit).
* **Rail 12VC**: PCIe slot and dual 6-pin auxiliary PCIe power headers (~25A limit).

When dual 100% CPU thread saturation coincided with the GPU P0 inrush current:
$$\text{Total 12V Load} = P_{\text{CPU0}} + P_{\text{CPU1}} + P_{\text{GPU}} + P_{\text{Chassis}} \approx 135\text{W} + 135\text{W} + 210\text{W}_{\text{peak}} + 75\text{W} \approx 555\text{W on 12V}$$
Instantaneous amperage on the combined 12V rail crossed the manufacturer's Over-Current Protection (OCP) and Over-Power Protection (OPP) thresholds. The PSU supervisory IC triggered an immediate crowbar shutdown to protect the motherboard traces and power planes from thermal melting.

```
Time ------------------------------------------------------------------->
CPU Util:  [ 5% ] -------------> [ 100% across 32 threads (JIT) ] ----> SHUTDOWN
GPU Power: [ 14W ] ------------> [ 170W-210W Peak (CUDA Graph) ] -----> SHUTDOWN
PSU Rail:  [ 12A ] ------------> [ 46A+ Instantaneous Draw ] ---------> [ OCP TRIP ]
```

### 4.5 Single Node OpenShift (SNO) Single-GPU Scheduling Deadlock

When the cluster recovered, an attempt to update the deployment revealed a secondary software architectural flaw:
* The default Kubernetes deployment update strategy is `RollingUpdate` with `maxSurge: 25%` and `maxUnavailable: 25%`.
* On an SNO cluster with a single physical GPU:
  1. The deployment controller created `Pod-v2` and attempted to schedule it while `Pod-v1` was still terminating.
  2. The Kubelet device plugin reported `Allocatable: nvidia.com/gpu: 1` and `Allocated: nvidia.com/gpu: 1`.
  3. `Pod-v2` could not be scheduled: `0/1 nodes available: Insufficient nvidia.com/gpu`.
  4. `Pod-v1` could not be fully deleted until `Pod-v2` indicated readiness or until the rollout progressed.
  5. The deployment remained permanently stalled in `Pending` state.

---

## 5. The 5 Whys Analysis

1. **Why did the OpenShift cluster lose power?**  
   The bare-metal Dell Precision T5600 workstation suffered an ungraceful hardware shutdown.
2. **Why did the host suffer an ungraceful shutdown?**  
   The host Power Supply Unit (PSU) triggered its Over-Current Protection (OCP) / Over-Power Protection (OPP) safety shutdown.
3. **Why did the PSU trigger OCP/OPP?**  
   The instantaneous current draw across the +12V rails exceeded the maximum permissible threshold during container initialization.
4. **Why was current draw abnormally high during container initialization?**  
   All 32 CPU threads hit 100% utilization simultaneously while the GPU simultaneously drew peak transient power during CUDA graph capture.
5. **Why did the CPU hit 100% utilization across 32 threads for a GPU workload?**  
   PyTorch Inductor initiated multi-threaded C++ JIT compilation routines because eager execution was not explicitly forced, and the legacy Xeon E5-2670 v1 CPU lacks AVX2 vector extensions.

---

## 6. Permanent Engineering Mitigations

### 6.1 Mitigation 1: Enforce Eager Execution Mode (`--enforce-eager`)

By passing `--enforce-eager` to the vLLM container command line, PyTorch Inductor JIT compilation and CUDA graph capture are completely disabled:
* **Mechanism**: PyTorch executes operations eagerly via standard cuBLAS and PyTorch CUDA kernels without compilation or graph tracing.
* **CPU Impact**: CPU utilization during initialization drops from 100% across 32 threads (3200% aggregate) to **<12%** on a single thread.
* **GPU Impact**: GPU initialization power consumption stabilizes at **45W-55W**, eliminating transient inrush current.
* **Latency Trade-off**: Marginal increase in token latency (<4%), but model startup time decreased from >2 minutes to **14 seconds** with absolute electrical stability.

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
        - "--enforce-eager"          # Critical: bypasses Inductor JIT & CUDA graph capture
        - "--gpu-memory-utilization"
        - "0.85"
        - "--max-model-len"
        - "4096"
```

### 6.2 Mitigation 2: Declarative `Recreate` Deployment Strategy

To prevent GPU resource allocation deadlocks on single-GPU nodes, the deployment strategy was explicitly configured to `Recreate`:
```yaml
  strategy:
    type: Recreate                   # Guarantees old pod termination and GPU release before new pod starts
```
* **Mechanism**: When an update is pushed, the Kubernetes deployment controller terminates `Pod-v1`, waits for the Kubelet device plugin to release the `nvidia.com/gpu` allocation, and only then creates and schedules `Pod-v2`.
* **Result**: Zero scheduling deadlocks during rolling deployments.

### 6.3 Mitigation 3: Automated Policy Guardrail Tests

To prevent accidental regressions in future pull requests or manifest edits, automated pytest guardrails were added to `tests/test_manifest_policies.py`:

```python
def test_vllm_deployment_eager_mode_guardrail():
    manifest = load_yaml(VLLM_DEPLOYMENT)
    containers = manifest["spec"]["template"]["spec"]["containers"]
    vllm_ctr = next((c for c in containers if c["name"] == "vllm"), None)
    args = vllm_ctr.get("args", [])
    assert "--enforce-eager" in args, (
        "CRITICAL: vLLM deployment must include '--enforce-eager' to prevent CPU/GPU "
        "power spikes on Xeon v1/v2 homelab hypervisors."
    )

def test_vllm_deployment_recreate_strategy():
    manifest = load_yaml(VLLM_DEPLOYMENT)
    strategy = manifest.get("spec", {}).get("strategy", {})
    assert strategy.get("type") == "Recreate", (
        "vLLM deployment strategy must be 'Recreate' to prevent multi-pod GPU contention "
        "on Single Node OpenShift during rollouts."
    )
```

### 6.4 Mitigation 4: Prometheus SRE Alerting Guardrails

Configured Prometheus alerting rules in `workloads/vllm/monitoring/prometheus-rules.yaml`:
* `VLLMEngineDown`: Fires within 1 minute if the inference engine drops offline.
* `VLLMQueueSaturation`: Fires if request queuing spikes, indicating compute backpressure.
* `VLLMKVCacheCritical`: Fires if GPU VRAM utilization exceeds 95%, warning of imminent OOM crashes.

---

## 7. Action Items & Verification Status

| Action Item | Category | Owner | Status | PR / Ticket |
| :--- | :--- | :--- | :--- | :--- |
| **AI-001**: Add `--enforce-eager` flag to vLLM deployment manifest | Workload Configuration | Systems SRE | **Complete** | PR #3 |
| **AI-002**: Configure `strategy: { type: Recreate }` in vLLM deployment | Workload Configuration | Systems SRE | **Complete** | PR #3 |
| **AI-003**: Add CI unit tests verifying eager mode and recreate strategy | Test & Quality Guardrails | Platform Engineering | **Complete** | PR #3 |
| **AI-004**: Implement ServiceMonitor & PrometheusRule alerting suite | Observability & SRE | Systems SRE | **Complete** | PR #5 |
| **AI-005**: Propagate `--enforce-eager` into OpenShift AI KServe `ServingRuntime` | Workload Configuration | Systems SRE | **Complete** | PR #6 |
| **AI-006**: Publish Day-2 GPU recovery runbook for PCIe bus resets | Documentation & Runbooks | Systems SRE | **Complete** | Milestone 3 |

---

## 8. Lessons Learned

### What Went Well
* **Hardware Self-Protection**: The Dell Precision T5600 PSU OCP circuitry functioned exactly as designed, preventing component damage or electrical hazards.
* **State Preservation**: Persisting Hugging Face model weights on a local NVMe PV meant zero model re-downloading was required after node recovery.
* **Rapid Diagnostic Isolation**: Eliminating software bug theories early allowed fast electrical and microarchitectural root cause identification.

### What Went Wrong
* **Assumed Modern Instruction Sets**: Modern containerized ML frameworks (PyTorch, vLLM, TensorRT-LLM) default to assumptions of modern datacenter hardware (AVX2, AVX-512, robust redundant 1600W+ PSUs). Deploying them on homelab or legacy server tiers without explicit runtime guardrails carries hidden physical risks.
* **Default Kubernetes Rollout Strategy**: Relying on default `RollingUpdate` for exclusive, un-shareable hardware resources (`nvidia.com/gpu: 1`) on Single Node OpenShift inevitably leads to scheduling deadlock.

### Where We Got Lucky
* The sudden power loss occurred during read-only model initialization rather than during an active etcd disk write transaction, avoiding etcd database corruption.
