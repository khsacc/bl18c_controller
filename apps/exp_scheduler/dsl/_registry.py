"""
Registry for DSL command metadata — the CommandSpec single source of truth.

REORGANISATION_PLAN.md Phase 3: every DSL command's category, LLM example,
Python signature, docstring, per-argument unit/bound/loop-variable rules, and
Action factory are declared once, at the @dsl_command decoration site in
dsl/api.py, and captured here as one CommandSpec. dsl/__init__.py
(ALLOWED_FUNCTIONS), dsl/parser.py (call binding + Action construction), and
dsl/validator.py (unit/bound/required-argument checks) all derive their data
from this registry instead of keeping independent hand-written tables.

Adding a new function to dsl/api.py with @dsl_command(factory=..., ...) is
the only step required to make every consumer aware of it — no edits to
this file needed.
"""
from __future__ import annotations

import functools
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable)


@dataclass(frozen=True)
class ArgumentRule:
    """Compile-time constraint on one keyword argument of one DSL command.

    All fields are optional and independent — a single argument can carry a
    unit/enum whitelist, a numeric lower bound, and loop-variable
    eligibility simultaneously (e.g. set_pressure's `pressure`: bounded
    *and* loop-var-eligible).
    """

    valid_values: frozenset[str] | None = None
    lower_bound: float | None = None
    lower_bound_inclusive: bool = True
    loop_var_allowed: bool = False


@dataclass
class CommandSpec:
    name: str
    category: str
    example: str
    doc: str
    signature: inspect.Signature
    #: Bound + defaulted keyword-argument dict -> Action. The single Action
    #: construction implementation for this command — see dsl/_factories.py.
    factory: Callable[[dict[str, Any]], Any]
    argument_rules: dict[str, ArgumentRule] = field(default_factory=dict)

    @property
    def required_kwargs(self) -> frozenset[str]:
        """Parameter names with no default in `signature` — positional
        arguments are rejected elsewhere (ASTValidator/SequenceBuilder), so
        every DSL call is keyword-only in practice and "has no default" is
        exactly "is required"."""
        return frozenset(
            p.name for p in self.signature.parameters.values()
            if p.default is inspect.Parameter.empty
        )


_registry: dict[str, CommandSpec] = {}


def dsl_command(
    category: str,
    example: str = "",
    *,
    factory: Callable[[dict[str, Any]], Any],
    argument_rules: dict[str, ArgumentRule] | None = None,
) -> Callable[[F], F]:
    """Decorator that registers a DSL command's full CommandSpec, then
    replaces the decorated function with a stub that always raises
    NotImplementedError.

    Parameters
    ----------
    category : str
        Logical group shown in the System Prompt
        (e.g. "Temperature", "Pressure", "Stage").
    example : str
        One or more lines of valid DSL illustrating typical usage.
        Injected verbatim into the ``### Examples`` section of the prompt.
    factory : Callable[[dict], Action]
        Turns a fully bound + defaulted keyword-argument dict into the
        Action this command represents — the only place that actually
        builds one; see dsl/_factories.py.
    argument_rules : dict[str, ArgumentRule], optional
        Per-keyword-argument unit/enum whitelist, numeric lower bound, and/or
        loop-variable eligibility, checked by dsl/validator.py and
        dsl/parser.py respectively.

    Every dsl/api.py function decorated with this exists to declare a
    signature/docstring/factory triple for the LLM System Prompt and the
    registry-driven compile pipeline (dsl/parser.py, dsl/validator.py) — none
    of them is ever actually called as a function. Rather than rely on each
    function body remembering to raise NotImplementedError itself (24 places
    that could individually be gotten wrong or forgotten for a new command),
    this decorator captures the real function's signature/docstring and then
    substitutes an always-raising wrapper in its place — REORGANISATION_PLAN.md
    Phase 9. See dsl/api.py's module docstring for why this exec()-based path
    exists at all (test/legacy contract surface, not the production pipeline).
    """

    def decorator(fn: F) -> F:
        # eval_str=True resolves dsl/api.py's `from __future__ import
        # annotations`-stringified annotations (e.g. "bool", "int | None")
        # back into real type objects — required by dsl/parser.py's
        # _annotation_accepts() and llm/prompt_builder.py's rendering.
        # Captured from the real `fn` BEFORE it is replaced below.
        sig = inspect.signature(fn, eval_str=True)
        doc = fn.__doc__ or ""
        _registry[fn.__name__] = CommandSpec(
            name=fn.__name__,
            category=category,
            example=example,
            doc=doc,
            signature=sig,
            factory=factory,
            argument_rules=dict(argument_rules or {}),
        )

        @functools.wraps(fn)
        def _not_implemented(*args, **kwargs):
            raise NotImplementedError(
                f"{fn.__name__}() is a DSL declaration only — it is never "
                "called directly. The production pipeline builds Actions "
                "via dsl/compiler.py -> dsl/_registry.py's CommandSpec.factory."
            )

        return _not_implemented

    return decorator


def get_registry() -> dict[str, CommandSpec]:
    """Return a snapshot of the current registry (name → CommandSpec)."""
    return dict(_registry)


def get_spec(name: str) -> CommandSpec | None:
    """Return the CommandSpec for *name*, or None if it's not registered."""
    return _registry.get(name)
