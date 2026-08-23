# -*- coding: utf-8 -*-
"""
logviewer.py - tkinter window showing the three dsh log streams
(dsh-web.log, dsh-web.log.err, dsh-ctl.log) with auto-refresh.
"""

import tkinter as tk
from tkinter import ttk, scrolledtext

import dsh_control as dc


class LogViewer:
    """Single-instance log window; show() re-raises an existing one."""

    def __init__(self, root):
        self.alive = True
        self.win = tk.Toplevel(root)
        self.win.title('dsh-ctl - Logs')
        self.win.geometry('780x540')
        self.win.minsize(480, 300)
        self.win.protocol('WM_DELETE_WINDOW', self.close)

        self.notebook = ttk.Notebook(self.win)
        self.notebook.pack(fill='both', expand=True)

        self.tabs = []
        streams = [
            ('dsh-web.log', dc.WEB_LOG),
            ('dsh-web.log.err', dc.ERR_LOG),
            ('dsh-ctl.log', dc.CTL_LOG),
        ]
        wsl_logs = dc.wsl_log_paths()
        if wsl_logs:
            streams += [
                ('dsh-web.log (WSL)', wsl_logs['web']),
                ('dsh-web.log.err (WSL)', wsl_logs['err']),
            ]
        for title, path in streams:
            frame = ttk.Frame(self.notebook)
            self.notebook.add(frame, text=title)
            txt = scrolledtext.ScrolledText(
                frame, wrap='none', state='disabled',
                font=('Consolas', 9))
            txt.pack(fill='both', expand=True)
            bar = ttk.Frame(frame)
            bar.pack(fill='x')
            ttk.Label(bar, text=path, foreground='gray').pack(side='left', padx=6)
            ttk.Button(
                bar, text='Refresh',
                command=lambda p=path, t=txt: self._refresh(p, t)
            ).pack(side='right', padx=6, pady=2)
            self.tabs.append((path, txt))

        self.win.after(2000, self._auto_refresh)

    def show(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        self._refresh_all()

    def _auto_refresh(self):
        if not self.alive:
            return
        try:
            if self.win.winfo_exists() and self.win.state() != 'iconic':
                self._refresh_all()
        except tk.TclError:
            return
        self.win.after(2000, self._auto_refresh)

    def _refresh_all(self):
        for path, txt in self.tabs:
            self._refresh(path, txt)

    def _refresh(self, path, txt):
        content = dc.tail(path)
        txt.config(state='normal')
        txt.delete('1.0', 'end')
        txt.insert('end', content if content else '(empty)')
        txt.config(state='disabled')
        txt.see('end')

    def close(self):
        self.alive = False
        self.win.destroy()
