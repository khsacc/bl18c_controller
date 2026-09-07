// PdiTypes.cs
//
// Type declarations mirrored (NOT a git submodule — see
// docs/PLAN_PDINDEXER_BRIDGE.md §1 "A について: Crystallography を
// submodule にするか?" for why) from seto77/Crystallography (MIT Licence,
// see LICENSE-Crystallography.txt in this directory), commit
// 99958eb4a5c5527134e8c303212acc349f5a6a6d — the revision pinned by
// PDIndexer's own submodule at commit f1ea57fd37a76513ec12f96108980314cee34f93.
//
// Sources:
//   Profile.cs      L16   (Profile), L442 (DiffractionProfile2), L1231 (HorizontalAxisProperty)
//   PointD.cs       L134  (PointD)
//   Enums.cs                (LengthUnitEnum, AngleUnitEnum, EnergyUnitEnum, TimeUnitEnum, ...)
//   UniversalConstants.cs L255 (XrayLine)
//
// Copyright (c) 2002-2026 Yusuke SETO.
//
// This file mirrors ONLY the members that MemoryPack serialises (in their
// exact upstream declaration order — MemoryPack carries no field names, so
// deserialisation success depends entirely on order + type matching the
// receiving PDIndexer build). Methods, non-serialised helper members, and
// [MemoryPackIgnore] members that are safe to drop have been removed --
// EXCEPT PointD.Tag, which must stay (see the comment on PointD below).
//
// Before touching this file, read docs/PLAN_PDINDEXER_BRIDGE.md §0.4 and
// the "ミラー作成時のチェックリスト" in §Phase 1-1. Any change here should
// also update ../schema_snapshot.json and re-run tools/check_pdindexer_schema.py.

using System.Runtime.InteropServices;
using MemoryPack;

namespace Crystallography;

public enum HorizontalAxis { Angle, d, WaveNumber, Length, EnergyXray, EnergyElectron, EnergyNeutron, NeutronTOF, None }
public enum WaveSource { Xray, Electron, Neutron, None }
public enum WaveColor { Monochrome, FlatWhite, CustomWhite, None }
public enum DiffractionProfileMode { Concentric, Radial }
public enum BackgroundMode { BSplineCurve, ReferrenceProfile }
public enum AngleUnitEnum { Degree, Radian, CentiDegree, MilliRadian }
public enum LengthUnitEnum
{
    None,
    Meter, CentiMeter, MilliMeter, MicroMeter, NanoMeter, Angstrom, PicoMeter,
    MeterInverse, CentiMeterInverse, MilliMeterInverse, MicroMeterInverse,
    NanoMeterInverse, AngstromInverse, PicoMeterInverse
}
public enum EnergyUnitEnum { eV, KeV, MeV }
public enum TimeUnitEnum { Seccond, MilliSecond, MicroSecond, NanoSecond }
public enum XrayLine { Ka, Ka1, Ka2, Kb1, Kb3, KbI2, KbII2, La1, La2, Lb1, Lb2 }

// PointD is a struct but is NOT unmanaged: it carries a [MemoryPackIgnore]
// `object? Tag` reference field. Dropping Tag here would make this struct
// unmanaged, which flips MemoryPack onto the raw-memory-blit code path
// instead of the 2-member Object encoding PDIndexer expects — see
// docs/PLAN_PDINDEXER_BRIDGE.md §0.4's warning box. Keep Tag.
[StructLayout(LayoutKind.Sequential)]
[MemoryPackable]
public partial struct PointD
{
    public double X { get; set; }
    public double Y { get; set; }

    [MemoryPackIgnore]
    public object? Tag { get; set; }

    [MemoryPackConstructor]
    public PointD() { X = 0; Y = 0; Tag = null; }

    public PointD(double x, double y) { X = x; Y = y; Tag = null; }
}

[MemoryPackable]
public partial class Profile
{
    public string? text;
    public List<PointD> Pt = [];
    public List<PointD> Err = [];
    public float LineWidth = 1f;
    // Color is [MemoryPackIgnore] upstream — omitted entirely, not mirrored.
}

// record struct with only value-type members => unmanaged => MemoryPack
// blits the raw struct layout (no Object framing at all). Offsets are
// documented in docs/PLAN_PDINDEXER_BRIDGE.md §0.4 and pending Phase 0
// confirmation on a real machine; this is why field ORDER here must match
// upstream exactly even though MemoryPack itself doesn't check it -- the
// CLR chooses the physical layout from declaration order for Sequential
// layout, mirroring it here reproduces the same padding upstream has.
[StructLayout(LayoutKind.Sequential)]
[MemoryPackable]
public partial record struct HorizontalAxisProperty
{
    public HorizontalAxis AxisMode { get; set; } = HorizontalAxis.Angle;
    public WaveSource WaveSource { get; set; } = WaveSource.Xray;
    public WaveColor WaveColor { get; set; } = WaveColor.Monochrome;
    public double WaveLength { get; set; } = 0.4;               // nanometres
    public int XrayElementNumber { get; set; } = 29;
    public XrayLine XrayLine { get; set; } = XrayLine.Ka1;
    public double ElectronAccVolatage { get; set; } = 200;
    public double EnergyTakeoffAngle { get; set; } = 5.0 / 180.0 * Math.PI;
    public double TofAngle { get; set; } = Math.PI / 4;
    public double TofLength { get; set; } = 25;
    public AngleUnitEnum TwoThetaUnit { get; set; } = AngleUnitEnum.Radian;
    public LengthUnitEnum DspacingUnit { get; set; } = LengthUnitEnum.Angstrom;
    public LengthUnitEnum WaveNumberUnit { get; set; } = LengthUnitEnum.NanoMeterInverse;
    public EnergyUnitEnum EnergyUnit { get; set; } = EnergyUnitEnum.eV;
    public TimeUnitEnum TofTimeUnit { get; set; } = TimeUnitEnum.MicroSecond;

    public HorizontalAxisProperty() { }
}

[MemoryPackable]
public partial class DiffractionProfile2
{
    public static byte ID => 2;

    [MemoryPackable]
    public partial class MaskingRange
    {
        public double[] X = new double[2];

        [MemoryPackConstructor]
        public MaskingRange() => X[0] = X[1] = 0;
    }

    // ---- 55 members, upstream declaration order (Profile.cs L442-1226) ----
    public List<MaskingRange> maskingRanges = [];                       //  1

    private int interpolationOrder = 2;
    public int InterpolationOrder                                       //  2
    {
        get => interpolationOrder;
        set { if (value > 0) interpolationOrder = value; }
    }

    private int interpolationPoints = 20;
    public int InterpolationPoints                                      //  3
    {
        get => interpolationPoints;
        set { if (value > 0) interpolationPoints = value; }
    }

    public bool DoesMaskAndInterpolate = false;                         //  4

    public Profile SourceProfile = new();                              //  5
    public PointD[] BgPoints = [];                                      //  6
    public Profile ConvertedProfile = new();                            //  7
    public Profile InterpolatedProfile = new();                         //  8
    public Profile SmoothedProfile = new();                             //  9
    public Profile Kalpha2RemovedProfile = new();                       // 10
    public Profile BackgroundProfile = new();                           // 11
    public Profile Profile = new();                                     // 12

    public DiffractionProfileMode Mode = DiffractionProfileMode.Concentric;  // 13

    public HorizontalAxisProperty SrcProperty;                          // 14
    public HorizontalAxisProperty DstProperty;                          // 15

    public bool DoesNormarizeIntensity = false;                         // 16
    public double NormarizeRangeStart = 0;                              // 17
    public double NormarizeRangeEnd = 180;                              // 18
    public bool NormarizeAsAverage = true;                              // 19
    public double NormarizeIntensity = 1000;                           // 20

    public bool DoesSmoothing = false;                                  // 21
    public int SazitkyGorayM = 3;                                       // 22
    public int SazitkyGorayN = 3;                                       // 23

    public bool DoesTwoThetaOffset = false;                             // 24
    public double TwoThetaOffsetCoeff0 = 0;                             // 25
    public double TwoThetaOffsetCoeff1 = 0;                             // 26
    public double TwoThetaOffsetCoeff2 = 0;                             // 27

    public bool DoesRemoveKalpha2 = false;                              // 28
    public double Kalpha1 = 0;                                          // 29
    public double Kalpha2 = 0;                                          // 30

    public bool IsShiftX = false;                                       // 31
    public double ShiftX = 0;                                           // 32

    public bool DoesBandpassFilter = false;                             // 33
    public bool DoesLowPath = false;                                    // 34
    public bool DoesHighPath = false;                                   // 35
    public double LowPathLimit = double.NaN;                            // 36
    public double HighPathLimit = double.NaN;                           // 37

    public bool IsCPS = true;                                           // 38
    public double ExposureTime = 1;                                     // 39

    public bool IsLogIntensity = false;                                 // 40

    public float LineWidth = 1f;                                        // 41
    public int? ColorARGB;                                              // 42

    public int BgPointsNumber = 15;                                     // 43
    public bool SubtractBackground = false;                             // 44
    public BackgroundMode BgMode = BackgroundMode.BSplineCurve;         // 45
    public Profile? BackgroundReferrenceProfile = null;                 // 46
    public double BackgroundReferrenceScale = 1;                        // 47

    public string? Name;                                                // 48
    public string? Comment { get; set; }                                // 49

    public bool IsLPOmain = false;                                      // 50
    public bool IsLPOchild = false;                                     // 51

    public double[]? ImageArray = null;                                 // 52
    public double ImageScale = 0;                                       // 53
    public int ImageWidth = 0;                                          // 54
    public int ImageHeight = 0;                                         // 55

    public DiffractionProfile2()
    {
        SourceProfile = new Profile();
        ConvertedProfile = new Profile();
        SmoothedProfile = new Profile();
        Kalpha2RemovedProfile = new Profile();
        InterpolatedProfile = new Profile();
        Profile = new Profile();
        BackgroundProfile = new Profile();
        SrcProperty = new HorizontalAxisProperty();
        DstProperty = new HorizontalAxisProperty();
        ColorARGB = null;
    }
}
