"""Pixel-defect coordinates must follow the displayed detector frame."""

import io
import unittest
from unittest import mock

import numpy as np

from apps.Rad_icon_2022.image_utils import parse_defect_file
from apps.Rad_icon_2022.radicon_ui import RadiconWindow


class _Toggle:
    def __init__(self, checked):
        self._checked = checked

    def isChecked(self):
        return self._checked


class _Kernel:
    def currentText(self):
        return "3×3"


class _DefectSettings:
    _apply_flip = RadiconWindow._apply_flip
    _defect_correct = RadiconWindow._defect_correct

    def __init__(self, mask, flip_v, flip_h, mode):
        self._defect_mask = mask
        self._flip_v_chk = _Toggle(flip_v)
        self._flip_h_chk = _Toggle(flip_h)
        self._defect_kernel_combo = _Kernel()
        self._mode = mode

    def _current_defect_mode(self):
        return self._mode


class DefectFlipTests(unittest.TestCase):
    def test_xfpcap_defect_columns_follow_the_backend_crop(self):
        contents = "$defect\nC,1032 0-3\nR,1547 1032-1035\nP,1119,10\n"
        with mock.patch("builtins.open", return_value=io.StringIO(contents)):
            defects = parse_defect_file("defects.txt", "2x2", 4, 1032, 1118)

        # XFPCAP01 crops 2x2 at column 3; our backend crops at column 4.
        self.assertEqual(defects, {
            (0, 515), (1, 515), (773, 515), (773, 516), (5, 558),
        })

    def test_unbinned_coordinates_use_the_xfpcap_crop_origin(self):
        contents = "$defect\nC,1032 0-1\nP,1119,10\n"
        with mock.patch("builtins.open", return_value=io.StringIO(contents)):
            defects = parse_defect_file("defects.txt", "1x1", 4, 2064, 2236)

        self.assertEqual(defects, {(0, 1035), (1, 1035), (10, 1122)})

    def test_defect_mask_follows_frame_flip_for_both_correction_modes(self):
        raw = np.full((5, 5), 10, dtype=np.uint16)
        raw[0, 1] = 1000  # Defective sensor pixel.
        raw[4, 3] = 777   # Valid signal at the opposite image position.
        mask = np.zeros(raw.shape, dtype=bool)
        mask[0, 1] = True

        for flip_v in (False, True):
            for flip_h in (False, True):
                for mode, replacement in (("median", 10), ("neg1", 65535)):
                    with self.subTest(flip_v=flip_v, flip_h=flip_h, mode=mode):
                        settings = _DefectSettings(mask, flip_v, flip_h, mode)
                        frame = settings._apply_flip(raw)
                        expected = frame.copy()
                        expected[settings._apply_flip(mask)] = replacement

                        corrected, warning = settings._defect_correct(frame)

                        self.assertEqual(warning, "")
                        np.testing.assert_array_equal(corrected, expected)
                        self.assertTrue(mask[0, 1])  # Original mask stays unflipped.


if __name__ == "__main__":
    unittest.main()
