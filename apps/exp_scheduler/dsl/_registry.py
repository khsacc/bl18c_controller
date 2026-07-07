"""
Registry for DSL command metadata.

The @dsl_command decorator attaches category and example strings to each DSL
function.  prompt_builder.py uses this registry to auto-generate a categorised,
example-rich System Prompt without any manual maintenance.

Adding a new function to dsl/api.py with @dsl_command is the only step required
to make the LLM aware of that function — no edits to the prompt files needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, TypeVar

F = TypeVar("F", bound=Callable)


@dataclass
class DslCommandMeta:
    category: str
    example: str = field(default="")


_registry: dict[str, DslCommandMeta] = {}


def dsl_command(category: str, example: str = "") -> Callable[[F], F]:
    """Decorator that registers DSL command metadata for prompt generation.

    Parameters
    ----------
    category : str
        Logical group shown in the System Prompt
        (e.g. "Temperature", "Pressure", "Stage").
    example : str
        One or more lines of valid DSL illustrating typical usage.
        Injected verbatim into the ``### Examples`` section of the prompt.
        Multi-line strings show common patterns such as function pairs.
    """

    def decorator(fn: F) -> F:
        _registry[fn.__name__] = DslCommandMeta(category=category, example=example)
        return fn

    return decorator


def get_registry() -> dict[str, DslCommandMeta]:
    """Return a snapshot of the current registry (name → DslCommandMeta)."""
    return dict(_registry)
