"""Read one still image from the beamline network camera (no camera controls).

Panasonic Network Camera Technical Notes, section 3.4:
https://www.videotechnology.com/jpeg/misc/cgi_interface_v23.pdf
"""
from __future__ import annotations

from collections.abc import Mapping
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPBasicAuthHandler, HTTPPasswordMgrWithDefaultRealm, HTTPRedirectHandler,
    ProxyHandler, Request, build_opener,
)

CAMERA_SNAPSHOT_URL = (
    "http://130.87.177.70/SnapshotJPEG?Resolution=640x480&Quality=Standard"
)
CAMERA_USERNAME_ENV = "BL18C_CAMERA2_USERNAME"
CAMERA_PASSWORD_ENV = "BL18C_CAMERA2_PASSWORD"
_TIMEOUT_S = 5
_MAX_IMAGE_BYTES = 5 * 1024 * 1024


class CameraSnapshotError(RuntimeError):
    """A camera failure safe to report to the requesting Slack member."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if fp is not None:
            fp.close()
        raise CameraSnapshotError("The camera redirected the snapshot request unexpectedly.")


def fetch_snapshot(env: Mapping[str, str]) -> bytes:
    """Blocking; call only on the camera worker. Never returns HTML as an image."""
    credentials = HTTPPasswordMgrWithDefaultRealm()
    username = env.get(CAMERA_USERNAME_ENV, "")
    password = env.get(CAMERA_PASSWORD_ENV, "")
    if username and password:
        credentials.add_password(None, CAMERA_SNAPSHOT_URL, username, password)
    # This camera is reached directly on the facility network. Slack keeps its
    # own proxy configuration. Authentication is scoped to this fixed URL.
    opener = build_opener(ProxyHandler({}), HTTPBasicAuthHandler(credentials), _NoRedirect())
    request = Request(CAMERA_SNAPSHOT_URL, headers={"Cache-Control": "no-cache"})
    try:
        with opener.open(request, timeout=_TIMEOUT_S) as response:
            content_type = response.headers.get_content_type()
            if content_type != "image/jpeg":
                raise CameraSnapshotError("The camera did not return a JPEG snapshot.")
            data = response.read(_MAX_IMAGE_BYTES + 1)
    except HTTPError as exc:
        code = exc.code
        exc.close()
        if code in (401, 403):
            raise CameraSnapshotError(
                "Camera authentication failed. Set BL18C_CAMERA2_USERNAME and "
                "BL18C_CAMERA2_PASSWORD on the beamline PC, then restart the application."
            ) from None
        raise CameraSnapshotError(f"The camera returned HTTP {code}.") from None
    except (URLError, TimeoutError, OSError):
        raise CameraSnapshotError(
            "The camera could not be reached or timed out. Check the beamline PC's camera connection."
        ) from None
    if len(data) > _MAX_IMAGE_BYTES:
        raise CameraSnapshotError("The camera snapshot exceeds the 5 MiB size limit.")
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise CameraSnapshotError("The camera returned an empty or incomplete JPEG snapshot.")
    import cv2
    import numpy as np

    image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise CameraSnapshotError("The camera returned an invalid JPEG snapshot.")
    return data
