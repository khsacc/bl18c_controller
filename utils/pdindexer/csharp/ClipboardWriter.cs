// ClipboardWriter.cs — places a byte[] on the clipboard in ONE Win32
// clipboard transaction, so clipboard viewers (PDIndexer's WM_DRAWCLIPBOARD
// handler) are notified exactly once per send.
//
// Why not Clipboard.SetDataObject(): it goes through OleSetClipboard, and a
// short-lived process must then flush the OLE clipboard (OleFlushClipboard
// for copy:true, or OleUninitialize at process exit for copy:false). The
// flush empties the clipboard and re-sets every format as a plain HGLOBAL,
// which is a SECOND clipboard change. PDIndexer has no de-duplication and
// adds the profile once per notification, so every send arrived twice.
// Measured on the beamline PC 2026-10-23: one SetDataObject call from a
// short-lived process -> 2 WM_DRAWCLIPBOARD, for both copy:true and
// copy:false. IPAnalyzer avoids this only because it is long-lived.
//
// Format compatibility: the HGLOBAL is produced by WinForms' own DataObject
// (via its COM IDataObject.GetData), i.e. the exact bytes OleFlushClipboard
// would have stored under the same "System.Byte[]" format — the bytes
// PDIndexer's Clipboard.GetDataObject().GetData(typeof(byte[])) already
// reads after a flush.

using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Windows.Forms;
using ComTypes = System.Runtime.InteropServices.ComTypes;

namespace PdiSender;

internal static class ClipboardWriter
{
    private const int OpenRetries = 20;
    private const int OpenRetryDelayMs = 50;

    /// <summary>Throws on failure; the clipboard is left unchanged unless
    /// SetClipboardData succeeded.</summary>
    public static void WriteBytes(byte[] payload)
    {
        // The same wrapping Clipboard.SetDataObject(payload) uses: the format
        // name is the object's type name, "System.Byte[]".
        string formatName = typeof(byte[]).FullName!;
        var dataObject = new DataObject(formatName, payload);
        ushort cf = (ushort)DataFormats.GetFormat(formatName).Id;

        var format = new ComTypes.FORMATETC
        {
            cfFormat = unchecked((short)cf),
            dwAspect = ComTypes.DVASPECT.DVASPECT_CONTENT,
            lindex = -1,
            ptd = IntPtr.Zero,
            tymed = ComTypes.TYMED.TYMED_HGLOBAL,
        };
        ((ComTypes.IDataObject)dataObject).GetData(ref format, out ComTypes.STGMEDIUM medium);
        if (medium.tymed != ComTypes.TYMED.TYMED_HGLOBAL || medium.unionmember == IntPtr.Zero)
        {
            ReleaseStgMedium(ref medium);
            throw new InvalidOperationException("DataObject did not render an HGLOBAL");
        }

        // EmptyClipboard() after OpenClipboard(NULL) leaves the clipboard
        // without an owner, which makes SetClipboardData fail — so open it
        // with a (message-only) window of our own.
        using var owner = new MessageOnlyWindow();
        bool handedOver = false;
        try
        {
            OpenWithRetry(owner.Handle);
            try
            {
                if (!EmptyClipboard())
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "EmptyClipboard failed");
                if (SetClipboardData(cf, medium.unionmember) == IntPtr.Zero)
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "SetClipboardData failed");
                handedOver = true; // the system now owns the HGLOBAL
            }
            finally
            {
                CloseClipboard();
            }
        }
        finally
        {
            if (!handedOver)
                ReleaseStgMedium(ref medium);
        }
    }

    private static void OpenWithRetry(IntPtr hwnd)
    {
        // Another process (often a clipboard viewer reacting to the previous
        // change) may briefly hold the clipboard open.
        for (int i = 0; ; i++)
        {
            if (OpenClipboard(hwnd))
                return;
            if (i >= OpenRetries)
                throw new Win32Exception(Marshal.GetLastWin32Error(), "OpenClipboard failed");
            Thread.Sleep(OpenRetryDelayMs);
        }
    }

    private sealed class MessageOnlyWindow : NativeWindow, IDisposable
    {
        private static readonly IntPtr HwndMessage = new(-3);

        public MessageOnlyWindow() => CreateHandle(new CreateParams { Parent = HwndMessage });

        public void Dispose() => DestroyHandle();
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool OpenClipboard(IntPtr hWndNewOwner);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool CloseClipboard();

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool EmptyClipboard();

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr SetClipboardData(uint uFormat, IntPtr hMem);

    [DllImport("ole32.dll")]
    private static extern void ReleaseStgMedium(ref ComTypes.STGMEDIUM pmedium);
}
