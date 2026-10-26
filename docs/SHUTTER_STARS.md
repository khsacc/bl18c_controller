# X-ray shutter via STARS

## Scope

The launcher owns the app-wide STARS connection defined in `utils/shutter.py`
and used by the BL-18C stopper and PF ring current reader. The
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

- STARS server: IP address in **Settings → STARS connection**; TCP port
  6057 is fixed. The initial IP address is `192.168.1.101`.
- Client node: `Auto18C`. The server's challenge number selects a keyword
  index; BL-18C uses a single keyword, so the same configured keyword is sent
  for any challenge number.
- The keyword is entered in Settings and stored in the Git-ignored
  `settings/__localdata/shutter_connection.json`. Do not put it in source,
  sequence files, screenshots or logs.
- The first shutter status operation sends `System flgon stopper.Status` to
  receive `_ChangedValue In/Out/Neutral` events. Reading ring current alone
  does not require shutter event registration.
- `stopper.Status GetValue` returns `In` (stopper inserted, X-rays blocked) or
  `Out` (stopper removed, X-rays reach the sample). During motion it may return
  `Neutral`. The tool treats this as an unknown position; after a Drive command,
  the controller polls through `Neutral` until `In` or `Out` is confirmed or
  the settle timeout expires.
- `stopper.Drive SetValue 1` requests **In**; `SetValue 0` requests **Out**.
  A Drive reply alone is not proof of motion: the controller polls Status
  GetValue until the requested position is reported or the timeout expires.

The launcher creates one STARS client and shares it with the shutter tool,
scheduler, Rad-icon shutter check, and ring current tool. Closing a consumer
window does not disconnect the socket; the launcher closes it on exit. A failed
request disconnects it, and the next request reconnects. Standalone
`ShutterController` instances still own their own socket.

The Experimental Scheduler's **Stop sequence if ring status is not User (4 or 5)**
option defaults to on. It reads `Ring.Status GetValue` on the shared connection
immediately before each XRD capture and after capture and saving. Only status 4
(User) or 5 (User Top-UP) permits the next operation. Other statuses, malformed
replies, and communication failures stop the sequence as errors. The check
does not run during exposure. When the scheduler manages the shutter, it
confirms In before the post-XRD check and leaves it In on a ring fault; a fault
does not offer restoration to an initially Out position. The option is
independent of **Only open shutter during XRD measurements**.

**Tools → Read ring current** sends `Ring.DCCT GetValue` and
`Ring.Status GetValue` over that connection. It displays the current in mA
and the numeric status with its operating-mode name: 0 Shutdown, 1 Linac,
2 Injection, 3 Storage, 4 User, or 5 User (Top-UP). Either reading remains
visible if the other fails. The nodes, commands, and units are specified by
the Japanese `readme_ja.txt`
in the on-site `C:\Users\HPG\Desktop\Ring` package. The bridge must be running
for these nodes to answer. The operation only reads; it never sends `SetValue`.

### Local ring bridge startup

Copy the five files from the on-site `Ring` package (`ringEPF.exe`,
`cshosts.csv`, `RingEPFStart.cmd`, `Ring.key`, `readme_ja.txt`) to
`__localdata/ring/` in this repository. This directory is Git-ignored because
the package contains the confidential `Ring.key`; never force-add these files.

On Windows in normal mode, the launcher looks for an existing `ringEPF.exe`
process. If one is running, it leaves it alone. Otherwise it starts the local
copy with the command file's working directory and `TINE_HOME=.` environment,
passing `Ring` and the server IP configured under **Settings → STARS connection**.
It starts the executable directly so it can stop only the process it owns on
app exit. A 30-second check restarts an app-owned bridge that has exited, or
starts the local copy if a manually launched bridge has stopped. Startup
failures are logged and do not prevent the rest of the app from opening.
`--debug` and non-Windows runs do not start the bridge.

For the first on-site ring check, open **Tools → Read ring current** and compare
the displayed mA value with the PF ring's independent current display. Record
the exact STARS reply (including any error text), then try **Read current and status**
again while the shutter tool is also open. Confirm that both functions keep
working after either window closes. If the node is unavailable, check that
`ringEPF.exe` is running and connected to the configured STARS server.

The shutter tool reads the actual status before every operation. It refuses to act
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
`ShutterControllerSim` and does not contact the STARS server. The ring tool
shows a labelled simulated value.

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

## Ring current observation (2026-10-25; former command)

At `192.168.1.101:6057`, authentication succeeded, but
`tinebridge.PFRING.RNGREADER.device_0.BEAMCUR GetValue` returned
`System>Auto18C @GetValue Er: tinebridge is down.` The stopper's read-only
`Status GetValue` still returned `Out`, confirming that STARS was reachable.
The ring current was not obtained. The on-site `Ring` package subsequently
identified `Ring.DCCT GetValue` as the command for beam current in mA, so the
tool now uses that node instead. The old error remains here as a record of why
the command changed.

## Ring current observation (2026-10-26)

On the control PC, `ringEPF.exe` was running and the configured STARS server
was `192.168.1.101`. A read-only `Ring.DCCT GetValue` using
`StarsController` returned `450.077278600000` mA. The reply
was parsed successfully; the PF ring's independent current display was not
checked during this test.

A later read-only `Ring.Status GetValue` attempt on the same PC received no
reply because STARS closed the connection. `ringEPF.exe` and TCP port 6057
were still available. The current `Ring.Status` reply format therefore remains
to be confirmed on the live shared app connection; malformed or missing
replies stop an XRD sequence when the ring check is enabled.
