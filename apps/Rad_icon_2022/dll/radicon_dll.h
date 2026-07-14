// Public C API for radicon_dll.dll
// Python calls these via ctypes.
//
// Error convention: all functions return 0 on success, -1 on failure.
// On failure call rad_get_last_error() for a human-readable description.
//
// Exposure control:
//   Exposure is NOT controlled through this DLL.
//   Use the CameraLink serial port (COM2, 115200 baud) and send:
//     "set <ms>\r"   — set exposure in milliseconds
//   This is handled by RadiconBackend in Python (radicon_backend.py).

#pragma once
#include <stdint.h>

#ifdef RADICON_DLL_EXPORTS
#define RADICON_API __declspec(dllexport)
#else
#define RADICON_API __declspec(dllimport)
#endif

#ifdef __cplusplus
extern "C" {
#endif

typedef void* RadiconHandle;

// Initialize frame grabber + camera using the given CCF file.
// Starts continuous FreeRun Grab() internally — no SoftwareTrigger needed.
// server_name : Sapera server name (e.g. "Xtium-CL_MX4_1").
// device_index: acquisition resource index (usually 0).
// ccf_path    : full path to the FreeRun .ccf file created in CamExpert.
// out_handle  : receives an opaque handle; pass to all subsequent calls.
RADICON_API int rad_init(const char* server_name, int device_index,
                         const char* ccf_path, RadiconHandle* out_handle);

// Destroy all Sapera objects and release the handle.
RADICON_API int rad_shutdown(RadiconHandle handle);

// Image geometry (valid after rad_init succeeds).
RADICON_API int rad_get_width(RadiconHandle handle);
RADICON_API int rad_get_height(RadiconHandle handle);

// Wait for the next complete frame and copy pixel data into out_buf.
// out_buf      : caller-allocated array of uint16, size = width * height.
// n_pixels     : must equal width * height (sanity check).
// timeout_ms   : milliseconds to wait for the frame before giving up.
//                Must be longer than the exposure time plus a safety margin.
RADICON_API int rad_snap(RadiconHandle handle,
                         uint16_t* out_buf, int n_pixels, int timeout_ms);

// Acquire n_frames sequentially, storing them contiguously in out_buf.
// out_buf layout: [frame0_row0..., frame0_rowH..., frame1_row0..., ...]
// out_buf size   : n_frames * width * height * sizeof(uint16_t)
// n_pixels_per_frame: must equal width * height.
RADICON_API int rad_acquire_sequence(RadiconHandle handle,
                                     uint16_t* out_buf,
                                     int n_frames,
                                     int n_pixels_per_frame,
                                     int timeout_ms_per_frame);

// Returns a pointer to a static string describing the last error.
// The string is overwritten on the next failing call, so copy it if needed.
RADICON_API const char* rad_get_last_error(void);

#ifdef __cplusplus
}
#endif
