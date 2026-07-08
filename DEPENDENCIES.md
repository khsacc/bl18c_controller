# Dependencies — `python main.py`

## Python version

Python **3.13** (see [`.python-version`](.python-version)).

---

## Python packages (pip)

Install with:

```bash
pip install PyQt6 opencv-python numpy pyserial matplotlib pyqtgraph lakeshore pyvisa tifffile
```

NI-VISAドライバなしで使う場合は追加で：

```bash
pip install pyvisa-py
```

| Package | `import` name | Used by | Purpose |
|---|---|---|---|
| `PyQt6` | `PyQt6` | all modules | GUI framework |
| `opencv-python` | `cv2` | `interactive_camera.py`, `radicon_ui.py` | Camera feed, image processing, template matching |
| `numpy` | `numpy` | `interactive_camera.py`, `radicon_backend.py`, `radicon_ui.py` | Array operations, image data |
| `pyserial` | `serial` | `pace5000_backend.py` | PACE5000 serial (COM) connection |
| `matplotlib` | `matplotlib` | `lakeshore335_app.py` | Temperature history plot |
| `pyvisa` | `pyvisa` | `main.py` | 起動時のGPIB機器スキャン（省略可） |
| `tifffile` | `tifffile` | `radicon_ui.py` | Rad-icon 2022 TIFFへのメタデータ埋め込み・読み出し |

---

## System / runtime dependencies

These cannot be installed via pip.

### Rad-icon 2022 (optional sub-app only)

| Dependency | Notes |
|---|---|
| **Sapera LT runtime** (`SapClassBasic86.dll`) | Installed by the Teledyne DALSA Sapera installer into `C:\Windows\System32`. Must be on `PATH`. |
| **`radicon_dll.dll`** | Built locally from [`dll/radicon_dll.cpp`](apps/Rad_icon_2022/dll/radicon_dll.cpp). Run `dll/build.bat` before first use. The built DLL is expected at `apps/Rad_icon_2022/dll/Release/radicon_dll.dll`. |
| **Xtium-CL MX4 frame grabber** | Physical PCIe card required for camera acquisition. |

---

## Hardware

| Device | Connection | Required? | Notes |
|---|---|---|---|
| **PM16C stepping motor controller** | TCP `192.168.1.55:7777` | No — use `--debug` to simulate | Stage motion for all sub-apps |
| **USB camera** | USB (OpenCV device index 0) | No | Interactive camera sub-app |
| **Druck PACE5000** | TCP (default `192.168.1.100:5025`) or serial COM | Optional | Enable via checkbox in UI |
| **LakeShore 335** | Serial COM (default `COM5`) | Optional | Enable via checkbox in UI |
| **Rad-icon 2022 detector** | Via Xtium-CL MX4 / Sapera | Optional | Enable via checkbox in UI |
| **GPIB機器（光電子増倍管・イオンチェンバー等）** | GPIB-USB変換アダプタ | Optional | 起動時に自動スキャン・一覧表示。NI-488.2またはAgilent I/O Librariesが別途必要 |

---

