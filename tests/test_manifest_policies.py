# ==============================================================================
# Manifest Policy Guardrail Tests: OpenShift AI Workloads
# ==============================================================================

import os
import subprocess
import yaml

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
VLLM_DEPLOYMENT = os.path.join(REPO_ROOT, "workloads", "vllm", "03-deployment.yaml")
VLLM_STORAGE = os.path.join(REPO_ROOT, "workloads", "vllm", "02-storage.yaml")
TIME_SLICING_CONFIG = os.path.join(REPO_ROOT, "operators", "nvidia-gpu-operator", "time-slicing-config.yaml")


def load_yaml(path):
    assert os.path.exists(path), f"Manifest file does not exist: {path}"
    with open(path, "r") as f:
        return yaml.safe_load(f)


def test_vllm_deployment_gpu_resources():
    manifest = load_yaml(VLLM_DEPLOYMENT)
    containers = manifest["spec"]["template"]["spec"]["containers"]
    vllm_ctr = next((c for c in containers if c["name"] == "vllm"), None)
    assert vllm_ctr is not None, "Container 'vllm' not found in deployment"

    resources = vllm_ctr.get("resources", {})
    limits = resources.get("limits", {})
    requests = resources.get("requests", {})

    assert limits.get("nvidia.com/gpu") == 1, "vLLM container must set limits nvidia.com/gpu: 1"
    assert requests.get("nvidia.com/gpu") == 1, "vLLM container must set requests nvidia.com/gpu: 1"
    assert "memory" in limits, "vLLM container must set memory limits"


def test_vllm_deployment_eager_mode_guardrail():
    manifest = load_yaml(VLLM_DEPLOYMENT)
    containers = manifest["spec"]["template"]["spec"]["containers"]
    vllm_ctr = next((c for c in containers if c["name"] == "vllm"), None)
    assert vllm_ctr is not None

    args = vllm_ctr.get("args", [])
    assert "--enforce-eager" in args, (
        "CRITICAL: vLLM deployment must include '--enforce-eager' to prevent CPU/GPU "
        "power spikes on Xeon v1/v2 homelab hypervisors."
    )


def test_vllm_deployment_shared_memory_mount():
    manifest = load_yaml(VLLM_DEPLOYMENT)
    pod_spec = manifest["spec"]["template"]["spec"]

    # Verify dshm volume exists with Memory medium
    volumes = pod_spec.get("volumes", [])
    dshm_vol = next((v for v in volumes if v["name"] == "dshm"), None)
    assert dshm_vol is not None, "Volume 'dshm' not declared in pod spec"
    assert dshm_vol.get("emptyDir", {}).get("medium") == "Memory", (
        "Volume 'dshm' must be configured as emptyDir with medium: Memory"
    )

    # Verify container mounts /dev/shm
    containers = pod_spec.get("containers", [])
    vllm_ctr = next((c for c in containers if c["name"] == "vllm"), None)
    mounts = vllm_ctr.get("volumeMounts", [])
    shm_mount = next((m for m in mounts if m.get("mountPath") == "/dev/shm"), None)
    assert shm_mount is not None, "/dev/shm mount missing from vLLM container"


def test_vllm_deployment_probes():
    manifest = load_yaml(VLLM_DEPLOYMENT)
    containers = manifest["spec"]["template"]["spec"]["containers"]
    vllm_ctr = next((c for c in containers if c["name"] == "vllm"), None)

    for probe_name in ["startupProbe", "readinessProbe", "livenessProbe"]:
        probe = vllm_ctr.get(probe_name)
        assert probe is not None, f"Container 'vllm' is missing {probe_name}"
        http_get = probe.get("httpGet", {})
        assert http_get.get("path") == "/health", f"{probe_name} path must be /health"
        assert http_get.get("port") == 8000, f"{probe_name} port must be 8000"


def test_vllm_deployment_recreate_strategy():
    manifest = load_yaml(VLLM_DEPLOYMENT)
    strategy = manifest.get("spec", {}).get("strategy", {})
    assert strategy.get("type") == "Recreate", (
        "vLLM deployment strategy must be 'Recreate' to prevent multi-pod GPU contention "
        "on Single Node OpenShift during rollouts."
    )


def load_all_yaml(path):
    assert os.path.exists(path), f"Manifest file does not exist: {path}"
    with open(path, "r") as f:
        return list(yaml.safe_load_all(f))


def test_vllm_storage_capacity():
    docs = load_all_yaml(VLLM_STORAGE)
    pv = next((d for d in docs if d.get("kind") == "PersistentVolume"), None)
    assert pv is not None, "PersistentVolume not found in 02-storage.yaml"
    storage_capacity = pv.get("spec", {}).get("capacity", {}).get("storage")
    assert storage_capacity == "50Gi", "Model cache PV capacity must be at least 50Gi"

    pvc = next((d for d in docs if d.get("kind") == "PersistentVolumeClaim"), None)
    assert pvc is not None, "PersistentVolumeClaim not found in 02-storage.yaml"
    claim_storage = pvc.get("spec", {}).get("resources", {}).get("requests", {}).get("storage")
    assert claim_storage == "50Gi", "Model cache PVC requested storage must be 50Gi"


def test_time_slicing_configuration():
    manifest = load_yaml(TIME_SLICING_CONFIG)
    data = manifest.get("data", {})
    # NVIDIA Time-Slicing ConfigMap keys can be 'any' or 'default'
    config_key = next((k for k in ["any", "default"] if k in data), None)
    assert config_key is not None, f"Time slicing ConfigMap missing 'any' or 'default' key: {data.keys()}"

    raw_config = data[config_key]
    parsed_config = yaml.safe_load(raw_config)
    resources = parsed_config.get("sharing", {}).get("timeSlicing", {}).get("resources", [])
    assert len(resources) > 0, "No time-slicing resources defined in ConfigMap"
    assert resources[0].get("replicas", 0) >= 2, "Time-slicing replicas must be at least 2"


def test_kustomize_build_vllm():
    kustomize_dir = os.path.join(REPO_ROOT, "workloads", "vllm")
    # Test using kubectl kustomize
    cmd = ["kubectl", "kustomize", kustomize_dir]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"kubectl kustomize failed on {kustomize_dir}: {res.stderr}"

    # Verify generated output contains valid YAML documents
    docs = list(yaml.safe_load_all(res.stdout))
    assert len(docs) >= 5, f"Expected at least 5 resources in kustomize output, got {len(docs)}"


def test_vllm_deployment_termination_grace_period():
    manifest = load_yaml(VLLM_DEPLOYMENT)
    pod_spec = manifest["spec"]["template"]["spec"]
    grace_period = pod_spec.get("terminationGracePeriodSeconds")
    assert grace_period is not None, "vLLM deployment must specify terminationGracePeriodSeconds"
    assert grace_period >= 60, f"terminationGracePeriodSeconds must be at least 60s, got {grace_period}"


def test_vllm_pod_disruption_budget():
    pdb_path = os.path.join(REPO_ROOT, "workloads", "vllm", "06-pdb.yaml")
    manifest = load_yaml(pdb_path)
    assert manifest.get("kind") == "PodDisruptionBudget"
    assert manifest["spec"]["minAvailable"] == 1
    assert manifest["spec"]["selector"]["matchLabels"]["app"] == "vllm-inference-engine"


def test_vllm_network_policy():
    netpol_path = os.path.join(REPO_ROOT, "workloads", "vllm", "07-network-policy.yaml")
    manifest = load_yaml(netpol_path)
    assert manifest.get("kind") == "NetworkPolicy"
    ingress_rules = manifest["spec"].get("ingress", [])
    assert len(ingress_rules) >= 2, "NetworkPolicy must define ingress rules for router and monitoring"

    # Verify ingress port 8000 is opened
    ports = [p["port"] for rule in ingress_rules for p in rule.get("ports", [])]
    assert 8000 in ports, "Port 8000 must be permitted in NetworkPolicy ingress"


def test_vllm_service_monitor():
    sm_path = os.path.join(REPO_ROOT, "workloads", "vllm", "monitoring", "service-monitor.yaml")
    manifest = load_yaml(sm_path)
    assert manifest.get("kind") == "ServiceMonitor"
    endpoints = manifest["spec"].get("endpoints", [])
    assert len(endpoints) > 0, "ServiceMonitor must define at least one endpoint"
    endpoint = endpoints[0]
    assert endpoint.get("port") == "http"
    assert endpoint.get("path") == "/metrics"


def test_vllm_prometheus_rules():
    rules_path = os.path.join(REPO_ROOT, "workloads", "vllm", "monitoring", "prometheus-rules.yaml")
    manifest = load_yaml(rules_path)
    assert manifest.get("kind") == "PrometheusRule"
    groups = manifest["spec"].get("groups", [])
    assert len(groups) > 0
    alert_names = [rule["alert"] for rule in groups[0].get("rules", [])]
    expected_alerts = [
        "VLLMEngineDown",
        "VLLMHighLatencyP95",
        "VLLMQueueSaturation",
        "VLLMKVCacheHigh",
        "VLLMKVCacheCritical",
        "VLLMEnginePreemptionsActive",
    ]
    for alert in expected_alerts:
        assert alert in alert_names, f"Expected alert '{alert}' not found in PrometheusRule"


def test_kserve_serving_runtime_specs():
    rt_path = os.path.join(REPO_ROOT, "workloads", "rhoai-kserve", "serving-runtime.yaml")
    manifest = load_yaml(rt_path)
    assert manifest.get("kind") == "ServingRuntime"
    spec = manifest.get("spec", {})

    # Verify model formats
    formats = [f["name"] for f in spec.get("supportedModelFormats", [])]
    assert "vLLM" in formats, "ServingRuntime must support vLLM model format"

    # Verify container configuration
    containers = spec.get("containers", [])
    assert len(containers) > 0
    kserve_ctr = containers[0]
    assert kserve_ctr.get("name") == "kserve-container"
    assert "--enforce-eager" in kserve_ctr.get("args", [])

    # Verify shared memory mount
    mounts = [m["mountPath"] for m in kserve_ctr.get("volumeMounts", [])]
    assert "/dev/shm" in mounts, "ServingRuntime must mount /dev/shm"


def test_kserve_inference_service_specs():
    isvc_path = os.path.join(REPO_ROOT, "workloads", "rhoai-kserve", "inference-service.yaml")
    manifest = load_yaml(isvc_path)
    assert manifest.get("kind") == "InferenceService"

    annotations = manifest.get("metadata", {}).get("annotations", {})
    assert annotations.get("serving.kserve.io/deploymentMode") == "RawDeployment"

    predictor = manifest.get("spec", {}).get("predictor", {})
    model = predictor.get("model", {})
    assert model.get("runtime") == "vllm-serving-runtime"
    assert model.get("modelFormat", {}).get("name") == "vLLM"

    # Verify GPU resource quotas
    resources = model.get("resources", {})
    limits = resources.get("limits", {})
    requests = resources.get("requests", {})
    assert str(limits.get("nvidia.com/gpu")) == "1"
    assert str(requests.get("nvidia.com/gpu")) == "1"


def test_kserve_storage_connection_secret():
    secret_path = os.path.join(REPO_ROOT, "workloads", "rhoai-kserve", "storage-connection.yaml")
    manifest = load_yaml(secret_path)
    assert manifest.get("kind") == "Secret"

    annotations = manifest.get("metadata", {}).get("annotations", {})
    assert annotations.get("opendatahub.io/connection-type") == "s3"

    string_data = manifest.get("stringData", {})
    required_keys = ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_S3_ENDPOINT", "AWS_S3_BUCKET"]
    for key in required_keys:
        assert key in string_data, f"Storage connection secret missing key: {key}"


def test_kustomize_build_rhoai_kserve():
    kustomize_dir = os.path.join(REPO_ROOT, "workloads", "rhoai-kserve")
    cmd = ["kubectl", "kustomize", kustomize_dir]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"kubectl kustomize failed on {kustomize_dir}: {res.stderr}"

    docs = list(yaml.safe_load_all(res.stdout))
    assert len(docs) == 3, f"Expected 3 resources in rhoai-kserve kustomize output, got {len(docs)}"


def test_machineconfig_iommu_specs():
    mc_path = os.path.join(REPO_ROOT, "operators", "machineconfig", "99-master-kernel-iommu.yaml")
    manifest = load_yaml(mc_path)
    assert manifest.get("kind") == "MachineConfig"
    assert manifest.get("apiVersion") == "machineconfiguration.openshift.io/v1"
    labels = manifest.get("metadata", {}).get("labels", {})
    assert labels.get("machineconfiguration.openshift.io/role") == "master"

    kernel_args = manifest.get("spec", {}).get("kernelArguments", [])
    assert "intel_iommu=on" in kernel_args
    assert "iommu=pt" in kernel_args


def test_machineconfig_hugepages_specs():
    mc_path = os.path.join(REPO_ROOT, "operators", "machineconfig", "99-master-hugepages.yaml")
    manifest = load_yaml(mc_path)
    assert manifest.get("kind") == "MachineConfig"
    assert manifest.get("apiVersion") == "machineconfiguration.openshift.io/v1"
    labels = manifest.get("metadata", {}).get("labels", {})
    assert labels.get("machineconfiguration.openshift.io/role") == "master"

    kernel_args = manifest.get("spec", {}).get("kernelArguments", [])
    assert "default_hugepagesz=1G" in kernel_args
    assert "hugepagesz=1G" in kernel_args
    assert "hugepages=4" in kernel_args


def test_kustomize_build_machineconfig():
    mc_dir = os.path.join(REPO_ROOT, "operators", "machineconfig")
    cmd = ["kubectl", "kustomize", mc_dir]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 0, f"kubectl kustomize failed on {mc_dir}: {res.stderr}"

    docs = list(yaml.safe_load_all(res.stdout))
    assert len(docs) == 2, f"Expected 2 MachineConfig resources, got {len(docs)}"
    names = [d["metadata"]["name"] for d in docs]
    assert "99-master-kernel-iommu" in names
    assert "99-master-hugepages" in names


def test_post_mortem_and_runbook_integrity():
    docs_to_check = [
        (
            os.path.join(REPO_ROOT, "docs", "post-mortems", "2026-10-21-kernel-lockup-gpu-scheduling-deadlock.md"),
            ["Root Cause Analysis", "5 Whys", "--enforce-eager", "Recreate", "Incident Timeline"],
        ),
        (
            os.path.join(REPO_ROOT, "docs", "linux-kernel-and-hardware.md"),
            ["PCIe Architecture", "IOMMU", "vfio-pci", "cgroups v2", "Hugepages"],
        ),
        (
            os.path.join(REPO_ROOT, "docs", "runbooks", "gpu-recovery.md"),
            ["Xid", "nvidia-smi", "PCIe", "Recovery", "Verification"],
        ),
        (
            os.path.join(REPO_ROOT, "docs", "runbooks", "node-drain.md"),
            ["--ignore-daemonsets", "--delete-emptydir-data", "PodDisruptionBudget", "Cordon"],
        ),
        (
            os.path.join(REPO_ROOT, "docs", "runbooks", "driver-upgrade.md"),
            ["ClusterPolicy", "NVIDIA GPU Operator", "Rollout", "Rollback"],
        ),
    ]

    for file_path, required_terms in docs_to_check:
        assert os.path.exists(file_path), f"Required documentation file missing: {file_path}"
        assert os.path.getsize(file_path) > 500, f"Documentation file is unexpectedly small: {file_path}"
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
            for term in required_terms:
                assert term in content, f"Term '{term}' missing from {file_path}"
