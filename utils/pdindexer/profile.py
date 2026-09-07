"""PDIndexer profile model and pyFAI -> PDIndexer conversion.

See utils/pdindexer/IMPLEMENTATION_DETAILS.md for the wire format this
feeds (MemoryPack-serialised Crystallography.DiffractionProfile2[], sent
via utils/pdindexer/csharp/PdiSender.exe).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# PDIndexer (FormMain.cs AddProfileToCheckedListBox) branches into "LPO"
# (azimuthal-division) mode when a profile name ends with "whole" — never
# produce that here. This check is shared by both transports: it lives
# inside AddProfileToCheckedListBox itself, which both the clipboard
# receive handler and the file-open/watcher path call into.
#
# The .pdi-file transport has a second, transport-specific quirk (a blind
# case-sensitive find/replace XYFile.ReadPdi2File runs across the raw file
# text before parsing) — that one is handled in pdi_file.py, at the point
# the XML is written, not here. An earlier version of this function tried
# to handle it here too via a case-insensitive substring *deletion*, which
# is both the wrong transform (upstream does a lossless "pt"->"Pt" swap,
# not a delete) and wrong for this shared function (the deletion doesn't
# apply to the clipboard transport at all, but was silently mangling
# ordinary words like "September" -> "Seember" in profile names sent that
# way too). See code review 2026-09-06.
_BAD_NAME_SUFFIX = "whole"


def sanitise_pdi_string(value: str) -> str:
    """Avoid the one name shape PDIndexer's AddProfileToCheckedListBox
    treats specially regardless of transport: a trailing "whole"."""
    out = value
    while out.rstrip().endswith(_BAD_NAME_SUFFIX):
        out = out.rstrip()[: -len(_BAD_NAME_SUFFIX)].rstrip()
    return out or "profile"


@dataclass(frozen=True)
class PdiProfile:
    """One profile to hand to PDIndexer.

    x/y are the 2theta [deg] / intensity arrays exactly as pyFAI's
    integrate1d(..., unit="2th_deg") returns them. wavelength_nm is in
    **nanometres** (not Angstrom) — see IMPLEMENTATION_DETAILS.md §unit
    mapping for why this matters even though Angstrom values under 1 nm
    happen to "work" for BL-18C's own wavelength.
    """

    name: str
    x: np.ndarray
    y: np.ndarray
    err: np.ndarray | None = None
    wavelength_nm: float = 0.0
    comment: str = ""
    # Identity defaults on purpose: DiffractionProfile2.convertSrcToDest
    # divides intensity by ExposureTime when IsCPS is set (Profile.cs
    # L1158), and IPAnalyzer's own clipboard-send path never overrides
    # either field — it always relies on these exact defaults. Passing the
    # real exposure time here (with is_cps still True) would silently
    # rescale intensity in a way IPAnalyzer itself never does, and would
    # disagree with the .pdi-file transport (whose legacy XML schema has no
    # ExposureTime/IsCPS elements at all, so XmlSerializer also leaves them
    # at these same defaults) — see code review 2026-09-06.
    exposure_time: float = 1.0
    is_cps: bool = True

    def __post_init__(self) -> None:
        x = np.asarray(self.x, dtype=np.float64)
        y = np.asarray(self.y, dtype=np.float64)
        if x.ndim != 1:
            raise ValueError(f"x must be 1-D, got shape {x.shape}")
        if x.shape != y.shape:
            raise ValueError(f"x/y length mismatch: {x.shape} vs {y.shape}")
        if x.size < 1:
            raise ValueError("profile must have at least one point")
        if not (np.isfinite(x).all() and np.isfinite(y).all()):
            raise ValueError("x/y must not contain NaN/Inf")
        err = None
        if self.err is not None:
            err = np.asarray(self.err, dtype=np.float64)
            if err.ndim != 1:
                raise ValueError(f"err must be 1-D, got shape {err.shape}")
            if err.shape != x.shape:
                raise ValueError(f"err length mismatch: {err.shape} vs {x.shape}")
            if not np.isfinite(err).all():
                raise ValueError("err must not contain NaN/Inf")
        if not (np.isfinite(self.wavelength_nm) and self.wavelength_nm > 0):
            raise ValueError(f"wavelength_nm must be finite and positive, got {self.wavelength_nm!r}")
        object.__setattr__(self, "x", x)
        object.__setattr__(self, "y", y)
        object.__setattr__(self, "err", err)
        object.__setattr__(self, "name", sanitise_pdi_string(self.name))
        # PDIndexer's LPO-mode check (AddProfileToCheckedListBox) only ever
        # looks at Name.EndsWith("whole") — Comment is untouched by it, so
        # sanitising Comment here too would just truncate a legitimate
        # comment ending in "whole" for no reason. See code review
        # 2026-09-06.
        object.__setattr__(self, "comment", self.comment or "")

    @classmethod
    def from_pyfai(
        cls,
        result,
        *,
        wavelength_m: float,
        name: str,
        comment: str = "",
        exposure_time: float = 1.0,
        is_cps: bool = True,
    ) -> "PdiProfile":
        """Build from a pyFAI integrate1d(..., unit="2th_deg") result.

        err comes from result.sigma when the integrator was called with an
        error_model (e.g. "poisson"); otherwise pyFAI's sigma is None and we
        leave PDIndexer's Err list empty (its own row-count check treats
        that as "no error bars", which is the correct reading here).
        """
        sigma = getattr(result, "sigma", None)
        return cls(
            name=name,
            x=np.asarray(result.radial, dtype=np.float64),
            y=np.asarray(result.intensity, dtype=np.float64),
            err=None if sigma is None else np.asarray(sigma, dtype=np.float64),
            wavelength_nm=wavelength_m * 1e9,
            comment=comment,
            exposure_time=exposure_time,
            is_cps=is_cps,
        )

    def to_json_dict(self) -> dict:
        """Serialise for PdiSender.exe's stdin JSON contract (see
        IMPLEMENTATION_DETAILS.md, Program.cs I/O section)."""
        import base64

        def _b64(arr: np.ndarray) -> str:
            return base64.b64encode(arr.astype("<f8").tobytes()).decode("ascii")

        return {
            "name": self.name,
            "comment": self.comment,
            "axis_mode": "Angle",
            "two_theta_unit": "Degree",
            "wavelength_nm": self.wavelength_nm,
            "x_b64": _b64(self.x),
            "y_b64": _b64(self.y),
            "err_b64": _b64(self.err) if self.err is not None else None,
            "exposure_time": self.exposure_time,
            "is_cps": self.is_cps,
        }
