"""
LLM conversation session for DSL generation.

Responsibilities
----------------
- Maintain the multi-turn message history sent to Ollama.
- Extract DSL code blocks from model responses (multi-stage fallback).
- Run DslCompiler (normalize → AST safety validation → SequenceBuilder.build())
  on extracted DSL — compile diagnostics only, never a device preflight (see
  REORGANISATION_PLAN.md §7 Phase 1 item 6).
- Compress history after a successful DSL round to prevent context overflow.
- Provide message lists for the self-fix loop (handled externally to keep
  the session stateless about in-flight network calls).
"""
from __future__ import annotations

import ast
import re
from typing import TYPE_CHECKING

from ..dsl.compiler import DslCompiler
from .prompt_builder import build_generate_prompt, build_selffix_prompt

if TYPE_CHECKING:
    from ..sequence import Sequence

# Keep at most this many messages (excluding system) after compression.
_MAX_HISTORY: int = 10


class LlmSession:
    """Manages a multi-turn DSL generation conversation.

    Typical usage::

        session = LlmSession()

        # User sends a message → build the full message list for Ollama
        messages = session.build_messages("圧力を1〜5MPaまで昇圧しながらXRDを撮りたい")

        # ... spin up OllamaChatWorker(messages=messages) ...
        # ... on worker.finished(response): ...

        session.record_assistant_response(response)
        dsl, errors = session.try_extract_and_validate(response)
        if dsl:
            # Success — session has compressed its history internally
            sequence = session.last_sequence
    """

    def __init__(self) -> None:
        self._system_prompt: str = build_generate_prompt().render()
        self._messages: list[dict[str, str]] = []
        self._last_dsl: str | None = None       # normalised, validated DSL
        self._last_sequence: "Sequence | None" = None
        self._last_errors: list[str] = []

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def last_dsl(self) -> str | None:
        """The most recently validated DSL text (normalised), or None."""
        return self._last_dsl

    @property
    def last_sequence(self) -> "Sequence | None":
        """The Sequence compiled from the most recently validated DSL, or
        None. Callers (ui/llm_panel.py::_on_apply()) should use this instead
        of re-parsing last_dsl, so Apply always uses the exact Sequence this
        session already validated — see REORGANISATION_PLAN.md §7 Phase 1."""
        return self._last_sequence

    @property
    def last_errors(self) -> list[str]:
        """Validation errors from the most recent DSL extraction attempt."""
        return list(self._last_errors)

    # ------------------------------------------------------------------
    # Message construction
    # ------------------------------------------------------------------

    def build_messages(self, user_text: str) -> list[dict[str, str]]:
        """Append *user_text* to history and return the full message list.

        The returned list is ready to pass to OllamaChatWorker.
        """
        self._messages.append({"role": "user", "content": user_text})
        return [{"role": "system", "content": self._system_prompt}] + self._messages

    def record_assistant_response(self, response: str) -> None:
        """Store an assistant response in history and trim if needed."""
        self._messages.append({"role": "assistant", "content": response})
        self._trim_history()

    def build_selffix_messages(
        self, dsl_text: str, errors: list[str]
    ) -> list[dict[str, str]]:
        """Build a standalone self-fix request (separate system prompt).

        The returned list is ready to pass to OllamaChatWorker.
        Self-fix messages are NOT appended to the main conversation history.
        """
        system = build_selffix_prompt(dsl_text, errors).render()
        return [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    "Fix all errors and output the corrected DSL. "
                    "Output ONLY a ```python code block."
                ),
            },
        ]

    # ------------------------------------------------------------------
    # DSL extraction and validation
    # ------------------------------------------------------------------

    def try_extract_and_validate(
        self, response: str
    ) -> tuple[str | None, list[str]]:
        """Extract a DSL code block from *response* and validate it.

        Returns
        -------
        (dsl_text, errors)
            On success: ``(normalised_dsl, [])``.
            On validation failure: ``(None, [error, ...])``.
            If no code block is found: ``(None, [])`` — treat as conversation.
        """
        raw = self._extract_dsl(response)
        if raw is None:
            return None, []

        dsl, sequence, errors = self._compile(raw)
        if not errors:
            self._last_dsl = dsl
            self._last_sequence = sequence
            self._last_errors = []
            self._compress_history()
            return dsl, []

        self._last_errors = errors
        return None, errors

    def apply_selffix_response(
        self, response: str
    ) -> tuple[str | None, list[str]]:
        """Process a self-fix response without touching the main history."""
        raw = self._extract_dsl(response)
        if raw is None:
            return None, ["Self-fix response contained no Python code block."]

        dsl, sequence, errors = self._compile(raw)
        if not errors:
            self._last_dsl = dsl
            self._last_sequence = sequence
            self._last_errors = []
            return dsl, []

        self._last_errors = errors
        return None, errors

    # ------------------------------------------------------------------
    # Session control
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear all history and last DSL (start a new conversation)."""
        self._messages.clear()
        self._last_dsl = None
        self._last_sequence = None
        self._last_errors = []
        # Rebuild system prompt in case api.py changed since last use.
        self._system_prompt = build_generate_prompt().render()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_dsl(response: str) -> str | None:
        """Multi-stage extraction with AST parse verification."""
        patterns = [
            r"```python\n(.*?)```",
            r"```py\n(.*?)```",
            r"```\n(.*?)```",
        ]
        for pattern in patterns:
            m = re.search(pattern, response, re.DOTALL)
            if m:
                candidate = m.group(1).strip()
                try:
                    ast.parse(candidate)
                    return candidate
                except SyntaxError:
                    continue

        # Last resort: find the largest contiguous block that parses.
        for block in re.findall(r"(?m)^((?:[ \t]*\S.*\n?)+)", response):
            candidate = block.strip()
            if not candidate:
                continue
            try:
                ast.parse(candidate)
                # Skip if it looks like a prose sentence rather than code.
                if any(c in candidate for c in ("(", "for ", "=")):
                    return candidate
            except SyntaxError:
                continue

        return None

    @staticmethod
    def _compile(dsl_text: str) -> tuple[str, "Sequence | None", list[str]]:
        """Run DslCompiler and adapt its Diagnostics to the (text, sequence,
        errors) shape this session's callers expect; return (normalised_text,
        sequence_or_None, error_messages). Compile diagnostics only — never
        calls PreValidator or touches a device (§7 Phase 1 item 6)."""
        result = DslCompiler().compile(dsl_text)
        normalised = result.normalised_source if result.normalised_source is not None else dsl_text
        if not result.ok:
            return normalised, None, [d.message for d in result.diagnostics]
        return normalised, result.sequence, []

    def _trim_history(self) -> None:
        if len(self._messages) > _MAX_HISTORY:
            self._messages = self._messages[-_MAX_HISTORY:]

    def _compress_history(self) -> None:
        """After a successful DSL round, replace history with a short summary."""
        if not self._last_dsl:
            return
        summary = self._summarise_dsl(self._last_dsl)
        self._messages = [
            {
                "role": "assistant",
                "content": (
                    "[Previous context compressed]\n"
                    + summary
                    + "\n\nA valid DSL was generated and applied to the timeline."
                ),
            }
        ]

    @staticmethod
    def _summarise_dsl(dsl_text: str) -> str:
        """Rule-based one-liner summary of the DSL (no LLM call needed)."""
        lines = dsl_text.splitlines()
        calls = [
            line.strip().split("(")[0]
            for line in lines
            if "(" in line and not line.strip().startswith("#")
        ]
        unique = list(dict.fromkeys(calls))[:12]
        return f"DSL functions used: {', '.join(unique)}"
