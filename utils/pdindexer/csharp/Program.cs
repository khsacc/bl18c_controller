// Program.cs — PdiSender: builds a Crystallography.DiffractionProfile2[]
// from stdin JSON, MemoryPack-serialises it, and places it on the Windows
// clipboard for PDIndexer to pick up.
//
// I/O contract, exit codes, and the "PDIndexer never ACKs" caveat are
// documented in docs/PLAN_PDINDEXER_BRIDGE.md Phase 1-2 and mirrored in
// utils/pdindexer/IMPLEMENTATION_DETAILS.md. Read those before changing
// this file's exit codes or stdout shape — utils/pdindexer/service.py
// parses both.

using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Windows.Forms;
using Crystallography;

namespace PdiSender;

internal sealed class ProfileInput
{
    public string Name { get; set; } = "";
    public string? Comment { get; set; }
    public string AxisMode { get; set; } = "Angle";
    public string TwoThetaUnit { get; set; } = "Degree";
    public double WavelengthNm { get; set; }
    public string XB64 { get; set; } = "";
    public string YB64 { get; set; } = "";
    public string? ErrB64 { get; set; }
    public double ExposureTime { get; set; } = 1.0;
    public bool IsCps { get; set; } = true;
}

internal sealed class StdinPayload
{
    public List<ProfileInput> Profiles { get; set; } = [];
}

internal static class Program
{
    // Exit codes — see docs/PLAN_PDINDEXER_BRIDGE.md Phase 1-2.
    private const int ExitOk = 0;
    private const int ExitBadInput = 2;
    private const int ExitClipboardWriteFailed = 3;
    private const int ExitMutexTimeout = 4;
    private const int ExitSelfVerifyFailed = 5;

    // Limits guarding against a malformed/hostile caller producing a
    // multi-hundred-MB HGLOBAL (docs/PLAN_PDINDEXER_BRIDGE.md Phase 1-2
    // "入力検証").
    private const int MaxProfiles = 64;
    private const int MaxPointsPerProfile = 1_000_000;

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNameCaseInsensitive = true,
        PropertyNamingPolicy = new SnakeCaseNamingPolicy(),
    };

    [STAThread]
    private static int Main(string[] args)
    {
        bool verify = !args.Contains("--no-verify");
        string? dryRunPath = ArgValue(args, "--dry-run");
        string? decodePath = ArgValue(args, "--decode");

        if (args.Contains("--check"))
            return PdiWindow.IsRunning() ? ExitOk : ExitClipboardWriteFailed;

        if (decodePath != null)
            return RunDecode(decodePath);

        StdinPayload input;
        try
        {
            string json = Console.In.ReadToEnd();
            input = JsonSerializer.Deserialize<StdinPayload>(json, JsonOpts)
                    ?? throw new JsonException("empty/null payload");
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[input] failed to parse stdin JSON: {ex.Message}");
            return ExitBadInput;
        }

        List<DiffractionProfile2> dpList;
        try
        {
            dpList = BuildProfiles(input);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[input] {ex.Message}");
            return ExitBadInput;
        }

        byte[] payload = MemoryPackEx.Serialize(DiffractionProfile2.ID, dpList.ToArray());

        if (verify)
        {
            string? verifyError = SelfVerify(payload, input.Profiles);
            if (verifyError != null)
            {
                Console.Error.WriteLine($"[verify] {verifyError}");
                return ExitSelfVerifyFailed;
            }
        }

        if (dryRunPath != null)
        {
            File.WriteAllBytes(dryRunPath, payload);
            WriteResultJson(dpList, payload.Length);
            return ExitOk;
        }

        int mutexResult = WriteToClipboardGuarded(payload);
        if (mutexResult != ExitOk)
            return mutexResult;

        WriteResultJson(dpList, payload.Length);
        return ExitOk;
    }

    private static string? ArgValue(string[] args, string flag)
    {
        int i = Array.IndexOf(args, flag);
        return (i >= 0 && i + 1 < args.Length) ? args[i + 1] : null;
    }

    // ------------------------------------------------------------------
    // Building DiffractionProfile2 — must satisfy every condition in
    // docs/PLAN_PDINDEXER_BRIDGE.md §0.5 or PDIndexer silently ignores
    // the profile (no exception, no dialog — see that section).
    // ------------------------------------------------------------------
    private static List<DiffractionProfile2> BuildProfiles(StdinPayload input)
    {
        if (input.Profiles.Count == 0)
            throw new ArgumentException("profiles must not be empty");
        if (input.Profiles.Count > MaxProfiles)
            throw new ArgumentException($"too many profiles: {input.Profiles.Count} > {MaxProfiles}");

        var result = new List<DiffractionProfile2>();
        foreach (var p in input.Profiles)
        {
            double[] x = DecodeF64(p.XB64, nameof(p.XB64));
            double[] y = DecodeF64(p.YB64, nameof(p.YB64));
            if (x.Length != y.Length)
                throw new ArgumentException($"x/y length mismatch: {x.Length} vs {y.Length}");
            if (x.Length < 1)
                throw new ArgumentException("profile must have at least one point");
            if (x.Length > MaxPointsPerProfile)
                throw new ArgumentException($"too many points: {x.Length} > {MaxPointsPerProfile}");
            if (!(x.All(double.IsFinite) && y.All(double.IsFinite)))
                throw new ArgumentException("x/y must not contain NaN/Infinity");

            double[]? err = null;
            if (p.ErrB64 != null)
            {
                err = DecodeF64(p.ErrB64, nameof(p.ErrB64));
                if (err.Length != x.Length)
                    throw new ArgumentException($"err length mismatch: {err.Length} vs {x.Length}");
                if (!err.All(double.IsFinite))
                    throw new ArgumentException("err must not contain NaN/Infinity");
            }

            if (!(double.IsFinite(p.WavelengthNm) && p.WavelengthNm > 0))
                throw new ArgumentException($"wavelength_nm must be finite and positive, got {p.WavelengthNm}");

            string name = SanitiseName(p.Name);

            var sourceProfile = new Profile
            {
                text = null,
                LineWidth = 1f,
            };
            for (int i = 0; i < x.Length; i++)
                sourceProfile.Pt.Add(new PointD(x[i], y[i]));
            if (err != null)
                for (int i = 0; i < x.Length; i++)
                    sourceProfile.Err.Add(new PointD(x[i], err[i]));

            var dp = new DiffractionProfile2
            {
                Mode = DiffractionProfileMode.Concentric,   // §0.5 condition 4
                Name = name,                                 // §0.5 condition 3 (never ends in "whole")
                Comment = string.IsNullOrEmpty(p.Comment) ? null : p.Comment,
                ColorARGB = null,                            // §0.5 condition 6
                ExposureTime = p.ExposureTime,
                IsCPS = p.IsCps,
                SourceProfile = sourceProfile,
                // The other 6 Profile fields are already non-null from the
                // DiffractionProfile2 constructor (§0.5 condition 5).
            };
            // axis_mode/two_theta_unit are accepted from the caller for forward
            // compatibility but hardcoded to Angle/Degree here — matching
            // utils/pdindexer/profile.py's PdiProfile.to_json_dict(), which
            // always sends "Angle"/"Degree" since pyFAI's integrate1d(...,
            // unit="2th_deg") is the only source this bridge currently feeds.
            dp.SrcProperty = new HorizontalAxisProperty
            {
                AxisMode = HorizontalAxis.Angle,
                WaveSource = WaveSource.Xray,
                WaveColor = WaveColor.Monochrome,
                WaveLength = p.WavelengthNm,
                XrayElementNumber = 0,                       // custom wavelength, not a tabulated line
                XrayLine = XrayLine.Ka1,
                TwoThetaUnit = AngleUnitEnum.Degree,
                DspacingUnit = LengthUnitEnum.Angstrom,
                WaveNumberUnit = LengthUnitEnum.NanoMeterInverse,
                EnergyUnit = EnergyUnitEnum.eV,
                TofTimeUnit = TimeUnitEnum.MicroSecond,
            };
            dp.DstProperty = dp.SrcProperty;

            result.Add(dp);
        }
        return result;
    }

    private static string SanitiseName(string name)
    {
        string n = name;
        while (n.TrimEnd().EndsWith("whole", StringComparison.Ordinal))
            n = n.TrimEnd()[..^"whole".Length].TrimEnd();
        return string.IsNullOrWhiteSpace(n) ? "profile" : n;
    }

    private static double[] DecodeF64(string b64, string fieldName)
    {
        byte[] bytes;
        try { bytes = Convert.FromBase64String(b64); }
        catch (FormatException) { throw new ArgumentException($"{fieldName}: not valid base64"); }
        if (bytes.Length % 8 != 0)
            throw new ArgumentException($"{fieldName}: byte length {bytes.Length} is not a multiple of 8");
        var result = new double[bytes.Length / 8];
        Buffer.BlockCopy(bytes, 0, result, 0, bytes.Length);
        return result;
    }

    // ------------------------------------------------------------------
    // Self-verification — round-trips the payload through
    // MemoryPackEx.Deserialize and checks it against the INPUT (not the
    // built DiffractionProfile2, so a bug in BuildProfiles itself can
    // still be caught if it also affects the round trip in the same way
    // — this is a real-value check, not just "did Deserialize return
    // non-null", per docs/PLAN_PDINDEXER_BRIDGE.md Phase 1-2.
    // ------------------------------------------------------------------
    private static string? SelfVerify(byte[] payload, List<ProfileInput> inputs)
    {
        DiffractionProfile2[]? roundTripped;
        try
        {
            roundTripped = MemoryPackEx.Deserialize<DiffractionProfile2[]>(payload[1..]);
        }
        catch (Exception ex)
        {
            return $"deserialisation threw: {ex.Message}";
        }
        if (roundTripped is null)
            return "Deserialize returned null";
        if (roundTripped.Length != inputs.Count)
            return $"profile count mismatch: sent {inputs.Count}, round-tripped {roundTripped.Length}";

        for (int i = 0; i < inputs.Count; i++)
        {
            var got = roundTripped[i];
            var want = inputs[i];
            double[] wantX = DecodeF64(want.XB64, "x");
            double[] wantY = DecodeF64(want.YB64, "y");

            for (int name = 0; name < 7; name++)
            {
                Profile? p = name switch
                {
                    0 => got.SourceProfile, 1 => got.ConvertedProfile, 2 => got.InterpolatedProfile,
                    3 => got.SmoothedProfile, 4 => got.Kalpha2RemovedProfile, 5 => got.BackgroundProfile,
                    _ => got.Profile,
                };
                if (p is null)
                    return $"profile[{i}]: a required Profile field round-tripped as null (PDIndexer NREs on this)";
            }

            if (got.SourceProfile.Pt.Count != wantX.Length)
                return $"profile[{i}]: point count mismatch: sent {wantX.Length}, round-tripped {got.SourceProfile.Pt.Count}";
            for (int j = 0; j < wantX.Length; j++)
            {
                if (got.SourceProfile.Pt[j].X != wantX[j] || got.SourceProfile.Pt[j].Y != wantY[j])
                    return $"profile[{i}] point[{j}]: coordinate mismatch after round trip";
            }
            if (want.ErrB64 != null)
            {
                double[] wantErr = DecodeF64(want.ErrB64, "err");
                if (got.SourceProfile.Err.Count != wantErr.Length)
                    return $"profile[{i}]: err count mismatch: sent {wantErr.Length}, round-tripped {got.SourceProfile.Err.Count}";
            }
            if (got.SrcProperty.WaveLength != want.WavelengthNm)
                return $"profile[{i}]: wavelength mismatch: sent {want.WavelengthNm}, round-tripped {got.SrcProperty.WaveLength}";
            if (got.SrcProperty.AxisMode != HorizontalAxis.Angle)
                return $"profile[{i}]: AxisMode round-tripped as {got.SrcProperty.AxisMode}, expected Angle";
            if (got.SrcProperty.TwoThetaUnit != AngleUnitEnum.Degree)
                return $"profile[{i}]: TwoThetaUnit round-tripped as {got.SrcProperty.TwoThetaUnit}, expected Degree";
            if (got.Mode != DiffractionProfileMode.Concentric)
                return $"profile[{i}]: Mode round-tripped as {got.Mode}, expected Concentric";
            if (got.Name is null || got.Name.TrimEnd().EndsWith("whole", StringComparison.Ordinal))
                return $"profile[{i}]: Name is null or ends in \"whole\" (would trigger PDIndexer's LPO branch)";
        }
        return null;
    }

    // ------------------------------------------------------------------
    // Clipboard write, guarded by the same named mutex IPAnalyzer uses.
    // See docs/PLAN_PDINDEXER_BRIDGE.md §0.6 for the protocol and the
    // 10.5s worst-case timing this implements.
    // ------------------------------------------------------------------
    private static int WriteToClipboardGuarded(byte[] payload)
    {
        using var mutex = new Mutex(false, "PDIndexer");
        bool acquired;
        try { acquired = mutex.WaitOne(5000, true); }
        catch (AbandonedMutexException) { acquired = true; }
        if (!acquired)
            return ExitMutexTimeout;
        mutex.ReleaseMutex();

        try
        {
            // copy: true is required — see docs/PLAN_PDINDEXER_BRIDGE.md
            // Phase 1-2 "copy: true" callout: this is a short-lived process,
            // and copy:false relies on OleFlushClipboard running at exit,
            // which a short-lived console app does not reliably trigger.
            Clipboard.SetDataObject(payload, copy: true);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[clipboard] SetDataObject failed: {ex.Message}");
            return ExitClipboardWriteFailed;
        }

        Thread.Sleep(500);

        // AbandonedMutexException still grants this thread ownership (see
        // Microsoft docs) — the exception only signals that a previous
        // owner terminated without releasing, not that acquisition failed.
        // The mutex MUST still be released in that case: skipping it here
        // (an earlier version's catch block did nothing) leaves this
        // process's own thread holding it at exit, so the OS abandons it
        // again on process termination — and PDIndexer's own WaitOne(...)
        // in its WM_DRAWCLIPBOARD handler has no catch for this exception
        // type at all, so the next abandonment there could throw out of
        // its message loop entirely. See code review 2026-09-06.
        bool secondAcquired;
        try { secondAcquired = mutex.WaitOne(5000, false); }
        catch (AbandonedMutexException) { secondAcquired = true; }
        if (secondAcquired)
            mutex.ReleaseMutex();

        return ExitOk;
    }

    private static void WriteResultJson(List<DiffractionProfile2> dpList, int payloadBytes)
    {
        int points = dpList.Sum(dp => dp.SourceProfile.Pt.Count);
        var result = new
        {
            ok = true,
            profiles = dpList.Count,
            points,
            payload_bytes = payloadBytes,
        };
        Console.WriteLine(JsonSerializer.Serialize(result));
    }

    private static int RunDecode(string path)
    {
        byte[] raw = File.ReadAllBytes(path);
        byte[] payload = raw.Length >= 16 && raw.AsSpan(0, 16).SequenceEqual(SerializedObjectGuidHelper.Guid)
            ? SerializedObjectGuidHelper.StripNrbf(raw)
            : raw;
        if (payload.Length == 0 || payload[0] != DiffractionProfile2.ID)
        {
            Console.Error.WriteLine("[decode] leading ID byte is not DiffractionProfile2.ID (2)");
            return ExitBadInput;
        }
        DiffractionProfile2[]? dp;
        try
        {
            dp = MemoryPackEx.Deserialize<DiffractionProfile2[]>(payload[1..]);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"[decode] {ex.Message}");
            return ExitBadInput;
        }
        if (dp is null)
        {
            Console.Error.WriteLine("[decode] Deserialize returned null");
            return ExitBadInput;
        }
        var summary = dp.Select(p => new
        {
            p.Name,
            p.Mode,
            Points = p.SourceProfile.Pt.Count,
            WavelengthNm = p.SrcProperty.WaveLength,
            AxisMode = p.SrcProperty.AxisMode.ToString(),
            TwoThetaUnit = p.SrcProperty.TwoThetaUnit.ToString(),
        });
        Console.WriteLine(JsonSerializer.Serialize(new { ok = true, profiles = summary }));
        return ExitOk;
    }
}

// Minimal helper so --decode can also accept a raw L1 (HGLOBAL) capture,
// not just an L2 payload from --dry-run. See docs/PLAN_PDINDEXER_BRIDGE.md
// §0.2 for the layout this strips.
internal static class SerializedObjectGuidHelper
{
    public static readonly byte[] Guid =
    [
        0x96, 0xa7, 0x9e, 0xfd, 0x13, 0x3b, 0x70, 0x43,
        0xa6, 0x79, 0x56, 0x10, 0x6b, 0xb2, 0x88, 0xfb,
    ];

    public static byte[] StripNrbf(byte[] raw)
    {
        int pos = 16 + 1 + 4 + 4 + 4 + 4;      // GUID + recordType + root + header + major + minor
        pos += 1 + 4;                          // ArraySinglePrimitive recordType + objectId
        int len = BitConverter.ToInt32(raw, pos);
        pos += 4 + 1;                          // length + PrimitiveType.Byte
        return raw[pos..(pos + len)];
    }
}

internal sealed class SnakeCaseNamingPolicy : JsonNamingPolicy
{
    public override string ConvertName(string name)
    {
        var sb = new System.Text.StringBuilder();
        for (int i = 0; i < name.Length; i++)
        {
            char c = name[i];
            if (char.IsUpper(c))
            {
                if (i > 0) sb.Append('_');
                sb.Append(char.ToLowerInvariant(c));
            }
            else sb.Append(c);
        }
        return sb.ToString();
    }
}

// PDIndexer window detection for --check (used by
// utils/pdindexer/service.py's pdindexer_running()).
internal static class PdiWindow
{
    public static bool IsRunning()
    {
        bool found = false;
        NativeMethods.EnumWindows((hWnd, _) =>
        {
            var sb = new System.Text.StringBuilder(256);
            NativeMethods.GetWindowText(hWnd, sb, sb.Capacity);
            if (sb.ToString().Contains("PDIndexer", StringComparison.Ordinal))
            {
                found = true;
                return false; // stop enumeration
            }
            return true;
        }, IntPtr.Zero);
        return found;
    }
}

internal static class NativeMethods
{
    public delegate bool EnumWindowsProc(IntPtr hWnd, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool EnumWindows(EnumWindowsProc lpEnumFunc, IntPtr lParam);

    [DllImport("user32.dll", CharSet = CharSet.Auto)]
    public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder lpString, int nMaxCount);
}
