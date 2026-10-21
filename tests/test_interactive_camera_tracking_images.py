import threading
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from apps.interactive_camera.interactive_camera import MainWindow


class _Capture:
    def __init__(self):
        self.read_count = 0

    def read(self):
        self.read_count += 1
        return True, np.full((8, 8, 3), self.read_count * 10, dtype=np.uint8)


@pytest.mark.parametrize(
    "similarity, calibrated, expected_pixel, expected_spectra",
    [
        (0.99, True, 20, 1),
        (0.10, True, 50, 0),
        (0.10, False, 10, 0),
    ],
)
def test_tracking_saves_one_image_even_when_similarity_fails(
        tmp_path, similarity, calibrated, expected_pixel, expected_spectra):
    capture = _Capture()
    spectra = []
    messages = []
    spinbox = SimpleNamespace(value=lambda: 0.95)
    lower_limit = SimpleNamespace(value=lambda: -1)
    upper_limit = SimpleNamespace(value=lambda: 1)
    tracker = SimpleNamespace(
        is_following_running=False,
        is_following=True,
        current_frame=np.zeros((8, 8, 3), dtype=np.uint8),
        _tracking_lease=object(),
        controller=SimpleNamespace(switch_to_rem=lambda **kwargs: None),
        autofocus=SimpleNamespace(focus_range=0),
        calibration_data={'matrix_inv': [[1, 0], [0, 1]]} if calibrated else None,
        cap=capture,
        _cap_lock=threading.Lock(),
        reference_frame=np.zeros((8, 8, 3), dtype=np.uint8),
        _compute_xy_shift=lambda *args: (0, 0),
        _compute_similarity=lambda *args: similarity,
        follow_similarity_spinbox=spinbox,
        limit_per_attempt_ch4=upper_limit,
        limit_per_attempt_ch5=upper_limit,
        limit_total_min_ch3=lower_limit,
        limit_total_max_ch3=upper_limit,
        limit_total_min_ch4=lower_limit,
        limit_total_max_ch4=upper_limit,
        limit_total_min_ch5=lower_limit,
        limit_total_max_ch5=upper_limit,
        follow_cumulative={3: 0, 4: 0, 5: 0},
        tracking_images_dir=str(tmp_path),
        tracking_image_counter=0,
        _tracking_save_images=True,
        _tracking_collect_ruby_spectrum=True,
        _add_information_band=lambda frame: frame,
        _start_ruby_spectrum_collection=lambda *args: spectra.append(args),
        _tracking_log_signal=SimpleNamespace(emit=messages.append),
        tracking_csv_writer=None,
        tracking_start_time=None,
        _tracking_sample_env_keys=[],
    )

    MainWindow._follow_task(tracker)

    images = list(tmp_path.glob('frame_*.png'))
    assert len(images) == 1, messages
    assert np.all(cv2.imread(str(images[0])) == expected_pixel)
    assert tracker.tracking_image_counter == 1
    assert len(spectra) == expected_spectra
    assert not tracker.is_following_running
