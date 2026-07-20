"""
Auto-generates PromptTemplate sections from dsl/_registry.py's CommandSpec
registry.

Design principle — Single Source of Truth
-----------------------------------------
The LLM knows about a DSL command if and only if it exists in
dsl/api.py with a @dsl_command decorator, which registers a CommandSpec
(category, example, signature, docstring, ...) in dsl/_registry.py — the
same registry dsl/__init__.ALLOWED_FUNCTIONS, dsl/parser.py, and
dsl/validator.py read (REORGANISATION_PLAN.md Phase 3).

Adding a new DSL function with @dsl_command automatically updates the
System Prompt — no edits to this file or prompts.py are needed.

Docstrings in dsl/api.py are treated as LLM specifications.  Write them
carefully: they are the primary source of truth for what the model learns
about each command's constraints and correct usage.
"""
from __future__ import annotations

import inspect
import re
import textwrap
from collections import defaultdict

from ..dsl import ALLOWED_FUNCTIONS, DSL_VERSION
from ..dsl._registry import CommandSpec, get_registry
from .prompts import (
    EXPLAIN_HEADER,
    GENERATE_FOOTER,
    GENERATE_HEADER_TEMPLATE,
    GRAMMAR,
    SELFFIX_FOOTER,
    SELFFIX_HEADER,
    PromptTemplate,
)


# ── Formatting helpers ────────────────────────────────────────────────────────


def _annotation_str(ann: object) -> str:
    if ann is inspect.Parameter.empty:
        return ""
    name = getattr(ann, "__name__", None)
    if name:
        return name
    s = str(ann)
    s = re.sub(r"\btyping\.", "", s)
    s = s.replace("NoneType", "None")
    return s


def _format_function_spec(spec: CommandSpec) -> str:
    """Format a single DSL command as a specification block."""
    param_lines: list[str] = []
    for pname, p in spec.signature.parameters.items():
        ann_str = (
            ""
            if p.annotation is inspect.Parameter.empty
            else f": {_annotation_str(p.annotation)}"
        )
        default_str = (
            ""
            if p.default is inspect.Parameter.empty
            else f" = {p.default!r}"
        )
        # Mark keyword-only params explicitly (after * in signature)
        param_lines.append(f"    {pname}{ann_str}{default_str}")

    doc = textwrap.dedent(spec.doc).strip()
    # Indent doc as a comment block
    doc_block = "\n".join(
        f"  # {line}" if line.strip() else "  #"
        for line in doc.splitlines()
    )

    if param_lines:
        sig_block = f"{spec.name}(\n{chr(10).join(param_lines)}\n)"
    else:
        sig_block = f"{spec.name}()"

    return f"{sig_block}\n{doc_block}" if doc_block.strip() else sig_block


# ── Section builders ──────────────────────────────────────────────────────────


def _build_commands_section() -> str:
    """Group all registered DSL commands by category and format them."""
    registry = get_registry()
    by_category: dict[str, list[str]] = defaultdict(list)

    for name, spec in registry.items():
        by_category[spec.category].append(name)

    sections: list[str] = ["=== Available DSL Commands ==="]
    for category in sorted(by_category):
        sections.append(f"\n--- {category} ---")
        for name in sorted(by_category[category]):
            sections.append(_format_function_spec(registry[name]))

    return "\n".join(sections)


def _build_examples_section() -> str:
    """Collect @dsl_command(example=...) strings across all registered functions."""
    registry = get_registry()
    examples: list[str] = []
    seen: set[str] = set()

    for name in sorted(ALLOWED_FUNCTIONS):
        meta = registry.get(name)
        if meta and meta.example.strip():
            ex = meta.example.strip()
            if ex not in seen:
                examples.append(ex)
                seen.add(ex)

    if not examples:
        return ""

    body = "\n\n".join(f"```python\n{ex}\n```" for ex in examples)
    return f"### Examples\n\n{body}"


# ── Public builders ───────────────────────────────────────────────────────────


def build_generate_prompt() -> PromptTemplate:
    """Build the System Prompt used for DSL generation requests."""
    return PromptTemplate(
        header=GENERATE_HEADER_TEMPLATE.format(version=DSL_VERSION),
        grammar=GRAMMAR,
        commands=_build_commands_section(),
        examples=_build_examples_section(),
        footer=GENERATE_FOOTER,
    )


def build_selffix_prompt(dsl_text: str, errors: list[str]) -> PromptTemplate:
    """Build the System Prompt used when asking the LLM to fix a failed DSL."""
    available = "\n".join(f"  - {fn}" for fn in sorted(ALLOWED_FUNCTIONS))
    lines = dsl_text.splitlines()

    annotated: list[str] = []
    for err in errors:
        m = re.match(r"[Ll]ine (\d+)[:\s]+(.+)", err)
        if m:
            lineno = int(m.group(1))
            msg = m.group(2).strip()
            code = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
            annotated.append(f"  Line {lineno}: {code!r}\n  Error: {msg}")
        else:
            annotated.append(f"  Error: {err}")

    error_block = "\n".join(annotated) if annotated else "  (no details)"
    body = (
        f"DSL with errors:\n```python\n{dsl_text}\n```\n\n"
        f"Validation errors:\n{error_block}\n\n"
        f"Available functions (only these are allowed):\n{available}"
    )
    return PromptTemplate(
        header=SELFFIX_HEADER,
        grammar=GRAMMAR,
        commands=_build_commands_section(),
        examples="",
        footer=f"{body}\n\n{SELFFIX_FOOTER}",
    )


def build_explain_prompt() -> PromptTemplate:
    """Build the System Prompt used for the Explain mode."""
    return PromptTemplate(
        header=EXPLAIN_HEADER,
        grammar="",
        commands="",
        examples="",
        footer="",
    )
