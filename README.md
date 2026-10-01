# dsh-ctl

English | [简体中文](README.zh-CN.md)

A Windows **system-tray controller** for [DeepSeek Harness](https://www.npmjs.com/package/@deepseek-ai/dsh) (`dsh`).
Start / stop / restart the web service from the tray icon — on Windows **and** inside WSL — plus control of the
NapCat QQ OneBot bridge, a live log viewer, and a PyInstaller build pipeline protected by an icon-verification gate.

Built as a single-file `dsh-ctl.exe` (~31 MB, no console window) that lives in the tray.

---

## Features

| Target | Actions | How it controls |
|---|---|---|
| **Windows dsh** | Start / Stop / Restart / Status | Launches `node ... dsh bin.js web --no-open` inside a *hidden* console; finds the service by the PID listening on `:3080` |
| **WSL dsh** | Start / Stop / Restart / Status | Feeds bash scripts to `wsl -d <distro> -- bash -s` via stdin; detaches with `setsid nohup`; keeps the WSL2 VM alive with a hold-open client |
| **NapCat (QQ bot bridge)** | Start / Stop / Restart / Status | Runs the NapCat injector against QQNT with fast-login by QQ number; idempotent, double-launch-proof |
| **Logs** | View Logs | Tabbed tkinter viewer tailing `dsh-web.log`, `dsh-web.log.err`, `dsh-ctl.log` (+ WSL logs over UNC), 2 s auto-refresh |

Extras:

- **Single instance** — a second copy binds `127.0.0.1:47632` and exits quietly.
- **Action serialization** — rapid menu clicks can't spawn two dsh instances fighting over port 3080.
- **UI opener discipline (incl. the 0.1.5 entry token)** — dsh runs with `--no-open`; the tray controller is the *only* thing that opens the window, so you never get a duplicate browser tab. **Restart** gracefully closes the current window first (WM_CLOSE only — processes are never killed) and opens a fresh one once the service is back.
  DSH >= 0.1.5 prints an entry-token URL (`http://127.0.0.1:3080/?token=...`) to stdout *once* and answers 401 to the bare origin. dsh-ctl already redirects dsh's stdout into `dsh-web.log`, so it captures that token and opens a **Chrome app window** on it (`--app=<tokenURL>`, sharing the PWA shortcut's profile) — the signed cookie it mints therefore also authenticates the PWA shortcut afterwards (until the cookie ages out). The tray's **Open Web UI** re-opens with a token it has *probed* against the live server, since a token belongs only to the process that printed it.
- **Stale writer-lock cleanup** — DSH >= 0.1.5 takes deadline-bounded file locks during profile boot; `taskkill /F` cannot run their cleanup, and the leftover lock fails the next start (`atomic-write: timed out waiting for the writer lock`). dsh-ctl clears locks whose content is a dead PID before/after start-stop. `task-board/ledger-v2.lock` is JSON and is never touched.
- **Sanitised child environment** — PATH for the dsh child comes from the registry (an inherited dev/agent PATH makes the Lingshu bridge's `python -m aeis.mcp.server` resolve to an interpreter without `aeis`, failing handshake on every boot); inherited `HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY` (honoured since DSH 0.1.5) and `NODE_OPTIONS` are dropped.
- **No silent path degradation** — missing environment variables fall back to real absolute directories (a missing `APPDATA` used to turn the log directory into the relative path `DeepSeekHarness`).
- **NAT-mode dual instance** — Windows dsh and WSL dsh can run simultaneously, each on its own loopback.
- **Audit trail** — every action is appended to `dsh-ctl.log`.

## Tray menu

```
State: Running (PID 12345)        <- dynamic
WSL State: Not running            <- dynamic
NapCat: Running (PID 6789)        <- dynamic
──────────────────────────────
Start dsh / Stop dsh / Restart dsh / Status
WSL dsh  >  Start / Stop / Restart / Status
NapCat   >  Start / Stop / Restart / Status
──────────────────────────────
View Logs
──────────────────────────────
Exit
```

## Safety guards

Stop operations refuse to kill anything that doesn't look like the real thing:

- Windows dsh stop only kills PIDs whose image is `node.exe` / `cmd.exe`;
- WSL dsh stop only kills PIDs whose `/proc/<pid>/cmdline` contains `node`;
- NapCat stop only kills PIDs whose image is `qq.exe`.

If an unrelated process grabs one of the managed ports, the tool reports and refuses instead of `taskkill`-ing blind.

## Requirements

- Windows 10 / 11
- Python 3.10+ (3.13 tested) with `pystray>=0.19`, `pillow`
- DeepSeek Harness installed via npm (`%APPDATA%\npm\node_modules\@deepseek-ai\dsh`)
- Optional: WSL2 distro with a `dsh` CLI under `~/.local/node-v*/bin/`
- Optional: NapCat shell + QQNT for the QQ bridge

## Build

```bat
build.bat
```

What it does:

1. Installs pinned build deps if missing;
2. Fixes stale `TCL_LIBRARY` / `TK_LIBRARY` (matters when building from inside a PyInstaller onefile app);
3. Records SHA-256 of the protected icon assets;
4. Builds `dist\dsh-ctl.exe` via `dsh-ctl.spec` (onefile, windowed);
5. Runs the **icon gate** (`verify_icon.py`): extracts the exe icon and compares it pixel-by-pixel against the
   authoritative reference `icon-check.png` — 0 differing bytes or the desktop copy is not replaced;
6. Copies the exe to the real Desktop (OneDrive-redirection aware).

## Configuration (optional environment variables)

| Variable | Purpose | Default |
|---|---|---|
| `DSH_NAPCAT_DIR` | NapCat shell install dir | `%USERPROFILE%\.dsh\napcat\napcat-shell` |
| `DSH_NAPCAT_QQ` | Bot QQ number for fast login | probed from `config/napcat_<qq>.json` |

## Ports used

| Port | Meaning |
|---|---|
| 3080 | dsh web (Windows and WSL independently) |
| 3001 | NapCat OneBot WebSocket |
| 6099 | NapCat WebUI |
| 3081 | Optional fixed portproxy entry to the WSL dsh (preferred UI URL) |
| 47632 | dsh-ctl single-instance lock (localhost only) |

## Logs & data

- `%APPDATA%\DeepSeekHarness\dsh-web.log(.err)` — Windows dsh output (4 rotations kept)
- `%APPDATA%\DeepSeekHarness\dsh-ctl.log` — audit trail (`timestamp | action | OK/FAIL | detail`)
- WSL logs are read live over `\\wsl$\<distro>\home\<user>\.dsh\`

## Repository layout

```
main.py           entry point: threading model, queue pump, single-instance lock
dsh_control.py    core logic: process/port management, NapCat, WSL support
tray.py           pystray icon + menu (callbacks only enqueue actions)
logviewer.py      tabbed log window
verify_icon.py    build-gate: exe icon pixel comparison
build.bat         one-command build + deploy-to-desktop pipeline
*.spec            PyInstaller specs (release / test)
test_wsl_*.py     WSL flow & dual-instance integration tests
backup/           pre-dsh-ctl batch scripts kept for history
```

## Threading model (hard rule)

> tkinter lives only on the main thread; pystray callbacks only enqueue actions;
> long actions run on daemon threads serialized by a lock; balloons and menu
> refresh return to the main thread via `root.after`.
