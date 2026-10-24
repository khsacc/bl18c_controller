# X-ray shutter via STARS

## Scope

`utils/shutter.py` controls the BL-18C stopper through a STARS server. The
manual window is the **X-ray shutter** button in the main window's XRD section. The Experimental Scheduler can
optionally manage it while running a sequence containing XRD measurements.

## Experimental Scheduler

The independent **X-ray Shutter Settings** panel appears when the sequence
contains a `take_xrd` action. Its checkbox is off by default. When off, the
scheduler makes no shutter calls. When on, it reads and records the starting
position, confirms In at sequence start, confirms Out before each XRD capture,
and confirms In after capture and file saving. Errors and user stops also
trigger an In attempt during cleanup. If In cannot be confirmed, the sequence
reports an error; do not assume that the beam is blocked. At the end, the UI
asks whether to restore the recorded starting position. Restoration runs off
the GUI thread and verifies the actual position before and after the command.
`--debug` uses the simulated shutter.

## Connection and commands

- STARS server: IP address in **Settings → X-ray shutter (STARS)**; TCP port
  6057 is fixed. The initial IP address is `192.168.1.101`.
- Client node: `Auto18C`. The server's challenge number selects a keyword
  index; BL-18C uses a single keyword, so the same configured keyword is sent
  for any challenge number.
- The keyword is entered in Settings and stored in the Git-ignored
  `settings/__localdata/shutter_connection.json`. Do not put it in source,
  sequence files, screenshots or logs.
- On connection, send `System flgon stopper.Status` to receive
  `_ChangedValue In/Out/Neutral` events.
- `stopper.Status GetValue` returns `In` (stopper inserted, X-rays blocked) or
  `Out` (stopper removed, X-rays reach the sample). During motion it may return
  `Neutral`. The tool treats this as an unknown position; after a Drive command,
  the controller polls through `Neutral` until `In` or `Out` is confirmed or
  the settle timeout expires.
- `stopper.Drive SetValue 1` requests **In**; `SetValue 0` requests **Out**.
  A Drive reply alone is not proof of motion: the controller polls Status
  GetValue until the requested position is reported or the timeout expires.

The tool reads the actual status before every operation. It refuses to act
when the status is unknown or has changed since the user initiated the
operation. Network I/O runs off the GUI thread; the window cannot close during
an operation. Disconnecting
the tool does not command the stopper to move.

## On-site check before scheduler integration

1. Confirm that the STARS server permits the control PC's host/IP and the
   `Auto18C` node, and enter the keyword in Settings.
2. Open the tool with the beamline in a safe state. Check that its **In** or
   **Out** indication matches the beamline's independent position indication.
3. Operate **In** first. Confirm physical insertion and that the tool reports
   In. Then, under normal beamline operating procedures, confirm Out and
   return to In.
4. Exercise **Refresh status**, an external/manual position change, and a
   disconnected server. The tool should update on a status event, recover on
   refresh, and disable the toggle whenever position cannot be confirmed.
5. Capture the exact authentication, `flgon`, `GetValue`, `SetValue`, and
   error replies if they differ from the simulated STARS server in
   `tests/test_shutter.py`. In particular, verify line endings and the time
   needed for Status to settle after a Drive command.

KEK's STARS documentation spells the event-registration command `flgon`;
the supplied BL-18C notes wrote `flagon`. Verify this spelling on the live
server before relying on event updates.

For hardware-free development, `python main.py --debug` uses
`ShutterControllerSim` and does not contact the STARS server.

## On-site observation (2026-10-23)

At `192.168.1.101:6057`, the server sent a numeric authentication challenge
terminated by LF. Authentication and `System flgon stopper.Status` succeeded;
the registration reply was `@flgon Node stopper.Status has been registered.`
`stopper.Status GetValue` returned `@GetValue Out`, `@GetValue Neutral` during
insertion, then `@GetValue In`. The insertion command replied
`@SetValue 1 Ok:` before the position had settled. The tool subsequently
completed an In → Out → In cycle, confirming each position with GetValue and
finishing at In. A second cycle captured unsolicited status events:
`_ChangedValue Neutral` followed by `_ChangedValue Out` on opening, and
`_ChangedValue Neutral` followed by `_ChangedValue In` on closing. The user
confirmed that the hardware moved. The independent physical position
indication, external/manual changes and disconnected-server behaviour were
not checked in this session.
