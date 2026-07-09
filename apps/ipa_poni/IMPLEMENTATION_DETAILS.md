# IPA → poni coordinate mapping — implementation details

Developer-facing detail for `apps/ipa_poni/`. Converts IPAnalyzer (IPA)
`.prm` XML files to pyFAI `.poni` v2 format for azimuthal integration with
pyFAI. For general usage, see the user-facing
[ipa_poni_file_conversion.md](ipa_poni_file_conversion.md). **For the pyFAI
poni v2 write format shared with `apps/xrd_scan`, see the project skill
`/pyfai-integration`.**

The full derivation (coordinate systems, rotation formulas, unit conventions)
lives in the module docstring of
[ipa_to_poni.py](ipa_to_poni.py) — treat that docstring as canonical and this
file as the summary/index.

## IPA coordinate system (origin = DirectSpot on detector)

- Z along beam, X = image right, Y = image down. Sample at `(0, 0, −CL)`.
- Tilt: Rodrigues rotation by angle τ (`tiltTau`) around axis `(cos φ, sin φ,
  0)` where φ = `tiltPhi`.
- `FootX`/`FootY`: foot of the perpendicular from sample to the tilted
  detector plane = pyFAI PONI.

**IPA units**: CameraLength in mm, PixSize in mm, wavelength in Å,
tiltPhi/Tau in degrees.

## Parameter mapping (`ipa_to_poni()`)

| pyFAI poni | IPA source | Formula |
|---|---|---|
| `Distance` | `CameraLength1`, `tiltTau` | `CameraLength1 × cos(τ) × 1e-3` m |
| `Poni1` | `FootY`, `pixSizeY` | `FootY × pixSizeY × 1e-3` m |
| `Poni2` | `FootX`, `pixSizeX` | `FootX × pixSizeX × 1e-3` m |
| `Rot1` | `tiltPhi`, `tiltTau` | `−arcsin(sin(τ)·sin(φ))` |
| `Rot2` | `tiltPhi`, `tiltTau` | `arcsin(−sin(τ)·cos(φ) / cos(Rot1))` |
| `Rot3` | — | `0` (in-plane rotation doesn't affect rings) |
| `PixelSize1` | `pixSizeY` | `pixSizeY × 1e-3` m |
| `PixelSize2` | `pixSizeX` | `pixSizeX × 1e-3` m |
| `Wavelength` | `waveLength` | `waveLength × 1e-10` m |

`pixKsi` (pixel skew angle) is not representable in poni format and is
silently ignored. The output uses `Detector: Flat` with pixel sizes in
`Detector_config`.

## Inverse conversion (`poni_to_ipa()`)

`ipa_to_poni.py` also implements the inverse — pyFAI poni parameters back to
IPA `.prm` fields — used by `apps/calibrate_instruments` to write `.prm`
files from a poni calibration. It assumes `rot3 == 0` (no in-plane rotation
offset), matching the assumption made throughout the forward conversion. See
`apps/calibrate_instruments/SPEC.md` for the derivation and real-file
validation of the sign conventions used here. `write_prm()` reproduces the
field set/order/format of real IPAnalyzer output files
(`DirectSpot`-referenced, `FootMode=False`), including the
geometry-independent `_DEFAULT_GANDOLFI_RADIUS_MM = 127.4` constant (an
IPAnalyzer print-layout setting, not a detector calibration parameter, so
there is nothing to derive it from beyond the value observed in real
IPAnalyzer output files).
