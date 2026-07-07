from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .actions import Action, action_from_dict

_SCHEMA = "exp_scheduler"
_VERSION = 1


@dataclass
class Sequence:
    """
    Ordered list of Actions with JSON persistence.

    JSON format:
        {
          "version": 1,
          "schema": "exp_scheduler",
          "name": "...",
          "global_xrd":    {...},   # optional — GlobalXrdSettings as dict
          "global_follow": {...},   # optional — GlobalFollowSettings as dict
          "global_limits": {...},   # optional — GlobalLimits as dict
          "actions": [ {...}, ... ]
        }
    """
    actions: list = field(default_factory=list)
    name: str = ""
    version: int = _VERSION
    global_xrd: dict | None = None
    global_follow: dict | None = None
    global_limits: dict | None = None

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        d: dict = {
            "version": self.version,
            "schema": _SCHEMA,
            "name": self.name,
        }
        if self.global_xrd is not None:
            d["global_xrd"] = self.global_xrd
        if self.global_follow is not None:
            d["global_follow"] = self.global_follow
        if self.global_limits is not None:
            d["global_limits"] = self.global_limits
        d["actions"] = [a.to_dict() for a in self.actions]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Sequence":
        if d.get("schema") != _SCHEMA:
            raise ValueError(f"Unexpected schema: {d.get('schema')!r}")
        actions = [action_from_dict(a) for a in d.get("actions", [])]
        return cls(
            actions=actions,
            name=d.get("name", ""),
            version=int(d.get("version", _VERSION)),
            global_xrd=d.get("global_xrd"),
            global_follow=d.get("global_follow"),
            global_limits=d.get("global_limits"),
        )

    # ------------------------------------------------------------------
    # File I/O
    # ------------------------------------------------------------------

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "Sequence":
        path = Path(path)
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(d)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.actions)

    def __iter__(self):
        return iter(self.actions)

    def to_dsl(self) -> str:
        """Convert the entire sequence to DSL text."""
        return "\n".join(a.to_dsl() for a in self.actions)
