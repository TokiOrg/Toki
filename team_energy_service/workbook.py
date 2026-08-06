from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
WORKBOOK_BUILDER = PROJECT_ROOT / "tools" / "build_analytics_workbook.mjs"


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Cannot serialize {type(value).__name__} to workbook JSON")


def build_analytics_workbook(payload: dict[str, Any]) -> bytes:
    """Create the Excel workbook through the bundled artifact-tool builder."""
    configured_node = os.getenv("TEAM_ENERGY_NODE_EXECUTABLE", "").strip()
    node_executable = configured_node or shutil.which("node")
    if not node_executable:
        raise RuntimeError(
            "Node.js is required to create the analytics workbook; set "
            "TEAM_ENERGY_NODE_EXECUTABLE to the Node.js executable"
        )
    if not WORKBOOK_BUILDER.is_file():
        raise RuntimeError(f"Workbook builder is missing: {WORKBOOK_BUILDER}")

    with tempfile.TemporaryDirectory(prefix="team-energy-workbook-") as temp_dir:
        temp_path = Path(temp_dir)
        input_path = temp_path / "analytics.json"
        output_path = temp_path / "team_energy_analytics.xlsx"
        input_path.write_text(
            json.dumps(payload, ensure_ascii=False, default=_json_default),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                node_executable,
                str(WORKBOOK_BUILDER),
                str(input_path),
                str(output_path),
            ],
            cwd=PROJECT_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0 or not output_path.is_file():
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise RuntimeError(f"Analytics workbook creation failed: {detail[-1500:]}")
        return output_path.read_bytes()
