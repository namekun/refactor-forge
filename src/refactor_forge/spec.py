from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from .sdk import Transformation
from .transforms import CommandTransformation, RegexTransformation


@dataclass
class TransformationSpec:
    name: str
    description: str
    steps: List[Transformation]
    verify: List[List[str]] = field(default_factory=list)


def load_spec(path: Path) -> TransformationSpec:
    raw: Dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("schema_version") != 1:
        raise ValueError("Only schema_version=1 is supported")
    steps: List[Transformation] = []
    for index, item in enumerate(raw.get("steps", []), start=1):
        step_type = item.get("type")
        name = item.get("name", f"step-{index}")
        if step_type == "regex":
            steps.append(RegexTransformation(name, item.get("includes", ["**/*"]), item["pattern"], item["replacement"]))
        elif step_type == "command":
            command = item.get("command")
            if not isinstance(command, list) or not all(isinstance(value, str) for value in command):
                raise ValueError(f"{name}: command must be a string array")
            steps.append(CommandTransformation(name, command))
        else:
            raise ValueError(f"Unsupported step type: {step_type!r}")
    verify = raw.get("verify", [])
    if not all(isinstance(cmd, list) and all(isinstance(value, str) for value in cmd) for cmd in verify):
        raise ValueError("verify must be an array of command arrays")
    return TransformationSpec(raw["name"], raw.get("description", ""), steps, verify)
