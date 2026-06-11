# -*- coding: utf-8 -*-
"""Resource monitor module for tracking GPU usage metrics.

Physics/Algorithm:
    Interfaces with nvidia-smi via subprocess execution to evaluate physical
    GPU core utilization and memory footprint. Resolves active VRAM usage of specific PIDs.
"""

import os
import subprocess
import shutil
import logging
from typing import List, Dict, Any
from gpu_scheduler.config import MEM_THRESHOLD, UTIL_THRESHOLD, MOCK_MODE

logger = logging.getLogger("gpusched.monitor")

def get_gpu_metrics() -> List[Dict[str, Any]]:
    """Retrieves physical metrics for all detected NVIDIA GPUs.
    
    Returns:
        List[Dict[str, Any]]: A list of dictionaries containing GPU metrics.
    """
    if MOCK_MODE or not shutil.which("nvidia-smi"):
        # Return mock telemetry if physical driver/GPU is absent or forced mock
        return [
            {"index": 0, "mem_used": 1024.0, "mem_total": 8192.0, "util": 5.0},
            {"index": 1, "mem_used": 7168.0, "mem_total": 8192.0, "util": 85.0}
        ]

    try:
        cmd = [
            "nvidia-smi",
            "--query-gpu=index,memory.used,memory.total,utilization.gpu",
            "--format=csv,noheader,nounits"
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        
        metrics = []
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 4:
                metrics.append({
                    "index": int(parts[0]),
                    "mem_used": float(parts[1]),
                    "mem_total": float(parts[2]),
                    "util": float(parts[3])
                })
        return metrics
    except Exception as e:
        logger.error("Failed to query nvidia-smi: %s. Falling back to empty telemetry.", e)
        return []

def get_available_gpus() -> List[int]:
    """Evaluates availability matrix and returns indexes of qualified GPUs.
    
    Returns:
        List[int]: List of available GPU indexes.
    """
    metrics = get_gpu_metrics()
    available = []
    
    for gpu in metrics:
        mem_ratio = gpu["mem_used"] / gpu["mem_total"]
        util_percent = gpu["util"]
        
        mem_ok = mem_ratio < MEM_THRESHOLD
        util_ok = util_percent < (UTIL_THRESHOLD * 100.0)
        
        if mem_ok and util_ok:
            available.append(gpu["index"])
            logger.debug(
                "GPU %d AVAILABLE (Mem: %.1f%% < %.1f%%, Util: %.1f%% < %.1f%%)",
                gpu["index"], mem_ratio * 100.0, MEM_THRESHOLD * 100.0,
                util_percent, UTIL_THRESHOLD * 100.0
            )
        else:
            logger.debug(
                "GPU %d BUSY (Mem: %.1f%% [Thresh: %.1f%%], Util: %.1f%% [Thresh: %.1f%%])",
                gpu["index"], mem_ratio * 100.0, MEM_THRESHOLD * 100.0,
                util_percent, UTIL_THRESHOLD * 100.0
            )
            
    return available

def get_process_vram_usage(pid: int) -> float:
    """Queries nvidia-smi to obtain the VRAM usage (in MB) for a specific process ID.
    
    Args:
        pid (int): Target process identifier.
        
    Returns:
        float: VRAM usage in MB. Returns 0.0 if not found or in error.
    """
    if MOCK_MODE:
        # In mock mode, we simulate usage.
        # We can dynamically increase the usage or return a fixed value for validation.
        mock_env = os.environ.get("GPUSCHED_MOCK_PID_MEM")
        if mock_env:
            return float(mock_env)
        return 1500.0

    if not shutil.which("nvidia-smi"):
        return 0.0

    try:
        cmd = [
            "nvidia-smi",
            "--query-compute-apps=pid,used_memory",
            "--format=csv,noheader,nounits"
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        
        for line in result.stdout.strip().split("\n"):
            if not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 2:
                app_pid = int(parts[0])
                app_mem = float(parts[1])
                if app_pid == pid:
                    return app_mem
        return 0.0
    except Exception as e:
        logger.debug("Failed to query process VRAM for PID %d: %s", pid, e)
        return 0.0
