---
description: pyFAI / poni-file conventions used by apps/xrd_scan and apps/ipa_poni — poni v2 format, AzimuthalIntegrator construction, in-memory integrate1d usage, IPA-to-poni coordinate mapping.
---

# pyFAI / poni-file integration conventions

This project touches pyFAI in two places: `apps/xrd_scan/` (in-memory
azimuthal integration of live detector frames) and `apps/ipa_poni/`
(converting IPAnalyzer `.prm` calibration files to pyFAI `.poni` format).
Both agree on the same poni v2 conventions below — follow them for any new
pyFAI integration rather than inventing a new pattern.

## Reading a `.poni` file: use the custom UTF-8-safe parser, not pyFAI's own

pyFAI's built-in poni loader is locale-dependent and has caused encoding
issues on this project. Instead, parse the file by hand
(`apps/xrd_scan/xrd_scan_backend.py::parse_poni`):

```python
def parse_poni(path: pathlib.Path) -> dict:
    result: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(":")
        key   = key.strip().lower()
        value = value.strip()
        if key == "detector_config":
            result[key] = json.loads(value)
        else:
            try:
                result[key] = float(value)
            except ValueError:
                result[key] = value
    return result
```

Then build the `AzimuthalIntegrator` explicitly rather than via pyFAI's own
poni-loading constructor:

```python
def build_ai(poni_dict: dict) -> AzimuthalIntegrator:
    dc  = poni_dict.get("detector_config", {})
    det = pf_detectors.Detector(
        pixel1=float(dc["pixel1"]),
        pixel2=float(dc["pixel2"]),
    )
    return AzimuthalIntegrator(
        dist=poni_dict["distance"],
        poni1=poni_dict["poni1"],
        poni2=poni_dict["poni2"],
        rot1=poni_dict["rot1"],
        rot2=poni_dict["rot2"],
        rot3=poni_dict["rot3"],
        wavelength=poni_dict["wavelength"],
        detector=det,
    )
```

`pyFAI.detectors.Detector` and `pyFAI.integrator.azimuthal.AzimuthalIntegrator`
should both be imported behind a `try/except ImportError` with a
`PYFAI_AVAILABLE` flag (see `xrd_scan_backend.py`) so the app still loads on
machines without pyFAI installed — the corresponding UI should disable
whatever depends on it.

## Writing a `.poni` file: poni v2 text format

`apps/ipa_poni/ipa_to_poni.py::write_poni` is the canonical writer. Match this
format for any new poni-writing code:

```python
detector_cfg = json.dumps({"pixel1": poni.pixel_size_1, "pixel2": poni.pixel_size_2})
content = (
    f"# pyFAI poni file — generated {datetime.now().isoformat(timespec='seconds')}\n"
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
```

Notes:
- `Detector: Flat` with pixel sizes given explicitly in `Detector_config` —
  don't use a named pyFAI detector model, this project always treats the
  detector as a flat panel with user-supplied pixel pitch.
- 12-digit exponential precision (`:.12e`) on every numeric field.
- `Rot3` is always `0` in this project — in-plane detector rotation doesn't
  affect Debye–Scherrer ring integration, so nothing here ever derives it.

## In-memory azimuthal integration (no TIFF round-trip)

`apps/xrd_scan/xrd_scan_backend.py::XrdScanWorker.run()` integrates each
acquired frame directly in memory:

```python
result = ai.integrate1d(
    img_f,                                   # float32, dark-subtracted
    npt=n_bins,
    unit="2th_deg",
    method=("no", "histogram", "cython"),
    correctSolidAngle=True,
    polarization_factor=0.95,
)
# result.radial, result.intensity — 1D spectrum for this frame
```

Reuse this exact `method=`/`correctSolidAngle`/`polarization_factor` triple
for any new in-memory integration in this project — it's the validated
combination for the Rad-icon 2022 sensor. Saving TIFFs is a separate,
optional path (see `apps/xrd_scan/IMPLEMENTATION_DETAILS.md`) — integration
never depends on a file existing on disk.

## IPA ↔ poni coordinate mapping

The full derivation (coordinate systems, Rodrigues tilt rotation, rotation
angle formulas, unit conventions) is documented in the module docstring of
`apps/ipa_poni/ipa_to_poni.py` — read that docstring first; it is the
canonical source. Summary table and inverse-conversion notes are in
`apps/ipa_poni/IMPLEMENTATION_DETAILS.md`.

Key formulas, if you need them without opening the file:

```
Distance = CameraLength1 × cos(tiltTau) × 1e-3                         [m]
Poni1    = FootY × pixSizeY × 1e-3                                     [m]
Poni2    = FootX × pixSizeX × 1e-3                                     [m]
Rot1     = -arcsin(sin(tiltTau) · sin(tiltPhi))
Rot2     = arcsin(-sin(tiltTau) · cos(tiltPhi) / cos(Rot1))
Rot3     = 0
```

`pixKsi` (pixel skew) has no pyFAI equivalent and is always dropped.
