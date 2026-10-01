"""Statistics, file helpers, and table printing for the module tools."""

from __future__ import annotations

import glob
import math
import re
import statistics
import time
from pathlib import Path
from typing import Any


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * pct / 100.0
    low, high = math.floor(rank), math.ceil(rank)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def summarize_latency(values: list[float]) -> dict[str, float | None]:
    return {
        "mean": statistics.mean(values) if values else None,
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": max(values) if values else None,
    }


def peak_vram_from_log(path: Path | None) -> dict[str, Any] | None:
    """Read the CSV written by m1/tools/sample_vram.sh and report the peak memory.used value."""
    if path is None or not path.is_file():
        return None
    peak_used = peak_util = 0.0
    total = None
    samples = 0
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4:
            continue
        try:
            used, total_value, util = float(parts[1]), float(parts[2]), float(parts[3])
        except ValueError:
            continue
        samples += 1
        peak_used = max(peak_used, used)
        peak_util = max(peak_util, util)
        total = total_value
    if not samples:
        return None
    return {"samples": samples, "peak_memory_used_mib": peak_used, "memory_total_mib": total,
            "peak_gpu_utilization_pct": peak_util, "source": str(path)}


def timestamp_slug() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def expand_globs(paths: list[Path]) -> list[Path]:
    """Expand glob patterns the host shell did not (e.g. when quoted through `podman compose run`)."""
    expanded: list[Path] = []
    for path in paths:
        text = str(path)
        if any(ch in text for ch in "*?["):
            expanded.extend(sorted(Path(p) for p in glob.glob(text)))
        else:
            expanded.append(path)
    return expanded


def safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-") or "model"


def print_table(headers: list[str], rows: list[list[Any]]) -> None:
    def fmt(cell: Any) -> str:
        if cell is None:
            return "-"
        if isinstance(cell, float):
            return f"{cell:.2f}"
        return str(cell)

    text_rows = [[fmt(cell) for cell in row] for row in rows]
    widths = [max(len(headers[i]), *(len(row[i]) for row in text_rows)) if text_rows else len(headers[i])
              for i in range(len(headers))]
    line = "  ".join(header.ljust(widths[i]) for i, header in enumerate(headers))
    print(line)
    print("  ".join("-" * width for width in widths))
    for row in text_rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
