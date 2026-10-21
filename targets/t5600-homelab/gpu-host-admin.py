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

def run(cmd):
    res = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return res

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
    print(json.dumps(result, indent=2))

def do_bind_vfio():
    print("[1/5] Stopping display manager and NoMachine...")
    run("systemctl stop lightdm.service nxserver.service 2>&1")
    time.sleep(2)
    
    print("[2/5] Terminating dangling GPU processes...")
    run("fuser -k -9 /dev/nvidia* /dev/dri/* 2>&1")
    time.sleep(1)
    
    print("[3/5] Unbinding VT consoles and framebuffers...")
    for vt in glob.glob("/sys/class/vtconsole/vtcon*/bind"):
        try:
            with open(vt, "w") as f:
                f.write("0\n")
        except Exception:
            pass
            
    for fb in glob.glob("/sys/bus/platform/drivers/*framebuffer*/*"):
        if os.path.exists(f"{fb}/driver/unbind"):
            try:
                base = os.path.basename(fb)
                with open(f"{fb}/driver/unbind", "w") as f:
                    f.write(f"{base}\n")
            except Exception:
                pass

    print("[4/5] Unbinding 0000:03:00.0 and 0000:03:00.1 from host drivers...")
    vga_dev = "/sys/bus/pci/devices/0000:03:00.0"
    audio_dev = "/sys/bus/pci/devices/0000:03:00.1"

    if os.path.exists(f"{vga_dev}/driver/unbind"):
        try:
            with open(f"{vga_dev}/driver/unbind", "w") as f:
                f.write("0000:03:00.0\n")
        except Exception as e:
            print(f"Notice unbinding VGA: {e}")

    if os.path.exists(f"{audio_dev}/driver/unbind"):
        try:
            with open(f"{audio_dev}/driver/unbind", "w") as f:
                f.write("0000:03:00.1\n")
        except Exception as e:
            print(f"Notice unbinding Audio: {e}")

    run("modprobe -r nvidia_drm nvidia_modeset nvidia_uvm nvidia 2>&1")

    print("[5/5] Binding to vfio-pci...")
    run("modprobe vfio-pci 2>&1")
    with open(f"{vga_dev}/driver_override", "w") as f:
        f.write("vfio-pci\n")
    with open(f"{audio_dev}/driver_override", "w") as f:
        f.write("vfio-pci\n")

    try:
        with open("/sys/bus/pci/drivers/vfio-pci/bind", "w") as f:
            f.write("0000:03:00.0\n")
    except Exception:
        pass
    try:
        with open("/sys/bus/pci/drivers/vfio-pci/bind", "w") as f:
            f.write("0000:03:00.1\n")
    except Exception:
        pass

    driver_vga = run("lspci -nnk -s 03:00.0 | grep 'Kernel driver in use' | awk '{print $5}'").stdout.strip()
    print(f"Status: GPU VGA driver is now '{driver_vga}'")

def do_restore_host():
    print("[1/5] Unbinding from vfio-pci...")
    vga_dev = "/sys/bus/pci/devices/0000:03:00.0"
    audio_dev = "/sys/bus/pci/devices/0000:03:00.1"

    if os.path.exists(f"{vga_dev}/driver/unbind"):
        try:
            with open(f"{vga_dev}/driver/unbind", "w") as f:
                f.write("0000:03:00.0\n")
        except Exception:
            pass

    if os.path.exists(f"{audio_dev}/driver/unbind"):
        try:
            with open(f"{audio_dev}/driver/unbind", "w") as f:
                f.write("0000:03:00.1\n")
        except Exception:
            pass

    print("[2/5] Clearing driver overrides...")
    with open(f"{vga_dev}/driver_override", "w") as f:
        f.write("\n")
    with open(f"{audio_dev}/driver_override", "w") as f:
        f.write("\n")

    print("[3/5] Loading host drivers (nvidia, snd_hda_intel)...")
    run("modprobe nvidia nvidia_modeset nvidia_uvm nvidia_drm snd_hda_intel 2>&1")

    if os.path.exists("/sys/bus/pci/drivers/nvidia/bind"):
        try:
            with open("/sys/bus/pci/drivers/nvidia/bind", "w") as f:
                f.write("0000:03:00.0\n")
        except Exception:
            pass

    if os.path.exists("/sys/bus/pci/drivers/snd_hda_intel/bind"):
        try:
            with open("/sys/bus/pci/drivers/snd_hda_intel/bind", "w") as f:
                f.write("0000:03:00.1\n")
        except Exception:
            pass

    print("[4/5] Re-enabling VT consoles...")
    for vt in glob.glob("/sys/class/vtconsole/vtcon*/bind"):
        try:
            with open(vt, "w") as f:
                f.write("1\n")
        except Exception:
            pass

    print("[5/5] Restarting display manager and NoMachine...")
    run("systemctl restart lightdm.service nxserver.service 2>&1")
    driver_vga = run("lspci -nnk -s 03:00.0 | grep 'Kernel driver in use' | awk '{print $5}'").stdout.strip()
    print(f"Status: GPU VGA driver is now '{driver_vga}'")

if __name__ == '__main__':
    act = get_action()
    if act == "bind_vfio":
        do_bind_vfio()
    elif act == "restore_host":
        do_restore_host()
    else:
        do_check()
