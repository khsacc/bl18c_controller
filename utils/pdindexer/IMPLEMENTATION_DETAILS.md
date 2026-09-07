# PDIndexer bridge — implementation details

Sends pyFAI 1D reductions from this app directly into PDIndexer, bypassing
IPAnalyzer. Full design rationale and the review history that shaped this
are in [docs/PLAN_PDINDEXER_BRIDGE.md](../../docs/PLAN_PDINDEXER_BRIDGE.md)
— read that first for *why*; this file is the maintained reference for
*what the wire format actually is* and is kept in sync with
`schema_snapshot.json` / `csharp/PdiTypes.cs`.

## Module layout

| File | Role |
|------|------|
| [profile.py](profile.py) | `PdiProfile` dataclass (validates + sanitises), `PdiProfile.from_pyfai()` |
| [service.py](service.py) | `PdiService` — app-wide sender; `Transport`, `Trigger` enums |
| [pdi_file.py](pdi_file.py) | Method C: legacy `.pdi` XML writer (unique name, exclusive create — no temp+rename) |
| [csharp/PdiTypes.cs](csharp/PdiTypes.cs) | Mirrored Crystallography type declarations (MIT, see header) |
| [csharp/MemoryPackEx.cs](csharp/MemoryPackEx.cs) | Mirrored `Serialize`/`Deserialize` helper (header byte + Brotli) |
| [csharp/Program.cs](csharp/Program.cs) | `PdiSender.exe` CLI — builds profiles, self-verifies, writes to clipboard |
| [schema_snapshot.json](schema_snapshot.json) | Baseline for `tools/check_pdindexer_schema.py` drift detection |
| [tests/pdi_payload_decoder.py](../../tests/pdi_payload_decoder.py) | L1/L2 byte-layout decoder, used by tests and available for manual capture analysis |

## Two transports

| | Clipboard (method A) | `.pdi` folder (method C) |
|---|---|---|
| Path | `PdiSender.exe` → MemoryPack → Brotli → Windows clipboard | `write_pdi_file()` → legacy XML → PDIndexer's `FileSystemWatcher` |
| Requires | Windows, `utils/pdindexer/bin/PdiSender.exe` built | A writable folder PDIndexer is told to watch |
| Upstream-change resilience | Medium (schema mirror; `tools/check_pdindexer_schema.py` watches for drift) | High (XML is name-based, tolerates added/removed fields) |
| `PdiService` capability check | `clipboard_available()` | `watch_folder_configured()` |

Both checks are independent on purpose — if the clipboard helper isn't
built, the `.pdi`-folder fallback must still be selectable, and vice versa.

## The wire format (clipboard transport)

Clipboard format name: `"System.Byte[]"`. The HGLOBAL WinForms actually
puts there is **not** the raw MemoryPack bytes — `Clipboard.SetDataObject`
wraps any `byte[]` in a 16-byte GUID marker + an MS-NRBF record stream
(`.NET`'s own serialisation envelope for a boxed array), because `byte[]`
satisfies `IsSerializable`. Two layers, referred to throughout this bridge
(tests, decoder, docs) as:

* **L1** (HGLOBAL) = `GUID(16) + NRBF envelope + L2 + MessageEnd(1)`
* **L2** (payload) = `[[1-byte ID=2]] + Brotli(MemoryPack(DiffractionProfile2[]))`

```
offset  size  value                                              meaning
0       16    96 A7 9E FD 13 3B 70 43 A6 79 56 10 6B B2 88 FB     s_serializedObjectID GUID
16      1     00                                                 NRBF SerializedStreamHeader
17      4     01 00 00 00                                        RootId = 1
21      4     FF FF FF FF                                        HeaderId = −1
25      4     01 00 00 00                                        MajorVersion = 1
29      4     00 00 00 00                                        MinorVersion = 0
33      1     0F                                                 NRBF ArraySinglePrimitive
34      4     01 00 00 00                                        ObjectId = 1
38      4     <N>                                                array length
42      1     02                                                 PrimitiveType.Byte
43      N     02 …                                                L2 payload: [ID=2] + Brotli(MemoryPack)
43+N    1     0B                                                 NRBF MessageEnd
```

`PdiSender.exe` never builds this by hand — `Clipboard.SetDataObject(payload,
copy: true)` does it. `tests/pdi_payload_decoder.py` parses both layers for
testing/diagnostics; `strip_nrbf()` converts L1 → L2.

## Type mirror, not a submodule

`csharp/PdiTypes.cs` mirrors (member-for-member, in upstream declaration
order — MemoryPack carries no field names) exactly four Crystallography
types: `PointD`, `Profile`, `HorizontalAxisProperty`, `DiffractionProfile2`.
Crystallography itself is **not** a submodule (see
docs/PLAN_PDINDEXER_BRIDGE.md §1 for why) — its `net10.0-windows` /
WinForms / 7-NuGet dependency tree is disproportionate to sending one
clipboard profile, and pinning a submodule commit doesn't solve the actual
problem (noticing when it goes stale). `tools/check_pdindexer_schema.py`
solves that instead.

Two type-level gotchas that look like simplifications but break wire
compatibility if "fixed":

* **`PointD` must keep its `[MemoryPackIgnore] object? Tag` field.**
  Dropping it makes the struct `unmanaged`, which switches MemoryPack from
  the intended 2-member Object encoding (`[0x02][X][Y]`, 17 bytes) to a
  raw 16-byte memory blit — a different, incompatible wire shape.
* **`HorizontalAxisProperty` must contain *only* value-type members.**
  It's `unmanaged` on purpose (all-value-type `record struct` → MemoryPack
  blits its raw memory layout, 88 bytes, no framing at all). Adding any
  reference-type member would flip it to Object encoding instead.

The basis commit for the mirror is **PDIndexer's own Crystallography
gitlink** (currently `99958eb4a5c5527134e8c303212acc349f5a6a6d`, pinned by
PDIndexer `f1ea57f`), not Crystallography's `main` branch HEAD — PDIndexer
decides which schema it will actually try to deserialise against.
IPAnalyzer pins a *different* Crystallography commit than PDIndexer does;
that's normal, not a problem to reconcile.

## Unit mapping (pyFAI → PDIndexer)

| Value | Field | Unit | Source |
|---|---|---|---|
| 2θ array | `SourceProfile.Pt[i].X` | degree | `integrate1d(..., unit="2th_deg").radial` |
| Intensity | `SourceProfile.Pt[i].Y` | as-is | `.intensity` |
| σ (optional) | `SourceProfile.Err[i].Y` | as-is | `.sigma` (only set if `error_model` was used) |
| Wavelength | `SrcProperty.WaveLength` | **nanometres** | `ai.wavelength [m] × 1e9` |

Wavelength is nm, not Å — PDIndexer has a legacy-format compatibility
branch (`if WaveSource==Xray && WaveLength>1: WaveLength = EnergyToXrayWaveLength(WaveLength*10000)`)
that silently "works" for Å values under 1 (BL-18C's ~0.62 Å) but breaks
for any wavelength above 1 Å. `PdiProfile` always converts to nm.

`PdiProfile.exposure_time`/`is_cps` default to `1.0`/`True` and callers
should not override them: `DiffractionProfile2.convertSrcToDest`
(`Profile.cs` L1158) divides intensity by `ExposureTime` whenever `IsCPS`
is set, and IPAnalyzer's own clipboard-send path never sets either field
on the profiles it sends — it always relies on exactly these defaults
(identity, i.e. no rescaling). An earlier version of `radicon_ui.py` (see
code review 2026-09-06) passed the window's real exposure time through
with `is_cps` still `True`, which silently rescaled every clipboard-sent
profile's intensity by 1/exposure — something neither IPAnalyzer nor the
`.pdi`-file transport (whose legacy XML schema has no `ExposureTime`/
`IsCPS` elements at all) ever does, so the same measurement showed
different intensity depending on which transport sent it.

## Conditions PDIndexer silently requires

`AddProfileToCheckedListBox` (PDIndexer `FormMain.cs`) throws or no-ops —
**never raises a dialog** — if any of these aren't met, which is why
`PdiSender.exe`'s self-verification step checks all of them before writing
to the clipboard:

1. `bytes[0] == 2`.
2. `Deserialize` returns non-null with the expected profile count.
3. `Name` (never `Comment` — `AddProfileToCheckedListBox` only ever checks
   `dp[0].Name.EndsWith("whole")`; an earlier version of `PdiProfile` ran
   the same check against `Comment` too, needlessly truncating a
   legitimate comment ending in "whole") does not end in `"whole"` (that
   string triggers PDIndexer's azimuthal-division/"LPO" mode instead of a
   normal profile). This check is shared by both transports (`profile.py`'s
   `sanitise_pdi_string`, since it lives inside `AddProfileToCheckedListBox`
   itself, called by both the clipboard-receive handler and the
   file-open/watcher path).
   The `.pdi`-file transport has one further, transport-specific quirk —
   `XYFile.ReadPdi2File` does a blind, case-sensitive find/replace of
   `pt`→`Pt` (and three longer legacy-tag names) across the raw file text
   before parsing it — handled in `pdi_file.py` at the point the XML is
   written (a lossless pre-emptive swap matching upstream exactly), not in
   the shared sanitiser. An earlier version handled it in the shared
   sanitiser instead, case-*insensitively* deleting the substring, which
   both broke ordinary words in profile names sent over the clipboard
   transport (where this quirk doesn't even apply) and didn't match what
   upstream actually does to `.pdi`-transport names (see code review
   2026-09-06).
4. `Mode == Concentric`.
5. All 7 `Profile`-typed fields (`SourceProfile`, `ConvertedProfile`,
   `InterpolatedProfile`, `SmoothedProfile`, `Kalpha2RemovedProfile`,
   `BackgroundProfile`, `Profile`) are non-null.
6. `ColorARGB == null` (PDIndexer then assigns its own colour).

## Send triggers (`utils.pdindexer.Trigger`)

`PdiService.send()` takes an explicit `Trigger`, not just "send this
profile" — because the same reduction function
(`RadiconWindow._maybe_run_instant_1d`) runs for live-view frames, poni/
setting recomputes, single shots, and every frame of a burst sequence, and
those need different queueing:

| Trigger | Auto-sent? | Queueing |
|---|---|---|
| `LIVE` | never | — |
| `RECOMPUTE` | never | — |
| `SINGLE_SHOT` | yes (if checkbox on) | coalesce to latest while busy |
| `SEQUENCE` | yes (if checkbox on) | never coalesced — one FIFO slot per frame |
| `MANUAL` | "Send now" button | coalesce to latest while busy |

`PdiService` itself enforces `LIVE`/`RECOMPUTE` never sending, so call
sites can invoke `send()` unconditionally without checking the trigger
first.

`transport` is a **required argument of every `send()` call**, not
mutable state on the service, even though `PdiService` is shared app-wide.
The service has no "current transport" property to set: each window keeps
its own combo box and reads it at send time. An earlier version stored the
choice as `self.transport` on the shared service and had each window's
`_refresh_pdi_controls()` write to it on open/refresh — which meant
merely opening a second window could silently change which transport the
*first* window's next send used (see code review 2026-09-06). If you're
adding a new call site, read the combo box's `currentData()` yourself and
pass it through; don't add a shared default back.

Only one function ever starts the next queued send:
`PdiService._advance_queue()`, called exactly once per completion (from
inside the clipboard-send completion handler, after `_busy` is cleared).
An earlier version also advanced the queue from a second path (a
per-send `on_done` callback used only for `SEQUENCE`), and the two paths
could both fire off of the same completion — sending two sequence frames
concurrently, with the second's `QProcess`/kill-timer silently clobbering
the first's bookkeeping (see code review 2026-09-06). If you touch the
queueing logic, keep it to this one function.

## The `.pdi`-file transport must never rename into place

`write_pdi_file()` (`pdi_file.py`) writes directly under the final
`<name>.pdi` path via an exclusive create (`O_CREAT | O_EXCL`), holding
the file open until the XML is fully written. It must **not** write to a
`.tmp` sibling and `os.replace()` it into place: PDIndexer's watcher only
subscribes to `FileSystemWatcher.Created` (`FormMain.cs`,
`watcher.Created += watcher_Created`) — there is no `.Renamed` handler at
all. A same-directory rename (which is exactly what `os.replace()` is on
both platforms) raises `Renamed`, not `Created`, on Windows, so PDIndexer
would never see the file (see code review 2026-09-06, confirmed against
the current upstream source). Writing under the final name directly is
safe because PDIndexer's own reader already tolerates a file mid-write: it
loops on `File.Open(path, FileMode.Open)` with a 100 ms retry
(`watcher_Created`) until our handle is closed.

## The `"PDIndexer"` named mutex must be released even when abandoned

`Program.cs` guards the clipboard write with the same OS-wide named
`Mutex("PDIndexer")` IPAnalyzer uses (see docs/PLAN_PDINDEXER_BRIDGE.md
§0.6 for the wait/write/wait protocol). `WaitOne()` throws
`AbandonedMutexException` when a previous owner terminated without
releasing it — but per the
[.NET docs](https://learn.microsoft.com/en-us/dotnet/api/system.threading.abandonedmutexexception),
**ownership is still granted to the calling thread** despite the
exception; it's a warning that shared state may be inconsistent, not a
failed acquisition. `ReleaseMutex()` must still be called from the
`catch` block. Skipping it (an earlier version's second wait did) leaves
this process's thread holding the mutex when it exits, so the OS abandons
it *again* on process termination — and PDIndexer's own `WaitOne()` in its
`WM_DRAWCLIPBOARD` handler has no `catch` for this exception type at all,
so a cascading abandonment there could throw out of its message loop
entirely. See code review 2026-09-06.

## `.pdi`-file transport: a sequence must be one file, not one per frame

PDIndexer's watcher sets `watcher.EnableRaisingEvents = false` for the
*entire time* it is reading a just-created `.pdi` file
(`watcher_Created`, `FormMain.cs`), and does not queue or replay
`Created` events raised while disabled — per the
[FileSystemWatcher docs](https://learn.microsoft.com/en-us/dotnet/api/system.io.filesystemwatcher.enableraisingevents),
a change that occurs while `EnableRaisingEvents` is false is simply never
observed. Writing one `.pdi` file per `SEQUENCE`-triggered frame (as the
clipboard transport's one-item-at-a-time FIFO does safely) therefore
risks losing any frame written while PDIndexer is still busy reading the
previous one — a real risk for a fast burst.

`PdiService.send()` handles this by never writing immediately for
`Trigger.SEQUENCE` + `Transport.WATCH_FOLDER`: profiles accumulate in
`_sequence_watch_folder_buffer` instead, and the caller must call
`PdiService.flush_sequence(transport)` once the burst is complete (in
`radicon_ui.py`, from `_on_seq_done` — after the averaged-image send, so
it's included in the same batch — and from `_on_seq_error` and
`closeEvent`, so a partial/aborted run isn't silently dropped either).
`flush_sequence()` is a no-op for `Transport.CLIPBOARD`: Windows queues
`WM_DRAWCLIPBOARD` notifications itself even while PDIndexer's handler is
busy with a previous one, so the per-frame FIFO there doesn't have this
failure mode. See code review 2026-09-06.

## PDIndexer never ACKs

A `PdiSender.exe` exit code of 0 means "wrote to the clipboard/file",
**not** "PDIndexer picked it up". There is no acknowledgement channel.
`PdiService.pdindexer_running_async()` (a window-title probe, run as a
separate `--check` subprocess) is the best available proxy, and is only
ever used to phrase the status message (`"Sent to PDIndexer"` vs `"wrote
to clipboard, PDIndexer not detected"`), never as a precondition for
sending. It's asynchronous (callback-based) rather than
`QProcess.waitForFinished()` on the calling thread — a self-contained
single-file .NET exe can take a few hundred ms just to start, and an
earlier version blocked the GUI thread on every successful clipboard send
waiting for it (worse during a `SEQUENCE` burst, once per frame) — see
code review 2026-09-06 and the Qt docs on `waitForFinished()` blocking the
calling thread's event loop.

## Keeping this current

Run `python tools/check_pdindexer_schema.py` before a beamtime or after
updating PDIndexer/IPAnalyzer. It resolves PDIndexer's current
Crystallography gitlink, fetches `Profile.cs`/`Enums.cs`/
`UniversalConstants.cs` at that commit, and diffs the live member/enum
order against `schema_snapshot.json`. It needs network access and is not
part of the automated test suite. This includes `XrayLine`: it has no
explicit member values, so a member inserted before `Ka1` upstream would
silently shift the wire value `PdiTypes.cs` hardcodes for
`XrayLine.Ka1` even though the name is unchanged — an earlier version of
the tool skipped it, reasoning backwards that sending only `Ka1` made its
order irrelevant. Any enum the tool fails to extract at all now counts as
drift too, not just a printed warning (see code review 2026-09-06).

**Not yet done** (requires a Windows machine with IPAnalyzer + PDIndexer
installed — see docs/PLAN_PDINDEXER_BRIDGE.md Phase 0):
capturing real golden `tests/data/ipa_clipboard_hglobal.bin` /
`ipa_payload.bin` samples, confirming `HorizontalAxisProperty`'s assumed
88-byte layout against a real capture, building `PdiSender.exe` for the
first time, and the V5–V11 acceptance tests (does PDIndexer actually show
the sent profile, with the right axis/wavelength, and does the UI hold up
under real acquisition sequences).
