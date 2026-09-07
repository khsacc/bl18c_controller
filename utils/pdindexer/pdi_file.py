"""Method C fallback: write a legacy-format .pdi (XML) file for PDIndexer's
folder watcher to pick up.

PDIndexer only reacts to FileSystemWatcher's *Created* event (see
FormMain.cs watcher_Created, referenced in IMPLEMENTATION_DETAILS.md) — it
never re-reads a modified file, and it never watches ``Renamed``. This is
why the file is written directly under its final ``.pdi`` name via an
exclusive create, rather than written to a ``.tmp`` sibling and renamed
into place: on Windows, a same-directory rename raises FileSystemWatcher's
*Renamed* event, not *Created* — PDIndexer would never see it. PDIndexer's
own reader already tolerates a file that is still being written: it loops
on ``File.Open(path, FileMode.Open)`` (default sharing = exclusive) with a
100 ms retry until our own ``os.fdopen`` handle below is closed, so holding
the file open for the duration of the write is enough to prevent it from
being read half-finished.
"""
from __future__ import annotations

import os
import pathlib
import xml.etree.ElementTree as ET
from datetime import datetime

from .profile import PdiProfile, sanitise_pdi_string

# Crystallography.HorizontalAxis.Angle / WaveSource.Xray / WaveColor.Monochrome
# — spelled out as the C# enum member names, since this is read back by
# System.Xml.Serialization.XmlSerializer(typeof(DiffractionProfile[])),
# which matches enum values by name.
_AXIS_MODE_ANGLE = "Angle"
_WAVE_SOURCE_XRAY = "Xray"
_WAVE_COLOR_MONOCHROME = "Monochrome"

# XYFile.ReadPdi2File (Crystallography/IO/PdiFile.cs) does a blind,
# case-sensitive find/replace of these exact substrings across the entire
# file body before parsing it as XML — a quirk of its own, unrelated to any
# element name. It is specific to this transport (the clipboard path goes
# through MemoryPackEx.Deserialize instead, which never touches string
# content). Rather than mangling the name/comment the user sees in this
# app's own UI (which the shared sanitiser used to do, deleting "pt"
# case-insensitively and corrupting ordinary words like "September" — see
# code review 2026-09-06), replicate the *exact* transform here, at the
# point where we write the XML PDIndexer will read: a lossless, ordinal,
# lowercase-only substitution, so what ends up in PDIndexer matches what we
# generate, with no surprise capitalisation swap discovered only after the
# fact.
_PDI_FILE_REPLACEMENTS = (
    ("OriginalFormatType", "SrcAxisMode"),
    ("OriginalWaveLength", "SrcWaveLength"),
    ("OriginalTakeoffAngle", "SrcTakeoffAngle"),
    ("pt", "Pt"),
)


def _pdi_file_text(value: str) -> str:
    """Pre-apply PDIndexer's own ReadPdi2File find/replace pass (see
    _PDI_FILE_REPLACEMENTS) so text round-trips unchanged through the
    .pdi-file transport."""
    out = value
    for old, new in _PDI_FILE_REPLACEMENTS:
        out = out.replace(old, new)
    return out


def _build_element(profiles: list[PdiProfile]) -> ET.Element:
    root = ET.Element(
        "ArrayOfDiffractionProfile",
        {
            "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
            "xmlns:xsd": "http://www.w3.org/2001/XMLSchema",
        },
    )
    for p in profiles:
        dp = ET.SubElement(root, "DiffractionProfile")
        orig = ET.SubElement(dp, "OriginalProfile")
        pts = ET.SubElement(orig, "Pt")
        for x, y in zip(p.x.tolist(), p.y.tolist()):
            pt = ET.SubElement(pts, "PointD")
            ET.SubElement(pt, "X").text = repr(x)
            ET.SubElement(pt, "Y").text = repr(y)
        err_el = ET.SubElement(orig, "Err")
        if p.err is not None:
            for x, e in zip(p.x.tolist(), p.err.tolist()):
                pt = ET.SubElement(err_el, "PointD")
                ET.SubElement(pt, "X").text = repr(x)
                ET.SubElement(pt, "Y").text = repr(e)
        ET.SubElement(dp, "SrcAxisMode").text = _AXIS_MODE_ANGLE
        ET.SubElement(dp, "SrcWaveLength").text = repr(p.wavelength_nm)
        ET.SubElement(dp, "Mode").text = "Concentric"
        ET.SubElement(dp, "WaveSource").text = _WAVE_SOURCE_XRAY
        ET.SubElement(dp, "WaveColor").text = _WAVE_COLOR_MONOCHROME
        ET.SubElement(dp, "XrayElementNumber").text = "0"
        ET.SubElement(dp, "Name").text = _pdi_file_text(p.name)
        if p.comment:
            ET.SubElement(dp, "Comment").text = _pdi_file_text(p.comment)
    return root


def _unique_final_path(directory: pathlib.Path, base_name: str) -> pathlib.Path:
    """A ``.pdi`` path, unused in *directory*, with enough entropy that two
    calls within the same second still land on different names. The
    exclusive create in write_pdi_file() is still the actual race-free
    guarantee — this just makes a collision on the first attempt unlikely."""
    stem = sanitise_pdi_string(base_name) or "profile"
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    candidate = directory / f"{stem}_{ts}.pdi"
    n = 0
    while candidate.exists():
        n += 1
        candidate = directory / f"{stem}_{ts}_{n}.pdi"
    return candidate


def write_pdi_file(directory: pathlib.Path, profiles: list[PdiProfile], *, base_name: str = "BL18C") -> pathlib.Path:
    """Write *profiles* as a legacy .pdi XML file under *directory*.

    Returns the final path. Raises OSError if the directory is not
    writable, ValueError if *profiles* is empty.
    """
    if not profiles:
        raise ValueError("profiles must not be empty")
    directory = pathlib.Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    root = _build_element(profiles)
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    # Retry the exclusive create a few times in the (very unlikely) case
    # _unique_final_path's check and this open raced with another writer.
    for attempt in range(5):
        final_path = _unique_final_path(directory, base_name)
        try:
            fd = os.open(str(final_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            if attempt == 4:
                raise
            continue
        break

    # Writing directly under the final ".pdi" name — see module docstring
    # for why this must not be a write-then-rename. Holding the handle
    # open until the XML is fully written is what keeps PDIndexer's own
    # open-retry loop from reading a half-written file.
    with os.fdopen(fd, "wb") as f:
        tree.write(f, encoding="utf-8", xml_declaration=True)

    return final_path
