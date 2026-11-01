# XRD 1D reduction — implementation details

This app reads saved 2D TIFF images and never acquires hardware. The launcher
injects its shared `PoniState` and `PdiService`. Standalone mode restores the
remembered poni calibration and owns its own state/service.
The window accepts one local `.tif` or `.tiff` file by drag and drop over the
image, controls or plot. Drop handling calls the same `load_image()` path as
the Open button; other URLs and multi-file drops are ignored. The drop action
is always Copy, so dragging from a file manager cannot move the source TIFF.

`reduction_app.py` owns the UI and a `QThread` worker. Every worker receives a
copy of the image and masks; the window deep-copies the integrator on the GUI
thread before starting the worker.
Only Qt signals deliver results to widgets. The newest requested reduction
supersedes an older result. Spot detection and reduction have separate pending
requests, so an edit cannot replace a requested spot search. An invalidated
active spot search is repeated with the current mask and threshold. Clearing
all masks or right-dragging to unmask cancels pending spot work and marks an
active spot worker as cancelled. Its result is invalidated, and later edits
cannot restart that cancelled search. The cancellation is shown immediately
and with the next reduction result, then cleared from subsequent status text.
Pressing Mask Spots explicitly starts a fresh search. A spot request for a
previous image is cancelled and announced. Pending work starts
after the active worker ends.
Closing waits for the active worker to finish before destroying the window.

The numerical engine is `apps/Rad_icon_2022/instant1d.py::reduce_frame`.
Its pyFAI full pixel splitting, solid-angle correction, polarisation factor,
angular bins and range behaviour remain shared with Rad-icon, XRD Scan and
Experimental Scheduler. The optional external mask is `True` for excluded
pixels. With no masked pixels the app passes `None`, matching Rad-icon's
unmasked call. When a detector mask exists, it is combined with an explicit
mask. Spot detection uses the same module's `find_spot_mask`, excluding the
existing spot and manual masks from radial statistics and candidate centres.

The app keeps spot and manual masks as separate Boolean arrays. The effective
integration mask is their union. A right drag removes pixels from both layers.
Both layers have independent next-image policies: retain current pixels only
for an identical shape, reload a selected file, or clear. Mask file failures
clear that layer and report an error; image loading continues. Native `.npz`
masks preserve the full shape. IPAnalyzer `.mas` files contain a little-endian
32-bit pixel count followed by row-major, low-bit-first packed bits, omitting
an incomplete final byte; they contain no width or height. The app checks the
pixel count against the current image. Saving from the manual-mask section
exports the visible union of spot and manual masks.

Diamond thickness and spot threshold live in `PoniState`; persistence remains
in `settings/__localdata/detector_calibration.json`. The Settings page and this
window listen to `poni_changed` and update controls with signals blocked, so
edits in either window are immediately visible in the other. Offline 1D
options and last directories are private to this app's `__localdata` file.
File dialogs remember their last directory. In standalone mode the calibration
wizard opens for file-based work, without a detector backend or stage.
Malformed local preferences are reset to defaults with a status message;
preference write errors are also shown. Automatic save and PDIndexer sending
apply only to the first unchanged reduction after an image is opened. The
status reports both outcomes, including partial or failed saves, and explains
when an intervening edit cancels enabled automatic actions. Spot threshold
changes alone do not restart reduction. Reloading a saved mask file is
announced in the image information and reduction status.
