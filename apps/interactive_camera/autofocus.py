import contextlib
import cv2
import numpy as np
import threading

try:
    from scipy.optimize import curve_fit as _scipy_curve_fit
    _SCIPY_AVAILABLE = True
except ImportError:
    _SCIPY_AVAILABLE = False


def _gaussian(x, a, mu, sigma, offset):
    return a * np.exp(-0.5 * ((x - mu) / sigma) ** 2) + offset


class AutoFocus:
    def __init__(self, controller, cap, focus_range=10, step_size=1,
                 completion_callback=None, method='laplacian', n_frames=1,
                 peak_method='highest', roi=None, channel=3, cap_lock=None):
        """
        Args:
            controller: Motor controller instance
            cap: OpenCV VideoCapture object
            focus_range: Scan half-range in pulses (start = current - focus_range)
            step_size: Step size in pulses
            completion_callback: Called with (sharpness_data, best_pos, best_sharpness, fit_result)
            method: Sharpness metric — 'laplacian' or 'tenengrad'
            n_frames: Number of frames to average per position (1 = no averaging)
            peak_method: How to find the best position — 'highest' or 'gaussian'
            channel: Controller channel used as the focus axis (default 3)
            cap_lock: threading.Lock shared with the caller to serialize access
                to `cap` across threads (falls back to a no-op if omitted)
        """
        self.controller = controller
        self.cap = cap
        self._cap_lock = cap_lock if cap_lock is not None else contextlib.nullcontext()
        self.focus_range = focus_range
        self.step_size = step_size
        self.is_focusing = False
        self.focus_thread = None
        self.completion_callback = completion_callback
        self.method = method
        self.n_frames = n_frames
        self.peak_method = peak_method
        self.roi = roi  # {'cx': int, 'cy': int, 'r': int} or None
        self.channel = channel

        print(self.focus_range, self.step_size)

    def calculate_sharpness(self, frame):
        """Compute sharpness of a single frame using the configured method."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if len(frame.shape) == 3 else frame

        if self.roi is not None:
            cx, cy, r = self.roi['cx'], self.roi['cy'], self.roi['r']
            x1, y1 = max(0, cx - r), max(0, cy - r)
            x2, y2 = min(gray.shape[1], cx + r), min(gray.shape[0], cy + r)
            gray_crop = gray[y1:y2, x1:x2]
            if gray_crop.size == 0:
                return 0.0
            mask = np.zeros(gray_crop.shape, dtype=np.uint8)
            cv2.circle(mask, (cx - x1, cy - y1), r, 255, -1)
            mask_bool = mask > 0
            if not mask_bool.any():
                return 0.0
            if self.method == 'tenengrad':
                gx = cv2.Sobel(gray_crop, cv2.CV_64F, 1, 0, ksize=3)
                gy = cv2.Sobel(gray_crop, cv2.CV_64F, 0, 1, ksize=3)
                return float(np.mean((gx ** 2 + gy ** 2)[mask_bool]))
            else:
                lap = cv2.Laplacian(gray_crop, cv2.CV_64F)
                vals = lap[mask_bool]
                return float(np.var(vals)) if vals.size > 0 else 0.0

        if self.method == 'tenengrad':
            gx = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
            gy = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
            return float(np.mean(gx ** 2 + gy ** 2))
        else:  # laplacian (default)
            return cv2.Laplacian(gray, cv2.CV_64F).var()

    def measure_sharpness(self):
        """Capture n_frames from cap and return the averaged sharpness."""
        values = []
        for _ in range(max(1, self.n_frames)):
            with self._cap_lock:
                ret, frame = self.cap.read()
            if ret:
                values.append(self.calculate_sharpness(frame))
        return float(np.mean(values)) if values else 0.0

    def _find_best_position(self, sharpness_data):
        """
        Determine the best focus position from sharpness_data.

        Returns (best_pos, best_sharpness, fit_result).
        fit_result is a dict with at least {'method': ..., 'success': bool}.
        """
        positions = np.array([p for p, _ in sharpness_data])
        sharpnesses = np.array([s for _, s in sharpness_data])
        idx_max = int(np.argmax(sharpnesses))
        fallback_pos = int(positions[idx_max])
        fallback_sharpness = float(sharpnesses[idx_max])

        if self.peak_method != 'gaussian':
            return fallback_pos, fallback_sharpness, {'method': 'highest', 'success': True}

        # ---- Gaussian fitting ----
        if not _SCIPY_AVAILABLE:
            print("Warning: scipy not available — falling back to highest sharpness.")
            return fallback_pos, fallback_sharpness, {
                'method': 'gaussian', 'success': False,
                'error': 'scipy not available',
            }

        try:
            a0 = float(sharpnesses[idx_max] - np.min(sharpnesses))
            mu0 = float(positions[idx_max])
            sigma0 = float((positions[-1] - positions[0]) / 4) or 1.0
            offset0 = float(np.min(sharpnesses))

            scan_span = float(positions[-1] - positions[0]) or 1.0

            popt, pcov = _scipy_curve_fit(
                _gaussian, positions, sharpnesses,
                p0=[a0, mu0, sigma0, offset0],
                maxfev=10000,
            )
            a, mu, sigma, offset = popt

            # --- Divergence guards ---
            if not (positions[0] <= mu <= positions[-1]):
                raise ValueError(
                    f"Peak mu={mu:.1f} outside scan range [{positions[0]}, {positions[-1]}]"
                )
            if a <= 0:
                raise ValueError(f"Non-positive amplitude a={a:.4f} (inverted Gaussian)")
            abs_sigma = abs(sigma)
            if abs_sigma < self.step_size:
                raise ValueError(
                    f"sigma={abs_sigma:.2f} < step_size={self.step_size} (fitting noise spike)"
                )
            if abs_sigma > scan_span:
                raise ValueError(
                    f"sigma={abs_sigma:.2f} > scan span={scan_span:.0f} (essentially flat, no clear peak)"
                )

            fitted = _gaussian(positions, *popt)
            ss_res = float(np.sum((sharpnesses - fitted) ** 2))
            ss_tot = float(np.sum((sharpnesses - np.mean(sharpnesses)) ** 2))
            r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

            best_pos = int(round(mu))
            best_sharpness = float(_gaussian(np.array([mu]), *popt)[0])

            fit_result = {
                'method': 'gaussian', 'success': True,
                'a': float(a), 'mu': float(mu), 'sigma': float(sigma),
                'offset': float(offset), 'r2': r2,
                'positions': positions, 'sharpnesses': sharpnesses, 'popt': popt,
            }
            print(f"  Gaussian fit: mu={mu:.2f}, sigma={abs(sigma):.2f}, R2={r2:.4f}")
            return best_pos, best_sharpness, fit_result

        except Exception as exc:
            print(f"Gaussian fitting failed ({exc}), falling back to highest sharpness.")
            return fallback_pos, fallback_sharpness, {
                'method': 'gaussian', 'success': False, 'error': str(exc),
                'positions': positions, 'sharpnesses': sharpnesses,
            }

    def get_current_focus_position(self):
        """Get current position of the focus channel."""
        pos = self.controller.get_ch_pos(self.channel)
        return int(pos) if pos is not None else None

    def move_focus_to(self, position):
        """Move focus channel to absolute position."""
        self.controller.move_ch_absolute(self.channel, position)

    def perform_autofocus(self, callback=None):
        """
        Scan Ch3 through focus_range and move to the best-focus position.

        Args:
            callback: Optional per-step callback (position, sharpness)
        """
        if self.is_focusing:
            print("Auto-focus already in progress")
            return False

        self.is_focusing = True

        def focus_routine():
            try:
                current_pos = self.get_current_focus_position()
                if current_pos is None:
                    print("Error: Could not read current focus position")
                    return

                print(f"Starting auto-focus (Ch{self.channel}) from position {current_pos}")

                start_pos = current_pos - self.focus_range
                end_pos = current_pos + self.focus_range
                sharpness_data = []

                print(f"Moving to start position: {start_pos}")
                self.move_focus_to(start_pos)
                self.controller.wait_until_stop(stay_in_rem=True)

                current_scan_pos = start_pos
                while current_scan_pos <= end_pos and self.is_focusing:
                    sharpness = self.measure_sharpness()
                    sharpness_data.append((current_scan_pos, sharpness))

                    if callback:
                        callback(current_scan_pos, sharpness)

                    print(f"  pos={current_scan_pos}, sharpness={sharpness:.3f}")

                    if current_scan_pos < end_pos:
                        next_pos = min(current_scan_pos + self.step_size, end_pos)
                        self.move_focus_to(next_pos)
                        self.controller.wait_until_stop(stay_in_rem=True)

                    current_scan_pos += self.step_size

                if not self.is_focusing:
                    print("Auto-focus cancelled")
                    self.controller.switch_to_loc()
                    return

                if sharpness_data:
                    best_pos, best_sharpness, fit_result = self._find_best_position(sharpness_data)
                    best_pos = max(start_pos, min(end_pos, best_pos))
                    print(f"Moving to best focus position: {best_pos}")
                    self.move_focus_to(best_pos)
                    self.controller.wait_until_stop()  # switches to LOC on completion
                    print("Auto-focus completed successfully")

                    if self.completion_callback:
                        self.completion_callback(sharpness_data, best_pos, best_sharpness, fit_result)
                else:
                    print("Error: No sharpness data collected")
                    self.controller.switch_to_loc()

            except Exception as e:
                print(f"Error during auto-focus: {e}")
                try:
                    self.controller.switch_to_loc()
                except Exception:
                    pass
            finally:
                self.is_focusing = False
                self.roi = None

        self.focus_thread = threading.Thread(target=focus_routine)
        self.focus_thread.daemon = True
        self.focus_thread.start()
        return True

    def stop_autofocus(self):
        """Stop ongoing auto-focus operation."""
        if self.is_focusing:
            self.is_focusing = False
            print("Auto-focus stopped")
            return True
        return False

    def is_autofocusing(self):
        """Check if auto-focus is currently running."""
        return self.is_focusing
