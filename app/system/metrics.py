"""
System & Server Resource Diagnostics Module.
Monitors CPU, RAM, Disk, Uptime, Redis, and Database health for VM server control.
"""

import time
import os
import psutil
from typing import Dict, Any
from app.config import settings
from app.storage.cache import cache
from app.storage.db import db

_PROCESS_START_TIME = time.time()


def get_system_metrics() -> Dict[str, Any]:
    """Retrieve detailed server hardware and system usage metrics."""
    # RAM Metrics
    vm = psutil.virtual_memory()
    ram_total_gb = round(vm.total / (1024 ** 3), 2)
    ram_used_gb = round(vm.used / (1024 ** 3), 2)
    ram_free_gb = round(vm.available / (1024 ** 3), 2)
    ram_pct = vm.percent

    # CPU Metrics
    cpu_pct = psutil.cpu_percent(interval=None)
    cpu_count = psutil.cpu_count(logical=True)

    # Disk Metrics
    try:
        disk = psutil.disk_usage("/")
        disk_total_gb = round(disk.total / (1024 ** 3), 2)
        disk_used_gb = round(disk.used / (1024 ** 3), 2)
        disk_pct = disk.percent
    except Exception:
        disk_total_gb, disk_used_gb, disk_pct = 0.0, 0.0, 0.0

    # Uptime
    uptime_seconds = int(time.time() - _PROCESS_START_TIME)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"

    # Database & Storage info
    db_size_kb = 0
    if settings.absolute_db_path.exists():
        db_size_kb = round(settings.absolute_db_path.stat().st_size / 1024, 2)

    cache_status = cache.get_status()
    account_state = db.get_account_state()

    return {
        "status": "healthy",
        "uptime": uptime_str,
        "uptime_seconds": uptime_seconds,
        "cpu": {
            "percent": cpu_pct,
            "cores": cpu_count,
        },
        "ram": {
            "total_gb": ram_total_gb,
            "used_gb": ram_used_gb,
            "free_gb": ram_free_gb,
            "percent": ram_pct,
            "status": "normal" if ram_pct < 85 else "high",
        },
        "disk": {
            "total_gb": disk_total_gb,
            "used_gb": disk_used_gb,
            "percent": disk_pct,
        },
        "cache": cache_status,
        "database": {
            "path": str(settings.absolute_db_path),
            "size_kb": db_size_kb,
        },
        "account": {
            "initial_capital_inr": account_state.get("initial_capital", 10000.0),
            "available_cash_inr": account_state.get("available_cash", 10000.0),
            "total_realized_pnl_inr": account_state.get("total_realized_pnl", 0.0),
            "auto_execute": bool(account_state.get("auto_execute", 0)),
        },
    }

