"""Tests for the PDIndexer bridge: profile validation, the .pdi (method C)
writer, and the L1/L2 payload decoder.

Two of the tests below are gated on golden capture files that must be taken
on a real Windows machine running IPAnalyzer + PDIndexer (see
docs/PLAN_PDINDEXER_BRIDGE.md Phase 0) — they are skipped, not failed, when
those files are absent, which is the normal state of this repository until
someone runs Phase 0. A synthetic round-trip test exercises the decoder
against a payload this test module builds itself (a minimal but
schema-accurate DiffractionProfile2[]), so the decoder's own logic is still
covered without real hardware.
"""
import os
import pathlib
import struct
import sys
import time
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_THIS_DIR))
sys.path.insert(0, _THIS_DIR)

import numpy as np
from PyQt6 import QtWidgets

from utils.pdindexer.profile import PdiProfile, sanitise_pdi_string
from utils.pdindexer.pdi_file import write_pdi_file
from utils.pdindexer import pdi_file
from utils.pdindexer import service as pdi_service_mod
from utils.pdindexer.service import PdiService, Transport, Trigger

try:
    import brotli
    _BROTLI_AVAILABLE = True
except ImportError:
    _BROTLI_AVAILABLE = False

import pdi_payload_decoder as dec

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_L1_GOLDEN = os.path.join(_DATA_DIR, "ipa_clipboard_hglobal.bin")
_L2_GOLDEN = os.path.join(_DATA_DIR, "ipa_payload.bin")


class SanitiserTests(unittest.TestCase):
    """sanitise_pdi_string (profile.py) now only avoids the trailing-"whole"
    LPO-mode trigger, shared by both transports. The .pdi-file transport's
    own "pt"->"Pt" quirk is transport-specific and handled separately in
    pdi_file._pdi_file_text — see PdiFileTextQuirkTests below and the
    2026-09-06 code review notes in profile.py."""

    def test_does_not_touch_pt_substrings(self):
        # An earlier version deleted "pt" case-insensitively here, which
        # corrupted ordinary words (e.g. "September" -> "Seember") even for
        # the clipboard transport, where this quirk doesn't apply at all.
        self.assertEqual(sanitise_pdi_string("sample_pt_01"), "sample_pt_01")
        self.assertEqual(sanitise_pdi_string("September"), "September")
        self.assertEqual(sanitise_pdi_string("PT-scan"), "PT-scan")

    def test_strips_trailing_whole(self):
        self.assertEqual(sanitise_pdi_string("run whole"), "run")
        # Repeated trailing "whole" is stripped down to nothing left that
        # still triggers PDIndexer's .EndsWith("whole") LPO-mode branch.
        self.assertEqual(sanitise_pdi_string("run wholewhole"), "run")
        self.assertFalse(sanitise_pdi_string("run wholewhole").endswith("whole"))

    def test_falls_back_to_default_when_emptied(self):
        self.assertEqual(sanitise_pdi_string("whole"), "profile")


class PdiFileTextQuirkTests(unittest.TestCase):
    """pdi_file._pdi_file_text: the .pdi-transport-specific, case-sensitive
    pre-emptive replication of XYFile.ReadPdi2File's own find/replace pass,
    so what we write and what PDIndexer ends up showing agree."""

    def test_lowercase_pt_becomes_uppercase_pt(self):
        self.assertEqual(pdi_file._pdi_file_text("sample_pt_01"), "sample_Pt_01")

    def test_uppercase_pt_variants_untouched(self):
        # C#'s default string.Replace is ordinal/case-sensitive — only
        # exact lowercase "pt" is affected upstream.
        self.assertEqual(pdi_file._pdi_file_text("PT-scan"), "PT-scan")
        self.assertEqual(pdi_file._pdi_file_text("Pt already"), "Pt already")

    def test_ordinary_words_containing_pt_change_case_only_not_deleted(self):
        # Lossless: upstream swaps case, it does not delete anything.
        self.assertEqual(pdi_file._pdi_file_text("September"), "SePtember")

    def test_reserved_tokens_replaced(self):
        s = pdi_file._pdi_file_text("OriginalWaveLength test OriginalFormatType")
        self.assertNotIn("OriginalWaveLength", s)
        self.assertNotIn("OriginalFormatType", s)
        self.assertIn("SrcWaveLength", s)
        self.assertIn("SrcAxisMode", s)


class PdiProfileValidationTests(unittest.TestCase):
    def test_rejects_mismatched_lengths(self):
        with self.assertRaises(ValueError):
            PdiProfile(name="a", x=np.array([1.0, 2.0]), y=np.array([1.0]), wavelength_nm=0.06)

    def test_rejects_empty(self):
        with self.assertRaises(ValueError):
            PdiProfile(name="a", x=np.array([]), y=np.array([]), wavelength_nm=0.06)

    def test_rejects_non_finite(self):
        with self.assertRaises(ValueError):
            PdiProfile(name="a", x=np.array([1.0, np.nan]), y=np.array([1.0, 2.0]), wavelength_nm=0.06)

    def test_rejects_non_positive_wavelength(self):
        with self.assertRaises(ValueError):
            PdiProfile(name="a", x=np.array([1.0]), y=np.array([1.0]), wavelength_nm=0.0)

    def test_accepts_valid_profile_and_sanitises_name(self):
        p = PdiProfile(name="run pt whole", x=np.array([1.0, 2.0]), y=np.array([3.0, 4.0]), wavelength_nm=0.062)
        # "pt" is untouched here (that quirk is .pdi-transport-specific,
        # applied in pdi_file.py, not by PdiProfile) — only the trailing
        # "whole" LPO-mode trigger is shared across both transports.
        self.assertIn("pt", p.name)
        self.assertFalse(p.name.endswith("whole"))

    def test_comment_is_not_sanitised(self):
        # PDIndexer's LPO-mode check only ever looks at Name, never
        # Comment — sanitising Comment too would truncate a legitimate
        # comment ending in "whole" for no reason.
        p = PdiProfile(
            name="run", x=np.array([1.0, 2.0]), y=np.array([3.0, 4.0]), wavelength_nm=0.062,
            comment="measured the whole sample",
        )
        self.assertEqual(p.comment, "measured the whole sample")

    def test_rejects_2d_arrays(self):
        with self.assertRaises(ValueError):
            PdiProfile(name="a", x=np.zeros((2, 2)), y=np.zeros((2, 2)), wavelength_nm=0.06)

    def test_rejects_2d_err(self):
        with self.assertRaises(ValueError):
            PdiProfile(
                name="a", x=np.array([1.0, 2.0]), y=np.array([3.0, 4.0]),
                err=np.zeros((2, 1)), wavelength_nm=0.06,
            )

    def test_to_json_dict_roundtrips_arrays(self):
        import base64
        x = np.linspace(5, 30, 10)
        y = np.arange(10, dtype=float)
        p = PdiProfile(name="p", x=x, y=y, wavelength_nm=0.062)
        d = p.to_json_dict()
        x_back = np.frombuffer(base64.b64decode(d["x_b64"]), dtype="<f8")
        np.testing.assert_array_equal(x_back, x)
        self.assertIsNone(d["err_b64"])


class PdiFileWriterTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.directory = self._tmpdir.name

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_writes_valid_xml(self):
        import xml.etree.ElementTree as ET
        p = PdiProfile(name="sample", x=np.array([1.0, 2.0]), y=np.array([3.0, 4.0]), wavelength_nm=0.062)
        path = write_pdi_file(self.directory, [p])
        self.assertTrue(path.name.endswith(".pdi"))
        root = ET.parse(path).getroot()
        self.assertEqual(root.tag, "ArrayOfDiffractionProfile")
        self.assertEqual(len(root.findall("DiffractionProfile")), 1)

    def test_rapid_writes_never_collide(self):
        p = PdiProfile(name="sample", x=np.array([1.0, 2.0]), y=np.array([3.0, 4.0]), wavelength_nm=0.062)
        paths = [write_pdi_file(self.directory, [p]) for _ in range(5)]
        self.assertEqual(len(set(paths)), 5)
        for path in paths:
            self.assertTrue(path.exists())

    def test_no_leftover_tmp_files(self):
        p = PdiProfile(name="sample", x=np.array([1.0]), y=np.array([2.0]), wavelength_nm=0.062)
        write_pdi_file(self.directory, [p])
        leftovers = [f for f in os.listdir(self.directory) if f.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_rejects_empty_profile_list(self):
        with self.assertRaises(ValueError):
            write_pdi_file(self.directory, [])


# ---------------------------------------------------------------------------
# Synthetic payload construction — a minimal, schema-accurate
# DiffractionProfile2[] encoder used ONLY to test the decoder in this
# repository (production encoding is PdiSender.exe's job via the real
# MemoryPack library; see utils/pdindexer/csharp/Program.cs). Kept
# deliberately separate from utils/pdindexer so nothing here is ever
# mistaken for a shipped encoder.
# ---------------------------------------------------------------------------

def _enc_string(s):
    if s is None:
        return struct.pack("<i", -1)
    b = s.encode("utf-8")
    return struct.pack("<i", ~len(b)) + struct.pack("<i", -1) + b


def _enc_point_d_list(points):
    if points is None:
        return struct.pack("<i", -1)
    out = struct.pack("<i", len(points))
    for x, y in points:
        out += bytes([2]) + struct.pack("<dd", x, y)
    return out


def _enc_profile(text=None, pt=None, err=None, line_width=1.0):
    out = bytes([4])
    out += _enc_string(text)
    out += _enc_point_d_list(pt or [])
    out += _enc_point_d_list(err or [])
    out += struct.pack("<f", line_width)
    return out


def _enc_hap(axis_mode=0, wave_length_nm=0.062, two_theta_unit=0):
    buf = bytearray(88)
    struct.pack_into("<iii", buf, 0, axis_mode, 0, 0)  # AxisMode, WaveSource=Xray, WaveColor=Monochrome
    struct.pack_into("<d", buf, 16, wave_length_nm)
    struct.pack_into("<ii", buf, 24, 0, 1)  # XrayElementNumber=0, XrayLine=Ka1
    struct.pack_into("<iiiii", buf, 64, two_theta_unit, 0, 0, 0, 0)
    return bytes(buf)


def _enc_nullable_i32(value):
    if value is None:
        return bytes(8)
    return bytes([1, 0, 0, 0]) + struct.pack("<i", value)


def _build_synthetic_diffraction_profile2(name, x, y, wavelength_nm):
    empty_profile = _enc_profile()
    out = bytearray()
    out += bytes([55])                                  # memberCount
    out += struct.pack("<i", -1)                         # maskingRanges = null
    out += struct.pack("<i", 2)                          # InterpolationOrder
    out += struct.pack("<i", 20)                         # InterpolationPoints
    out += bytes([0])                                    # DoesMaskAndInterpolate
    out += _enc_profile(pt=list(zip(x, y)))              # SourceProfile
    out += struct.pack("<i", -1)                         # BgPoints = null
    out += empty_profile                                 # ConvertedProfile
    out += empty_profile                                 # InterpolatedProfile
    out += empty_profile                                 # SmoothedProfile
    out += empty_profile                                 # Kalpha2RemovedProfile
    out += empty_profile                                 # BackgroundProfile
    out += empty_profile                                 # Profile
    out += struct.pack("<i", 0)                           # Mode = Concentric
    out += _enc_hap(wave_length_nm=wavelength_nm)          # SrcProperty
    out += _enc_hap(wave_length_nm=wavelength_nm)          # DstProperty
    out += bytes([0])                                    # DoesNormarizeIntensity
    out += struct.pack("<d", 0.0)                         # NormarizeRangeStart
    out += struct.pack("<d", 180.0)                       # NormarizeRangeEnd
    out += bytes([1])                                     # NormarizeAsAverage
    out += struct.pack("<d", 1000.0)                      # NormarizeIntensity
    out += bytes([0])                                     # DoesSmoothing
    out += struct.pack("<i", 3)                           # SazitkyGorayM
    out += struct.pack("<i", 3)                           # SazitkyGorayN
    out += bytes([0])                                     # DoesTwoThetaOffset
    out += struct.pack("<d", 0.0) * 3                     # TwoThetaOffsetCoeff0-2
    out += bytes([0])                                     # DoesRemoveKalpha2
    out += struct.pack("<d", 0.0) * 2                      # Kalpha1, Kalpha2
    out += bytes([0])                                     # IsShiftX
    out += struct.pack("<d", 0.0)                          # ShiftX
    out += bytes([0, 0, 0])                                # DoesBandpassFilter, DoesLowPath, DoesHighPath
    out += struct.pack("<d", float("nan")) * 2             # LowPathLimit, HighPathLimit
    out += bytes([1])                                      # IsCPS
    out += struct.pack("<d", 1.0)                          # ExposureTime
    out += bytes([0])                                      # IsLogIntensity
    out += struct.pack("<f", 1.0)                          # LineWidth
    out += _enc_nullable_i32(None)                         # ColorARGB
    out += struct.pack("<i", 15)                           # BgPointsNumber
    out += bytes([0])                                      # SubtractBackground
    out += struct.pack("<i", 0)                            # BgMode
    out += bytes([255])                                    # BackgroundReferrenceProfile = null
    out += struct.pack("<d", 1.0)                          # BackgroundReferrenceScale
    out += _enc_string(name)                               # Name
    out += _enc_string(None)                               # Comment
    out += bytes([0, 0])                                   # IsLPOmain, IsLPOchild
    out += struct.pack("<i", -1)                            # ImageArray = null
    out += struct.pack("<d", 0.0)                           # ImageScale
    out += struct.pack("<i", 0) * 2                          # ImageWidth, ImageHeight
    return bytes(out)


def _build_synthetic_l2_payload(name, x, y, wavelength_nm):
    dp = _build_synthetic_diffraction_profile2(name, x, y, wavelength_nm)
    body = struct.pack("<i", 1) + dp  # DiffractionProfile2[] of length 1
    compressed = brotli.compress(body)
    return bytes([dec.DIFFRACTION_PROFILE2_ID]) + compressed


@unittest.skipUnless(_BROTLI_AVAILABLE, "brotli package not installed")
class DecoderSyntheticRoundtripTests(unittest.TestCase):
    def test_decodes_own_synthetic_payload(self):
        x = np.linspace(5.0, 30.0, 20)
        y = np.arange(20, dtype=float) * 3.0
        payload = _build_synthetic_l2_payload("BL18C_test", x, y, 0.062)
        result = dec.decode_payload(payload)
        self.assertEqual(result["profile_count"], 1)
        prof = result["profiles"][0]
        self.assertEqual(prof["Name"], "BL18C_test")
        self.assertEqual(prof["Mode"], 0)
        pts = prof["SourceProfile"]["Pt"]
        self.assertEqual(len(pts), 20)
        np.testing.assert_allclose([p[0] for p in pts], x)
        np.testing.assert_allclose([p[1] for p in pts], y)
        self.assertAlmostEqual(prof["SrcProperty"]["WaveLength"], 0.062)
        self.assertIsNone(prof["ColorARGB"])
        self.assertIsNone(prof["BackgroundReferrenceProfile"])
        for key in ("ConvertedProfile", "InterpolatedProfile", "SmoothedProfile",
                    "Kalpha2RemovedProfile", "BackgroundProfile", "Profile"):
            self.assertIsNotNone(prof[key], f"{key} must not be null (PDIndexer NREs on null)")

    def test_rejects_wrong_leading_id(self):
        payload = bytes([0x03]) + brotli.compress(struct.pack("<i", 0))
        with self.assertRaises(dec.PdiDecodeError):
            dec.decode_payload(payload)

    def test_rejects_truncated_payload(self):
        payload = bytes([dec.DIFFRACTION_PROFILE2_ID]) + brotli.compress(b"\x01\x00\x00\x00")
        with self.assertRaises(dec.PdiDecodeError):
            dec.decode_payload(payload)


@unittest.skipUnless(os.path.isfile(_L2_GOLDEN), f"golden data not captured yet: {_L2_GOLDEN} (see Phase 0)")
class GoldenL2Tests(unittest.TestCase):
    """Regression test against a real IPAnalyzer-produced payload. Skipped
    until someone runs docs/PLAN_PDINDEXER_BRIDGE.md Phase 0 on Windows and
    commits the two golden files under tests/data/."""

    def test_decodes_golden_payload(self):
        with open(_L2_GOLDEN, "rb") as f:
            payload = f.read()
        result = dec.decode_payload(payload)
        self.assertGreaterEqual(result["profile_count"], 1)


@unittest.skipUnless(os.path.isfile(_L1_GOLDEN), f"golden data not captured yet: {_L1_GOLDEN} (see Phase 0)")
class GoldenL1Tests(unittest.TestCase):
    def test_decodes_golden_hglobal(self):
        with open(_L1_GOLDEN, "rb") as f:
            raw = f.read()
        result = dec.decode_hglobal(raw)
        self.assertGreaterEqual(result["profile_count"], 1)


def _one_profile(name: str) -> list[PdiProfile]:
    return [PdiProfile(name=name, x=np.array([1.0, 2.0]), y=np.array([3.0, 4.0]), wavelength_nm=0.062)]


class WatchFolderSequenceBatchingTests(unittest.TestCase):
    """Regression for silently dropped sequence frames over the .pdi-folder
    transport: PDIndexer's FileSystemWatcher disables itself
    (EnableRaisingEvents = false) for the entire time it is reading a
    just-created file and does not queue/replay events raised while
    disabled, so one file per frame risks losing any frame written during
    that window. SEQUENCE + WATCH_FOLDER sends must accumulate and be
    written as a single file via flush_sequence() instead."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        self.directory = pathlib.Path(self._tmpdir.name)

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_sequence_frames_are_buffered_not_written_immediately(self):
        svc = PdiService(watch_folder=self.directory)
        svc.send(_one_profile("frame1"), trigger=Trigger.SEQUENCE, transport=Transport.WATCH_FOLDER)
        svc.send(_one_profile("frame2"), trigger=Trigger.SEQUENCE, transport=Transport.WATCH_FOLDER)
        self.assertEqual(list(self.directory.glob("*.pdi")), [])
        self.assertEqual(len(svc._sequence_watch_folder_buffer), 2)

    def test_flush_writes_every_buffered_frame_as_one_file(self):
        import xml.etree.ElementTree as ET

        svc = PdiService(watch_folder=self.directory)
        events = []
        svc.finished.connect(lambda ok, msg, transport: events.append((ok, msg, transport)))
        for i in range(3):
            svc.send(_one_profile(f"frame{i}"), trigger=Trigger.SEQUENCE, transport=Transport.WATCH_FOLDER)
        svc.flush_sequence(Transport.WATCH_FOLDER)

        files = list(self.directory.glob("*.pdi"))
        self.assertEqual(len(files), 1, "all buffered frames must land in exactly one file")
        root = ET.parse(files[0]).getroot()
        self.assertEqual(len(root.findall("DiffractionProfile")), 3)
        self.assertEqual(events, [(True, "wrote .pdi file", Transport.WATCH_FOLDER)])
        self.assertEqual(svc._sequence_watch_folder_buffer, [])

    def test_flush_is_a_no_op_for_clipboard(self):
        svc = PdiService(watch_folder=self.directory)
        svc.send(_one_profile("frame1"), trigger=Trigger.SEQUENCE, transport=Transport.WATCH_FOLDER)
        events = []
        svc.finished.connect(lambda ok, msg, transport: events.append((ok, msg, transport)))
        svc.flush_sequence(Transport.CLIPBOARD)
        self.assertEqual(events, [])
        self.assertEqual(len(svc._sequence_watch_folder_buffer), 1)  # still buffered

    def test_flush_with_nothing_buffered_is_a_no_op(self):
        svc = PdiService(watch_folder=self.directory)
        events = []
        svc.finished.connect(lambda ok, msg, transport: events.append((ok, msg, transport)))
        svc.flush_sequence(Transport.WATCH_FOLDER)
        self.assertEqual(events, [])
        self.assertEqual(list(self.directory.glob("*.pdi")), [])

    def test_shutdown_clears_buffered_frames(self):
        svc = PdiService(watch_folder=self.directory)
        svc.send(_one_profile("frame1"), trigger=Trigger.SEQUENCE, transport=Transport.WATCH_FOLDER)
        svc.shutdown()
        self.assertEqual(svc._sequence_watch_folder_buffer, [])

    def test_clipboard_sequence_frames_still_use_the_per_frame_fifo(self):
        # Only WATCH_FOLDER batches — CLIPBOARD's existing one-at-a-time
        # queue is unaffected (Windows queues WM_DRAWCLIPBOARD messages
        # itself, so this transport doesn't have the same failure mode).
        svc = PdiService()
        started = []
        svc._start_clipboard_send = lambda item: (started.append(item.profiles[0].name), setattr(svc, "_busy", True))
        svc.send(_one_profile("frame1"), trigger=Trigger.SEQUENCE, transport=Transport.CLIPBOARD)
        svc.send(_one_profile("frame2"), trigger=Trigger.SEQUENCE, transport=Transport.CLIPBOARD)
        self.assertEqual(started, ["frame1"])
        self.assertEqual(len(svc._sequence_queue), 1)


class PdiServiceApiShapeTests(unittest.TestCase):
    """transport must be a required per-call argument, not shared state on
    the service — see code review 2026-09-06 (issue: one window's UI
    refresh could silently change which transport another window's next
    send used, because transport used to be a mutable attribute here)."""

    def test_no_shared_transport_attribute(self):
        svc = PdiService()
        self.assertFalse(hasattr(svc, "transport"))

    def test_send_requires_transport_kwarg(self):
        svc = PdiService()
        with self.assertRaises(TypeError):
            svc.send(_one_profile("a"), trigger=Trigger.MANUAL)  # type: ignore[call-arg]


class SequenceQueueConcurrencyTests(unittest.TestCase):
    """Regression for the FIFO double-pump bug: an earlier version
    advanced the sequence queue from two places on the same completion (a
    per-send on_done callback, and an unconditional call at the end of
    cleanup) — starting two sequence frames concurrently and clobbering
    each other's QProcess/kill-timer bookkeeping. _advance_queue is now
    the only place that starts the next item, and is a no-op while a send
    is already in flight."""

    def test_advance_queue_is_a_no_op_while_busy(self):
        svc = PdiService()
        started: list[str] = []

        def fake_start(item):
            started.append(item.profiles[0].name)
            svc._busy = True  # what the real _start_clipboard_send does

        svc._start_clipboard_send = fake_start

        svc.send(_one_profile("A"), trigger=Trigger.SEQUENCE, transport=Transport.CLIPBOARD)
        svc.send(_one_profile("B"), trigger=Trigger.SEQUENCE, transport=Transport.CLIPBOARD)
        svc.send(_one_profile("C"), trigger=Trigger.SEQUENCE, transport=Transport.CLIPBOARD)

        # Only the first item starts immediately; B and C wait in the queue.
        self.assertEqual(started, ["A"])
        self.assertEqual(len(svc._sequence_queue), 2)

        # Simulate A's completion the way _on_clipboard_send_done does:
        # clear _busy, then advance exactly once.
        svc._busy = False
        svc._advance_queue()
        self.assertEqual(started, ["A", "B"])
        self.assertEqual(len(svc._sequence_queue), 1)
        self.assertTrue(svc._busy)  # fake_start put it back to True for B

        # This is the exact shape of the old bug: something calls
        # _advance_queue again before B (still in flight) has finished.
        # It must be a no-op, not start C too.
        svc._advance_queue()
        self.assertEqual(started, ["A", "B"])
        self.assertEqual(len(svc._sequence_queue), 1)

    def test_pending_single_shot_takes_priority_over_sequence_queue(self):
        svc = PdiService()
        started: list[str] = []
        svc._start_clipboard_send = lambda item: (started.append(item.profiles[0].name), setattr(svc, "_busy", True))

        svc.send(_one_profile("seq-A"), trigger=Trigger.SEQUENCE, transport=Transport.CLIPBOARD)
        svc.send(_one_profile("manual-B"), trigger=Trigger.MANUAL, transport=Transport.CLIPBOARD)
        self.assertEqual(started, ["seq-A"])

        svc._busy = False
        svc._advance_queue()
        self.assertEqual(started, ["seq-A", "manual-B"])


class FailedToStartTests(unittest.TestCase):
    """Regression for the "busy forever" bug: an earlier version keyed
    cleanup off proc.state()==NotRunning, which is also true for a
    FailedToStart error — but QProcess never emits finished() after
    FailedToStart, so that guard skipped the one case that actually needed
    cleanup here, leaving the service permanently busy after any failed
    launch (bad exe path, non-executable file, ...)."""

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_nonexistent_exe_clears_busy_and_reports_failure(self):
        svc = PdiService()
        svc.clipboard_available = lambda: True
        orig_exe = pdi_service_mod._EXE_PATH
        pdi_service_mod._EXE_PATH = pathlib.Path("/this/path/does/not/exist/PdiSender.exe")
        try:
            events = []
            svc.finished.connect(lambda ok, msg, transport: events.append((ok, msg, transport)))
            svc.send(_one_profile("x"), trigger=Trigger.MANUAL, transport=Transport.CLIPBOARD)

            deadline = time.time() + 5
            while not events and time.time() < deadline:
                self.app.processEvents()

            self.assertEqual(len(events), 1, "finished() must fire exactly once for a failed launch")
            ok, msg, transport = events[0]
            self.assertFalse(ok)
            self.assertIn("failed to start", msg)
            self.assertIs(transport, Transport.CLIPBOARD)
            self.assertFalse(svc._busy, "service must not be stuck busy after a failed launch")
            self.assertIsNone(svc._process)
        finally:
            pdi_service_mod._EXE_PATH = orig_exe
            svc.shutdown()


class PdindexerRunningAsyncTests(unittest.TestCase):
    """Regression for blocking the GUI thread: an earlier version of this
    probe used QProcess.waitForFinished(3000) synchronously, called from a
    signal handler that runs on the GUI thread on every completed
    clipboard send — freezing the whole app's UI for however long the
    (self-contained, single-file) helper takes just to start."""

    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_callback_fires_exactly_once_and_never_blocks(self):
        svc = PdiService()
        svc.clipboard_available = lambda: True
        orig_exe = pdi_service_mod._EXE_PATH
        pdi_service_mod._EXE_PATH = pathlib.Path("/this/path/does/not/exist/PdiSender.exe")
        try:
            results = []
            start = time.time()
            svc.pdindexer_running_async(results.append)
            call_returned_immediately = (time.time() - start) < 1.0

            deadline = time.time() + 5
            while not results and time.time() < deadline:
                self.app.processEvents()

            self.assertTrue(call_returned_immediately, "pdindexer_running_async must not block its caller")
            self.assertEqual(results, [False])
        finally:
            pdi_service_mod._EXE_PATH = orig_exe

    def test_unavailable_on_this_platform_calls_back_false_synchronously(self):
        svc = PdiService()
        svc.clipboard_available = lambda: False
        results = []
        svc.pdindexer_running_async(results.append)
        self.assertEqual(results, [False])


if __name__ == "__main__":
    unittest.main()
