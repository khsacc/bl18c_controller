# Ruby Finder — implementation details

Ruby Finder is a Ch4 (X) / Ch5 (Y) fixed specialisation of
`apps/scan2d/Free2DScanWindow`. It shares the scan grid, positive-direction
backlash approach, motion lease, stop handling, profile fitting, and move-to-fit
behaviour with General 2D Scan.

## FluoRaPressée acquisition

`FluoraPresseeReader` calls the FluoRaPressée HTTP API (default
`http://192.168.1.102:8765`):

1. `GET /status` is called before the stage motion lease is acquired. Invalid
   authentication, a busy server, or a disconnected detector aborts without
   moving Ch4 or Ch5. Its opaque `instrument_state_token` is captured when the
   server supplies one.
2. At each scan point, `POST /acquire` is sent with `dark.mode="none"`. The
   detector ROI, calibration, and spectrometer position currently selected in
   FluoRaPressée always remain in effect. Exposure time and accumulation
   count follow FluoRaPressée's current setting unless the user checks
   **Exposure time (s)** / **Accumulations** in the "FluoRaPressée
   acquisition override" panel, in which case that value is sent as
   `exposure_time_s` / `accumulations` in the request (FluoRaPressée's
   `AcquireRequest` schema supports both as per-request overrides). EM gain
   has no equivalent field in the FluoRaPressée API — it must still be set in
   the FluoRaPressée application itself.
   The captured token is sent as `expected_state_token`, so FRP rejects an
   acquisition if its instrument settings changed during the map.
3. The response must be a 1-D spectrum. Ruby intensity is `sum(y)` over all
   acquired spectral points. `y` is the API's processed spectrum; with the
   current `dark.mode`, it is equivalent to `y_raw`.

The scan's **Reads per point** setting performs multiple complete API
acquisitions and averages their integrated intensities. Its Ruby Finder default
is 1 because FluoRaPressée already has its own detector-accumulation setting.

The FluoRaPressee PC IP address and API key are configured app-wide under
Settings > Online spectrometer. Environment variables
`FLUORA_PRESSEE_IP`/`FLUORA_PRESSEE_API_KEY` (and the legacy
`FLUORA_PRESSEE_URL`) provide initial fallbacks when no local setting exists.
The key is never written to scan logs. Ruby Finder logs use
`__localdata/ruby_finder/` and store the map as
`fluorescence_intensity_map` in the `.npz` file.

Ruby Finder is an optional positioning aid. The normal Experimental Scheduler
workflow is to use Follow Settings > Reference Image > Capture Now to save the
photo and XRD reference coordinates together, then move to the
ruby-fluorescence position by eye and record only that position in the nested
Ruby Fluorescence Settings panel.

In application `--debug` mode, the existing scan2d Gaussian reader simulation
is used and no network request is made.

`FluoraPresseeReader.acquire_spectrum()` exposes the validated complete API
response for callers that need to retain the spectrum. `read_transmitted()` is
the scalar Ruby Finder adapter and returns `sum(acquire_spectrum()["y"])`.
The Experimental Scheduler uses the complete-response method for its generic
`take_spectrum()` operation.

## Per-grid spectrum inspection

Ruby Finder retains the processed `y` spectrum from every acquisition. When
**Reads per point** is greater than one, those spectra are averaged per grid
point; summing the averaged spectrum therefore matches the intensity displayed
in the map. After a completed or partially aborted scan, left-clicking a
measured map cell displays its spectrum and outlines the selected cell in cyan.
The existing right-click move action is unchanged.

When scan logging is enabled, `spectrum_x` and `spectrum_y_map` are included in
the compressed NPZ file. `spectrum_y_map` has shape
`(n_y, n_x, spectral_points)` and uses NaN for unmeasured cells. Spectral
intensities are retained as float32 to limit memory use. Debug mode supplies a
synthetic ruby-like spectrum so the interaction can be tested without hardware.
