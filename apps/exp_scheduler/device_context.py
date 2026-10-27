from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from utils.stage.control_stage import PM16CController
    from utils.stage.control_stage_sim import PM16CControllerSim
    from apps.PACE5000.pace5000_backend import Pace5000Backend
    from apps.LakeShore335.lakeshore335_backend import LakeShore335Backend
    from apps.Rad_icon_2022.radicon_backend import RadiconBackend
    from utils.shutter import SharedShutterController, ShutterController, ShutterControllerSim
    from settings.poni_state import PoniState
    from apps.interactive_camera.camera_session import CameraSession


@dataclass
class DeviceContext:
    """
    All device backends passed to ExperimentalSchedulerWindow and SequenceRunner.
    The launcher injects its shared microscope capture when available.
    Standalone scheduler use retains the existing local capture fallback.
    """
    controller: "PM16CController | PM16CControllerSim | None" = None
    pace5000: "Pace5000Backend | None" = None
    lakeshore: "LakeShore335Backend | None" = None
    radicon: "RadiconBackend | None" = None
    shutter: "ShutterController | SharedShutterController | ShutterControllerSim | None" = None
    poni_state: "PoniState | None" = None
    camera: "CameraSession | None" = None
