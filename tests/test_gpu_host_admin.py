# ==============================================================================
# Unit Tests: GPU Host Administration Helper
# ==============================================================================

import importlib.util
import json
import os
import subprocess
from unittest.mock import mock_open, patch

# Dynamically import targets/t5600-homelab/gpu-host-admin.py as a module
MODULE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "targets", "t5600-homelab", "gpu-host-admin.py")
)
spec = importlib.util.spec_from_file_location("gpu_host_admin", MODULE_PATH)
gpu_host_admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gpu_host_admin)


def test_get_action_default_when_missing():
    with patch("os.path.exists", return_value=False):
        assert gpu_host_admin.get_action() == "check"


def test_get_action_valid_json():
    mock_data = json.dumps({"action": "bind_vfio"})
    with patch("os.path.exists", return_value=True), patch("builtins.open", mock_open(read_data=mock_data)):
        assert gpu_host_admin.get_action() == "bind_vfio"


def test_get_action_malformed_json():
    with patch("os.path.exists", return_value=True), patch("builtins.open", mock_open(read_data="{invalid_json")):
        assert gpu_host_admin.get_action() == "check"


@patch("builtins.print")
@patch.object(gpu_host_admin, "run")
def test_do_check_ready_for_vfio(mock_run, mock_print):
    def side_effect(cmd, **kwargs):
        if "03:00.0" in cmd and "lspci" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="vfio-pci\n")
        elif "03:00.1" in cmd and "lspci" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="vfio-pci\n")
        elif "fuser" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="")
        elif "lightdm" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="inactive\n")
        elif "nxserver" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="inactive\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    mock_run.side_effect = side_effect

    gpu_host_admin.do_check()

    assert mock_print.called
    printed_json = json.loads(mock_print.call_args[0][0])
    assert printed_json["vga_driver"] == "vfio-pci"
    assert printed_json["ready_for_vfio"] is True
    assert printed_json["lightdm_active"] is False
    assert printed_json["active_gpu_pids"] == []


@patch("builtins.print")
@patch.object(gpu_host_admin, "run")
def test_do_check_busy_host(mock_run, mock_print):
    def side_effect(cmd, **kwargs):
        if "03:00.0" in cmd and "lspci" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="nvidia\n")
        elif "03:00.1" in cmd and "lspci" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="snd_hda_intel\n")
        elif "fuser" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="1234 5678\n")
        elif "lightdm" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="active\n")
        elif "nxserver" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="active\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="")

    mock_run.side_effect = side_effect

    gpu_host_admin.do_check()

    printed_json = json.loads(mock_print.call_args[0][0])
    assert printed_json["vga_driver"] == "nvidia"
    assert printed_json["ready_for_vfio"] is False
    assert printed_json["lightdm_active"] is True
    assert printed_json["active_gpu_pids"] == ["1234", "5678"]


@patch.object(gpu_host_admin, "time")
@patch.object(gpu_host_admin, "write_sysfs", return_value=True)
@patch.object(gpu_host_admin, "run")
def test_do_bind_vfio(mock_run, mock_write_sysfs, mock_time):
    mock_run.return_value = subprocess.CompletedProcess("lspci", 0, stdout="vfio-pci\n")

    gpu_host_admin.do_bind_vfio()

    # Verify stop and termination commands
    mock_run.assert_any_call("systemctl stop lightdm.service nxserver.service 2>&1")
    mock_run.assert_any_call("fuser -k -9 /dev/nvidia* /dev/dri/* 2>&1")
    mock_run.assert_any_call("modprobe vfio-pci 2>&1")

    # Verify sysfs driver override and bind
    mock_write_sysfs.assert_any_call("/sys/bus/pci/devices/0000:03:00.0/driver_override", "vfio-pci")
    mock_write_sysfs.assert_any_call("/sys/bus/pci/devices/0000:03:00.1/driver_override", "vfio-pci")
    mock_write_sysfs.assert_any_call("/sys/bus/pci/drivers/vfio-pci/bind", "0000:03:00.0")
    mock_write_sysfs.assert_any_call("/sys/bus/pci/drivers/vfio-pci/bind", "0000:03:00.1")


@patch.object(gpu_host_admin, "time")
@patch.object(gpu_host_admin, "write_sysfs", return_value=True)
@patch.object(gpu_host_admin, "run")
def test_do_restore_host(mock_run, mock_write_sysfs, mock_time):
    mock_run.return_value = subprocess.CompletedProcess("lspci", 0, stdout="nvidia\n")

    gpu_host_admin.do_restore_host()

    # Verify driver overrides cleared
    mock_write_sysfs.assert_any_call("/sys/bus/pci/devices/0000:03:00.0/driver_override", "")
    mock_write_sysfs.assert_any_call("/sys/bus/pci/devices/0000:03:00.1/driver_override", "")

    # Verify kernel module load & device bind
    mock_run.assert_any_call("modprobe nvidia snd_hda_intel 2>&1")
    mock_write_sysfs.assert_any_call("/sys/bus/pci/drivers/nvidia/bind", "0000:03:00.0")
    mock_write_sysfs.assert_any_call("/sys/bus/pci/drivers/snd_hda_intel/bind", "0000:03:00.1")
    mock_run.assert_any_call("systemctl restart lightdm.service nxserver.service 2>&1")
