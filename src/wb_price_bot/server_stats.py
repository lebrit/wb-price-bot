from __future__ import annotations

import asyncio
import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ServerStats:
    cpu_percent: float | None
    cpu_count: int
    load_1: float | None
    load_5: float | None
    load_15: float | None
    memory_total: int
    memory_available: int
    swap_total: int
    swap_free: int
    disk_total: int
    disk_free: int
    uptime_seconds: int | None
    process_rss: int | None


async def collect_server_stats(data_path: Path) -> ServerStats:
    cpu_start = _read_cpu_times()
    if cpu_start is not None:
        await asyncio.sleep(0.25)
    cpu_end = _read_cpu_times()
    memory = _read_meminfo()
    disk = shutil.disk_usage(data_path)
    load = _read_load()
    return ServerStats(
        cpu_percent=_cpu_percent(cpu_start, cpu_end),
        cpu_count=os.cpu_count() or 1,
        load_1=load[0],
        load_5=load[1],
        load_15=load[2],
        memory_total=memory.get("MemTotal", 0),
        memory_available=memory.get("MemAvailable", memory.get("MemFree", 0)),
        swap_total=memory.get("SwapTotal", 0),
        swap_free=memory.get("SwapFree", 0),
        disk_total=disk.total,
        disk_free=disk.free,
        uptime_seconds=_read_uptime(),
        process_rss=_read_process_rss(),
    )


def format_bytes(value: int | None) -> str:
    if value is None:
        return "—"
    amount = float(max(0, value))
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
    unit = units[0]
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            break
        amount /= 1024
    return f"{amount:.1f} {unit}" if unit != "Б" else f"{int(amount)} Б"


def format_duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    days, remainder = divmod(max(0, seconds), 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes = remainder // 60
    if days:
        return f"{days} д. {hours} ч. {minutes} мин."
    if hours:
        return f"{hours} ч. {minutes} мин."
    return f"{minutes} мин."


def _read_cpu_times() -> tuple[int, int] | None:
    try:
        first = Path("/proc/stat").read_text(encoding="ascii").splitlines()[0].split()
        values = [int(value) for value in first[1:9]]
    except (OSError, ValueError, IndexError):
        return None
    total = sum(values)
    idle = values[3] + values[4]
    return total, idle


def _cpu_percent(start: tuple[int, int] | None, end: tuple[int, int] | None) -> float | None:
    if start is None or end is None:
        return None
    total_delta = end[0] - start[0]
    idle_delta = end[1] - start[1]
    if total_delta <= 0:
        return None
    return max(0.0, min(100.0, (total_delta - idle_delta) * 100 / total_delta))


def _read_meminfo() -> dict[str, int]:
    try:
        lines = Path("/proc/meminfo").read_text(encoding="ascii").splitlines()
    except OSError:
        return {}
    result: dict[str, int] = {}
    for line in lines:
        name, separator, raw = line.partition(":")
        if not separator:
            continue
        parts = raw.split()
        if not parts or not parts[0].isdigit():
            continue
        multiplier = 1024 if len(parts) > 1 and parts[1].lower() == "kb" else 1
        result[name] = int(parts[0]) * multiplier
    return result


def _read_load() -> tuple[float | None, float | None, float | None]:
    try:
        parts = Path("/proc/loadavg").read_text(encoding="ascii").split()
        return float(parts[0]), float(parts[1]), float(parts[2])
    except (OSError, ValueError, IndexError):
        return None, None, None


def _read_uptime() -> int | None:
    try:
        raw = Path("/proc/uptime").read_text(encoding="ascii").split()[0]
        return int(float(raw))
    except (OSError, ValueError, IndexError):
        return None


def _read_process_rss() -> int | None:
    try:
        lines = Path("/proc/self/status").read_text(encoding="ascii").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("VmRSS:"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) * 1024
    return None
