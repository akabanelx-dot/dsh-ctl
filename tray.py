# -*- coding: utf-8 -*-
"""
tray.py - pystray icon, menu and notification helpers.

Menu callbacks only push actions onto a queue; they never touch tkinter
or block. The main thread owns all UI updates.
"""

import os
import sys

import pystray
from PIL import Image, ImageDraw

import dsh_control as dc


def _resource_path(name):
    """Resolve a bundled asset (PyInstaller onefile extracts to _MEIPASS)."""
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def make_icon():
    """Tray icon: use the program icon (DeepSeek Harness.ico) when bundled,
    otherwise fall back to app_icon.png, then to a simple drawn icon."""
    for name in ('DeepSeek Harness.ico', 'app_icon.png'):
        try:
            img = Image.open(_resource_path(name)).convert('RGBA')
            img.thumbnail((64, 64))
            return img
        except Exception:
            continue
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, 62, 62], radius=14, fill=(24, 119, 242, 255))
    d.rounded_rectangle([14, 14, 50, 50], radius=8, outline=(255, 255, 255, 230), width=4)
    d.line([24, 40, 32, 24, 40, 40], fill=(255, 255, 255, 255), width=5, joint='curve')
    return img


def status_text(_icon=None, _item=None):
    """Dynamic first menu row: current service state.
    pystray calls text callables as text(icon, item)."""
    st = dc.status()
    if st['running']:
        return 'State: Running (PID {})'.format(st['pid'])
    return 'State: Not running'


def wsl_status_text(_icon=None, _item=None):
    """Dynamic second menu row: WSL dsh state."""
    if not dc.wsl_available():
        return 'WSL State: unavailable'
    st = dc.wsl_status()
    if st['running']:
        return 'WSL State: Running (PID {})'.format(st['pid'])
    return 'WSL State: Not running'


def remote_status_text(_icon=None, _item=None):
    """Dynamic fourth menu row: phone remote link (Tailscale + Caddy) state."""
    st = dc.remote_status()
    if not st['tailnet']:
        return 'Phone Link: Tailscale not running'
    if not st['dsh']:
        return 'Phone Link: desktop DSH down'
    if st['running']:
        return 'Phone Link: Ready (Caddy PID {}, phone {})'.format(
            st['pid'], st['phone'])
    parts = []
    if st['caddy']:
        parts.append('Caddy up')
    if st['serve']:
        parts.append('Serve on')
    return 'Phone Link: Stopped ({})'.format(', '.join(parts) or 'all off')


def _make_cb(action, q):
    """Build a menu callback with exactly 2 positional args (icon, item).
    pystray requires callbacks to accept 2 args; the queue is captured
    by closure so it can never be overwritten by a menu argument."""
    def cb(icon, item):
        q.put((action,))
    return cb


def build_menu(q):
    """Build the tray menu. Callbacks enqueue ('action',) tuples."""
    return pystray.Menu(
        pystray.MenuItem(status_text, None, enabled=False),
        pystray.MenuItem(wsl_status_text, None, enabled=False),
        pystray.MenuItem(remote_status_text, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Start dsh', _make_cb('start', q)),
        pystray.MenuItem('Stop dsh', _make_cb('stop', q)),
        pystray.MenuItem('Restart dsh', _make_cb('restart', q)),
        # DSH >= 0.1.5: the bare origin returns 401, so re-opening needs the
        # launch token dsh-ctl captured at start.
        pystray.MenuItem('Open Web UI', _make_cb('open-ui', q)),
        pystray.MenuItem('Status', _make_cb('status', q)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('WSL dsh', pystray.Menu(
            pystray.MenuItem('Start WSL dsh', _make_cb('wsl-start', q)),
            pystray.MenuItem('Stop WSL dsh', _make_cb('wsl-stop', q)),
            pystray.MenuItem('Restart WSL dsh', _make_cb('wsl-restart', q)),
            pystray.MenuItem('WSL Status', _make_cb('wsl-status', q)),
        )),
        pystray.MenuItem('Phone Link (手机访问)', pystray.Menu(
            pystray.MenuItem('Start Phone Link', _make_cb('remote-start', q)),
            pystray.MenuItem('Stop Phone Link', _make_cb('remote-stop', q)),
            pystray.MenuItem('Phone Link Status', _make_cb('remote-status', q)),
            pystray.MenuItem('Copy Pair Link (配对到剪贴板)', _make_cb('remote-pair', q)),
        )),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('View Logs', _make_cb('logs', q)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Exit', _make_cb('quit', q)),
    )
