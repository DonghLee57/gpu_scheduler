# -*- coding: utf-8 -*-
"""Daemon process module orchestrating the main resource-aware scheduler loop.

Physics/Algorithm:
    Maintains a continuous loop running at POLLING_INTERVAL.
    Performs dynamic VRAM budgeting:
        M_budget = M_total - sum( M_req for active jobs )
    Gates pending jobs by enforcing:
        M_budget >= M_req_job
    Runs an active watchdog to terminate PIDs exceeding safety thresholds.
    Updates peak VRAM telemetry *before* resolving process terminations.
"""

import os
import sys
import time
import signal
import logging
from typing import List, Dict, Any
from gpu_scheduler.config import (
    PID_FILE_PATH, DAEMON_LOG_PATH, POLLING_INTERVAL
)
import gpu_scheduler.database as db
import gpu_scheduler.monitor as monitor
import gpu_scheduler.executor as executor

# Configure Logger for the Daemon
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    handlers=[
        logging.FileHandler(str(DAEMON_LOG_PATH), encoding="utf-8"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("gpusched.daemon")

_running = True

def handle_shutdown(signum, frame):
    """Gracefully terminates the scheduling loop upon OS signal request."""
    global _running
    logger.info("Termination signal received (Signal: %d). Initiating shutdown...", signum)
    _running = False

def is_daemon_running() -> bool:
    """Checks if a daemon instance is already active.
    
    Returns:
        bool: True if daemon is running, False otherwise.
    """
    if not PID_FILE_PATH.exists():
        return False
    try:
        pid = int(PID_FILE_PATH.read_text().strip())
    except (ValueError, OSError):
        return False

    # Check if OS process actually exists
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

def write_pid_file() -> None:
    """Writes current process identifier to PID file."""
    PID_FILE_PATH.write_text(str(os.getpid()))

def remove_pid_file() -> None:
    """Removes PID file if it exists."""
    if PID_FILE_PATH.exists():
        try:
            PID_FILE_PATH.unlink()
        except OSError:
            pass

def start_scheduler() -> None:
    """Starts the main scheduling loop as a persistent daemon.
    
    Enforces VRAM reservation budgets and runs a dynamic watchdog on active processes.
    """
    global _running
    
    if is_daemon_running():
        print("Error: Scheduler daemon is already running.", file=sys.stderr)
        sys.exit(1)
        
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, handle_shutdown)
        signal.signal(signal.SIGINT, handle_shutdown)
        
    write_pid_file()
    logger.info("Initializing Single-User Resource-Aware GPU Job Scheduler Daemon (PID: %d)", os.getpid())
    
    try:
        db.init_db()
        db.reset_stuck_jobs()
        logger.info("Database initialized. Polling interval: %.1fs", POLLING_INTERVAL)
        
        while _running:
            try:
                # 1. Fetch currently active jobs reserving resources (ASSIGNED or RUNNING)
                # We fetch before polling termination so we can profile active VRAM of PIDs
                # that might terminate during this polling cycle.
                active_jobs = db.get_running_jobs()
                
                # 2. Retrieve system physical GPU metrics (total capacity, used, etc.)
                gpu_metrics = {gpu["index"]: gpu for gpu in monitor.get_gpu_metrics()}
                
                # 3. Monitor VRAM of running processes (Watchdog & Peak VRAM telemetry update)
                for job in active_jobs:
                    if job["status"] == "RUNNING" and job["pid"] and job["gpu_assigned"] is not None:
                        job_id = job["id"]
                        pid = job["pid"]
                        gpu_idx = job["gpu_assigned"]
                        
                        # Fetch current active VRAM usage of the process
                        active_vram = monitor.get_process_vram_usage(pid)
                        if active_vram > 0:
                            db.update_job_peak_memory(job_id, int(active_vram))
                            
                            # Watchdog OOM prevention checks:
                            # If active VRAM exceeds 95% of total GPU memory capacity
                            gpu_info = gpu_metrics.get(gpu_idx)
                            if gpu_info:
                                total_mem = gpu_info["mem_total"]
                                if active_vram > (total_mem * 0.95):
                                    logger.warning(
                                        "Watchdog TRIGGERED for Job #%d (PID: %d): VRAM usage %dMB exceeds 95%% threshold of GPU %d (%dMB). Terminating process...",
                                        job_id, pid, active_vram, gpu_idx, total_mem
                                    )
                                    executor.kill_job(job_id)
                                    continue
                                    
                                # Or if it exceeds user-specified budget, and physical GPU memory is dangerously high (>90%)
                                if job["req_mem"] > 0 and active_vram > job["req_mem"] and (gpu_info["mem_used"] > total_mem * 0.90):
                                    logger.warning(
                                        "Watchdog TRIGGERED for Job #%d (PID: %d): VRAM usage %dMB exceeds reservation %dMB with high system load. Terminating process...",
                                        job_id, pid, active_vram, job["req_mem"]
                                    )
                                    executor.kill_job(job_id)
                                    continue

                # 4. Update active jobs completion status in executor session
                executor.poll_active_jobs()

                # Refresh active jobs list after watchdog and poll passes
                active_jobs = db.get_running_jobs()
                
                # 5. Compute available VRAM budgets for each physical GPU
                # Formula: M_budget = M_total - sum( M_req for active jobs on this GPU )
                gpu_vram_budgets = {}
                for gpu_idx, gpu in gpu_metrics.items():
                    available_gpus = monitor.get_available_gpus()
                    if gpu_idx not in available_gpus:
                        gpu_vram_budgets[gpu_idx] = 0.0
                        continue
                        
                    reserved_vram = sum(
                        job["req_mem"] for job in active_jobs 
                        if job["gpu_assigned"] == gpu_idx
                    )
                    budget = gpu["mem_total"] - reserved_vram
                    gpu_vram_budgets[gpu_idx] = budget
                    
                # 6. Dispatch pending jobs based on FIFO queue and budget gating
                pending_jobs = db.get_pending_jobs()
                for job in pending_jobs:
                    req_mem = job["req_mem"]
                    allocated_gpu = None
                    
                    # Search for a GPU with enough VRAM budget
                    for gpu_idx, budget in gpu_vram_budgets.items():
                        if budget >= req_mem:
                            allocated_gpu = gpu_idx
                            break
                            
                    if allocated_gpu is not None:
                        # Allocate and run immediately (Single User model)
                        db.assign_job_to_gpu(job["id"], allocated_gpu)
                        executor.run_job(job["id"], job["command"], allocated_gpu)
                        
                        # Update budget since this GPU is now reserved
                        gpu_vram_budgets[allocated_gpu] -= req_mem
                    else:
                        # FIFO gate
                        break
                        
            except Exception as e:
                logger.error("Error in scheduling cycle: %s", e, exc_info=True)
                
            time.sleep(POLLING_INTERVAL)
            
    except KeyboardInterrupt:
        logger.info("Daemon interrupted by keyboard.")
    finally:
        remove_pid_file()
        logger.info("Scheduler daemon shut down. PID file removed.")
