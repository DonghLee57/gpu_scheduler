# Resource-Aware GPU Job Scheduler User Manual (Single-User Resource-Aware Edition)

This scheduler is a lightweight GPU task management system operating entirely within user-space without administrative (`sudo`) privileges. When a user submits tasks to the queue, the scheduler daemon evaluates the physical VRAM availability against the declared reservations of each job to control sequential or parallel execution, actively preventing Out-Of-Memory (OOM) failures through real-time telemetry tracking.

---

## 1. Environment Setup

All scheduler state and job configurations are persisted in a local SQLite file database (`gpusched.db`).

### A. Environment Variables
Define the directory where the database and execution logs will reside. If omitted, the scheduler defaults to `~/.gpusched` in the user's home directory.

*   **Linux/macOS (bash, zsh)**:
    ```bash
    export GPUSCHED_DIR="$HOME/.gpusched"
    ```
*   **Windows (PowerShell)**:
    ```powershell
    $env:GPUSCHED_DIR="C:\Users\user\.gpusched"
    ```

### B. Normalized Threshold Configurations (Optional)
You can customize the resource utilization limits used to evaluate target physical hardware:
*   `GPUSCHED_MEM_THRESHOLD`: Upper bound of physical GPU memory allocation ratio (Default: `0.20` -> 20%)
*   `GPUSCHED_UTIL_THRESHOLD`: Upper bound of physical GPU core load utilization percentage (Default: `0.15` -> 15%)
*   `GPUSCHED_POLLING_INTERVAL`: Telemetry polling and queue scans frequency in seconds (Default: `5.0`s)

---

## 2. Installation & Execution

This scheduler can be installed locally as a Python package using `pip` and executed globally via the exposed CLI command `gpusched`.

### A. Package Installation
Execute the following commands in the project root directory (`gpu_scheduler`):
*   **Editable Mode Installation (Recommended)**:
    ```bash
    pip install -e .
    ```
    Source code modifications are immediately reflected in your environment without needing a package re-installation.
*   **Standard Installation**:
    ```bash
    pip install .
    ```

### B. Executing the Command-Line Tool (`gpusched`)
Once installed, instead of the verbose `python -m gpu_scheduler.cli`, you can invoke all scheduler features using the clean `gpusched` shorthand:
```bash
# Initialize database schema and folders
gpusched init

# Start the scheduler daemon in the background
gpusched start

# Check queue status and telemetry
gpusched status
```
*   **PATH Environment Variable Warning**: On Windows systems, if you receive a warning indicating that the Python Scripts folder is not on your `PATH`, append the output path (e.g. `C:\Users\user\AppData\Local\Packages\...\Scripts`) to your system `PATH` environment variable to run `gpusched` globally.

---

## 3. Initialization & Master Daemon Lifecycle

The master scheduler daemon calculates the physical GPU VRAM budget in the background and spawns eligible tasks from the queue directly as subprocesses.

### A. Database Initialization
Run once before first execution to bootstrap the schema and directories:
```bash
gpusched init
```

### B. Launching the Daemon (Background Run)
Start the automated scheduling loop:
```bash
gpusched start
```
*   Upon startup, the scheduler writes the active process ID to `gpusched.pid` and pipes execution traces to `gpusched.log`.
*   **Note**: To prevent process interruption when closing remote terminal (SSH) connections, it is highly recommended to run the daemon inside a terminal multiplexer like `tmux`, or use `nohup`.

---

## 4. Job Registration & Resource Allocation Policy

When tasks are registered in the queue, VRAM budgets are evaluated dynamically based on three core strategies:

### A. Explicit VRAM Reservations
Users can explicitly declare the maximum memory capacity (in Megabytes) required for the command:
```bash
gpusched add "python train.py --epochs 100" --req-mem 6000
```
*   The scheduler gates execution until the target GPU has an allocated budget of $6000\text{MB}$ or more available.

### B. Historical Auto-Estimation (When VRAM is Omitted)
If `--req-mem` is omitted, the scheduler estimates VRAM needs by learning from past runs:
```bash
gpusched add "python train.py --epochs 100"
```
*   **Run History Exists**: The scheduler queries identical command pattern matches from completed (`COMPLETED`) database records, retrieves the peak memory usage ($M_{\text{peak}}$), and allocates a 10% safety buffer:
    $$M_{\text{req}} = \max \left( M_{\text{peak}} \right) \times 1.10$$
*   **No Run History**: The system applies a default placeholder reservation of `4000MB` to execute the first run.

### C. Active Watchdog and OOM Prevention
Once a job transitions to the `RUNNING` state, the daemon queries `nvidia-smi` to monitor the real-time VRAM allocation $M_{\text{active}}(t)$ of the subprocess (PID) and dynamically updates `PeakVRAM` in the database.
*   **Hardware Capacity Violations**: If active consumption surges past $95\%$ of the physical GPU limit, the watchdog triggers a forced termination signal ($\text{SIGTERM}$) to prevent server-wide OOM failures.
*   **Reservation Violations**: If active usage exceeds the declared budget ($M_{\text{req}}$) while the physical GPU capacity utilization is in a critical state ($> 90\%$), the job is reclaimed.

---

## 5. Queue Status & Telemetry Monitoring

To view the active daemon state, pending queue order, and allocation metrics in a table layout, run:

```bash
gpusched status
```

### Telemetry Layout Example

```
====================================================================================
                        GPU Job Scheduler Status                                    
====================================================================================
Daemon Status : RUNNING
Daemon PID    : 14612
Storage Dir   : /home/user/.gpusched
------------------------------------------------------------------------------------
ID    Status     GPU   PID      ExitCode   ReqVRAM(MB)  PeakVRAM(MB) Command
----------------------------------------------------------------------------------------------------
1     COMPLETED  0     19960    0          4000         1500         python train.py --run 1
2     RUNNING    0     2304     -          1650         920          python train.py --run 2
3     PENDING    -     -        -          4000         0            python evaluate.py
====================================================================================
```

*   **Status Indicators**:
    *   `PENDING`: Task is queued, awaiting sufficient resource allocations.
    *   `ASSIGNED`: GPU is allocated; the task is in a transient state right before execution.
    *   `RUNNING`: Subprocess has been spawned and is actively executing compute operations on the GPU.
    *   `COMPLETED`: Task terminated with return code `0`.
    *   `FAILED`: Task aborted due to errors, user cancellation, or OOM watchdog reclamation.
*   **ReqVRAM(MB)**: VRAM reserved by the user or dynamically predicted from logs.
*   **PeakVRAM(MB)**: Peak memory footprint recorded by the watchdog. This acts as the baseline for future run estimations.

---

## 6. Logs & Output Verification

Console standard outputs (stdout) and standard errors (stderr) are captured and saved to individual log files:

```bash
# View stdout/stderr details of a specific job ID (e.g. ID: 2)
gpusched logs 2
```

---

## 7. Job Cancellation

You can cancel queued tasks or kill running subprocesses to free up resources immediately:

```bash
# Cancel or terminate job ID 3
gpusched kill 3
```
*   Tasks in `PENDING` or `ASSIGNED` states are canceled before execution starts.
*   `RUNNING` tasks are securely terminated at the process-group level, changing their state to `FAILED` (Exit Code `-15`).

---

## 8. Daemon Shutdown & State Recovery

### A. Normal Daemon Termination
```bash
gpusched stop
```
*   The system removes the active PID file and shuts down the background daemon. Running jobs are preserved independently to finish execution.

### B. Failure Recovery & Session Resilience
If the host server crashes or the daemon is killed unexpectedly, the relational SQLite state remains intact.
*   Upon restarting the daemon (`gpusched start`), the system sweeps the database to detect jobs left stranded in `ASSIGNED` or `RUNNING` states.
*   Stranded records are automatically reset back to `PENDING` with cleared PID allocations, allowing the queue to resume safely from the exact point of interruption.