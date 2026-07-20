"""
DSL package for Experimental Scheduler.

ALLOWED_FUNCTIONS is the single source of truth for which function names are
valid in the DSL. It is derived from dsl/_registry.py's CommandSpec
registry, which dsl/api.py populates via @dsl_command as each command is
defined (REORGANISATION_PLAN.md Phase 3) — importing dsl/api.py here is what
triggers that registration before ALLOWED_FUNCTIONS is computed. There is
nothing to keep in sync by hand: adding/removing a @dsl_command in api.py is
the only step required.

DSL_VERSION is embedded in the LLM System Prompt so the model knows which
version of the DSL it is targeting.  Bump it whenever a breaking change is
made to the DSL syntax or available functions.
"""

from . import api as _api  # noqa: F401 - populates dsl/_registry.py's CommandSpec registry
from ._registry import get_registry as _get_registry

DSL_VERSION: str = "2.0.0"

ALLOWED_FUNCTIONS: frozenset[str] = frozenset(_get_registry().keys())
