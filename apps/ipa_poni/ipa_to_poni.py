"""
Convert IPAnalyzer (IPA) .prm detector parameter files to pyFAI .poni format.

Coordinate systems
------------------
IPA:
  - Origin at DirectSpot (point where the direct beam hits the detector surface).
  - Z axis: along beam propagation direction (towards detector).
  - X axis: image right (laser scan direction when IP is not tilted).
  - Y axis: image down (right-hand system).
  - Sample position: (0, 0, -CL) in this frame.
  - Tilt: Rodrigues rotation by angle tau around axis (cos(phi), sin(phi), 0) in the XY plane.
  - Foot: foot of the perpendicular from sample to the tilted detector plane (= pyFAI PONI).

pyFAI:
  - Origin at sample.
  - axis1 (row direction, Y in IPA) — positive downward in image.
  - axis2 (column direction, X in IPA) — positive rightward in image.
  - axis3 (beam direction, Z in IPA).
  - PONI = point of normal incidence = Foot of perpendicular from sample to detector plane.
  - poni1 = FootY * pixSizeY  [m]
  - poni2 = FootX * pixSizeX  [m]
  - Distance = CL * cos(tau)  [m]  (perpendicular distance from sample to detector plane)
  - Rotation: R = R3(rot3) · R2(-rot2) · R1(-rot1)
      R1 around axis1 (Y), R2 around axis2 (X), R3 around axis3 (Z=beam).

Rotation angle formulas
-----------------------
IPA detector normal after tilt:
    n = R_IPA @ [0,0,1] = (sin(tau)*sin(phi), -sin(tau)*cos(phi), cos(tau))

pyFAI (with rot3=0):
    R @ [0,0,1] = (-sin(rot1), sin(rot2)*cos(rot1), cos(rot2)*cos(rot1))

Matching components:
    rot1 = -arcsin( sin(tau) * sin(phi) )
    rot2 = arcsin( -sin(tau) * cos(phi) / cos(rot1) )
    rot3 = 0

Unit conventions
----------------
IPA  waveLength  : Angstroms (1 Å = 1e-10 m)
IPA  CameraLength: mm
IPA  PixSizeX/Y  : mm
IPA  tiltPhi/Tau : degrees
IPA  pixKsi      : degrees (pixel skew — not representable in poni format, ignored)
"""

from __future__ import annotations

import json
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class IpaPrmParams:
    camera_length_1: float    # mm
    camera_length_2: float    # mm
    direct_spot_x: float      # pixels
    direct_spot_y: float      # pixels
    foot_x: float             # pixels
    foot_y: float             # pixels
    wavelength: float         # Angstroms
    pix_size_x: float         # mm
    pix_size_y: float         # mm
    pix_ksi: float            # degrees (skew — ignored in poni)
    tilt_phi: float           # degrees
    tilt_tau: float           # degrees


@dataclass
class PoniParams:
    distance: float      # m
    poni1: float         # m
    poni2: float         # m
    rot1: float          # rad
    rot2: float          # rad
    rot3: float          # rad
    pixel_size_1: float  # m  (axis1 = Y = row direction)
    pixel_size_2: float  # m  (axis2 = X = col direction)
    wavelength: float    # m


def parse_ipa_prm(path: str | Path) -> IpaPrmParams:
    """Parse an IPA .prm XML file and return the parameters."""
    tree = ET.parse(str(path))
    root = tree.getroot()

    def get(name: str) -> float:
        el = root.find(name)
        if el is None or el.text is None:
            raise ValueError(f"Parameter '{name}' not found in prm file")
        return float(el.text)

    return IpaPrmParams(
        camera_length_1=get("CameraLength1"),
        camera_length_2=get("CameraLength2"),
        direct_spot_x=get("DirectSpotX"),
        direct_spot_y=get("DirectSpotY"),
        foot_x=get("FootX"),
        foot_y=get("FootY"),
        wavelength=get("waveLength"),
        pix_size_x=get("pixSizeX"),
        pix_size_y=get("pixSizeY"),
        pix_ksi=get("pixKsi"),
        tilt_phi=get("tiltPhi"),
        tilt_tau=get("tiltTau"),
    )


def ipa_to_poni(prm: IpaPrmParams) -> PoniParams:
    """
    Convert IPA detector parameters to pyFAI poni parameters.

    See module docstring for full derivation of the coordinate mapping.
    """
    phi = math.radians(prm.tilt_phi)
    tau = math.radians(prm.tilt_tau)

    # Perpendicular distance from sample to tilted detector plane
    # dist_pyFAI = CL * cos(tau)   (CL is along-beam distance in IPA)
    distance = prm.camera_length_1 * math.cos(tau) * 1e-3  # mm → m

    # PONI on the detector = Foot of perpendicular from sample to detector plane
    # pyFAI measures from the top-left pixel corner, same as IPA FootX/FootY
    poni1 = prm.foot_y * prm.pix_size_y * 1e-3  # row (axis1 = Y)
    poni2 = prm.foot_x * prm.pix_size_x * 1e-3  # col (axis2 = X)

    # Rotation angles
    # IPA normal: n = (sin(tau)*sin(phi),  -sin(tau)*cos(phi),  cos(tau))
    # pyFAI:      R @ (0,0,1) = (-sin(rot1), sin(rot2)*cos(rot1), cos(rot2)*cos(rot1))
    # → rot1 = -arcsin( sin(tau)*sin(phi) )
    # → rot2 = arcsin( -sin(tau)*cos(phi) / cos(rot1) )
    sin_tau_sin_phi = math.sin(tau) * math.sin(phi)
    rot1 = -math.asin(max(-1.0, min(1.0, sin_tau_sin_phi)))

    cos_rot1 = math.cos(rot1)
    if abs(cos_rot1) > 1e-12:
        val = -math.sin(tau) * math.cos(phi) / cos_rot1
        rot2 = math.asin(max(-1.0, min(1.0, val)))
    else:
        rot2 = 0.0  # degenerate: detector nearly perpendicular to beam axis

    rot3 = 0.0  # in-plane rotation doesn't affect Debye–Scherrer ring integration

    return PoniParams(
        distance=distance,
        poni1=poni1,
        poni2=poni2,
        rot1=rot1,
        rot2=rot2,
        rot3=rot3,
        pixel_size_1=prm.pix_size_y * 1e-3,  # mm → m
        pixel_size_2=prm.pix_size_x * 1e-3,
        wavelength=prm.wavelength * 1e-10,   # Å → m
    )


def write_poni(poni: PoniParams, path: str | Path, source_path: str | Path | None = None) -> None:
    """Write a pyFAI poni v2 file."""
    detector_cfg = json.dumps({
        "pixel1": poni.pixel_size_1,
        "pixel2": poni.pixel_size_2,
    })
    source_line = f"# Converted from: {source_path}\n" if source_path else ""
    content = (
        f"# pyFAI poni file — generated {datetime.now().isoformat(timespec='seconds')}\n"
        f"{source_line}"
        f"# pixKsi (pixel skew angle) from IPA is not represented in poni format and was ignored.\n"
        f"poni_version: 2\n"
        f"Detector: Flat\n"
        f"Detector_config: {detector_cfg}\n"
        f"Distance: {poni.distance:.12e}\n"
        f"Poni1: {poni.poni1:.12e}\n"
        f"Poni2: {poni.poni2:.12e}\n"
        f"Rot1: {poni.rot1:.12e}\n"
        f"Rot2: {poni.rot2:.12e}\n"
        f"Rot3: {poni.rot3:.12e}\n"
        f"Wavelength: {poni.wavelength:.12e}\n"
    )
    Path(path).write_text(content, encoding="utf-8")


def convert_prm_to_poni(prm_path: str | Path, poni_path: str | Path) -> tuple[IpaPrmParams, PoniParams]:
    """Parse prm, compute poni, write file. Returns both parameter sets."""
    prm = parse_ipa_prm(prm_path)
    poni = ipa_to_poni(prm)
    write_poni(poni, poni_path, source_path=prm_path)
    return prm, poni


# Default Gandolfi radius written into generated .prm files. This value is
# geometry-independent (an IPAnalyzer print-layout setting, not a detector
# calibration parameter) so there is nothing to derive it from — it is kept
# at the value observed in real IPAnalyzer output files.
_DEFAULT_GANDOLFI_RADIUS_MM = 127.4


def poni_to_ipa(poni: PoniParams) -> IpaPrmParams:
    """
    Convert pyFAI poni parameters to IPA detector parameters (inverse of
    :func:`ipa_to_poni`).

    Assumes rot3 == 0 (no in-plane rotation offset), matching the assumption
    made throughout this module. See apps/calibrate_instruments/SPEC.md /
    the pyFAI-to-IPAnalyzer conversion writeup for the derivation and the
    real-file validation of the sign conventions used here.
    """
    rot1 = poni.rot1
    rot2 = poni.rot2
    L = poni.distance  # m, perpendicular sample->plane distance (pyFAI Distance)

    cos_rot1 = math.cos(rot1)
    cos_rot2 = math.cos(rot2)
    cos_tau = max(-1.0, min(1.0, cos_rot1 * cos_rot2))

    camera_length_2 = L * 1e3  # m -> mm
    camera_length_1 = (L / (cos_rot1 * cos_rot2)) * 1e3  # m -> mm

    tilt_tau = math.degrees(math.acos(cos_tau))
    tilt_phi = math.degrees(math.atan2(math.tan(rot1), math.tan(rot2) / cos_rot1))

    pix_size_x = poni.pixel_size_2 * 1e3  # m -> mm (axis2 = X = col)
    pix_size_y = poni.pixel_size_1 * 1e3  # m -> mm (axis1 = Y = row)

    foot_x = poni.poni2 / poni.pixel_size_2
    foot_y = poni.poni1 / poni.pixel_size_1
    direct_spot_x = (poni.poni2 - L * math.tan(rot1)) / poni.pixel_size_2
    direct_spot_y = (poni.poni1 + L * math.tan(rot2) / cos_rot1) / poni.pixel_size_1

    return IpaPrmParams(
        camera_length_1=camera_length_1,
        camera_length_2=camera_length_2,
        direct_spot_x=direct_spot_x,
        direct_spot_y=direct_spot_y,
        foot_x=foot_x,
        foot_y=foot_y,
        wavelength=poni.wavelength * 1e10,  # m -> Angstrom
        pix_size_x=pix_size_x,
        pix_size_y=pix_size_y,
        pix_ksi=0.0,
        tilt_phi=tilt_phi,
        tilt_tau=tilt_tau,
    )


def write_prm(prm: IpaPrmParams, path: str | Path,
              gandolfi_radius: float = _DEFAULT_GANDOLFI_RADIUS_MM) -> None:
    """Write an IPA .prm XML file, matching the field set/order/format of
    real IPAnalyzer output files (DirectSpot-referenced, FootMode=False)."""
    content = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<Parameter xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
        'xmlns:xsd="http://www.w3.org/2001/XMLSchema">\n'
        '  <cameraMode>FlatPanel</cameraMode>\n'
        '  <FootMode>False</FootMode>\n'
        f'  <DirectSpotX>{prm.direct_spot_x:.8f}</DirectSpotX>\n'
        f'  <DirectSpotY>{prm.direct_spot_y:.8f}</DirectSpotY>\n'
        f'  <CameraLength1>{prm.camera_length_1:.8f}</CameraLength1>\n'
        f'  <FootX>{prm.foot_x:.8f}</FootX>\n'
        f'  <FootY>{prm.foot_y:.8f}</FootY>\n'
        f'  <CameraLength2>{prm.camera_length_2:.8f}</CameraLength2>\n'
        '  <waveSource>0</waveSource>\n'
        '  <xRayElement>0</xRayElement>\n'
        '  <xRayLine>0</xRayLine>\n'
        f'  <waveLength>{prm.wavelength:.12f}</waveLength>\n'
        f'  <pixSizeX>{prm.pix_size_x:.10f}</pixSizeX>\n'
        f'  <pixSizeY>{prm.pix_size_y:.10f}</pixSizeY>\n'
        f'  <pixKsi>{prm.pix_ksi:.8f}</pixKsi>\n'
        f'  <tiltPhi>{prm.tilt_phi:.8f}</tiltPhi>\n'
        f'  <tiltTau>{prm.tilt_tau:.8f}</tiltTau>\n'
        '  <sphericalRadiusInverse>0</sphericalRadiusInverse>\n'
        f'  <GandolfiRadius>{gandolfi_radius:g}</GandolfiRadius>\n'
        '</Parameter>\n'
    )
    Path(path).write_text(content, encoding="utf-8")


def poni_params_from_ai(ai) -> PoniParams:
    """Build a :class:`PoniParams` from a pyFAI AzimuthalIntegrator."""
    return PoniParams(
        distance=float(ai.dist),
        poni1=float(ai.poni1),
        poni2=float(ai.poni2),
        rot1=float(ai.rot1),
        rot2=float(ai.rot2),
        rot3=float(ai.rot3),
        pixel_size_1=float(ai.detector.pixel1),
        pixel_size_2=float(ai.detector.pixel2),
        wavelength=float(ai.wavelength),
    )
