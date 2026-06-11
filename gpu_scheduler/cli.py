# -*- coding: utf-8 -*-
"""Command-line interface for interaction with the resource-aware GPU Job Scheduler.

Physics/Algorithm:
    Handles administrative commands (init, start, stop, status, add, logs, kill)
    and formats VRAM allocations for operator display.
"""

import os
import sys
import argparse
import subprocess
import getpass
from pathlib import Path
from gpu_scheduler.config import (
    PID_FILE_PATH, SCHEDULER_DIR, DAEMON_LOG_PATH, LOGS_DIR
)
import gpu_scheduler.database as db
import gpu_scheduler.daemon as daemon
import gpu_scheduler.executor as executor

def start_daemon() -> None:
    """Dispatches the scheduler daemon as an isolated background process."""
    if daemon.is_daemon_running():
        print("Error: Scheduler daemon is already running.")
        sys.exit(1)
        
    cmd = [sys.executable, "-c", "import gpu_scheduler.daemon as d; d.start_scheduler()"]
    
    try:
        if sys.platform == "win32":
            # Detached process flags for Windows
            DETACHED_PROCESS = 0x00000008
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            subprocess.Popen(
                cmd,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True
            )
        else:
            # Fork detached process on Unix/Linux
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setpgrp,
                close_fds=True
            )
        print("Scheduler daemon started in the background.")
    except Exception as e:
        print(f"Failed to start scheduler daemon: {e}", file=sys.stderr)
        sys.exit(1)

def stop_daemon() -> None:
    """Gracefully terminates the background daemon by checking PID file registry."""
    if not daemon.is_daemon_running():
        print("Scheduler daemon is not running.")
        # Cleanup orphan PID file if any
        daemon.remove_pid_file()
        return

    try:
        pid = int(PID_FILE_PATH.read_text().strip())
        print(f"Stopping scheduler daemon (PID: {pid})...")
        
        if sys.platform == "win32":
            # On Windows, kill process using taskkill
            subprocess.run(f"taskkill /F /PID {pid}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            # On Unix, send SIGTERM
            os.kill(pid, 15) # SIGTERM
            
        # Wait for daemon to release resources and remove PID file
        for _ in range(10):
            if not daemon.is_daemon_running():
                break
            import time
            time.sleep(0.5)
            
        if daemon.is_daemon_running():
            # Force kill if still active
            print("Daemon not responding. Force killing...")
            if sys.platform == "win32":
                subprocess.run(f"taskkill /F /PID {pid}", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                os.kill(pid, 9) # SIGKILL
        
        daemon.remove_pid_file()
        print("Scheduler daemon stopped.")
    except Exception as e:
        print(f"Failed to stop daemon: {e}", file=sys.stderr)

def show_status() -> None:
    """Prints the status of the scheduler daemon and active job queue."""
    db.init_db()
    is_running = daemon.is_daemon_running()
    daemon_pid = PID_FILE_PATH.read_text().strip() if PID_FILE_PATH.exists() else "None"
    
    print("====================================================================================")
    print("                        GPU Job Scheduler Status                                    ")
    print("====================================================================================")
    print(f"Daemon Status : {'RUNNING' if is_running else 'STOPPED'}")
    print(f"Daemon PID    : {daemon_pid}")
    print(f"Storage Dir   : {SCHEDULER_DIR}")
    print("------------------------------------------------------------------------------------")
    
    jobs = db.get_all_jobs(limit=20)
    if not jobs:
        print("No jobs registered in the queue.")
        print("====================================================================================")
        return
        
    # Format Job table
    header_fmt = "{:<5} {:<10} {:<5} {:<8} {:<10} {:<12} {:<12} {}"
    row_fmt = "{:<5} {:<10} {:<5} {:<8} {:<10} {:<12} {:<12} {}"
    
    print(header_fmt.format("ID", "Status", "GPU", "PID", "ExitCode", "ReqVRAM(MB)", "PeakVRAM(MB)", "Command"))
    print("-" * 100)
    for job in reversed(jobs):
        gpu = str(job["gpu_assigned"]) if job["gpu_assigned"] is not None else "-"
        pid = str(job["pid"]) if job["pid"] is not None else "-"
        exit_code = str(job["exit_code"]) if job["exit_code"] is not None else "-"
        req_mem = str(job["req_mem"]) if job["req_mem"] is not None else "0"
        peak_mem = str(job["peak_mem"]) if job["peak_mem"] is not None else "0"
        
        print(row_fmt.format(
            job["id"],
            job["status"],
            gpu,
            pid,
            exit_code,
            req_mem,
            peak_mem,
            job["command"][:30] + ("..." if len(job["command"]) > 30 else "")
        ))
    print("====================================================================================")

def add_job_command(command: str, req_mem: int) -> None:
    """Adds a new command string to the job execution database.
    
    Args:
        command (str): Shell command to execute.
        req_mem (int): Dynamic VRAM memory budget requested.
    """
    db.init_db()
    username = getpass.getuser()
    job_id = db.add_job(command, username, req_mem)
    
    # Query back details
    job = db.get_job(job_id)
    assigned_mem = job["req_mem"] if job else 0
    print(f"Successfully queued Job #{job_id} (VRAM Requirement: {assigned_mem}MB): '{command}'")

def view_logs(job_id: int) -> None:
    """Prints the execution stdout/stderr logs of the specified job ID.
    
    Args:
        job_id (int): Target job ID.
    """
    log_file = LOGS_DIR / f"job_{job_id}.log"
    if not log_file.exists():
        print(f"No log file found for Job #{job_id} at {log_file}")
        sys.exit(1)
    
    print(f"=== Standard Output/Error Logs for Job #{job_id} ===")
    sys.stdout.flush()
    sys.stdout.buffer.write(log_file.read_bytes())
    sys.stdout.buffer.flush()
    print("\n====================================================")

def kill_job_command(job_id: int) -> None:
    """Kills an active or running job.
    
    Args:
        job_id (int): Target job ID.
    """
    db.init_db()
    job = db.get_job(job_id)
    if not job:
        print(f"Error: Job #{job_id} not found.")
        sys.exit(1)
        
    if job["status"] not in ("PENDING", "ASSIGNED", "RUNNING"):
        print(f"Job #{job_id} is already in a terminal state: {job['status']}.")
        return
        
    if job["status"] in ("PENDING", "ASSIGNED"):
        db.set_job_finished(job_id, "FAILED", -15)
        print(f"Cancelled pending/assigned Job #{job_id}.")
    else:
        success = executor.kill_job(job_id)
        if success:
            print(f"Successfully killed running Job #{job_id}.")
        else:
            print(f"Failed to kill running Job #{job_id} process.")

def main() -> None:
    """CLI entry point parsing user arguments."""
    parser = argparse.ArgumentParser(description="Resource-Aware GPU Job Scheduler CLI Tool")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # init
    subparsers.add_parser("init", help="Initialize the database and storage directory")
    
    # start
    subparsers.add_parser("start", help="Start the scheduler daemon in the background")
    
    # stop
    subparsers.add_parser("stop", help="Stop the running scheduler daemon")
    
    # status
    subparsers.add_parser("status", help="Show current daemon state and job queue")
    
    # add
    add_parser = subparsers.add_parser("add", help="Add a new job command to the queue")
    add_parser.add_argument("job_cmd", type=str, help="Shell command string to run")
    add_parser.add_argument("--req-mem", type=int, default=0, help="VRAM memory requirement in MB (Default: 0 for auto-profile)")
    
    # logs
    logs_parser = subparsers.add_parser("logs", help="View standard output/error logs of a job")
    logs_parser.add_argument("job_id", type=int, help="Target job ID")
    
    # kill
    kill_parser = subparsers.add_parser("kill", help="Cancel or terminate a job")
    kill_parser.add_argument("job_id", type=int, help="Target job ID")
    
    args = parser.parse_args()
    
    if args.command == "init":
        db.init_db()
        print(f"Initialized storage space and database under {SCHEDULER_DIR}")
    elif args.command == "start":
        start_daemon()
    elif args.command == "stop":
        stop_daemon()
    elif args.command == "status":
        show_status()
    elif args.command == "add":
        add_job_command(args.job_cmd, args.req_mem)
    elif args.command == "logs":
        view_logs(args.job_id)
    elif args.command == "kill":
        kill_job_command(args.job_id)

if __name__ == "__main__":
    main()
