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
