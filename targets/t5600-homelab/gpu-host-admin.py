#!/usr/bin/env python3
# ==============================================================================
# GPU Host Administration Helper (Dell Precision T5600)
# Manages VFIO binding, display manager release, and host restoration
# ==============================================================================

import json
import os
import sys
import time
import subprocess
import glob

ACTION_FILE = "/home/bjarrett/admin_tools/action.json"

def log(msg):
    print(msg, flush=True)

def run(cmd, timeout=15):
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return res
    except subprocess.TimeoutExpired:
        log(f"Warning: command timed out after {timeout}s: {cmd}")
        return subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr="Timed out")

def write_sysfs(path, content, timeout=5):
    if not os.path.exists(path):
        return False
    cmd = f"printf '%s\\n' '{content}' > '{path}'"
    res = run(cmd, timeout=timeout)
    return res.returncode == 0

def get_action():
    if os.path.exists(ACTION_FILE):
        try:
            with open(ACTION_FILE) as f:
                data = json.load(f)
                return data.get("action", "check")
        except Exception:
            pass
    return "check"

def do_check():
    driver_vga = run("lspci -nnk -s 03:00.0 | grep 'Kernel driver in use' | awk '{print $5}'").stdout.strip() or "none"
    driver_audio = run("lspci -nnk -s 03:00.1 | grep 'Kernel driver in use' | awk '{print $5}'").stdout.strip() or "none"
    procs = run("fuser /dev/nvidia* /dev/dri/* 2>/dev/null").stdout.strip()
    dm_active = run("systemctl is-active lightdm.service 2>/dev/null").stdout.strip()
    nx_active = run("systemctl is-active nxserver.service 2>/dev/null").stdout.strip()
    
    result = {
        "vga_driver": driver_vga,
        "audio_driver": driver_audio,
        "active_gpu_pids": procs.split() if procs else [],
        "lightdm_active": dm_active == "active",
        "nxserver_active": nx_active == "active",
        "ready_for_vfio": driver_vga == "vfio-pci" and not procs
    }
    log(json.dumps(result, indent=2))

def do_bind_vfio():
    log("[1/4] Stopping display manager and NoMachine...")
    run("systemctl stop lightdm.service nxserver.service 2>&1")
    time.sleep(2)
    
    log("[2/4] Terminating dangling GPU processes...")
    run("fuser -k -9 /dev/nvidia* /dev/dri/* 2>&1")
    time.sleep(1)

    log("[3/4] Unbinding 0000:03:00.0 and 0000:03:00.1 from host drivers...")
    vga_dev = "/sys/bus/pci/devices/0000:03:00.0"
    audio_dev = "/sys/bus/pci/devices/0000:03:00.1"

    write_sysfs(f"{vga_dev}/driver/unbind", "0000:03:00.0")
    write_sysfs(f"{audio_dev}/driver/unbind", "0000:03:00.1")

    log("[4/4] Setting driver_override and binding to vfio-pci...")
    run("modprobe vfio-pci 2>&1")
    write_sysfs(f"{vga_dev}/driver_override", "vfio-pci")
    write_sysfs(f"{audio_dev}/driver_override", "vfio-pci")

    write_sysfs("/sys/bus/pci/drivers/vfio-pci/bind", "0000:03:00.0")
    write_sysfs("/sys/bus/pci/drivers/vfio-pci/bind", "0000:03:00.1")

    driver_vga = run("lspci -nnk -s 03:00.0 | grep 'Kernel driver in use' | awk '{print $5}'").stdout.strip()
    log(f"Status: GPU VGA driver is now '{driver_vga}'")

def do_restore_host():
    log("[1/4] Unbinding from vfio-pci...")
    vga_dev = "/sys/bus/pci/devices/0000:03:00.0"
    audio_dev = "/sys/bus/pci/devices/0000:03:00.1"

    write_sysfs(f"{vga_dev}/driver/unbind", "0000:03:00.0")
    write_sysfs(f"{audio_dev}/driver/unbind", "0000:03:00.1")

    log("[2/4] Clearing driver overrides...")
    write_sysfs(f"{vga_dev}/driver_override", "")
    write_sysfs(f"{audio_dev}/driver_override", "")

    log("[3/4] Binding to host drivers (nvidia, snd_hda_intel)...")
    run("modprobe nvidia snd_hda_intel 2>&1")
    write_sysfs("/sys/bus/pci/drivers/nvidia/bind", "0000:03:00.0")
    write_sysfs("/sys/bus/pci/drivers/snd_hda_intel/bind", "0000:03:00.1")

    log("[4/4] Restarting display manager and NoMachine...")
    run("systemctl restart lightdm.service nxserver.service 2>&1")
    driver_vga = run("lspci -nnk -s 03:00.0 | grep 'Kernel driver in use' | awk '{print $5}'").stdout.strip()
    log(f"Status: GPU VGA driver is now '{driver_vga}'")

if __name__ == '__main__':
    act = get_action()
    if act == "bind_vfio":
        do_bind_vfio()
    elif act == "restore_host":
        do_restore_host()
    else:
        do_check()
