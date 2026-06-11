# -*- coding: utf-8 -*-
"""Database interface module for the resource-aware GPU Job Scheduler.

Physics/Algorithm:
    Manages persistent state of job queues and profiles execution history
    to estimate future VRAM usage constraints.
"""

import sqlite3
from datetime import datetime
from typing import List, Dict, Any, Optional
from gpu_scheduler.config import DB_PATH

def get_db_connection() -> sqlite3.Connection:
    """Establishes and returns a database connection.
    
    Returns:
        sqlite3.Connection: Database connection instance.
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    """Initializes the database and creates tables under the WAL journal mode."""
    conn = get_db_connection()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('PENDING', 'ASSIGNED', 'RUNNING', 'COMPLETED', 'FAILED')),
                username TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                gpu_assigned INTEGER,
                pid INTEGER,
                exit_code INTEGER,
                req_mem INTEGER DEFAULT 0,
                peak_mem INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()

def calculate_estimated_memory(command: str) -> int:
    """Estimates the required memory for a command based on historical peak memory.
    
    Formula:
        M_est = max( M_peak ) * 1.10
        
    Args:
        command (str): The command to check.
        
    Returns:
        int: Estimated VRAM usage in MB. Defaults to 4000 if no history exists.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        # Query peak memory of similar commands that completed successfully
        cursor.execute(
            """SELECT MAX(peak_mem) as max_peak 
               FROM jobs 
               WHERE command = ? AND status = 'COMPLETED' AND peak_mem > 0""",
            (command,)
        )
        row = cursor.fetchone()
        if row and row["max_peak"] is not None:
            # Add 10% safety margin
            return int(round(float(row["max_peak"]) * 1.10))
        return 4000 # Default fallback VRAM requirement: 4000MB
    finally:
        conn.close()

def add_job(command: str, username: str, req_mem: int = 0) -> int:
    """Adds a new job to the PENDING queue, auto-estimating memory if req_mem is 0.
    
    Args:
        command (str): The execution command string.
        username (str): Submitting OS username.
        req_mem (int): User-declared memory requirement in MB.
        
    Returns:
        int: The job unique identifier.
    """
    if req_mem <= 0:
        req_mem = calculate_estimated_memory(command)
        
    now = datetime.now().isoformat()
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO jobs (command, status, username, priority, req_mem, created_at) 
               VALUES (?, 'PENDING', ?, 0, ?, ?)""",
            (command, username, req_mem, now)
        )
        conn.commit()
        last_id = cursor.lastrowid
        return last_id
    finally:
        conn.close()

def get_pending_jobs() -> List[Dict[str, Any]]:
    """Retrieves pending jobs, ordered by creation time (FIFO).
    
    Returns:
        List[Dict[str, Any]]: Pending jobs.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM jobs WHERE status = 'PENDING' ORDER BY created_at ASC")
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()

def get_running_jobs() -> List[Dict[str, Any]]:
    """Retrieves jobs currently running or assigned (actively reserving GPUs).
    
    Returns:
        List[Dict[str, Any]]: Active GPU reserving jobs.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM jobs WHERE status IN ('ASSIGNED', 'RUNNING')")
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()

def assign_job_to_gpu(job_id: int, gpu_assigned: int) -> None:
    """Invoked by Scheduler to allocate a GPU and transition job to ASSIGNED state.
    
    Args:
        job_id (int): Target job ID.
        gpu_assigned (int): Allocated GPU index.
    """
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE jobs SET status = 'ASSIGNED', gpu_assigned = ? WHERE id = ?",
            (gpu_assigned, job_id)
        )
        conn.commit()
    finally:
        conn.close()

def set_job_running(job_id: int, pid: int) -> None:
    """Invoked by Scheduler Daemon to transition task state to RUNNING under target PID.
    
    Args:
        job_id (int): Target job ID.
        pid (int): Operating system process PID.
    """
    now = datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE jobs SET status = 'RUNNING', pid = ?, started_at = ? WHERE id = ?",
            (pid, now, job_id)
        )
        conn.commit()
    finally:
        conn.close()

def update_job_peak_memory(job_id: int, peak_mem: int) -> None:
    """Updates the peak memory value of a job if the new reading is higher.
    
    Args:
        job_id (int): Target job ID.
        peak_mem (int): Measured active memory in MB.
    """
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE jobs SET peak_mem = ? WHERE id = ? AND (? > IFNULL(peak_mem, 0))",
            (peak_mem, job_id, peak_mem)
        )
        conn.commit()
    finally:
        conn.close()

def set_job_finished(job_id: int, status: str, exit_code: int) -> None:
    """Updates a job to terminal status with exit code registry.
    
    Args:
        job_id (int): Target job ID.
        status (str): Terminal state ('COMPLETED' or 'FAILED').
        exit_code (int): Return exit code.
    """
    now = datetime.now().isoformat()
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE jobs SET status = ?, exit_code = ?, ended_at = ? WHERE id = ?",
            (status, exit_code, now, job_id)
        )
        conn.commit()
    finally:
        conn.close()

def get_all_jobs(limit: int = 50) -> List[Dict[str, Any]]:
    """Retrieves log history of all jobs.
    
    Args:
        limit (int): Max records.
        
    Returns:
        List[Dict[str, Any]]: List of all job records.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()

def get_job(job_id: int) -> Optional[Dict[str, Any]]:
    """Retrieves metadata for a specific job.
    
    Args:
        job_id (int): Job identifier.
        
    Returns:
        Optional[Dict[str, Any]]: Job details if found.
    """
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()

def reset_stuck_jobs() -> None:
    """Resets jobs stuck in ASSIGNED or RUNNING state back to PENDING.
    
    Invoked upon Scheduler restarts to clean up state matrices.
    """
    conn = get_db_connection()
    try:
        conn.execute(
            """UPDATE jobs 
               SET status = 'PENDING', gpu_assigned = NULL, pid = NULL, started_at = NULL 
               WHERE status IN ('ASSIGNED', 'RUNNING')"""
        )
        conn.commit()
    finally:
        conn.close()
