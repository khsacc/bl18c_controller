"""Bridge from bl18c_controller's pyFAI 1D reductions to PDIndexer.

See docs/PLAN_PDINDEXER_BRIDGE.md for the design and
utils/pdindexer/IMPLEMENTATION_DETAILS.md for the wire-format reference.

Public API:
    PdiProfile         — the profile data model (see profile.py)
    sanitise_pdi_string — name/comment sanitiser shared by both transports
    PdiService         — app-wide sender (clipboard + .pdi-file transports)
    Transport, Trigger — enums controlling PdiService behaviour
    write_pdi_file     — low-level .pdi (method C) writer, used by PdiService
"""
from .profile import PdiProfile, sanitise_pdi_string
from .service import PdiService, Transport, Trigger
from .pdi_file import write_pdi_file

__all__ = [
    "PdiProfile",
    "sanitise_pdi_string",
    "PdiService",
    "Transport",
    "Trigger",
    "write_pdi_file",
]
