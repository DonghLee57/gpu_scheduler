# -*- coding: utf-8 -*-
"""Execution module for managing subprocesses and capturing execution logs.

Physics/Algorithm:
    Handles OS-level subprocess dispatching under user runner contexts.
    Restricts tasks to assigned GPU indexes using environment variables.
"""

import os
import sys
import subprocess
import logging
from typing import Dict, Optional
from gpu_scheduler.config import LOGS_DIR
import gpu_scheduler.database as db

logger = logging.getLogger("gpusched.executor")

# Memory tracking for active subprocesses: job_id -> subprocess.Popen
_active_processes: Dict[int, subprocess.Popen] = {}

def run_job(job_id: int, command: str, gpu_index: int) -> None:
    """Dispatches a job as a background subprocess bound to a designated GPU.
    
    Args:
        job_id (int): Database identifier of the job.
        command (str): Shell command execution string.
        gpu_index (int): Hardware GPU index assigned to the job.
    """
    log_file_path = LOGS_DIR / f"job_{job_id}.log"
    
    # Ensure parent log directories exist
    log_file_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Configure environment targeting specific GPU
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = str(gpu_index)
    
    logger.info("Dispatching job %d on GPU %d: '%s'", job_id, gpu_index, command)
    
    try:
        # Open output log file
        log_file = open(log_file_path, "w", encoding="utf-8", buffering=1)
        
        # Start subprocess (using shell execution to match CLI expectations)
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=log_file,
            stderr=log_file,
            env=env,
            text=True
        )
        
        # Close the local handle immediately to prevent resource leaks.
        # The child process retains its duplicated descriptor.
        log_file.close()
        
        # Track processes in memory and DB
        _active_processes[job_id] = proc
        db.set_job_running(job_id, proc.pid)
        logger.info("Job %d running under PID %d", job_id, proc.pid)
        
    except Exception as e:
        logger.error("Failed to execute job %d command: %s", job_id, e)
        db.set_job_finished(job_id, "FAILED", -1)

def poll_active_jobs() -> None:
    """Checks the completion status of all memory-tracked subprocesses.
    
    Updates SQLite database states on task termination.
    """
    finished_jobs = []
    
    for job_id, proc in _active_processes.items():
        exit_code = proc.poll()
        if exit_code is not None:
            # Subprocess completed
            status = "COMPLETED" if exit_code == 0 else "FAILED"
            logger.info("Job %d finished with exit code %d (Status: %s)", job_id, exit_code, status)
            
            # Update DB state
            db.set_job_finished(job_id, status, exit_code)
            finished_jobs.append(job_id)
            
    # Clean up referenced Popen objects
    for job_id in finished_jobs:
        _active_processes.pop(job_id)

def kill_job(job_id: int) -> bool:
    """Terminates an active process.
    
    Args:
        job_id (int): Database identifier of the target job.
        
    Returns:
        bool: True if process was terminated, False if not found.
    """
    proc = _active_processes.get(job_id)
    if proc:
        logger.info("Terminating active Job %d (PID: %d)", job_id, proc.pid)
        try:
            # Terminate process tree if needed, otherwise standard terminate
            proc.terminate()
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        
        db.set_job_finished(job_id, "FAILED", -15) # -15 typically indicates SIGTERM
        _active_processes.pop(job_id)
        return True
        
    # If the process isn't tracked in memory, but is marked RUNNING in the DB
    job = db.get_job(job_id)
    if job and job["status"] == "RUNNING" and job["pid"]:
        pid = job["pid"]
        logger.info("Terminating untracked Active Job %d with OS PID %d", job_id, pid)
        try:
            if sys.platform == "win32":
                subprocess.run(f"taskkill /F /PID {pid}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.kill(pid, 9)
            db.set_job_finished(job_id, "FAILED", -9)
            return True
        except Exception as e:
            logger.error("Failed to kill OS process %d: %s", pid, e)
            
    return False

def get_active_process_count() -> int:
    """Returns number of active running processes managed by this executor session.
    
    Returns:
        int: Active subprocess count.
    """
    return len(_active_processes)
