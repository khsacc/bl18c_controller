# Slack notifications — implementation details

Posts Experimental Scheduler run results to a Slack channel and answers
slash commands in a direct message with the bot.

## What is notified

| Run end | Runner signal | Slack |
|---|---|---|
| All steps finished | `sequence_completed` | posted (switch: *Sequence completed*) |
| A step's own stop condition (ruby pressure threshold) | `sequence_condition_stopped(reason)` | posted (switch: *stop condition*) |
| Error | `error_occurred(index, message)` | posted (switch: *error*), grouped when repeated |
| Stop / Emergency stop by the user | `sequence_stopped` | **not** posted |
| Error raised as a side effect of a user stop | `error_occurred` with `runner.user_stop_requested` | **not** posted |
| Sequence start | — | **not** posted |

`sequence_condition_stopped` is separate from `sequence_stopped` so a
condition stop (a successful end) is never confused with a user stop. The
runner sets `_condition_stop` where the condition is detected
(`_do_take_spectrum`), and `run()` picks the signal at the end.
`runner.last_error_code` gives the Diagnostic code used for grouping.

## Module layout

| File | Role |
|---|---|
| `service.py` | `SlackService`: sender thread, Web API calls, Socket Mode, slash-command replies, message text |
| `screenshot.py` | Qt GUI-thread bridge for capturing the scheduler window |
| `throttle.py` | `ErrorAggregator`: groups identical errors (pure, injectable clock) |
| `status.py` | `RunStatusBoard`: thread-safe run snapshot; text for slash-command replies |
| `settings/slack_prefs.py` | persisted settings (`settings/__localdata/slack_settings.json`) and tokens (`settings/__localdata/slack_credentials.json`) |
| `settings/pages/slack_page.py` | Settings → Slack notifications page |

`ModeSelectorLauncher` owns one `SlackService` (`main.py`) and shuts it down
on exit. `ExperimentalSchedulerWindow` reads it from `main_window.slack_service`
(no Slack when the window runs without the launcher). The window updates
`status_board` from its GUI-thread slots and calls `notify_*` at run end,
always through `_slack_call()`, which swallows any exception.
The displayed sequence name comes from `Sequence.name` when present; otherwise
it uses the Logging panel's Run name (the base name of the run directory).

## On/off switch

Notifications are optional. The scheduler toolbar has a **Notify on Slack**
checkbox; it is the same saved setting as *Send notifications to Slack* on
the settings page (`SlackService.set_enabled()`, both views kept in sync via
`add_listener()`). When Slack is not set up (`setup_problem()`: no
`slack_sdk`, no bot token or no channel) the checkbox is greyed out as
"Notify on Slack (not set up)" and runs work exactly as without Slack. It is
hidden when the scheduler runs without the launcher.

## Keeping measurements safe

* `notify_*` only puts an item on a bounded queue (100 items, oldest dropped
  when full) and returns. One daemon thread (`SlackSender`) sends with a
  10 s HTTP timeout, at most one message per 1.1 s, and catches everything;
  the last failure is shown on the settings page.
* The slash-command thread only reads `RunStatusBoard` snapshots — it never
  touches Qt widgets or the runner.
* Missing `slack_sdk` or tokens disables the feature; the app still starts.
* `--debug`: nothing is sent automatically and Socket Mode is not started,
  so a development PC never answers commands meant for the beamline PC
  (Slack hands each command to one of the connected clients). *Refresh
  channel list* and *Send test message* still work.

## Tokens

Entered under Settings → Slack notifications → *App credentials*:

| Field | Slack app page | Needed for |
|---|---|---|
| Bot User OAuth Token (`xoxb-…`) | OAuth & Permissions | everything |
| App-Level Token (`xapp-…`) | Basic Information → App-Level Tokens | slash commands only |

The *App Credentials* block on Slack's Basic Information page (Client ID,
Client Secret, Signing Secret, Verification Token) is **not** used: those are
for OAuth distribution and HTTP request signing, and Socket Mode needs
neither.

* Saved to `settings/__localdata/slack_credentials.json` (kept apart from the
  ordinary settings, not tracked by git, owner-only permissions where the OS
  supports them — on Windows anyone who can read the user's files can read
  it, as with the STARS keyword).
* A field left empty falls back to `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN`; the
  page shows which source is in use.
* *Save credentials* applies them at once (Web API client recreated, Socket
  Mode reconnected) — no restart needed.
* *Check credentials* tests the typed tokens without saving: `auth.test`
  (workspace, bot name, and the `X-OAuth-Scopes` header compared with
  `REQUIRED_BOT_SCOPES`) and `apps.connections.open` (App-Level Token valid
  and Socket Mode enabled).

## Grouping identical errors

Key = Diagnostic code + message with numbers replaced by `#`. The first
error is sent at once and opens a window of *N* minutes (setting; 0 = off).
Repeats inside the window are counted; when it ends, one summary
("occurred X more time(s)…") is sent and a new window opens. A different
error is never suppressed.

## Slash commands

`/bl18c` (or `/bl18c status`) and `/bl18c progress` show both the sequence
start time and the current step's start time while a step is running. Times are
in beamline-PC local time, taken from the runner's step-start signal. Before
the first step starts, only the sequence start time is shown.

`/bl18c sample` reports the latest cached LakeShore 335 Ch B temperature and
PACE5000 measured gas pressure, each prefixed by its poll time in beamline-PC
local time. A cached poll older than 10 s is marked stale. It also reports the
last valid ruby pressure measured in the current scheduler run, with its
acquisition time, named ruby position and raw fitted R1 wavelength. When ruby
temperature correction is enabled, the response includes the temperature
actually used, its source and (for LakeShore) its poll time. Ruby pressure is a
discrete measurement, not a live reading. A new run clears the previous run's
ruby value. The per-ruby Slack reading is separate from the mean sent to the
monitor plot for an all-positions measurement. No instrument or spectrometer
request is made from the Socket Mode thread.

One command (e.g. `/bl18c`) with sub-commands `status` (default),
`progress`, `steps`, `lakeshore`, `pace5000`, `sample`, `screenshot`, `help`. The two instrument
commands return the connected launcher's cached readings (including ramp and
heater range) with units, or say when the instrument is not connected. They do
not query the instruments or touch Qt widgets from the Socket Mode thread.
Replies are ephemeral. Any member of the workspace where the app is installed
can use the command in a direct message with the bot. Commands without a Slack
user ID or sent from a channel instead of the DM are refused. Older saved
`allowed_user_ids` settings are ignored.

`screenshot` acknowledges immediately, then asks `SchedulerScreenshotBridge`
to capture the visible scheduler window on the GUI thread. PNG encoding and
`files_upload_v2` run on a separate daemon worker, never on the GUI or runner
thread. Only one screenshot may be pending at a time. Capture and upload
errors are reported in the requesting DM and never propagate to a sequence.
There is no screenshot when the scheduler window is closed, hidden or minimised.
The image contains the Qt window content, not the Windows title bar.

## Slack app setup (once, at api.slack.com/apps)

1. *Create New App* → *From scratch*, pick the workspace.
2. *OAuth & Permissions* → *Bot Token Scopes*: `chat:write`,
   `chat:write.public` (post without inviting the bot), `channels:read`
   (channel list), `commands`, `files:write` (scheduler screenshots). Reinstall
   the Slack app after adding the scope.
3. *Socket Mode* → enable → create an App-Level Token with
   `connections:write` → the App-Level Token (`xapp-…`).
4. *Slash Commands* → *Create New Command* → `/bl18c` (no Request URL is
   needed with Socket Mode). Usage hint: `[status|progress|steps|lakeshore|pace5000|sample|screenshot]`.
5. *App Home* → enable the *Messages Tab* and tick *Allow users to send
   Slash commands and messages from the messages tab*.
6. *Install to Workspace* → copy the *Bot User OAuth Token* (`xoxb-…`).
   Reinstall after changing scopes.

## Beamline PC (Windows 11)

```bat
pip install slack_sdk
rem only if the network needs a proxy (new processes only; restart the app):
setx HTTPS_PROXY "http://proxy.example:8080"
```

`slack_sdk` reads `HTTPS_PROXY` for both the Web API and the Socket Mode
WebSocket. Tokens can instead be set as environment variables with `setx`
(new processes only), but the settings page is the intended way.

On-site checks:

1. Paste both tokens → *Check credentials* shows only ✔ lines → *Save credentials*.
2. *Refresh channel list* lists the channels; *Send test message* arrives.
3. With slash commands enabled and saved, the status line says
   "Slash commands: connected"; `/bl18c` in the bot's DM answers.
4. Outbound HTTPS to `slack.com` and WebSocket to `wss-primary.slack.com`
   are allowed by the facility network (check 2 and 3 fail otherwise).
5. Run a short sequence to the end → one "completed" message; press Stop on
   another → no message.

## Limitations

* If the application itself crashes or hangs, nothing is sent.
* A message still queued when the app exits gets up to 2 s to go out.
* `error_occurred` can fire twice in one run (main error, then a cleanup
  failure); both are posted because they are different errors.
