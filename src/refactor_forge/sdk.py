from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List


@dataclass(frozen=True)
class TransformationContext:
    root: Path
    options: Dict[str, Any] = field(default_factory=dict)
    dry_run: bool = True
    allow_commands: bool = False


@dataclass
class TransformationResult:
    name: str
    changed_files: List[str] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)


class Transformation(ABC):
    """Extension point for deterministic code transformations."""

    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def apply(self, context: TransformationContext) -> TransformationResult:
        raise NotImplementedError
