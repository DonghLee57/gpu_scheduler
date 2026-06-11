# -*- coding: utf-8 -*-
"""Configuration management module for the GPU Job Scheduler.

Physics/Algorithm:
    Handles global constants, filesystem pathing, and environmental configurations.
    All resource thresholds and intervals are loaded with strict dimensional units
    and default values.
"""

import os
from pathlib import Path

# Base workspace directory for the scheduler configuration and state.
# Default to ~/.gpusched
SCHEDULER_DIR = Path(os.environ.get("GPUSCHED_DIR", Path.home() / ".gpusched"))
SCHEDULER_DIR.mkdir(parents=True, exist_ok=True)

# Database path for job metadata tracking.
DB_PATH = SCHEDULER_DIR / "gpusched.db"

# Logs directory to store standard outputs and errors of dispatched processes.
LOGS_DIR = SCHEDULER_DIR / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Core scheduler daemon system log file.
DAEMON_LOG_PATH = SCHEDULER_DIR / "gpusched.log"

# PID file to track active scheduler daemon instance.
PID_FILE_PATH = SCHEDULER_DIR / "gpusched.pid"

# Core scheduling execution control interval.
# units: seconds
POLLING_INTERVAL = float(os.environ.get("GPUSCHED_POLLING_INTERVAL", 5.0))

# Resource Availability Threshold limits.
# units: ratio (0.0 to 1.0)
MEM_THRESHOLD = float(os.environ.get("GPUSCHED_MEM_THRESHOLD", 0.20))
UTIL_THRESHOLD = float(os.environ.get("GPUSCHED_UTIL_THRESHOLD", 0.15))

# Fallback simulation/mock flag for non-NVIDIA execution hosts.
MOCK_MODE = os.environ.get("GPUSCHED_MOCK", "false").lower() in ("true", "1", "yes")
