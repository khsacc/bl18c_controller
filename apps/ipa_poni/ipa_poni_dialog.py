"""Qt dialog for converting IPAnalyzer .prm files to pyFAI .poni format."""

from __future__ import annotations

import os
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

_this_dir = os.path.dirname(os.path.abspath(__file__))
_apps_dir = os.path.dirname(_this_dir)
_root_dir = os.path.dirname(_apps_dir)
for _p in (_root_dir, _apps_dir):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from apps.ipa_poni.ipa_to_poni import (
    IpaPrmParams, PoniParams,
    convert_prm_to_poni, ipa_to_poni, parse_ipa_prm,
)
from settings.i18n import tr

_LOCALDATA = os.path.join(_this_dir, "__localdata")


def _load_last_dir(key: str) -> str:
    path = os.path.join(_LOCALDATA, f"{key}_last_dir.txt")
    if os.path.exists(path):
        d = open(path).read().strip()
        if os.path.isdir(d):
            return d
    return os.path.expanduser("~")


def _save_last_dir(key: str, directory: str) -> None:
    os.makedirs(_LOCALDATA, exist_ok=True)
    with open(os.path.join(_LOCALDATA, f"{key}_last_dir.txt"), "w") as f:
        f.write(directory)


def _ro_field(text: str = "") -> QLineEdit:
    w = QLineEdit(text)
    w.setReadOnly(True)
    w.setStyleSheet("background: #F5F5F5; color: #333;")
    return w


class IpaPoniDialog(QDialog):
    """Dialog that converts an IPA .prm file to a pyFAI .poni file."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("IPA .prm → pyFAI .poni Converter"))
        self.resize(900, 0)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)

        self._prm: IpaPrmParams | None = None
        self._poni: PoniParams | None = None

        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(8)

        # ── Input file ──────────────────────────────────────────────────
        in_group = QGroupBox(tr("Input: IPA .prm file"))
        in_layout = QHBoxLayout(in_group)
        self._prm_path_edit = QLineEdit()
        self._prm_path_edit.setPlaceholderText(tr("Select a .prm file…"))
        self._prm_path_edit.setReadOnly(True)
        in_browse = QPushButton(tr("Browse…"))
        in_browse.clicked.connect(self._browse_prm)
        in_layout.addWidget(self._prm_path_edit, 1)
        in_layout.addWidget(in_browse)
        root.addWidget(in_group)

        # ── IPA parameters + computed poni parameters (side by side) ────
        cols_layout = QHBoxLayout()
        cols_layout.setSpacing(8)

        ipa_group = QGroupBox(tr("IPA Parameters (from .prm)"))
        ipa_form = QFormLayout(ipa_group)
        ipa_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._f_cl1 = _ro_field()
        self._f_cl2 = _ro_field()
        self._f_ds = _ro_field()
        self._f_foot = _ro_field()
        self._f_pix = _ro_field()
        self._f_phi = _ro_field()
        self._f_tau = _ro_field()
        self._f_wav = _ro_field()
        self._f_ksi = _ro_field()

        ipa_form.addRow(tr("CameraLength1:"), self._f_cl1)
        ipa_form.addRow(tr("CameraLength2:"), self._f_cl2)
        ipa_form.addRow(tr("DirectSpot (X, Y):"), self._f_ds)
        ipa_form.addRow(tr("Foot (X, Y):"), self._f_foot)
        ipa_form.addRow(tr("PixSize (X, Y):"), self._f_pix)
        ipa_form.addRow(tr("TiltPhi:"), self._f_phi)
        ipa_form.addRow(tr("TiltTau:"), self._f_tau)
        ipa_form.addRow(tr("Wavelength:"), self._f_wav)
        ipa_form.addRow(tr("PixKsi (skew):"), self._f_ksi)
        cols_layout.addWidget(ipa_group, 1)

        poni_group = QGroupBox(tr("Computed pyFAI poni Parameters"))
        poni_form = QFormLayout(poni_group)
        poni_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._p_dist = _ro_field()
        self._p_p1 = _ro_field()
        self._p_p2 = _ro_field()
        self._p_rot1 = _ro_field()
        self._p_rot2 = _ro_field()
        self._p_rot3 = _ro_field()
        self._p_pix1 = _ro_field()
        self._p_pix2 = _ro_field()
        self._p_wav = _ro_field()

        poni_form.addRow(tr("Distance:"), self._p_dist)
        poni_form.addRow(tr("Poni1:"), self._p_p1)
        poni_form.addRow(tr("Poni2:"), self._p_p2)
        poni_form.addRow(tr("Rot1:"), self._p_rot1)
        poni_form.addRow(tr("Rot2:"), self._p_rot2)
        poni_form.addRow(tr("Rot3:"), self._p_rot3)
        poni_form.addRow(tr("PixelSize1 (axis1/Y):"), self._p_pix1)
        poni_form.addRow(tr("PixelSize2 (axis2/X):"), self._p_pix2)
        poni_form.addRow(tr("Wavelength:"), self._p_wav)
        cols_layout.addWidget(poni_group, 1)

        root.addLayout(cols_layout)

        # ── Note ────────────────────────────────────────────────────────
        note = QLabel(
            tr("Note: PixKsi (pixel skew angle) is not representable in poni format "
               "and is ignored. For detectors with significant skew, use pyFAI spline correction.")
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #666; font-style: italic; font-size: 11px;")
        root.addWidget(note)

        # ── Output file ─────────────────────────────────────────────────
        out_group = QGroupBox(tr("Output: pyFAI .poni file"))
        out_layout = QHBoxLayout(out_group)
        self._poni_path_edit = QLineEdit()
        self._poni_path_edit.setPlaceholderText(tr("Select output path…"))
        self._poni_path_edit.setReadOnly(True)
        out_browse = QPushButton(tr("Browse…"))
        out_browse.clicked.connect(self._browse_poni)
        out_layout.addWidget(self._poni_path_edit, 1)
        out_layout.addWidget(out_browse)
        root.addWidget(out_group)

        # ── Save button ──────────────────────────────────────────────────
        self._save_btn = QPushButton(tr("Save .poni File"))
        self._save_btn.setEnabled(False)
        self._save_btn.setFixedHeight(36)
        self._save_btn.clicked.connect(self._do_save)
        root.addWidget(self._save_btn)

    # ── Slots ─────────────────────────────────────────────────────────────

    def _browse_prm(self) -> None:
        start = _load_last_dir("ipa_prm")
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Open IPA .prm file"), start, "IPA parameter files (*.prm);;All files (*)"
        )
        if not path:
            return
        _save_last_dir("ipa_prm", os.path.dirname(path))
        self._load_prm(path)

    def _browse_poni(self) -> None:
        if self._prm is not None:
            default_dir = _load_last_dir("poni_out")
            prm_name = os.path.splitext(os.path.basename(self._prm_path_edit.text()))[0]
            default_path = os.path.join(default_dir, prm_name + ".poni")
        else:
            default_path = _load_last_dir("poni_out")
        path, _ = QFileDialog.getSaveFileName(
            self, tr("Save pyFAI .poni file"), default_path,
            "pyFAI poni files (*.poni);;All files (*)"
        )
        if not path:
            return
        _save_last_dir("poni_out", os.path.dirname(path))
        self._poni_path_edit.setText(path)
        self._update_save_btn()

    def _load_prm(self, path: str) -> None:
        try:
            prm = parse_ipa_prm(path)
        except Exception as exc:
            QMessageBox.critical(self, tr("Error"), tr("Could not parse .prm file:\n{error}", error=exc))
            return

        self._prm = prm
        self._poni = ipa_to_poni(prm)
        self._prm_path_edit.setText(path)

        # Suggest output path next to input
        out_dir = os.path.dirname(path)
        stem = os.path.splitext(os.path.basename(path))[0]
        suggested = os.path.join(out_dir, stem + ".poni")
        self._poni_path_edit.setText(suggested)
        _save_last_dir("poni_out", out_dir)

        self._fill_ipa_fields(prm)
        self._fill_poni_fields(self._poni)
        self._update_save_btn()

    def _fill_ipa_fields(self, p: IpaPrmParams) -> None:
        self._f_cl1.setText(f"{p.camera_length_1:.6f} mm")
        self._f_cl2.setText(f"{p.camera_length_2:.6f} mm")
        self._f_ds.setText(f"({p.direct_spot_x:.4f}, {p.direct_spot_y:.4f}) px")
        self._f_foot.setText(f"({p.foot_x:.4f}, {p.foot_y:.4f}) px")
        self._f_pix.setText(f"({p.pix_size_x:.7f}, {p.pix_size_y:.7f}) mm")
        self._f_phi.setText(f"{p.tilt_phi:.6f}°")
        self._f_tau.setText(f"{p.tilt_tau:.6f}°")
        self._f_wav.setText(f"{p.wavelength:.9f} Å")
        self._f_ksi.setText(tr("{value:.6f}°  (ignored)", value=p.pix_ksi))

    def _fill_poni_fields(self, p: PoniParams) -> None:
        self._p_dist.setText(f"{p.distance:.9e} m")
        self._p_p1.setText(f"{p.poni1:.9e} m")
        self._p_p2.setText(f"{p.poni2:.9e} m")
        self._p_rot1.setText(f"{p.rot1:.9e} rad")
        self._p_rot2.setText(f"{p.rot2:.9e} rad")
        self._p_rot3.setText(f"{p.rot3:.9e} rad")
        self._p_pix1.setText(f"{p.pixel_size_1:.9e} m")
        self._p_pix2.setText(f"{p.pixel_size_2:.9e} m")
        self._p_wav.setText(f"{p.wavelength:.9e} m")

    def _update_save_btn(self) -> None:
        self._save_btn.setEnabled(
            self._prm is not None and bool(self._poni_path_edit.text())
        )

    def _do_save(self) -> None:
        if self._prm is None or self._poni is None:
            return
        out_path = self._poni_path_edit.text()
        if not out_path:
            return
        try:
            from apps.ipa_poni.ipa_to_poni import write_poni
            write_poni(self._poni, out_path, source_path=self._prm_path_edit.text())
        except Exception as exc:
            QMessageBox.critical(self, tr("Error"), tr("Failed to save .poni file:\n{error}", error=exc))
            return
        QMessageBox.information(self, tr("Saved"), tr("Saved:\n{path}", path=out_path))


# ── Standalone execution ────────────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    dlg = IpaPoniDialog()
    dlg.show()
    sys.exit(app.exec())
