// Thin C wrapper around Sapera LT C++ classes for the Rad-icon 2022 detector.
//
// Architecture:
//   Python (ctypes) -> radicon_dll.dll (this file) -> SapClassBasic86.dll -> Xtium-CL MX4
//
// Acquisition strategy — permanent Grab():
//   rad_init()  : create objects, start Grab() once.
//   rad_snap()  : wait for the next frame-complete callback, copy buffer.
//   rad_shutdown: Freeze(), destroy objects.
//
// The camera (FreeRun CCF) outputs frames continuously — no SoftwareTrigger()
// needed.  One Grab() call at init is enough for the entire session.
//
// Exposure control:
//   Exposure is set via the CameraLink serial port (COM2, 115200 baud) using
//   "set <ms>\r" ASCII commands.  This is handled entirely in Python
//   (RadiconBackend) — this DLL does NOT touch exposure.
//
// Key differences from naive implementation (derived by reverse-engineering
// XFPCAP01.exe, the commercial Rad-icon control software):
//   - SapBufferWithTrash instead of SapBuffer: the trash slot absorbs frames
//     that arrive faster than the host can consume, preventing ring-buffer
//     corruption under high frame rates.
//   - No SoftwareTrigger(): the FreeRun CCF asserts CC1 automatically.
//     Calling SoftwareTrigger() on top of that can cause timing conflicts.
//   - First frame after Grab() is discarded: Grab() startup produces one
//     incomplete/unreliable frame; skip_first suppresses it.
//
// Thread safety:
//   rad_* functions must be called from a single thread.
//   snap_callback fires on a Sapera worker thread; it only touches
//   frame_seq / skip_first (via Interlocked ops) and the auto-reset Event.

#include "radicon_dll.h"

#pragma warning(disable: 4995)
#include "SapClassBasic.h"
#pragma warning(default: 4995)

#include <Windows.h>
#include <cstdio>
#include <cstring>
#include <cstdarg>

// ---------------------------------------------------------------------------
// Error state
// ---------------------------------------------------------------------------

static char g_last_error[512] = {};

static void set_error(const char* fmt, ...) {
    va_list args;
    va_start(args, fmt);
    vsnprintf(g_last_error, sizeof(g_last_error) - 1, fmt, args);
    va_end(args);
}

// ---------------------------------------------------------------------------
// Internal context
// ---------------------------------------------------------------------------

static const int RING_BUF_COUNT = 20;  // match iMaxGrabBuf in XFPCAP01

struct RadiconCtx {
    SapAcquisition*    acq  = nullptr;
    SapBufferWithTrash* buf = nullptr;  // trash slot absorbs overflow frames
    SapAcqToBuf*       xfer = nullptr;
    HANDLE frame_event       = nullptr; // auto-reset; set by snap_callback
    volatile LONG frame_seq  = 0;       // monotonically incrementing frame count
    volatile LONG skip_first = 1;       // discard first frame after Grab()
    // last_buf_idx is written by snap_callback (Sapera worker thread) and
    // read by rad_snap (caller thread).  Use InterlockedExchange / volatile.
    volatile LONG last_buf_idx = 0;
    int width  = 0;
    int height = 0;
};

// ---------------------------------------------------------------------------
// Sapera transfer callback — runs on a Sapera worker thread
// ---------------------------------------------------------------------------

static void snap_callback(SapXferCallbackInfo* pInfo) {
    RadiconCtx* ctx = static_cast<RadiconCtx*>(pInfo->GetContext());

    // The first frame after Grab() is unreliable (Grab startup artifact).
    // Use a compare-and-swap so exactly one callback thread wins the skip.
    if (InterlockedCompareExchange(&ctx->skip_first, 0L, 1L) == 1L) {
        return;
    }

    // Save the completed buffer index BEFORE signalling rad_snap().
    // buf->GetIndex() returns the slot that was just filled by the DMA.
    // Using InterlockedExchange ensures the write is visible across threads.
    InterlockedExchange(&ctx->last_buf_idx, (LONG)ctx->buf->GetIndex());

    InterlockedIncrement(&ctx->frame_seq);
    SetEvent(ctx->frame_event);
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

static void destroy_ctx(RadiconCtx* ctx) {
    if (!ctx) return;
    if (ctx->xfer) { ctx->xfer->Destroy(); delete ctx->xfer; }
    if (ctx->buf)  { ctx->buf->Destroy();  delete ctx->buf;  }
    if (ctx->acq)  { ctx->acq->Destroy();  delete ctx->acq;  }
    if (ctx->frame_event) CloseHandle(ctx->frame_event);
    delete ctx;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

extern "C" {

RADICON_API int rad_init(const char* server_name, int device_index,
                         const char* ccf_path, RadiconHandle* out_handle)
{
    *out_handle = nullptr;
    g_last_error[0] = '\0';

    if (SapManager::GetServerIndex(server_name) < 0) {
        set_error("Sapera server not found: \"%s\". "
                  "Run CamExpert to verify the server name.", server_name);
        return -1;
    }
    if (SapManager::GetResourceCount(server_name, SapManager::ResourceAcq) == 0) {
        set_error("No CameraLink acquisition resources on server \"%s\". "
                  "Check that the Xtium-CL MX4 driver is loaded.", server_name);
        return -1;
    }

    auto* ctx = new RadiconCtx();
    ctx->frame_event = CreateEvent(nullptr, /*bManualReset=*/FALSE,
                                   /*bInitialState=*/FALSE, nullptr);
    if (!ctx->frame_event) {
        set_error("CreateEvent() failed (Windows error %lu)", GetLastError());
        delete ctx;
        return -1;
    }

    SapLocation loc(server_name, device_index);
    ctx->acq  = new SapAcquisition(loc, ccf_path);
    ctx->buf  = new SapBufferWithTrash(RING_BUF_COUNT, ctx->acq);
    ctx->xfer = new SapAcqToBuf(ctx->acq, ctx->buf, snap_callback, ctx);

    if (!ctx->acq->Create()) {
        set_error("SapAcquisition::Create() failed — check CCF path and camera "
                  "connection.\nCCF: %s", ccf_path);
        goto fail;
    }

    // Check that a CameraLink signal is actually present before continuing.
    // CORACQ_PRM_SIGNAL_STATUS returns 0 when no signal is detected.
    {
        UINT32 sig_status = 0;
        ctx->acq->GetParameter(CORACQ_PRM_SIGNAL_STATUS, &sig_status);
        if (sig_status == 0) {
            set_error("No CameraLink signal detected on server \"%s\". "
                      "Check camera power and CameraLink cable.", server_name);
            goto fail;
        }
    }

    if (!ctx->buf->Create()) {
        set_error("SapBufferWithTrash::Create() failed");
        goto fail;
    }
    if (!ctx->xfer->Create()) {
        set_error("SapAcqToBuf::Create() failed");
        goto fail;
    }

    ctx->width  = ctx->buf->GetWidth();
    ctx->height = ctx->buf->GetHeight();

    // Start permanent continuous acquisition.
    // The FreeRun CCF asserts CC1 automatically — no SoftwareTrigger() needed.
    // skip_first == 1 so the callback will discard the first unreliable frame.
    if (!ctx->xfer->Grab()) {
        set_error("SapAcqToBuf::Grab() failed");
        goto fail;
    }

    *out_handle = ctx;
    return 0;

fail:
    destroy_ctx(ctx);
    return -1;
}

RADICON_API int rad_shutdown(RadiconHandle handle) {
    if (!handle) return 0;
    auto* ctx = static_cast<RadiconCtx*>(handle);
    if (ctx->xfer && *ctx->xfer) {
        ctx->xfer->Freeze();
        if (!ctx->xfer->Wait(3000)) {
            ctx->xfer->Abort();
        }
    }
    destroy_ctx(ctx);
    return 0;
}

RADICON_API int rad_get_width(RadiconHandle handle) {
    return static_cast<RadiconCtx*>(handle)->width;
}

RADICON_API int rad_get_height(RadiconHandle handle) {
    return static_cast<RadiconCtx*>(handle)->height;
}

RADICON_API int rad_snap(RadiconHandle handle,
                         uint16_t* out_buf, int n_pixels, int timeout_ms)
{
    auto* ctx = static_cast<RadiconCtx*>(handle);

    if (n_pixels != ctx->width * ctx->height) {
        set_error("rad_snap: buffer size mismatch — expected %d pixels (%dx%d), got %d",
                  ctx->width * ctx->height, ctx->width, ctx->height, n_pixels);
        return -1;
    }

    // ResetEvent first so we block until a NEW frame arrives after this point,
    // not a stale signal from a previous snap.
    ResetEvent(ctx->frame_event);

    DWORD wait_result = WaitForSingleObject(ctx->frame_event,
                                            static_cast<DWORD>(timeout_ms));
    if (wait_result != WAIT_OBJECT_0) {
        set_error("rad_snap: timeout after %d ms — no frame received. "
                  "Check camera power, CameraLink cable, and serial port "
                  "startup sequence (was 'set <ms>' sent before Grab?).",
                  timeout_ms);
        return -1;
    }

    // Use the buffer index saved by snap_callback — this is guaranteed to be
    // the slot whose DMA has fully completed, regardless of how many additional
    // frames arrived between SetEvent and this read.
    int buf_idx = (int)ctx->last_buf_idx;

    // Read row-by-row via ReadLine, matching XFPCAP01's exact approach.
    // SapBuffer::Read() with a flat offset does NOT account for DMA pitch
    // (bytes-per-row may exceed width*2 due to alignment), causing zeros in
    // the lower portion of the image.  ReadLine handles pitch internally.
    int pitch = ctx->buf->GetPitch();
    uint16_t* dst = out_buf;
    for (int row = 0; row < ctx->height; ++row) {
        int nRead = 0;
        if (!ctx->buf->ReadLine(buf_idx, 0, row, ctx->width - 1, row, dst, &nRead)) {
            set_error("SapBuffer::ReadLine(%d, row=%d) failed — pitch=%d w=%d h=%d",
                      buf_idx, row, pitch, ctx->width, ctx->height);
            return -1;
        }
        dst += ctx->width;
    }
    return 0;
}

RADICON_API int rad_acquire_sequence(RadiconHandle handle,
                                     uint16_t* out_buf,
                                     int n_frames, int n_pixels_per_frame,
                                     int timeout_ms_per_frame)
{
    if (n_pixels_per_frame != rad_get_width(handle) * rad_get_height(handle)) {
        set_error("rad_acquire_sequence: n_pixels_per_frame (%d) != width*height (%d)",
                  n_pixels_per_frame,
                  rad_get_width(handle) * rad_get_height(handle));
        return -1;
    }

    for (int i = 0; i < n_frames; ++i) {
        uint16_t* dst = out_buf + static_cast<size_t>(i) * n_pixels_per_frame;
        if (rad_snap(handle, dst, n_pixels_per_frame, timeout_ms_per_frame) != 0) {
            char tmp[512];
            snprintf(tmp, sizeof(tmp), "Frame %d of %d: %s", i, n_frames, g_last_error);
            strncpy_s(g_last_error, sizeof(g_last_error), tmp, _TRUNCATE);
            return -1;
        }
    }
    return 0;
}

RADICON_API const char* rad_get_last_error(void) {
    return g_last_error;
}

} // extern "C"
