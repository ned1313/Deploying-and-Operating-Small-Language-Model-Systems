"""The module 2 teaching script, run as a subprocess against an unreachable endpoint."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "examples" / "basic_agent.py"


def test_script_reports_unreachable_endpoint(tmp_path: Path) -> None:
    env = {"INFERENCE_BASE_URL": "http://127.0.0.1:9/v1", "INFERENCE_MODEL": "x", "INFERENCE_TIMEOUT_SECONDS": "5",
           "INFERENCE_API_KEY_FILE": str(tmp_path / "none"), "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    completed = subprocess.run([sys.executable, str(SCRIPT), "--mode", "chat", "--scenario", "A01"],
                               capture_output=True, text=True, env=env, timeout=120)
    assert completed.returncode == 1
    assert "not a model-quality problem" in completed.stdout
