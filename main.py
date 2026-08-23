# -*- coding: utf-8 -*-
"""
main.py - DeepSeek Harness tray controller entry point.

Threading model (hard rule):
  - tkinter lives only on the main thread.
  - pystray callbacks only enqueue actions (no tk, no blocking).
  - long actions (start/stop/restart/status) run on daemon threads.
  - balloons and menu refresh return to the main thread via root.after.
"""

import queue
import socket
import sys
import threading
import tkinter as tk

import pystray

import dsh_control as dc
import logviewer
import tray

SINGLE_INSTANCE_PORT = 47632


class SingleInstance:
    """Bind a localhost port; a second instance fails to bind and exits."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.sock.bind(('127.0.0.1', SINGLE_INSTANCE_PORT))
            self.sock.listen(1)
        except OSError:
            raise RuntimeError('another instance is already running')


class App:
    def __init__(self, root):
        self.root = root
        self.root.withdraw()
        self.q = queue.Queue()
        self.icon = None
        self.viewer = None
        self._actions = {
            'start': dc.start_dsh,
            'stop': dc.stop_dsh,
            'restart': dc.restart_dsh,
            'status': dc.status,
            'wsl-start': dc.wsl_start_dsh,
            'wsl-stop': dc.wsl_stop_dsh,
            'wsl-restart': dc.wsl_restart_dsh,
            'wsl-status': dc.wsl_status,
            'napcat-start': dc.napcat_start,
            'napcat-stop': dc.napcat_stop,
            'napcat-restart': dc.napcat_restart,
            'napcat-status': dc.napcat_status,
        }
        # Serialize start/stop/restart: two quick clicks used to spawn two dsh
        # instances and the loser crashed with EADDRINUSE on port 3080.
        self._action_lock = threading.Lock()

    def start(self):
        self.icon = pystray.Icon(
            'dsh-ctl', tray.make_icon(), 'DeepSeek Harness Control',
            tray.build_menu(self.q))
        self.icon.run_detached()          # pystray >= 0.19
        self.root.after(200, self._poll)

    # ------------------------------------------------------------------
    # event pump (main thread)
    # ------------------------------------------------------------------

    def _poll(self):
        while True:
            try:
                action = self.q.get_nowait()
            except queue.Empty:
                break

            if action[0] == 'quit':
                self.root.after(100, self._quit)
                return
            if action[0] == 'logs':
                self._show_logs()
            else:
                threading.Thread(
                    target=self._run, args=(action[0],), daemon=True
                ).start()

        self.root.after(200, self._poll)

    def _run(self, action):
        """Worker thread: execute the action, audit, then notify on UI thread."""
        fn = self._actions.get(action)
        if fn is None:
            return
        if action in ('start', 'restart'):
            # immediate feedback: dsh cold start can take 30-60s
            self.root.after(0, lambda: self._notify('Starting dsh, please wait...', 'dsh-ctl'))
        elif action in ('wsl-start', 'wsl-restart'):
            self.root.after(0, lambda: self._notify('Starting WSL dsh, please wait...', 'dsh-ctl'))
        elif action in ('napcat-start', 'napcat-restart'):
            self.root.after(0, lambda: self._notify('Starting NapCat, please wait...', 'dsh-ctl'))
        try:
            with self._action_lock:
                result = fn()
        except Exception as exc:  # never let a worker thread die silently
            result = {'ok': False, 'msg': 'error: {}'.format(exc)}

        if action in ('status', 'wsl-status', 'napcat-status'):
            # status queries return {'running': bool, 'pid': ...} - no 'ok'
            # key, so they must never be judged by result.get('ok').
            running = result.get('running', False)
            detail = 'Running (PID {})'.format(result.get('pid')) if running else 'Not running'
            ok = True
        else:
            ok = bool(result.get('ok'))
            detail = result.get('msg', '')

        dc.audit(action, ok, detail)
        status = 'OK' if ok else 'FAIL'
        self.root.after(0, lambda: self._notify('{}: {} {}'.format(action, status, detail)))
        self.root.after(0, self._refresh_menu)

    def _notify(self, msg, title='dsh-ctl'):
        try:
            self.icon.notify(msg, title)
        except Exception:
            pass

    def _refresh_menu(self):
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def _show_logs(self):
        if self.viewer is None or not self.viewer.alive:
            self.viewer = logviewer.LogViewer(self.root)
        else:
            self.viewer.show()

    def _quit(self):
        try:
            self.icon.stop()
        except Exception:
            pass
        self.root.destroy()


def main():
    try:
        single = SingleInstance()         # keep a reference! GC would close
    except RuntimeError:                  # the socket and release the lock
        sys.exit(0)                       # second instance: exit quietly

    root = tk.Tk()
    app = App(root)
    app._single = single                  # socket stays alive for app lifetime
    app.start()
    root.mainloop()


if __name__ == '__main__':
    main()
