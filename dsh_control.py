# -*- coding: utf-8 -*-
"""
dsh_control.py - core control logic for the DeepSeek Harness (dsh) service.

All paths are derived from environment variables at runtime; nothing is
hard-coded, so this works regardless of the Windows user name.
"""

import ctypes
from ctypes import wintypes
import glob
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime

APPDATA = os.environ.get('APPDATA', '')
DSH_DIR = os.path.join(APPDATA, 'DeepSeekHarness')
WEB_LOG = os.path.join(DSH_DIR, 'dsh-web.log')
ERR_LOG = os.path.join(DSH_DIR, 'dsh-web.log.err')
CTL_LOG = os.path.join(DSH_DIR, 'dsh-ctl.log')
NPM_DIR = os.path.join(APPDATA, 'npm')
NODE_EXE = os.path.join(NPM_DIR, 'node.exe')
DSH_BIN = os.path.join(NPM_DIR, 'node_modules', '@deepseek-ai', 'dsh', 'lib', 'bin.js')
PWA_GLOB = os.path.join(
    APPDATA, 'Microsoft', 'Windows', 'Start Menu',
    'Programs', 'Chrome*', 'DeepSeek Harness.lnk')
PWA_CHROME_DIR = os.path.join(
    APPDATA, 'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Chrome 应用')
PWA_WINDOWS_LNK = os.path.join(PWA_CHROME_DIR, 'DeepSeek Harness-windows.lnk')
PWA_WSL_LNK = os.path.join(PWA_CHROME_DIR, 'DeepSeek Harness-wsl.lnk')
PORT = 3080

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

# keep a module-level reference so GC never closes the subprocess handles
_detached_procs = []


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def find_node():
    """Locate node.exe: prefer the npm copy, fall back to PATH."""
    for cand in (NODE_EXE, shutil.which('node')):
        if cand and os.path.isfile(cand):
            return cand
    return None


def run_capture(cmd, timeout=15):
    """Run a helper command with no window; return (rc, stdout, stderr)."""
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='mbcs',          # system ANSI code page (GBK on zh-CN)
            errors='replace',
            creationflags=CREATE_NO_WINDOW,
            timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return -1, '', 'command not found'
    except subprocess.TimeoutExpired:
        return -2, '', 'timeout'
    except OSError as exc:
        return -3, '', str(exc)


def _get_port_pid(port):
    """Return the PID listening on <port> (any local host), or None."""
    rc, out, _ = run_capture(['netstat', '-ano'])
    if rc != 0:
        return None
    # netstat line:  TCP  127.0.0.1:3080  0.0.0.0:0  LISTENING  12345
    pat = re.compile(
        r'^\s*\S+\s+\S+:{}\s+\S+\s+LISTENING\s+(\d+)\s*$'.format(port),
        re.M)
    m = pat.search(out)
    if m:
        return int(m.group(1))
    return None


def get_dsh_pid():
    """Return the PID listening on :3080, or None."""
    return _get_port_pid(3080)


def is_running():
    return get_dsh_pid() is not None


def wait_port_ready(proc, timeout_s=60):
    """Wait until :3080 is LISTENING and the spawned process tree is alive.

    Note: dsh is launched via a hidden 'cmd /c' wrapper, so the listening
    PID is the node child, not proc.pid - we therefore only require that
    (a) the wrapper is still alive and (b) something is listening on 3080.
    If the node child crashes, the cmd wrapper exits and we fail fast.

    Returns (ok, reason). dsh cold start with many plugins can take
    30-60s, so the timeout is generous.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            return False, 'process exited (rc={})'.format(proc.returncode)
        if get_dsh_pid() is not None:
            try:
                with socket.create_connection(('127.0.0.1', PORT), timeout=2):
                    return True, None
            except OSError:
                pass
        time.sleep(0.5)
    return False, 'timeout after {}s'.format(timeout_s)


def wait_port_closed(timeout_s=10):
    """Wait until nothing is LISTENING on :3080."""
    return _wait_port_closed(3080, timeout_s)


def _wait_port_closed(port, timeout_s=10):
    """Wait until nothing is LISTENING on 127.0.0.1:<port>."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _get_port_pid(port) is None:
            return True
        time.sleep(0.5)
    return False


# --------------------------------------------------------------------------
# logging: rotation + audit
# --------------------------------------------------------------------------

def rotate_logs():
    """Keep 4 history copies of the stdout log and the stderr log."""
    for base in (WEB_LOG, ERR_LOG):
        for i in range(4, 0, -1):
            src = f'{base}.{i - 1}' if i > 1 else base
            dst = f'{base}.{i}'
            if os.path.exists(src):
                if os.path.exists(dst):
                    try:
                        os.remove(dst)
                    except OSError:
                        pass
                try:
                    os.rename(src, dst)
                except OSError:
                    pass


def audit(action, ok, detail=''):
    """Append one audit line to dsh-ctl.log (UTF-8)."""
    try:
        with open(CTL_LOG, 'a', encoding='utf-8') as f:
            stamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            f.write('{} | {} | {} | {}\n'.format(
                stamp, action, 'OK' if ok else 'FAIL', detail))
    except OSError:
        pass


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------

def open_pwa():
    """Open the DeepSeek Harness WINDOWS PWA (best effort).

    Preferred: the dedicated "DeepSeek Harness-windows" Chrome app shortcut;
    fallback: any legacy "DeepSeek Harness.lnk" under a Chrome dir, then the
    plain local URL.
    """
    try:
        if os.path.isfile(PWA_WINDOWS_LNK):
            os.startfile(PWA_WINDOWS_LNK)
            _remember_pwa('windows')
            return
    except OSError:
        pass
    try:
        hits = glob.glob(PWA_GLOB)
        if hits:
            os.startfile(hits[0])
            _remember_pwa('windows')
            return
    except OSError:
        pass
    try:
        os.startfile('http://127.0.0.1:{}'.format(PORT))
        _remember_pwa('windows')
    except OSError:
        pass


# --------------------------------------------------------------------------
# PWA window control
#
# The DeepSeek Harness UI is a Chrome PWA window (class Chrome_WidgetWin_1,
# the SAME class as ordinary browser windows - and the PWA window may even
# be owned by the very chrome.exe browser process that hosts the user's
# normal tabs). Killing or matching PROCESSES is therefore unsafe. Instead:
#   1. every PWA window this tool itself opened is remembered by hwnd
#      (_remember_pwa, fired after each shortcut launch);
#   2. close_pwa() sends a graceful WM_CLOSE to the remembered windows;
#      when none are known (PWA launched manually) it falls back to a
#      conservative title heuristic that cannot match normal browser
#      windows (those carry a "- Google Chrome" / "- Microsoft Edge"
#      title suffix). Processes are NEVER killed.
# --------------------------------------------------------------------------

PWA_TITLE = 'DeepSeek Harness'
WM_CLOSE = 0x0010

# hwnds of PWA windows this tool opened, per target ('windows' / 'wsl')
_pwa_windows = {'windows': set(), 'wsl': set()}


def _chromeish_windows():
    """[(hwnd, pid)] of visible top-level Chrome-family windows."""
    return [(hwnd, pid) for hwnd, pid, _title, cls in _top_windows()
            if cls == 'Chrome_WidgetWin_1']


def _pwa_title_like(title):
    """True if a window title looks like the PWA (never a browser window)."""
    t = (title or '').strip().lower()
    if not t:
        return False
    for suffix in ('- google chrome', '- microsoft edge'):
        if t.endswith(suffix):
            return False
    return PWA_TITLE.lower() in t


def _remember_pwa(which):
    """Watch (background thread) for the PWA window appearing after a launch."""
    def _work():
        try:
            before = set(_chromeish_windows())
            deadline = time.time() + 8.0
            while time.time() < deadline:
                time.sleep(0.5)
                new = [h for h, _p in _chromeish_windows()
                       if (h, _p) not in before]
                if new:
                    _pwa_windows[which].update(new)
                    return
        except Exception:
            pass
    try:
        threading.Thread(target=_work, daemon=True).start()
    except RuntimeError:
        pass


def _forget_closed(which):
    """Drop remembered hwnds that no longer exist."""
    _pwa_windows[which] = {
        h for h in _pwa_windows[which]
        if ctypes.windll.user32.IsWindow(h)}


def _top_windows():
    """[(hwnd, pid, title, class)] for all visible top-level windows."""
    user32 = ctypes.windll.user32
    wins = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(n + 1) if n else None
        if title:
            user32.GetWindowTextW(hwnd, title, n + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        wins.append((hwnd, pid.value, title.value if title else '', cls.value))
        return True

    user32.EnumWindows(proto(_cb), 0)
    return wins


def find_pwa_windows(which='windows'):
    """Locate the PWA's top-level windows. Returns [(hwnd, pid)].

    Primary: windows this tool launched (remembered hwnds, still alive).
    Fallback: conservative title heuristic - never matches normal browser
    windows because those carry a "- Google Chrome"/"- Microsoft Edge"
    title suffix; the other target's known windows are excluded so the
    two PWAs never close each other.
    """
    _forget_closed(which)
    other = _pwa_windows['wsl' if which == 'windows' else 'windows']
    pid_of = dict(_chromeish_windows())
    hits = [(hwnd, pid_of[hwnd]) for hwnd in sorted(_pwa_windows[which])
            if hwnd in pid_of]
    if hits:
        return hits
    for hwnd, pid, title, cls in _top_windows():
        if cls != 'Chrome_WidgetWin_1' or hwnd in other:
            continue
        if _pwa_title_like(title):
            hits.append((hwnd, pid))
    return hits


def close_pwa(which='windows'):
    """Gracefully close the running PWA window(s). Best effort, never raises.

    Sends WM_CLOSE and waits up to 3s. Processes are NEVER killed - the
    PWA window may share its chrome.exe with the user's normal browsing.
    Returns (ok, detail).
    """
    try:
        hits = find_pwa_windows(which)
        if not hits:
            return True, 'pwa not open'
        hwnds = [h for h, _pid in hits]
        user32 = ctypes.windll.user32
        for hwnd in hwnds:
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        deadline = time.time() + 3.0
        while time.time() < deadline:
            if not any(user32.IsWindow(h) for h in hwnds):
                _pwa_windows[which].difference_update(hwnds)
                return True, 'pwa closed ({})'.format(len(hwnds))
            time.sleep(0.2)
        return False, 'pwa window did not close'
    except Exception as exc:              # never block a restart on this
        return False, 'close_pwa error: {}'.format(exc)


def start_dsh():
    """Start dsh web hidden, detached, logging to files. Returns result dict."""
    if is_running():
        return {'ok': False, 'msg': 'dsh already running'}
    # NAT mode (default WSL2) gives WSL its own loopback, so the Windows dsh
    # and the WSL dsh can listen on :3080 simultaneously. No cross-env mutex.

    # Ensure the NapCat OneBot bridge is up before dsh starts (the QQ remote
    # plugin connects to ws://127.0.0.1:3001/ws). Best effort: a failure here
    # is audited but must never block dsh itself.
    ok, why = ensure_napcat()
    if not ok:
        audit('napcat', False, why)

    node = find_node()
    if not node:
        return {'ok': False, 'msg': 'node.exe not found'}
    if not os.path.isfile(DSH_BIN):
        return {'ok': False, 'msg': 'dsh entry not found: ' + DSH_BIN}

    os.makedirs(DSH_DIR, exist_ok=True)
    rotate_logs()

    try:
        out_f = open(WEB_LOG, 'ab')
        err_f = open(ERR_LOG, 'ab')
    except OSError as exc:
        return {'ok': False, 'msg': 'cannot open log files: {}'.format(exc)}

    # Launch dsh inside a HIDDEN console (not "no console"):
    # a hidden console is inherited by dsh's own child processes
    # (git, node, cloudflared...), so they never pop up new windows.
    # CREATE_NO_WINDOW would give dsh no console at all, which makes
    # every console child spawn a visible terminal window.
    #
    # "web --no-open": dsh's web-app plugin opens the URL in the default
    # browser once the server is ready (openBrowser defaults to true),
    # which adds a plain Chrome TAB on top of the PWA app window opened
    # by open_pwa() below. --no-open is the official flag for that
    # (dsh web --help); this tray controller is the only opener.
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0                    # SW_HIDE
        proc = subprocess.Popen(
            ['cmd', '/c', node, DSH_BIN, 'web', '--no-open'],
            cwd=DSH_DIR,
            stdin=subprocess.DEVNULL,
            stdout=out_f,
            stderr=err_f,
            startupinfo=si,
            creationflags=0,
            close_fds=True,
        )
    except OSError as exc:
        out_f.close()
        err_f.close()
        return {'ok': False, 'msg': 'cannot start dsh: {}'.format(exc)}

    _detached_procs.append(proc)

    ok, reason = wait_port_ready(proc, 90)
    if ok:
        open_pwa()
        return {'ok': True, 'pid': proc.pid, 'msg': 'started (PID {})'.format(proc.pid)}

    err_tail = _err_tail()
    msg = 'start failed (PID {}): {}'.format(proc.pid, reason)
    if err_tail:
        msg += ': ' + err_tail
    return {'ok': False, 'pid': proc.pid, 'msg': msg}


def _err_tail(max_bytes=2048):
    try:
        size = os.path.getsize(ERR_LOG)
        with open(ERR_LOG, 'rb') as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        return data.decode('utf-8', errors='replace').strip()[-500:]
    except OSError:
        return ''


def _process_image(pid):
    """Return the lowercased image name of a PID via tasklist, or None."""
    rc, out, _ = run_capture(['tasklist', '/FI', 'PID eq {}'.format(pid), '/FO', 'CSV', '/NH'])
    if rc != 0:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith('"'):
            return line.split('","')[0].strip('"').lower()
    return None


def stop_dsh():
    """Stop dsh via the port PID; returns result dict.

    Safety guard: only kill a PID that looks like the dsh process tree
    (node.exe / cmd.exe). If something else grabbed port 3080, refuse
    instead of taskkilling an unrelated process.
    """
    pid = get_dsh_pid()
    if pid is None:
        return {'ok': False, 'msg': 'dsh not running'}
    image = _process_image(pid)
    if image not in ('node.exe', 'cmd.exe'):
        return {'ok': False, 'pid': pid,
                'msg': 'port 3080 is held by {} (PID {}), not a dsh process - refusing to kill'.format(
                    image or 'unknown', pid)}
    run_capture(['taskkill', '/F', '/T', '/PID', str(pid)])
    if wait_port_closed(10):
        return {'ok': True, 'pid': pid, 'msg': 'stopped (PID {})'.format(pid)}
    return {'ok': False, 'pid': pid, 'msg': 'port 3080 still in use'}


def restart_dsh():
    """Close the current PWA window, stop if running, then start.

    start_dsh() opens a fresh PWA once the port is ready, so every
    restart ends with a brand-new UI window.
    """
    ok, why = close_pwa('windows')
    audit('pwa-close', ok, why)
    if is_running():
        stop_dsh()
    return start_dsh()


# --------------------------------------------------------------------------
# NapCat (OneBot bridge for the QQ remote plugin)
#
# The QQ remote plugin (@dsh-external/dsh-qq-remote) connects to the NapCat
# OneBot WebSocket at ws://127.0.0.1:3001/ws. dsh-ctl ensures NapCat is up
# whenever it starts/restarts dsh (best effort, never blocking dsh), and
# exposes manual Start/Stop/Restart/Status control in the tray menu.
# --------------------------------------------------------------------------

NAPCAT_PORT = 3001
NAPCAT_WEBUI_PORT = 6099
NAPCAT_DIR_DEFAULT = os.path.join(
    os.path.expanduser('~'), '.dsh', 'napcat', 'napcat-shell')
# A launched NapCat takes a few seconds to inject QQ and open :3001; within
# this window another ensure_napcat() call must not launch a second instance
# (two injected QQ processes would fight over the same bot account).
NAPCAT_LAUNCH_WINDOW = 15.0

_napcat_lock = threading.Lock()
_napcat_launching_ts = 0.0


def napcat_status():
    """Return {'running': bool, 'pid': pid} for the NapCat WS port."""
    pid = _get_port_pid(NAPCAT_PORT)
    return {'running': pid is not None, 'pid': pid}


def _napcat_dir():
    """NapCat install dir: $DSH_NAPCAT_DIR or the default under ~/.dsh."""
    return os.environ.get('DSH_NAPCAT_DIR') or NAPCAT_DIR_DEFAULT


def _qq_exe_path():
    """Resolve QQ.exe: registry first (same as launcher-user.bat), fallback.

    Returns an absolute path or None.
    """
    rc, out, _ = run_capture([
        'reg', 'query',
        r'HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ',
        '/v', 'UninstallString',
    ])
    if rc == 0:
        # UninstallString line looks like:
        #   UninstallString    REG_SZ    "C:\Program Files\Tencent\QQNT\Uninstall.exe"
        m = re.search(r'REG_SZ\s+("?)([^\r\n]+?)\1\s*$', out, re.M)
        if m:
            uninst = m.group(2).strip().strip('"')
            qq = os.path.join(os.path.dirname(uninst), 'QQ.exe')
            if os.path.isfile(qq):
                return qq
    fallback = r'C:\Program Files\Tencent\QQNT\QQ.exe'
    return fallback if os.path.isfile(fallback) else None


def _napcat_qq_number():
    """Bot QQ number for fast login: $DSH_NAPCAT_QQ, else probe
    config/napcat_<qq>.json (created after the first login)."""
    env_q = (os.environ.get('DSH_NAPCAT_QQ') or '').strip()
    if env_q:
        return env_q
    cfg_dir = os.path.join(_napcat_dir(), 'config')
    try:
        for name in os.listdir(cfg_dir):
            m = re.match(r'^napcat_(\d+)\.json$', name)
            if m:
                return m.group(1)
    except OSError:
        pass
    return None


def ensure_napcat():
    """Ensure the NapCat OneBot bridge is running; launch it if not.

    Idempotent: skips while port 3001 already listens, and a short launch
    window prevents double-launching while NapCat is still coming up.
    Returns (ok, reason).
    """
    global _napcat_launching_ts
    with _napcat_lock:
        now = time.time()
        if now - _napcat_launching_ts < NAPCAT_LAUNCH_WINDOW:
            return False, 'already launching'
        # Any NapCat footprint already up? Port 3001 = OneBot WS ready;
        # port 6099 = NapCat WebUI up (bot may still be logging in). Either
        # way a second injector would only fight the existing instance and
        # bounce the same bot account (multi-instance 顶号). Never relaunch
        # when either is listening.
        if _get_port_pid(NAPCAT_PORT) is not None:
            return True, 'already running'
        if _get_port_pid(NAPCAT_WEBUI_PORT) is not None:
            return False, 'napcat webui already up (bot not ready) - not relaunching'

        base = _napcat_dir()
        launcher = os.path.join(base, 'NapCatWinBootMain.exe')
        hook = os.path.join(base, 'NapCatWinBootHook.dll')
        for name in ('qqnt.json', 'napcat.mjs'):
            if not os.path.isfile(os.path.join(base, name)):
                return False, 'napcat files missing in {}'.format(base)
        if not os.path.isfile(launcher) or not os.path.isfile(hook):
            return False, 'napcat launcher missing in {}'.format(base)

        qq = _qq_exe_path()
        if not qq:
            return False, 'QQ.exe not found'

        # Mirror launcher-user.bat: rebuild loadNapCat.js, then run the
        # injector with the NAPCAT_* environment it expects.
        load_path = os.path.join(base, 'loadNapCat.js')
        main_path = os.path.join(base, 'napcat.mjs').replace('\\', '/')
        try:
            with open(load_path, 'w', encoding='utf-8') as f:
                f.write('(async () => {{await import("file:///{}")}})()'.format(main_path))
            env = dict(os.environ)
            env.update({
                'NAPCAT_PATCH_PACKAGE': os.path.join(base, 'qqnt.json'),
                'NAPCAT_LOAD_PATH': load_path,
                'NAPCAT_INJECT_PATH': hook,
                'NAPCAT_LAUNCHER_PATH': launcher,
                'NAPCAT_MAIN_PATH': os.path.join(base, 'napcat.mjs'),
            })
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0                    # SW_HIDE
            args = [launcher, qq, hook]
            qq_num = _napcat_qq_number()
            if qq_num:
                # Fast login with the saved session. The parameter is the
                # BARE QQ number (see quickLoginExample.bat) — a "-q" prefix
                # is NOT recognized and silently falls back to QR login.
                args += [qq_num]
            proc = subprocess.Popen(
                args,
                cwd=base,               # injector resolves relative assets from its dir
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                startupinfo=si,
                close_fds=True,
            )
        except OSError as exc:
            return False, 'cannot start napcat: {}'.format(exc)

        _detached_procs.append(proc)
        _napcat_launching_ts = time.time()
        return True, 'launched'


def napcat_start():
    """Start the NapCat bridge if it is not already running."""
    ok, why = ensure_napcat()
    return {'ok': ok, 'msg': why}


def napcat_stop():
    """Stop the NapCat bridge: kill the injected QQ instance.

    Target selection: port 3001 (OneBot WS) first; if the bot has not
    finished logging in, fall back to the NapCat WebUI port (6099). Safety
    guard: only kill a PID whose image is qq.exe. The user's daily QQ
    never listens on these ports, so it can never be matched here.
    """
    pid = _get_port_pid(NAPCAT_PORT) or _get_port_pid(NAPCAT_WEBUI_PORT)
    if pid is None:
        return {'ok': False, 'msg': 'napcat not running'}
    image = _process_image(pid)
    if image != 'qq.exe':
        return {'ok': False, 'pid': pid,
                'msg': 'port {} is held by {} (PID {}), not a napcat instance - refusing to kill'.format(
                    NAPCAT_PORT, image or 'unknown', pid)}
    run_capture(['taskkill', '/F', '/T', '/PID', str(pid)])
    if (_wait_port_closed(NAPCAT_PORT, 10)
            and _wait_port_closed(NAPCAT_WEBUI_PORT, 10)):
        return {'ok': True, 'pid': pid, 'msg': 'napcat stopped (PID {})'.format(pid)}
    return {'ok': False, 'pid': pid, 'msg': 'napcat port still in use'}


def napcat_restart():
    """Stop NapCat if running, then start it again."""
    if napcat_status()['running']:
        napcat_stop()
    return napcat_start()


def status():
    pid = get_dsh_pid()
    return {'running': pid is not None, 'pid': pid}


def tail(path, n_lines=500, max_bytes=256 * 1024):
    """Read the tail of a log file; utf-8 first, gbk fallback."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        try:
            text = data.decode('utf-8')
        except UnicodeDecodeError:
            text = data.decode('gbk', errors='replace')
        lines = text.splitlines()
        return '\n'.join(lines[-n_lines:])
    except OSError:
        return ''


# --------------------------------------------------------------------------
# WSL support: control the dsh instance running inside a WSL distro the
# same way as the Windows one (start/stop/restart/status + logs).
#
# Every WSL script is fed to `wsl -d <distro> -- bash -s` via stdin:
# passing scripts as command-line arguments is unreliable (wsl.exe mangles
# variable assignments), so scripts always arrive on stdin.
# --------------------------------------------------------------------------

WSL_DISTRO_DEFAULT = 'Ubuntu-24.04'
WSL_AVAIL_TTL = 10.0           # seconds before re-probing wsl availability
WSL_STATUS_TTL = 3.0           # status cache window

_wsl_cache = {}


def wsl_list_distros():
    """Return installed WSL distros, default first (UTF-16LE aware)."""
    try:
        p = subprocess.run(
            ['wsl', '-l', '-q'], capture_output=True, timeout=15,
            creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return []
    raw = p.stdout
    text = None
    for enc in ('utf-16-le', 'utf-8'):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        return []
    distros, default = [], None
    for line in text.splitlines():
        line = line.strip().lstrip('\xef\xbb\xbf').strip('\x00').strip()
        if not line:
            continue
        if line.startswith('*'):
            default = line.lstrip('*').strip()
        else:
            distros.append(line)
    if default:
        distros.insert(0, default)
    return distros


def wsl_distro():
    """Pick the WSL distro to control (cached)."""
    d = _wsl_cache.get('distro')
    if not d:
        distros = wsl_list_distros()
        d = distros[0] if distros else WSL_DISTRO_DEFAULT
        _wsl_cache['distro'] = d
    return d


def wsl_run(script, timeout=60):
    """Run a bash script inside the WSL distro via stdin.

    Returns (rc, stdout, stderr); rc < 0 means the wsl.exe call itself
    failed (missing executable / timeout), rc == 0 is script success.
    """
    try:
        p = subprocess.run(
            ['wsl', '-d', wsl_distro(), '--', 'bash', '-s'],
            input=script.encode('utf-8'),
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        out = p.stdout.decode('utf-8', errors='replace')
        err = p.stderr.decode('utf-8', errors='replace')
        return p.returncode, out, err
    except FileNotFoundError:
        return -1, '', 'wsl.exe not found'
    except subprocess.TimeoutExpired:
        return -2, '', 'timeout'
    except OSError as exc:
        return -3, '', str(exc)


def wsl_available(timeout=8):
    """True if wsl.exe runs and the configured distro responds (cached)."""
    now = time.time()
    if now - _wsl_cache.get('avail_ts', 0) < WSL_AVAIL_TTL:
        return _wsl_cache.get('avail_ok', False)
    rc, _, _ = wsl_run('true', timeout=timeout)
    _wsl_cache['avail_ts'] = now
    _wsl_cache['avail_ok'] = rc == 0
    return rc == 0


def wsl_home():
    """WSL home directory of the controlling user (cached)."""
    home = _wsl_cache.get('home')
    if not home:
        rc, out, _ = wsl_run('echo "$HOME"', timeout=15)
        home = out.strip() if rc == 0 and out.strip() else '/home/dev'
        _wsl_cache['home'] = home
    return home


def wsl_dsh_bin():
    """Absolute path of the dsh CLI inside WSL (cached), or ''."""
    dsh = _wsl_cache.get('dsh')
    if dsh is None:
        dsh = ''
        rc, out, _ = wsl_run(
            'echo "$HOME"/.local/node-v*/bin/dsh', timeout=15)
        if rc == 0:
            for c in out.splitlines():
                c = c.strip()
                if c and not c.endswith('*'):
                    dsh = c
                    break
        _wsl_cache['dsh'] = dsh
    return dsh


def wsl_get_pid():
    """Return the PID listening on :3080 inside WSL, or None."""
    rc, out, _ = wsl_run(
        'ss -ltnp 2>/dev/null | grep ":3080 " | head -1', timeout=15)
    if rc != 0:
        return None
    m = re.search(r'pid=(\d+)', out)
    if m:
        return int(m.group(1))
    return None


# --------------------------------------------------------------------------
# WSL VM lifetime: hold-open client
#
# WSL2 shuts the VM down (vmIdleTimeout, ~60s) after the last wsl.exe client
# exits - killing every process inside it, including a nohup'd dsh. A live
# wsl.exe client keeps the VM alive, so after starting the WSL dsh we spawn
# one and keep it until the WSL dsh is stopped.
# --------------------------------------------------------------------------

def _wsl_hold_open():
    """Spawn a persistent wsl.exe client so the WSL VM never idles out."""
    if _wsl_cache.get('hold'):
        return
    try:
        p = subprocess.Popen(
            ['wsl', '-d', wsl_distro(), '--', 'sleep', 'infinity'],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    except OSError:
        return
    _detached_procs.append(p)
    _wsl_cache['hold'] = p


def _wsl_release_hold():
    """Kill the hold-open client, letting the VM idle out after the dsh stops."""
    p = _wsl_cache.pop('hold', None)
    if p:
        try:
            p.terminate()
        except OSError:
            pass


def wsl_is_running():
    return wsl_get_pid() is not None


def wsl_status():
    """Status dict, cached for WSL_STATUS_TTL seconds."""
    now = time.time()
    if now - _wsl_cache.get('status_ts', 0) < WSL_STATUS_TTL:
        return dict(_wsl_cache.get('status', {'running': False, 'pid': None}))
    pid = wsl_get_pid()
    st = {'running': pid is not None, 'pid': pid}
    _wsl_cache['status'] = st
    _wsl_cache['status_ts'] = now
    return st


def wsl_wait_port(timeout_s=90):
    """Poll inside WSL until :3080 is LISTENING; return (ok, pid)."""
    script = (
        'for i in $(seq 1 {t}); do\n'
        '  if ss -ltnp 2>/dev/null | grep -q ":3080 "; then\n'
        '    ss -ltnp 2>/dev/null | grep ":3080 " | head -1\n'
        '    exit 0\n'
        '  fi\n'
        '  sleep 1\n'
        'done\n'
        'exit 1\n'
    ).format(t=timeout_s)
    rc, out, _ = wsl_run(script, timeout=timeout_s + 15)
    if rc == 0:
        m = re.search(r'pid=(\d+)', out)
        return True, int(m.group(1)) if m else None
    return False, None


def wsl_wait_stopped(timeout_s=10):
    """Poll inside WSL until nothing is LISTENING on :3080."""
    script = (
        'for i in $(seq 1 {t}); do\n'
        '  if ! ss -ltn 2>/dev/null | grep -q ":3080 "; then exit 0; fi\n'
        '  sleep 1\n'
        'done\n'
        'exit 1\n'
    ).format(t=timeout_s)
    rc, _, _ = wsl_run(script, timeout=timeout_s + 15)
    return rc == 0


def _wsl_err_tail(max_bytes=2048):
    paths = wsl_log_paths()
    if not paths:
        return ''
    try:
        size = os.path.getsize(paths['err'])
        with open(paths['err'], 'rb') as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        return data.decode('utf-8', errors='replace').strip()[-500:]
    except OSError:
        return ''


def _wsl_unc(rel):
    """Map a WSL-relative path (e.g. 'home/dev/.dsh/x.log') to a UNC path."""
    return '\\\\wsl$\\{}\\{}'.format(wsl_distro(), rel.replace('/', '\\'))


def wsl_log_paths():
    """UNC paths of the WSL dsh logs, or None when WSL is unavailable."""
    if not wsl_available():
        return None
    home = wsl_home()
    if not home:
        return None
    base = _wsl_unc(home.lstrip('/') + '/.dsh')
    return {
        'web': base + '\\dsh-web.log',
        'err': base + '\\dsh-web.log.err',
    }


def wsl_ui_url(timeout=2):
    """Pick the best URL for the WSL dsh web UI, or ''.

    Candidates, in order:
      1. the fixed local portproxy entry (127.0.0.1:3081) when it answers -
         localhost is immune to the system proxy and stable across WSL IP
         changes;
      2. the WSL IP directly (http://<ip>:3080) - valid while the WSL dsh
         listens on 0.0.0.0;
      3. the cloudflared quick-tunnel URL from the WSL web log, when present
         (public fallback).
    """
    # 1) fixed local portproxy entry
    try:
        with socket.create_connection(('127.0.0.1', 3081), timeout=timeout):
            return 'http://127.0.0.1:3081'
    except OSError:
        pass
    # 2) WSL IP direct
    try:
        rc, out, _ = wsl_run('hostname -I', timeout=10)
        ip = (out or '').strip().split()[0] if rc == 0 else ''
        if ip:
            return 'http://{}:{}'.format(ip, PORT)
    except Exception:
        pass
    # 3) cloudflared quick-tunnel URL from the WSL web log
    try:
        paths = wsl_log_paths()
        if paths:
            m = re.search(
                r'https://[a-z0-9-]+\.trycloudflare\.com',
                tail(paths['web'], n_lines=300, max_bytes=256 * 1024))
            if m:
                return m.group(0)
    except Exception:
        pass
    return ''


def wsl_open_ui():
    """Open the WSL dsh web UI (best effort).

    Preferred: the dedicated "DeepSeek Harness-wsl" Chrome app shortcut;
    fallback: the best URL (fixed portproxy entry / WSL IP / tunnel).
    """
    try:
        if os.path.isfile(PWA_WSL_LNK):
            os.startfile(PWA_WSL_LNK)
            _remember_pwa('wsl')
            return True
    except OSError:
        pass
    url = wsl_ui_url()
    if not url:
        return False
    try:
        os.startfile(url)
        _remember_pwa('wsl')
        return True
    except OSError:
        return False


def wsl_start_dsh():
    """Start dsh web inside WSL, detached, logging into ~/.dsh.

    NAT mode (default WSL2) gives WSL its own loopback, so the WSL dsh can
    run alongside the Windows dsh, both listening on :3080 independently.
    """
    if wsl_is_running():
        return {'ok': False, 'msg': 'WSL dsh already running'}
    if not wsl_available():
        return {'ok': False, 'msg': 'WSL unavailable'}
    dsh = wsl_dsh_bin()
    if not dsh:
        return {'ok': False, 'msg': 'dsh CLI not found inside WSL'}
    script = (
        'mkdir -p "$HOME/.dsh"\n'
        'cd "$HOME"\n'
        # Non-interactive `bash -s` does not load ~/.bashrc, so `node` (and the
        # dsh shebang's `env node`) is not on PATH; derive the bin dir from the
        # resolved dsh path instead.
        'export PATH="$(dirname "{dsh}"):$PATH"\n'
        # setsid detaches the dsh from the wsl.exe session: without it the
        # session teardown on stdin EOF kills the backgrounded dsh. The sleep
        # after the launch gives setsid + node init time to finish before the
        # session is torn down - quitting instantly races the teardown and
        # the dsh dies (verified empirically).
        # --no-open: same reason as the Windows start path - the web-app
        # plugin would otherwise try to hand the URL to a browser (there is
        # none inside WSL) while wsl_open_ui() below already opens the PWA.
        'setsid nohup "{dsh}" web --no-open >> "$HOME/.dsh/dsh-web.log" 2>> "$HOME/.dsh/dsh-web.log.err" < /dev/null &\n'
        'sleep 3\n'
        'echo "LAUNCHED $!"\n'
    ).format(dsh=dsh)
    rc, out, err = wsl_run(script, timeout=30)
    if rc != 0:
        return {'ok': False, 'msg': 'cannot start WSL dsh: {}'.format(
            (err or out).strip() or 'wsl call failed')}
    ok, pid = wsl_wait_port(90)
    if ok:
        # Keep the VM alive: without a live wsl.exe client the VM idles out
        # (~60s) and kills the dsh we just started.
        _wsl_hold_open()
        # Mirror the Windows behaviour: open the web UI after a successful
        # start/restart (best effort - no UI when nothing answers).
        wsl_open_ui()
        return {'ok': True, 'pid': pid, 'msg': 'started (PID {})'.format(pid)}
    tail = _wsl_err_tail()
    msg = 'start failed'
    if tail:
        msg += ': ' + tail
    return {'ok': False, 'msg': msg}


def wsl_stop_dsh():
    """Stop the WSL dsh: kill the node process listening on :3080.

    Safety guard, same as the Windows path: only kill a PID whose image
    is node; anything else holding the port is left alone.
    """
    pid = wsl_get_pid()
    if pid is None:
        _wsl_release_hold()
        return {'ok': False, 'msg': 'WSL dsh not running'}
    # node renames its main thread to "MainThread", so `ps -o comm` reads
    # MainThread rather than node; judge from the cmdline instead.
    rc, out, _ = wsl_run(
        'tr "\\0" " " < /proc/{pid}/cmdline 2>/dev/null | tr -d "\\n"'.format(pid=pid),
        timeout=15)
    cmdline = (out or '').strip().lower()
    if 'node' not in cmdline:
        return {'ok': False, 'pid': pid,
                'msg': 'port 3080 is held by {} (PID {}), not a dsh process - refusing to kill'.format(
                    cmdline[:40] or 'unknown', pid)}
    wsl_run('kill {pid} 2>/dev/null; pkill -P {pid} 2>/dev/null; true'.format(pid=pid),
            timeout=15)
    if wsl_wait_stopped(10):
        # dsh is down; release the hold-open client so the VM can idle out.
        _wsl_release_hold()
        return {'ok': True, 'pid': pid, 'msg': 'stopped (PID {})'.format(pid)}
    return {'ok': False, 'pid': pid, 'msg': 'port 3080 still in use'}


def wsl_restart_dsh():
    """Close the WSL PWA window, stop the WSL dsh if running, start again."""
    ok, why = close_pwa('wsl')
    audit('pwa-close-wsl', ok, why)
    if wsl_is_running():
        wsl_stop_dsh()
    return wsl_start_dsh()
