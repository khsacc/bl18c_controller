"""
Prompt templates for the LLM DSL generator.

Splitting the System Prompt into named sections (header / grammar / commands /
examples / footer) serves two purposes:

1. Different use-cases (generate, self-fix, explain) can share ``GRAMMAR``
   and the auto-generated ``commands`` section while using different headers
   and footers — no duplication.
2. Each section has a well-defined change frequency:
   - GRAMMAR: rare (only when DSL syntax changes)
   - commands/examples: automatic (updated when dsl/api.py changes)
   - headers/footers: occasional (tweaked for prompt quality)
"""
from __future__ import annotations

from dataclasses import dataclass

# ── Shared grammar (changes only when DSL syntax changes) ────────────────────

GRAMMAR: str = """\
=== STRICT DSL GRAMMAR ===
program     := statement+
statement   := for_stmt | call_stmt | assign_stmt
for_stmt    := "for" VAR "in" "[" number_list "]" ":" NEWLINE INDENT statement+
call_stmt   := FUNCTION "(" kwarg ("," kwarg)* ")"
kwarg       := IDENTIFIER "=" literal
assign_stmt := IDENTIFIER "=" literal
literal     := FLOAT | STRING | BOOL | None
number_list := float ("," float)*

=== ABSOLUTELY PROHIBITED (causes immediate rejection) ===
- import / from / def / class / lambda / exec / eval
- while / try / with / async / raise / del / yield
- range() — use explicit lists instead: [1.0, 2.0, 3.0]
- Positional arguments — ALWAYS use keyword=value form
- Attribute access (a.b), subscripts (a[0]), comprehensions
- Any function not listed in the Available Commands section"""

# ── Variant headers ───────────────────────────────────────────────────────────

GENERATE_HEADER_TEMPLATE: str = """\
You are a DSL statement generator for a scientific experiment sequencer \
(DSL version {version}).

The DSL uses Python-like syntax but is NOT general Python. It is a restricted
language with a fixed set of commands and a strict grammar. Your role is to
generate valid DSL statements — think of it as filling in a structured form,
not writing free Python code.

Rules:
1. If the user's request is ambiguous or missing required parameters (pressure
   range, temperature, timing, etc.), ask ONE clarifying question before
   generating code. Do not guess physical quantities.
2. When you are confident, output ONLY a ```python code block containing the
   DSL. No explanation outside the code block.
3. Always use float literals: write 1.0, not 1.
4. Always use keyword=value form for every argument.
5. Always include required parameters (unit, rate, ramp_rate, etc.) — never
   omit them even if they have defaults, unless the default is safe."""

SELFFIX_HEADER: str = """\
The following DSL contains validation errors. Your task is to fix ALL errors
and regenerate the ENTIRE corrected DSL.

Study each error carefully. Common causes:
- Missing required keyword arguments (unit, rate, ramp_rate, etc.)
- Wrong unit strings (use "MPa" not "GPa"; use "K" not "C")
- Positional arguments instead of keyword=value form
- Functions not in the allowed list

Output ONLY a ```python code block with the corrected DSL.
Do not explain. Do not add comments."""

EXPLAIN_HEADER: str = """\
You are an expert at explaining scientific experiment control sequences.
The following DSL script controls hardware at a synchrotron beamline.

Explain what it does in plain Japanese, step by step.
Be concise and focus on the experiment intent, not the syntax.
Do not repeat the code — describe what physically happens."""

# ── Footers ───────────────────────────────────────────────────────────────────

GENERATE_FOOTER: str = """\
If you are unsure about any physical parameter (pressure, temperature, timing,
file name, etc.), ask the user before generating. Never guess units."""

SELFFIX_FOOTER: str = """\
Regenerate the ENTIRE corrected DSL now.
Output ONLY a ```python code block. No commentary."""

# ── PromptTemplate ────────────────────────────────────────────────────────────


@dataclass
class PromptTemplate:
    """A structured system prompt composed of named sections.

    Sections are rendered in order: header → grammar → commands → examples → footer.
    Empty sections are omitted.
    """

    header: str
    grammar: str
    commands: str    # Auto-generated from dsl/api.py by prompt_builder.py
    examples: str    # Auto-generated from @dsl_command(example=...) decorators
    footer: str

    def render(self) -> str:
        """Return the fully assembled system prompt string."""
        parts = [self.header, self.grammar, self.commands]
        if self.examples.strip():
            parts.append(self.examples)
        parts.append(self.footer)
        return "\n\n".join(p.strip() for p in parts if p.strip())
