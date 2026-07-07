"""
LLM backend for Experimental Scheduler DSL generation.

Submodules
----------
client        Ollama HTTP client (QThread-based, non-streaming).
prompts       PromptTemplate dataclass and shared text components.
prompt_builder Auto-generates prompt sections from dsl/api.py.
session       Multi-turn conversation management and self-fix loop.
"""
