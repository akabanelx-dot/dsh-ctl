# -*- coding: utf-8 -*-
"""
dsh_control.py - core control logic for the DeepSeek Harness (dsh) service.

All paths are derived from environment variables at runtime; nothing is
hard-coded, so this works regardless of the Windows user name.
"""

import ctypes
from ctypes import wintypes
import glob
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
from datetime import datetime

def _env_dir(name, fallback=''):
    """Read a directory-ish environment variable, tolerating a missing one.

    Every path in this module is derived from the environment (no hard-coded
    user name), but an *empty* variable used to silently degrade paths into
    relative ones - e.g. when APPDATA is unset, DSH_DIR became 'DeepSeekHarness'
    and the token log was written next to the process' working directory
    instead of under the roaming profile. Always fall back to a real absolute
    directory so the tool behaves the same from Explorer, a service or a
    POSIX-flavoured shell.
    """
    value = (os.environ.get(name) or '').strip().strip('"')
    if value and os.path.isabs(value):
        return value
    if fallback and os.path.isabs(fallback):
        return fallback
    return value


_USERPROFILE = _env_dir('USERPROFILE', os.path.expanduser('~'))
APPDATA = _env_dir('APPDATA', os.path.join(_USERPROFILE, 'AppData', 'Roaming'))
LOCALAPPDATA = _env_dir('LOCALAPPDATA',
                        os.path.join(_USERPROFILE, 'AppData', 'Local'))
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

# --------------------------------------------------------------------------
# DSH >= 0.1.5 Web 入口鉴权适配
#
# 0.1.5 起 `dsh web` 启动时把「带入口 token 的 URL」打印到 stdout
# （形如 http://127.0.0.1:3080/?token=...），裸 `/` 一律 401
# "dsh web authentication required; reopen the URL printed by dsh web."。
# 本工具把 dsh 的 stdout 重定向到 WEB_LOG，token 因此就在日志里：启动就绪后
# 读出来、存盘、并用它打开窗口。用同一个 Chrome profile 打开还能把签名 cookie
# 种进 PWA 共用 profile，此后 PWA 快捷方式自己也能免 token 打开（cookie 有
# 有效期，过期后再由本工具重新种一次）。
#
# token 本身由内核的 processLaunchToken() 用 randomBytes 生成、只存在进程内存里
# —— **每个进程一份、仅存内存、无法从外部复原**，但在该进程存活期内可重复使用
# （不是一次性消耗）；换进程即换 token，旧 URL 立刻失效。
# --------------------------------------------------------------------------

UI_URL_FILE = os.path.join(DSH_DIR, 'dsh-web.url')
TOKEN_RE = re.compile(r'dsh web:\s*(http://[^\s]*?token=[A-Za-z0-9_\-]+)')

# PWA 快捷方式所用的 Chrome profile（从 .lnk 里读，取不到则 Default）。必须与
# PWA 同一个 profile，种下的 cookie 才会被 PWA 窗口共享。
CHROME_PROFILE_DEFAULT = 'Default'
_PROGRAM_FILES = _env_dir('ProgramFiles', r'C:\Program Files')
_PROGRAM_FILES_X86 = _env_dir('ProgramFiles(x86)', r'C:\Program Files (x86)')
CHROME_EXE_CANDIDATES = (
    os.path.join(_PROGRAM_FILES, 'Google', 'Chrome', 'Application', 'chrome.exe'),
    os.path.join(_PROGRAM_FILES_X86, 'Google', 'Chrome', 'Application', 'chrome.exe'),
    os.path.join(LOCALAPPDATA, 'Google', 'Chrome', 'Application', 'chrome.exe'),
)
# node 定位：优先官方安装目录（PATH 里第一个 node 未必是最干净的那个）
NODE_EXE_CANDIDATES = (
    os.path.join(_PROGRAM_FILES, 'nodejs', 'node.exe'),
    NODE_EXE,
)

# 0.1.5 给 profile boot 的写锁加了 deadline；被强杀的进程留下的锁会让下一次启动
# 直接失败（atomic-write: timed out waiting for the writer lock）。这里在启动前
# 清理「内容是一个已消失 PID」的锁；task-board\ledger-v2.lock 是 JSON，绝不碰。
DSH_HOME = os.path.join(_USERPROFILE, '.dsh')
STALE_LOCK_DIRS = (DSH_HOME, os.path.join(DSH_HOME, 'profiles'))
_PROXY_VARS = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY',
               'http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')

# keep a module-level reference so GC never closes the subprocess handles
_detached_procs = []


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def find_node():
    """Locate node.exe: official install dir, then the npm copy, then PATH."""
    for cand in NODE_EXE_CANDIDATES + (shutil.which('node'),):
        if cand and os.path.isfile(cand):
            return cand
    return None


def _registry_path():
    """Real user PATH from the registry (Machine + User), or ''.

    The tray must hand dsh an environment whose PATH is the one a normal
    user session has: if it inherits a developer/agent shell's PATH, the
    managed runtimes there win and dsh's children resolve the wrong
    interpreter. Concretely the Lingshu (灵枢) bridge shells out to
    `python -m aeis.mcp.server`; pointing at a Python without `aeis`
    makes it fail handshake in a loop on every boot.
    """
    try:
        import winreg
    except ImportError:
        return ''
    parts = []
    for hive, sub in (
            (winreg.HKEY_LOCAL_MACHINE,
             r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment'),
            (winreg.HKEY_CURRENT_USER, r'Environment')):
        try:
            with winreg.OpenKey(hive, sub) as key:
                val, _kind = winreg.QueryValueEx(key, 'Path')
            if val:
                parts.append(os.path.expandvars(val))
        except OSError:
            continue
    return os.pathsep.join(parts)


def child_env():
    """Environment for the dsh child process.

    Three 0.1.5-era hazards are neutralised here:

    1. PATH — replaced with the registry PATH so dsh's children (notably the
       Lingshu bridge's `python`) resolve to the user's real interpreters.
    2. outbound proxies — DSH >= 0.1.5 honours HTTP_PROXY / HTTPS_PROXY /
       ALL_PROXY / NO_PROXY from its launch environment for *every* outbound
       request (0.1.1-rc.2 ignored them). A stale proxy inherited from
       whatever launched the tray would silently reroute model traffic, so
       they are removed to preserve the pre-upgrade behaviour. Set them here
       explicitly to route dsh through Clash on purpose.
    3. NODE_OPTIONS — dropped as cheap insurance against an injected
       --require shim (a sandboxed parent can wrap fs.rm, which breaks
       dsh-atomic-write's lock release).
    """
    env = dict(os.environ)
    reg = _registry_path()
    if reg:
        env['PATH'] = reg
    for name in _PROXY_VARS:
        env.pop(name, None)
    env.pop('NODE_OPTIONS', None)
    return env


def _pid_alive(pid):
    """True when a process with this PID exists."""
    if not pid or pid <= 0:
        return False
    rc, out, _ = run_capture(
        ['tasklist', '/FI', 'PID eq {}'.format(pid), '/FO', 'CSV', '/NH'], timeout=10)
    if rc != 0:
        return True                     # cannot tell -> assume alive, never delete
    for line in out.splitlines():
        line = line.strip()
        if line.startswith('"') and str(pid) in line.split('","')[1:2][0]:
            return True
    return False


def clean_stale_locks():
    """Drop dead-owner writer locks left behind by a killed dsh.

    DSH >= 0.1.5 takes deadline-bounded file locks during profile boot
    (`dsh-atomic-write`), e.g. `~/.dsh/profiles/node_modules.lock` and
    `~/.dsh/.credentials.yaml.lock`. `taskkill /F` cannot run their cleanup,
    so the next start dies with "timed out waiting for the writer lock".

    Only files whose *entire* content is an integer PID owned by a process
    that no longer exists are removed. Anything else is left alone: e.g.
    `~/.dsh/task-board/ledger-v2.lock` holds JSON.
    """
    removed = []
    for root in STALE_LOCK_DIRS:
        if not os.path.isdir(root):
            continue
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            if not name.endswith('.lock'):
                continue
            path = os.path.join(root, name)
            try:
                if not os.path.isfile(path):
                    continue
                with open(path, 'r', encoding='utf-8', errors='ignore') as f:
                    raw = f.read(64).strip()
                pid = int(raw)
            except (OSError, ValueError):
                continue                # not a bare PID -> not ours to touch
            if not _pid_alive(pid):
                try:
                    os.remove(path)
                    removed.append(name)
                except OSError:
                    pass
    return removed


def _log_offset():
    """Current byte size of WEB_LOG (start of this launch's output)."""
    try:
        return os.path.getsize(WEB_LOG)
    except OSError:
        return 0


def ui_url_from_log(offset=0, attempts=40, delay=0.5):
    """Wait for and return the authenticated root URL printed by `dsh web`.

    `dsh web` prints `dsh web: http://127.0.0.1:3080/?token=...` once the
    server is ready. Re-reads only the bytes appended after `offset` so a
    token from an earlier run can never be picked up. Returns '' on timeout.
    """
    for _ in range(attempts):
        try:
            with open(WEB_LOG, 'r', encoding='utf-8', errors='replace') as f:
                f.seek(offset)
                chunk = f.read()
        except OSError:
            chunk = ''
        hits = TOKEN_RE.findall(chunk)
        if hits:
            return hits[-1]
        time.sleep(delay)
    return ''


def save_ui_url(url):
    """Persist the current launch's authenticated URL for later re-opening."""
    try:
        os.makedirs(DSH_DIR, exist_ok=True)
        with open(UI_URL_FILE, 'w', encoding='utf-8') as f:
            f.write(url)
    except OSError:
        pass


def load_ui_url():
    """Read the saved authenticated URL ('' when absent)."""
    try:
        with open(UI_URL_FILE, 'r', encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


def clear_ui_url():
    """Forget the saved URL (its token belongs to a dead process)."""
    try:
        os.remove(UI_URL_FILE)
    except OSError:
        pass


def find_chrome():
    """Path of chrome.exe, or ''."""
    for cand in CHROME_EXE_CANDIDATES:
        if cand and os.path.isfile(cand):
            return cand
    return ''


def chrome_profile():
    """Chrome profile the PWA shortcut uses (defaults to 'Default').

    Read straight out of the .lnk: the shortcut passes
    `--profile-directory=<name>` to chrome_proxy.exe, and only the same
    profile shares the signed browser cookie.
    """
    for lnk in (PWA_WINDOWS_LNK, PWA_WSL_LNK):
        try:
            with open(lnk, 'rb') as f:
                raw = f.read()
        except OSError:
            continue
        for match in re.finditer(rb'(?:--profile-directory=)([\x20-\x7e]{1,40})',
                                 raw):
            return match.group(1).decode('ascii', 'ignore').strip()
        for blob in re.findall(rb'(?:[\x20-\x7e]\x00){6,}', raw):
            text = blob.decode('utf-16-le', 'ignore')
            m = re.search(r'--profile-directory=([^\s"\x00]+)', text)
            if m:
                return m.group(1).strip()
    return CHROME_PROFILE_DEFAULT


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

def open_ui(url=None):
    """Open the DeepSeek Harness Web UI (best effort).

    DSH >= 0.1.5 requires the launch token in the URL; the bare origin always
    answers 401. So when an authenticated URL is known it MUST be used --
    otherwise the window shows only
    "dsh web authentication required; reopen the URL printed by dsh web.".

    Order:
      1. authenticated URL -> Chrome *app* window in the SAME profile the PWA
         shortcut uses. Chrome mints the signed cookie in that profile and
         lands on clean `/`, so the PWA shortcut keeps working afterwards on
         its own (until the cookie ages out).
      2. authenticated URL -> default browser (still no stray tab: it
         redirects to `/`, and no duplicate tab appears because dsh runs with
         --no-open).
      3. no token -> legacy PWA shortcut, then the bare origin (may 401).
    """
    # 1) app window on the authenticated URL, in the PWA's own profile
    if url and url.startswith('http'):
        chrome = find_chrome()
        if chrome:
            try:
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 1                  # SW_SHOWNORMAL
                proc = subprocess.Popen(
                    [chrome,
                     '--profile-directory={}'.format(chrome_profile()),
                     '--app={}'.format(url)],
                    cwd=os.path.dirname(chrome),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    startupinfo=si,
                    close_fds=True,
                )
                _detached_procs.append(proc)
                _remember_pwa('windows')
                return
            except OSError:
                pass
        try:
            os.startfile(url)
            _remember_pwa('windows')
            return
        except OSError:
            pass

    # 2) legacy: the dedicated PWA shortcut
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

    # 3) last resort: the plain local URL (401 under DSH >= 0.1.5)
    try:
        os.startfile('http://127.0.0.1:{}'.format(PORT))
        _remember_pwa('windows')
    except OSError:
        pass


def _token_is_live(url, timeout=5):
    """True when the server accepts <url>'s token.

    `authorizeIndex` answers 303 (minting the cookie) for a matching token and
    401 for anything else, so a single non-following request tells them apart.
    Deliberately bypasses any proxy the tray process inherited.
    """
    try:
        parts = urllib.parse.urlparse(url)
        path = parts.path or '/'
        if parts.query:
            path += '?' + parts.query
        conn = http.client.HTTPConnection(
            parts.hostname or '127.0.0.1', parts.port or PORT, timeout=timeout)
        conn.request('GET', path)
        resp = conn.getresponse()
        code = resp.status
        resp.read()
        conn.close()
        return code in (200, 301, 302, 303)
    except Exception:
        return False


def live_ui_url():
    """A currently-acceptable authenticated URL, or ''.

    The saved URL is tried first, then the most recent token lines still in
    WEB_LOG - each is probed against the server, because a token only belongs
    to the process that printed it. This keeps "Open Web UI" useful even when
    the running dsh was started before this build of dsh-ctl existed.
    """
    candidates = []
    saved = load_ui_url()
    if saved:
        candidates.append(saved)
    try:
        with open(WEB_LOG, 'r', encoding='utf-8', errors='replace') as f:
            text = f.read()
    except OSError:
        text = ''
    for url in reversed(TOKEN_RE.findall(text)[-5:]):
        if url not in candidates:
            candidates.append(url)
    for url in candidates:
        if _token_is_live(url):
            save_ui_url(url)            # remember the one that actually works
            return url
    return ''


def open_ui_action():
    """Tray action: (re)open the UI with a token the live server accepts."""
    if not is_running():
        return {'ok': False, 'msg': 'dsh is not running'}
    url = live_ui_url()
    if not url:
        return {'ok': False,
                'msg': 'no live token found - use Restart dsh to mint one'}
    open_ui(url)
    return {'ok': True, 'msg': 'opened UI'}


def open_pwa():
    """Backwards-compatible alias: open the UI (authenticated when possible)."""
    open_ui(load_ui_url() or None)


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

    # Ensure the QQ bot chain (SnowLuma + qq-bridge) is up before dsh starts
    # (qq-bridge talks to the DSH Web API). Best effort: a failure here is
    # audited but must never block dsh itself.
    ok, why = ensure_bot()
    if not ok:
        audit('bot', False, why)

    node = find_node()
    if not node:
        return {'ok': False, 'msg': 'node.exe not found'}
    if not os.path.isfile(DSH_BIN):
        return {'ok': False, 'msg': 'dsh entry not found: ' + DSH_BIN}

    os.makedirs(DSH_DIR, exist_ok=True)
    rotate_logs()

    # DSH >= 0.1.5: a writer lock left behind by the previous kill makes this
    # boot fail outright ("timed out waiting for the writer lock"). Clear the
    # dead-owner ones before launching, and forget the previous launch's URL
    # (its token belongs to a process that is going away).
    stale = clean_stale_locks()
    if stale:
        audit('stale-lock', True, 'cleared: ' + ', '.join(stale))
    clear_ui_url()
    # offset = where THIS launch's stdout begins, so only its own token is read
    offset = _log_offset()

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
            # sanitised environment: real user PATH (so the Lingshu bridge
            # resolves the right `python`), no inherited outbound proxies
            # (DSH >= 0.1.5 now honours them), no injected NODE_OPTIONS.
            env=child_env(),
        )
    except OSError as exc:
        out_f.close()
        err_f.close()
        return {'ok': False, 'msg': 'cannot start dsh: {}'.format(exc)}

    _detached_procs.append(proc)

    ok, reason = wait_port_ready(proc, 90)
    if ok:
        # dsh prints the authenticated root URL right around the time it binds
        # the port; read only this launch's slice of the log and keep it, so the
        # tray can re-open the window later without another restart.
        url = ui_url_from_log(offset, attempts=60, delay=0.5)
        if url:
            save_ui_url(url)
            audit('ui-token', True, 'captured')
        else:
            audit('ui-token', False, 'token line not found in ' + WEB_LOG)
        open_ui(url or None)
        msg = 'started (PID {})'.format(proc.pid)
        if not url:
            msg += ' (no token captured - the window may show 401)'
        return {'ok': True, 'pid': proc.pid, 'msg': msg}

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
        # The token died with the process, and the kill could not release
        # dsh's own writer locks: drop both so the next start is clean.
        clear_ui_url()
        clean_stale_locks()
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
# QQ 机器人（SnowLuma + qq-bridge）
#
# qq-bridge 链路：
#   SnowLuma（OneBot v11：WS 3001 + HTTP 3000，QQ 客户端实现）
#   └─ qq-bridge 桥接进程（DSH Web API 3080 + 控制台 3100）
# dsh-ctl 在启动/重启 dsh 时最佳努力确保机器人链路可用（失败只审计、
# 不阻塞 dsh 本身），并提供托盘 Start/Stop/Restart/Status 控制。
# --------------------------------------------------------------------------

BOT_WS_PORT = 3001             # SnowLuma OneBot WebSocket 端口
BOT_HTTP_PORT = 3000           # SnowLuma OneBot HTTP API 端口
QQ_AGENT_PORT = 3210           # QQ-agent 控制台 / 服务端口
SNOWLUMA_DIR_DEFAULT = os.path.join('C:', os.sep, 'SnowLuma')
QQ_AGENT_DIR_DEFAULT = os.path.join('F:', os.sep, 'WorkSpace', 'QQ-agent')
BOT_LAUNCH_WINDOW = 15.0
# SnowLuma 的 WebUI 端口：首个实例占 5099，重复启动会顺延到 5100/5101…
# 用它判断"SnowLuma 进程在跑但没接入 QQ"（此时绝不能再去拉新实例）。
SNOWLUMA_WEBUI_PORTS = (5099, 5100, 5101, 5102, 5103)

_bot_lock = threading.Lock()
_bot_launching_ts = 0.0


def bot_status():
    """机器人链路状态：SnowLuma（OneBot 3001）+ QQ-agent（控制台 3210）。"""
    snowluma = _get_port_pid(BOT_WS_PORT) is not None
    agent = _get_port_pid(QQ_AGENT_PORT) is not None
    webui_port, webui_pid = _snowluma_webui()
    return {
        'running': snowluma and agent,
        'snowluma': snowluma,
        # SnowLuma 有进程但 OneBot 没起来 = 未接入 QQ（死实例／待登录）
        'snowluma_no_qq': (not snowluma) and webui_pid is not None,
        'snowluma_webui_port': webui_port,
        'agent': agent,
        'pid': _get_port_pid(QQ_AGENT_PORT),
    }


def _snowluma_webui():
    """返回正在监听的 SnowLuma WebUI (port, pid)；都没有则 (None, None)。"""
    for port in SNOWLUMA_WEBUI_PORTS:
        pid = _get_port_pid(port)
        if pid is not None:
            return port, pid
    return None, None


def _snowluma_all_pids():
    """所有 SnowLuma 实例 PID（OneBot 端口 + 各 WebUI 端口，去重）。"""
    pids = []
    for port in (BOT_WS_PORT,) + SNOWLUMA_WEBUI_PORTS:
        pid = _get_port_pid(port)
        if pid is not None and pid not in pids:
            pids.append(pid)
    return pids


def _kill_parent_launcher(pid):
    """若 pid 的父进程是启动 SnowLuma 的 cmd（launcher.bat），一并结束，
    避免留下停在 pause 的隐藏控制台窗口。父进程不匹配时不动。"""
    try:
        out = run_capture(['powershell', '-NoProfile', '-Command',
                           "(Get-CimInstance Win32_Process -Filter 'ProcessId={}')"
                           " | Select-Object -ExpandProperty ParentProcessId".format(int(pid))])
        ppid = ''.join(ch for ch in str(out) if ch.isdigit())
        if not ppid:
            return
        cmdline = run_capture(['powershell', '-NoProfile', '-Command',
                               "(Get-CimInstance Win32_Process -Filter 'ProcessId={}')"
                               " | Select-Object -ExpandProperty CommandLine".format(int(ppid))])
        if 'launcher.bat' in str(cmdline) or 'SnowLuma' in str(cmdline):
            run_capture(['taskkill', '/F', '/PID', ppid])
    except Exception:
        pass


def _snowluma_dir():
    """SnowLuma 目录：$DSH_SNOWLUMA_DIR 或默认 C:/SnowLuma。"""
    return os.environ.get('DSH_SNOWLUMA_DIR') or SNOWLUMA_DIR_DEFAULT


def _qq_agent_dir():
    """QQ-agent 目录：$DSH_QQ_AGENT_DIR 或默认 F:/WorkSpace/QQ-agent。"""
    return os.environ.get('DSH_QQ_AGENT_DIR') or QQ_AGENT_DIR_DEFAULT


def _wait_port_open(port, timeout_s=60):
    """Wait until something is LISTENING on 127.0.0.1:<port>."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _get_port_pid(port) is not None:
            return True
        time.sleep(0.5)
    return False


def _launch_hidden(target, cwd, log_name=None):
    """隐藏窗口启动子进程。

    target 为 .bat 路径（用 cmd /c 包装）或 argv 列表（直接启动）。
    log_name 非空时把 stdout/stderr 落到 <DSH_DIR>/<log_name> 与同名 .err。
    """
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        if isinstance(target, (list, tuple)):
            argv = list(target)
        else:
            argv = ['cmd', '/c', str(target)]
        if log_name:
            out_f = open(os.path.join(DSH_DIR, log_name), 'ab')
            err_f = open(os.path.join(DSH_DIR, log_name + '.err'), 'ab')
        else:
            out_f = open(os.devnull, 'wb')
            err_f = open(os.devnull, 'wb')
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=out_f,
            stderr=err_f,
            startupinfo=si,
            creationflags=0,
            close_fds=True,
        )
        out_f.close()
        err_f.close()
        _detached_procs.append(proc)
        return True, 'launched PID {}'.format(proc.pid)
    except OSError as exc:
        return False, str(exc)


def bot_start():
    """启动机器人链路（SnowLuma + QQ-agent），缺哪块补哪块。

    QQ-agent 以 headless 服务模式运行（node src/server.js）：控制台在
    http://127.0.0.1:3210，机器人核心逻辑与桌面端一致，且不依赖 Electron
    （本机 electron 的 postinstall 被 allow-scripts 策略拦截，桌面壳未装）。
    """
    status = bot_status()
    launched = []
    # 1) SnowLuma（QQ 协议端 / OneBot v11）
    if not status['snowluma']:
        # 护栏：SnowLuma 已在跑（WebUI 端口在听）却没开 OneBot，说明它没接入 QQ
        # （机器人 QQ 未登录/未注入）。此时绝不能再拉新实例——否则会累积一堆
        # 只占 WebUI 端口的僵尸实例（5099→5100→5101…）而 3001 永远不开。
        webui_port, webui_pid = _snowluma_webui()
        if webui_pid is not None:
            return {'ok': False, 'pid': webui_pid,
                    'msg': ('SnowLuma 已在运行（WebUI :{}）但没有接入 QQ：'
                            'OneBot :{} 未监听。请打开 http://127.0.0.1:{}/ 确认'
                            '机器人 QQ（3288828554）已登录/已注入，再点 Start。'
                            ).format(webui_port, BOT_WS_PORT, webui_port)}
        launcher = os.path.join(_snowluma_dir(), 'launcher.bat')
        if not os.path.isfile(launcher):
            return {'ok': False, 'msg': 'snowluma launcher missing: ' + launcher}
        ok, why = _launch_hidden(launcher, _snowluma_dir(), 'bot-snowluma.log')
        if not ok:
            return {'ok': False, 'msg': 'snowluma start failed: ' + why}
        launched.append('snowluma')
        if not _wait_port_open(BOT_WS_PORT, 90):
            port, pid = _snowluma_webui()
            hint = ('请在 SnowLuma WebUI（http://127.0.0.1:{}/）确认机器人 QQ 已登录/已注入'
                    ).format(port) if pid is not None else '请检查 SnowLuma 是否正常启动'
            return {'ok': False,
                    'msg': 'SnowLuma 起来了但 OneBot :{} 未监听——{}'.format(BOT_WS_PORT, hint)}
    # 2) QQ-agent（独立 QQ agent 应用）
    if not status['agent']:
        agent_dir = _qq_agent_dir()
        entry = os.path.join(agent_dir, 'src', 'server.js')
        if not os.path.isfile(entry):
            return {'ok': False, 'msg': 'QQ-agent entry missing: ' + entry}
        node = find_node()
        if not node:
            return {'ok': False, 'msg': 'node.exe not found'}
        ok, why = _launch_hidden([node, entry], agent_dir, 'bot-qq-agent.log')
        if not ok:
            return {'ok': False, 'msg': 'QQ-agent start failed: ' + why}
        launched.append('qq-agent')
        if not _wait_port_open(QQ_AGENT_PORT, 60):
            return {'ok': False,
                    'msg': 'QQ-agent did not listen on :{}'.format(QQ_AGENT_PORT)}
    msg = 'bot already running'
    if launched:
        msg = 'started: ' + ', '.join(launched)
    return {'ok': True, 'msg': msg}


def bot_stop():
    """停止机器人链路：先 QQ-agent（3210），再 SnowLuma（3001）。

    安全护栏：只杀监听这些端口且镜像名为 node.exe 的进程，绝不动其它 node。
    """
    stopped = []
    # 1) QQ-agent
    pid = _get_port_pid(QQ_AGENT_PORT)
    if pid is not None:
        image = _process_image(pid)
        if image != 'node.exe':
            return {'ok': False, 'pid': pid,
                    'msg': 'port {} held by {} - refusing to kill'.format(
                        QQ_AGENT_PORT, image or 'unknown')}
        run_capture(['taskkill', '/F', '/T', '/PID', str(pid)])
        stopped.append('qq-agent')
        _wait_port_closed(QQ_AGENT_PORT, 10)
    # 2) SnowLuma：清掉**全部**实例（含只占 WebUI 端口、未接入 QQ 的重复实例），
    #    否则重复实例会一直顺延占用 5100/5101… 且 OneBot 永远起不来。
    killed = 0
    for pid in _snowluma_all_pids():
        image = _process_image(pid)
        if image != 'node.exe':
            continue  # 端口被别人占了：跳过而不是误杀
        run_capture(['taskkill', '/F', '/T', '/PID', str(pid)])
        _kill_parent_launcher(pid)
        killed += 1
    if killed:
        stopped.append('snowluma x{}'.format(killed))
        _wait_port_closed(BOT_WS_PORT, 10)
    if not stopped:
        return {'ok': False, 'msg': 'bot not running'}
    return {'ok': True, 'msg': 'bot stopped ({})'.format(', '.join(stopped))}


def bot_restart():
    """重启机器人链路。"""
    if bot_status()['snowluma'] or bot_status()['agent']:
        bot_stop()
    return bot_start()


def ensure_bot():
    """确保机器人链路可用；缺失时启动（带启动窗口防并发）。"""
    global _bot_launching_ts
    now = time.time()
    with _bot_lock:
        if now - _bot_launching_ts < BOT_LAUNCH_WINDOW:
            return True, 'bot already starting'
        _bot_launching_ts = now
        st = bot_status()
        if st['snowluma'] and st['agent']:
            return True, 'bot running'
        result = bot_start()
        return result['ok'], result['msg']


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
        return chr(10).join(lines[-n_lines:])
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


# ---------------------------------------------------------------------------
# 手机远程访问（Phone Link）— Tailscale Serve + Caddy + remote-web-ui 配对
#
# 链路: 手机 → https://<TS_HOST>(Tailscale Serve 终结 TLS) → 127.0.0.1:8443
#       (Caddy: /pair* 透传真实主机名，其余 /api/* 把 Host 改写回本机权威)
#       → 127.0.0.1:19387 (桌面端 DSH)。
# 配对凭证 365 天，桌面端重启不失效；Caddy 不开机自启，由本工具按需启停。
# 2026-09-30 实测规则:
#   · /api/* 若不带 Host 改写，DSH 对非本机权威回 403；
#   · 配对路径若被改写 Host，pair-app 跳转会指向 127.0.0.1；
#   · X-Forwarded-Proto 必须强制 https，否则配对跳转生成 http:// 地址
#     （Tailscale Serve 只监听 443）。
# ---------------------------------------------------------------------------

REMOTE_PORT = 8443
REMOTE_DESKTOP_PORT = 19387
CADDY_DIR = os.path.join(_USERPROFILE, 'dsh-caddy')
CADDY_EXE = os.path.join(CADDY_DIR, 'caddy.exe')
CADDYFILE = os.path.join(CADDY_DIR, 'Caddyfile')
TS_HOST = 'laptop-rt4r6ce8.tail8b7e5b.ts.net'
PHONE_NAME = 'moricalliope'
TS_EXE_CANDIDATES = (
    os.path.join(_env_dir('ProgramFiles', r'C:\Program Files'),
                 'Tailscale', 'tailscale.exe'),
    'tailscale',
)


def _tailscale_exe():
    for cand in TS_EXE_CANDIDATES:
        if os.path.isabs(cand):
            if os.path.isfile(cand):
                return cand
        else:
            found = shutil.which(cand)
            if found:
                return found
    return None


def _remote_caddy_pid():
    """监听 8443 且镜像名含 caddy 的进程 PID，否则 None。"""
    pid = _get_port_pid(REMOTE_PORT)
    if pid is None:
        return None
    image = _process_image(pid)
    if image and 'caddy' in image.lower():
        return pid
    return None


def remote_serve_status():
    """返回 (configured, raw)：Tailscale Serve 是否已把 443 指向 8443。"""
    ts = _tailscale_exe()
    if not ts:
        return False, 'tailscale.exe not found'
    rc, out, _err = run_capture([ts, 'serve', 'status'], timeout=15)
    if rc != 0:
        return False, out
    return (TS_HOST in out and '8443' in out), out


def remote_status():
    """整条手机链路的状态。running=True 表示四件套全部就绪。"""
    caddy_pid = _remote_caddy_pid()
    dsh_pid = _get_port_pid(REMOTE_DESKTOP_PORT)
    serve_ok, _raw = remote_serve_status()
    tailnet = False
    phone = 'unknown'
    ts = _tailscale_exe()
    if ts:
        rc, out, _err = run_capture([ts, 'status'], timeout=15)
        tailnet = (rc == 0)
        for line in out.splitlines():
            if PHONE_NAME in line:
                phone = 'offline' if 'offline' in line else 'online'
    running = bool(caddy_pid) and serve_ok and bool(dsh_pid) and tailnet
    return {
        'running': running,
        'pid': caddy_pid,
        'caddy': bool(caddy_pid),
        'serve': serve_ok,
        'dsh': bool(dsh_pid),
        'tailnet': tailnet,
        'phone': phone,
    }


def remote_start():
    """按需拉起手机链路：Caddy（隐藏窗口）+ Tailscale Serve 规则（幂等）。

    不开机自启，也不动桌面端本体——桌面端 DSH（:19387）由用户正常启动。
    """
    if not os.path.isfile(CADDY_EXE):
        return {'ok': False, 'msg': 'caddy.exe missing: ' + CADDY_EXE}
    if not os.path.isfile(CADDYFILE):
        return {'ok': False, 'msg': 'Caddyfile missing: ' + CADDYFILE}
    ts = _tailscale_exe()
    if not ts:
        return {'ok': False, 'msg': 'tailscale.exe not found'}
    launched = []
    if _remote_caddy_pid() is None:
        ok, why = _launch_hidden(
            [CADDY_EXE, 'run', '--config', 'Caddyfile', '--adapter', 'caddyfile'],
            CADDY_DIR, 'remote-caddy.log')
        if not ok:
            return {'ok': False, 'msg': 'caddy start failed: ' + why}
        launched.append('caddy')
        if not _wait_port_open(REMOTE_PORT, 15):
            return {'ok': False,
                    'msg': 'caddy started but :{} not listening'.format(REMOTE_PORT)}
    serve_ok, _raw = remote_serve_status()
    if not serve_ok:
        rc, out, err = run_capture(
            [ts, 'serve', '--bg', '--https=443', 'http://127.0.0.1:8443'],
            timeout=30)
        serve_ok, raw = remote_serve_status()
        if not serve_ok:
            return {'ok': False,
                    'msg': 'tailscale serve failed: ' + (err or out or raw)[:180]}
        launched.append('tailscale serve')
    st = remote_status()
    msg = 'phone link up ({})'.format(' + '.join(launched) or 'already running')
    if not st['dsh']:
        msg += ' — 桌面端 DSH 未启动，先启动桌面端'
    if st['phone'] == 'offline':
        msg += ' — 手机 Tailscale 离线'
    return {'ok': True, 'msg': msg}


def remote_stop():
    """停掉链路：杀 Caddy（镜像守卫）+ 撤 Tailscale Serve 规则。不动桌面端。"""
    stopped = []
    pid = _remote_caddy_pid()
    if pid is not None:
        image = _process_image(pid)
        if image and 'caddy' not in image.lower():
            return {'ok': False, 'pid': pid,
                    'msg': 'port {} held by {} - refusing to kill'.format(
                        REMOTE_PORT, image or 'unknown')}
        run_capture(['taskkill', '/F', '/T', '/PID', str(pid)])
        _wait_port_closed(REMOTE_PORT, 10)
        stopped.append('caddy')
    ts = _tailscale_exe()
    if ts:
        serve_ok, _raw = remote_serve_status()
        if serve_ok:
            run_capture([ts, 'serve', '--https=443', 'off'], timeout=30)
            serve_ok, _raw2 = remote_serve_status()
            if serve_ok:
                # 兜底：本机只配置了这一条规则，reset 不会误伤别人
                run_capture([ts, 'serve', 'reset'], timeout=30)
            stopped.append('serve off')
    if not stopped:
        return {'ok': False, 'msg': 'phone link not running'}
    return {'ok': True, 'msg': 'phone link stopped ({})'.format(', '.join(stopped))}


def remote_pair():
    """签发一枚新的手机配对链接，复制到剪贴板并返回。

    配对链接约 10 分钟有效；配对一次，设备凭证 365 天，桌面端重启不失效。
    """
    if _get_port_pid(REMOTE_DESKTOP_PORT) is None:
        return {'ok': False,
                'msg': '桌面端 DSH 未启动（:19387 无监听），先启动桌面端再配对'}
    conn = http.client.HTTPConnection('127.0.0.1', REMOTE_DESKTOP_PORT, timeout=10)
    try:
        conn.request('POST', '/api/pair/issue', json.dumps({}),
                     {'Content-Type': 'application/json'})
        resp = conn.getresponse()
        raw = resp.read().decode('utf-8', 'replace')
    finally:
        conn.close()
    if resp.status != 200:
        return {'ok': False,
                'msg': 'pair/issue HTTP {}: {}'.format(resp.status, raw[:120])}
    try:
        data = json.loads(raw)
    except ValueError:
        return {'ok': False, 'msg': 'pair/issue returned non-JSON'}
    url = data.get('url')
    if not url:
        return {'ok': False, 'msg': 'pair/issue missing url: ' + raw[:120]}
    expires = ''
    if data.get('expiresAt'):
        try:
            expires = '，{} 前有效'.format(
                datetime.fromtimestamp(data['expiresAt'] / 1000).strftime('%H:%M'))
        except Exception:
            pass
    # 剪贴板：优先 Set-Clipboard（UTF-16 稳），失败退回 clip.exe
    rc, _out, _err = run_capture(
        ['powershell', '-NoProfile', '-Command',
         'Set-Clipboard -Value "{}"'.format(url)], timeout=15)
    if rc != 0:
        try:
            p = subprocess.Popen(['clip'], stdin=subprocess.PIPE,
                                 creationflags=CREATE_NO_WINDOW)
            p.communicate(url.encode('utf-16'))
        except OSError:
            pass
    return {'ok': True,
            'msg': '配对链接已复制到剪贴板{}，手机（Tailscale 在线）直接打开: {}'.format(
                expires, url)}
