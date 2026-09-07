"""Decoder for the two clipboard layers used by the PDIndexer bridge.

See docs/PLAN_PDINDEXER_BRIDGE.md §0.2-0.4 and
utils/pdindexer/IMPLEMENTATION_DETAILS.md for the byte layouts this
implements. Two layers:

  L1 (HGLOBAL)  = 16-byte GUID marker + MS-NRBF SerializedStreamHeader +
                  ArraySinglePrimitive<byte> record + MessageEnd, wrapping...
  L2 (payload)  = [1-byte ID=2][Brotli(MemoryPack(DiffractionProfile2[]))]

This is NOT a general-purpose MemoryPack or NRBF reader — it is a
purpose-built reader for exactly the Crystallography schema mirrored in
utils/pdindexer/csharp/PdiTypes.cs, matched member-for-member and in
declaration order (MemoryPack carries no field names, so a generic reader
is not possible without the schema).

Known-unverified-until-Phase-0 (see IMPLEMENTATION_DETAILS.md): the exact
byte offsets of HorizontalAxisProperty (assumed natural x64 alignment) and
whether int? truly serialises as a raw 8-byte Nullable<int> blob (matches
MemoryPack's NullableFormatter<T> source, which special-cases unmanaged T
to DangerousWriteUnmanaged — i.e. no MemoryPack framing at all for this
member). Both are implemented per that documented/sourced understanding,
not yet cross-checked against a real IPAnalyzer/PDIndexer capture.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

try:
    import brotli
except ImportError as _exc:  # pragma: no cover - exercised via clear error at call time
    brotli = None
    _BROTLI_IMPORT_ERROR = _exc

# s_serializedObjectID, dotnet/winforms Composition.cs — the GUID
# FD9EA796-3B13-4370-A679-56106BB288FB in ToByteArray() (little-endian
# components) order.
SERIALIZED_OBJECT_GUID = bytes.fromhex("96a79efd133b7043a67956106bb288fb")
assert len(SERIALIZED_OBJECT_GUID) == 16

DIFFRACTION_PROFILE2_ID = 0x02

_NRBF_SERIALIZED_STREAM_HEADER = 0x00
_NRBF_ARRAY_SINGLE_PRIMITIVE = 0x0F
_NRBF_MESSAGE_END = 0x0B
_NRBF_PRIMITIVE_TYPE_BYTE = 0x02


class PdiDecodeError(ValueError):
    """Raised when a byte stream doesn't match the expected layout."""


# ---------------------------------------------------------------------------
# L1 <-> L2
# ---------------------------------------------------------------------------

def strip_nrbf(raw: bytes) -> bytes:
    """L1 (HGLOBAL bytes, GUID + NRBF-wrapped byte[]) -> L2 (the byte[]
    payload itself). Raises PdiDecodeError if *raw* doesn't start with the
    expected GUID marker."""
    if raw[:16] != SERIALIZED_OBJECT_GUID:
        raise PdiDecodeError(
            f"missing serializedObjectID GUID at offset 0 (got {raw[:16].hex()})"
        )
    pos = 16
    record_type, pos = _u8(raw, pos)
    if record_type != _NRBF_SERIALIZED_STREAM_HEADER:
        raise PdiDecodeError(f"expected SerializedStreamHeader (0x00) at offset 16, got 0x{record_type:02X}")
    root_id, pos = _i32(raw, pos)
    header_id, pos = _i32(raw, pos)
    major, pos = _i32(raw, pos)
    minor, pos = _i32(raw, pos)
    if (root_id, header_id, major, minor) != (1, -1, 1, 0):
        raise PdiDecodeError(
            f"unexpected SerializedStreamHeader fields: root={root_id} header={header_id} "
            f"major={major} minor={minor} (expected 1, -1, 1, 0)"
        )
    record_type, pos = _u8(raw, pos)
    if record_type != _NRBF_ARRAY_SINGLE_PRIMITIVE:
        raise PdiDecodeError(f"expected ArraySinglePrimitive (0x0F) at offset {pos-1}, got 0x{record_type:02X}")
    object_id, pos = _i32(raw, pos)
    array_len, pos = _i32(raw, pos)
    primitive_type, pos = _u8(raw, pos)
    if primitive_type != _NRBF_PRIMITIVE_TYPE_BYTE:
        raise PdiDecodeError(f"expected PrimitiveType.Byte (0x02), got 0x{primitive_type:02X}")
    if array_len < 0:
        raise PdiDecodeError(f"negative array length {array_len}")
    payload = raw[pos : pos + array_len]
    if len(payload) != array_len:
        raise PdiDecodeError(f"truncated array: expected {array_len} bytes, got {len(payload)}")
    pos += array_len
    end_marker, pos = _u8(raw, pos)
    if end_marker != _NRBF_MESSAGE_END:
        raise PdiDecodeError(f"expected MessageEnd (0x0B) at offset {pos-1}, got 0x{end_marker:02X}")
    return payload


def decode_hglobal(raw: bytes) -> dict:
    """Validate and decode an L1 (HGLOBAL) byte string end-to-end."""
    payload = strip_nrbf(raw)
    result = decode_payload(payload)
    result["_l1_total_bytes"] = len(raw)
    return result


def decode_payload(payload: bytes) -> dict:
    """Validate and decode an L2 payload: [ID=2][Brotli(MemoryPack(...))]."""
    if not payload:
        raise PdiDecodeError("empty payload")
    if payload[0] != DIFFRACTION_PROFILE2_ID:
        raise PdiDecodeError(f"expected leading ID byte 0x02, got 0x{payload[0]:02X}")
    if brotli is None:  # pragma: no cover
        raise RuntimeError(
            "the 'brotli' package is required to decode PDIndexer payloads "
            "(pip install brotli)"
        ) from _BROTLI_IMPORT_ERROR
    try:
        body = brotli.decompress(payload[1:])
    except Exception as exc:
        raise PdiDecodeError(f"brotli decompression failed: {exc}") from exc

    cur = _Cursor(body)
    profiles = _read_diffraction_profile2_array(cur)
    if cur.pos != len(cur.buf):
        raise PdiDecodeError(f"{len(cur.buf) - cur.pos} trailing byte(s) after decoding profile array")
    return {
        "payload_bytes": len(payload),
        "compressed_bytes": len(payload) - 1,
        "decompressed_bytes": len(body),
        "profile_count": len(profiles),
        "profiles": profiles,
    }


# ---------------------------------------------------------------------------
# Low-level cursor + primitive readers
# ---------------------------------------------------------------------------

def _u8(buf: bytes, pos: int) -> tuple[int, int]:
    if pos + 1 > len(buf):
        raise PdiDecodeError(f"truncated stream: need 1 byte at offset {pos}, have {len(buf) - pos}")
    return buf[pos], pos + 1


def _i32(buf: bytes, pos: int) -> tuple[int, int]:
    if pos + 4 > len(buf):
        raise PdiDecodeError(f"truncated stream: need 4 bytes at offset {pos}, have {len(buf) - pos}")
    return struct.unpack_from("<i", buf, pos)[0], pos + 4


@dataclass
class _Cursor:
    buf: bytes
    pos: int = 0

    def take(self, n: int) -> bytes:
        if self.pos + n > len(self.buf):
            raise PdiDecodeError(f"truncated stream: need {n} byte(s) at offset {self.pos}, have {len(self.buf) - self.pos}")
        out = self.buf[self.pos : self.pos + n]
        self.pos += n
        return out

    def u8(self) -> int:
        return self.take(1)[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def f64(self) -> float:
        return struct.unpack("<d", self.take(8))[0]

    def f32(self) -> float:
        return struct.unpack("<f", self.take(4))[0]

    def raw_bool(self) -> bool:
        return self.u8() != 0

    def string(self) -> str | None:
        marker = self.i32()
        if marker == -1:
            return None
        if marker >= 0:
            return self.take(marker * 2).decode("utf-16-le")
        byte_count = ~marker  # complement, per MemoryPack's string wire format
        self.i32()  # utf16-length hint, informational only
        return self.take(byte_count).decode("utf-8")

    def nullable_int32(self) -> int | None:
        """Nullable<int>: MemoryPack's NullableFormatter special-cases
        unmanaged T to a raw DangerousWriteUnmanaged blit — no MemoryPack
        framing at all. CLR layout of Nullable<int> is {bool hasValue (+3
        pad); int value} = 8 bytes total (4-byte alignment of the int)."""
        raw = self.take(8)
        has_value = raw[0] != 0
        if not has_value:
            return None
        return struct.unpack_from("<i", raw, 4)[0]


# ---------------------------------------------------------------------------
# Schema-aware readers
# ---------------------------------------------------------------------------

def _read_point_d(cur: _Cursor) -> tuple[float, float]:
    count = cur.u8()
    if count == 255:
        raise PdiDecodeError("PointD encoded as null (unexpected)")
    if count != 2:
        raise PdiDecodeError(f"PointD: expected memberCount 2, got {count}")
    x = cur.f64()
    y = cur.f64()
    return (x, y)


def _read_point_d_list(cur: _Cursor) -> list[tuple[float, float]] | None:
    n = cur.i32()
    if n == -1:
        return None
    return [_read_point_d(cur) for _ in range(n)]


def _read_profile(cur: _Cursor) -> dict | None:
    count = cur.u8()
    if count == 255:
        return None
    if count != 4:
        raise PdiDecodeError(f"Profile: expected memberCount 4, got {count}")
    text = cur.string()
    pt = _read_point_d_list(cur)
    err = _read_point_d_list(cur)
    line_width = cur.f32()
    return {"text": text, "Pt": pt, "Err": err, "LineWidth": line_width}


# HorizontalAxisProperty: unmanaged record struct -> raw memory blit, no
# MemoryPack framing. Offsets per docs/PLAN_PDINDEXER_BRIDGE.md §0.4
# (x64 natural alignment prediction, NOT yet confirmed against a real
# capture — see module docstring).
_HAP_SIZE = 88


def _read_horizontal_axis_property(cur: _Cursor) -> dict:
    raw = cur.take(_HAP_SIZE)
    (axis_mode, wave_source, wave_color) = struct.unpack_from("<iii", raw, 0)
    wave_length = struct.unpack_from("<d", raw, 16)[0]
    xray_element_number, xray_line = struct.unpack_from("<ii", raw, 24)
    electron_acc_voltage = struct.unpack_from("<d", raw, 32)[0]
    energy_takeoff_angle = struct.unpack_from("<d", raw, 40)[0]
    tof_angle = struct.unpack_from("<d", raw, 48)[0]
    tof_length = struct.unpack_from("<d", raw, 56)[0]
    (two_theta_unit, dspacing_unit, wave_number_unit, energy_unit, tof_time_unit) = struct.unpack_from(
        "<iiiii", raw, 64
    )
    return {
        "AxisMode": axis_mode,
        "WaveSource": wave_source,
        "WaveColor": wave_color,
        "WaveLength": wave_length,
        "XrayElementNumber": xray_element_number,
        "XrayLine": xray_line,
        "ElectronAccVolatage": electron_acc_voltage,
        "EnergyTakeoffAngle": energy_takeoff_angle,
        "TofAngle": tof_angle,
        "TofLength": tof_length,
        "TwoThetaUnit": two_theta_unit,
        "DspacingUnit": dspacing_unit,
        "WaveNumberUnit": wave_number_unit,
        "EnergyUnit": energy_unit,
        "TofTimeUnit": tof_time_unit,
    }


def _read_masking_range(cur: _Cursor) -> list[float]:
    count = cur.u8()
    if count != 1:
        raise PdiDecodeError(f"MaskingRange: expected memberCount 1, got {count}")
    n = cur.i32()  # double[] X, always length 2 in practice
    if n == -1:
        return []
    return [cur.f64() for _ in range(n)]


def _read_masking_range_list(cur: _Cursor) -> list | None:
    n = cur.i32()
    if n == -1:
        return None
    return [_read_masking_range(cur) for _ in range(n)]


def _read_double_array(cur: _Cursor) -> list[float] | None:
    n = cur.i32()
    if n == -1:
        return None
    return [cur.f64() for _ in range(n)]


# Declaration-order member list for DiffractionProfile2 (55 members), each
# entry (name, reader). See docs/PLAN_PDINDEXER_BRIDGE.md §0.4 for the
# authoritative table this mirrors.
_DP2_MEMBERS: list[tuple[str, str]] = [
    ("maskingRanges", "masking_range_list"),
    ("InterpolationOrder", "i32"),
    ("InterpolationPoints", "i32"),
    ("DoesMaskAndInterpolate", "bool"),
    ("SourceProfile", "profile"),
    ("BgPoints", "point_d_list"),
    ("ConvertedProfile", "profile"),
    ("InterpolatedProfile", "profile"),
    ("SmoothedProfile", "profile"),
    ("Kalpha2RemovedProfile", "profile"),
    ("BackgroundProfile", "profile"),
    ("Profile", "profile"),
    ("Mode", "i32"),
    ("SrcProperty", "hap"),
    ("DstProperty", "hap"),
    ("DoesNormarizeIntensity", "bool"),
    ("NormarizeRangeStart", "f64"),
    ("NormarizeRangeEnd", "f64"),
    ("NormarizeAsAverage", "bool"),
    ("NormarizeIntensity", "f64"),
    ("DoesSmoothing", "bool"),
    ("SazitkyGorayM", "i32"),
    ("SazitkyGorayN", "i32"),
    ("DoesTwoThetaOffset", "bool"),
    ("TwoThetaOffsetCoeff0", "f64"),
    ("TwoThetaOffsetCoeff1", "f64"),
    ("TwoThetaOffsetCoeff2", "f64"),
    ("DoesRemoveKalpha2", "bool"),
    ("Kalpha1", "f64"),
    ("Kalpha2", "f64"),
    ("IsShiftX", "bool"),
    ("ShiftX", "f64"),
    ("DoesBandpassFilter", "bool"),
    ("DoesLowPath", "bool"),
    ("DoesHighPath", "bool"),
    ("LowPathLimit", "f64"),
    ("HighPathLimit", "f64"),
    ("IsCPS", "bool"),
    ("ExposureTime", "f64"),
    ("IsLogIntensity", "bool"),
    ("LineWidth", "f32"),
    ("ColorARGB", "nullable_i32"),
    ("BgPointsNumber", "i32"),
    ("SubtractBackground", "bool"),
    ("BgMode", "i32"),
    ("BackgroundReferrenceProfile", "profile"),
    ("BackgroundReferrenceScale", "f64"),
    ("Name", "string"),
    ("Comment", "string"),
    ("IsLPOmain", "bool"),
    ("IsLPOchild", "bool"),
    ("ImageArray", "double_array"),
    ("ImageScale", "f64"),
    ("ImageWidth", "i32"),
    ("ImageHeight", "i32"),
]
assert len(_DP2_MEMBERS) == 55, len(_DP2_MEMBERS)

_READERS = {
    "i32": lambda cur: cur.i32(),
    "f64": lambda cur: cur.f64(),
    "f32": lambda cur: cur.f32(),
    "bool": lambda cur: cur.raw_bool(),
    "string": lambda cur: cur.string(),
    "nullable_i32": lambda cur: cur.nullable_int32(),
    "profile": _read_profile,
    "point_d_list": _read_point_d_list,
    "masking_range_list": _read_masking_range_list,
    "double_array": _read_double_array,
    "hap": _read_horizontal_axis_property,
}


def _read_diffraction_profile2(cur: _Cursor) -> dict | None:
    count = cur.u8()
    if count == 255:
        return None
    if count != len(_DP2_MEMBERS):
        raise PdiDecodeError(f"DiffractionProfile2: expected memberCount {len(_DP2_MEMBERS)}, got {count}")
    out: dict = {}
    for name, kind in _DP2_MEMBERS:
        out[name] = _READERS[kind](cur)
    return out


def _read_diffraction_profile2_array(cur: _Cursor) -> list[dict]:
    n = cur.i32()
    if n == -1:
        return []
    return [_read_diffraction_profile2(cur) for _ in range(n)]
