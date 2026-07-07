from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from utils.control_stage import PM16CController
    from utils.control_stage_sim import PM16CControllerSim
    from apps.PACE5000.pace5000_backend import Pace5000Backend
    from apps.LakeShore335.lakeshore335_backend import LakeShore335Backend
    from apps.dac_scan.keithley2000_reader import Keithley2000Reader
    from apps.Rad_icon_2022.radicon_backend import RadiconBackend


@dataclass
class DeviceContext:
    """
    All device backends passed to ExperimentalSchedulerWindow and SequenceRunner.
    Camera (USB VideoCapture) is NOT included — each camera action opens its own
    VideoCapture instance as needed.
    """
    controller: "PM16CController | PM16CControllerSim | None" = None
    pace5000: "Pace5000Backend | None" = None
    lakeshore: "LakeShore335Backend | None" = None
    keithley: "Keithley2000Reader | None" = None
    radicon: "RadiconBackend | None" = None
