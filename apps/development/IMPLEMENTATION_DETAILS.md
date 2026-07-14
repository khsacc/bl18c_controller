# Development menu — implementation details

Developer-facing detail for tools under the menu-bar **Development** menu
(`apps/development/`). Read this before touching any tool here or adding a
new one.

## Purpose and audience

**This menu is for developers who know both this codebase and the BL-18C
beamline well — not for general beamline users.** Its tools may skip the
safety/UX guardrails used elsewhere in the app (agreed with the user,
2026-07-14). New Development tools go in their own subfolder under
`apps/development/`.

**Exempt from i18n entirely**: tools here are English-only and must not use
`tr()`/`settings.i18n` — translating diagnostic tooling isn't worth the
upkeep (agreed with the user, 2026-07-14). See
[settings/i18n.py](../../settings/i18n.py) for the i18n mechanism used
everywhere else in the app.

## `KeithleyReaderWindow` ([keithley_reader/keithley_reader_app.py](keithley_reader/keithley_reader_app.py))

Reads the shared `Keithley2000Reader` on demand (`read_transmitted()`) and
includes a raw SCPI console. Built to confirm the Model 2000 has no
remote-switchable multi-input scanning, so a second (incident/ion-chamber)
reading isn't obtainable — `read_incident()` was removed entirely as a
result (2026-07-14).

## `Pm16cConsoleWindow` ([pm16c_console/pm16c_console_app.py](pm16c_console/pm16c_console_app.py))

Sends a raw ASCII command typed by the user straight to the shared
`PM16CController`/`Sim` connection via `send_cmd()` and displays the reply,
or "No response" if the socket read times out (the controller's existing
2.0 s timeout, shared by every window on that connection).

**Bypasses `MOVE_CONSTRAINTS` and per-channel speed/move limits entirely**
(see [utils/stage/IMPLEMENTATION_DETAILS.md](../../utils/stage/IMPLEMENTATION_DETAILS.md)
for what those normally enforce) — commands go out direct and unmodified.
Because of this, before first opening it the window shows a developer-only
warning and requires the exact answer `STS4?` to a basic protocol quiz; a
permanent warning label remains in the console UI thereafter (agreed with
the user, 2026-07-14).
