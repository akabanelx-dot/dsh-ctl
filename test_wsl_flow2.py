# -*- coding: utf-8 -*-
"""
test_wsl_flow2.py - diagnostic WSL dsh flow test (v2).

Same self-contained pattern as v1 (runs OUTSIDE the dsh tree via Task
Scheduler), plus port-holder diagnostics after the Windows dsh stops:
  [1] stop Windows dsh
  [1.5] who holds 3080/3128 on the WINDOWS side (netstat + tasklist)
  [1.6] who holds 3080/3128 inside WSL (ss + /proc/net/tcp inode scan)
  [2] start WSL dsh and wait for readiness
  [3] verify status/pid
  [4] stop WSL dsh
  [7] ALWAYS restore Windows dsh (finally, retries)
Every step is appended to test-result.log.
"""

import os
import re
import time
import traceback

import dsh_control as dc

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'test-result.log')
PORTS = ('3080', '3128')


def log(msg):
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write('[{}] {}\n'.format(time.strftime('%H:%M:%S'), msg))


def diag_windows():
    """Record every netstat line touching 3080/3128 and its process name."""
    rc, out, _ = dc.run_capture(['netstat', '-ano'])
    for line in (out or '').splitlines():
        if any(':{} '.format(p) in line for p in PORTS):
            parts = line.split()
            pid = parts[-1] if parts else '?'
            image = '?'
            if pid.isdigit():
                _, tout, _ = dc.run_capture(
                    ['tasklist', '/FI', 'PID eq {}'.format(pid), '/FO', 'CSV', '/NH'])
                if tout:
                    image = tout.splitlines()[0][:60]
            log('[1.5] WIN {} | image={}'.format(line.strip(), image))
    log('[1.5] win diag done')


def diag_wsl():
    """Record WSL listeners on 3080/3128 (ss) and /proc/net/tcp holders."""
    script = (
        'echo "--- ss ---"\n'
        'ss -ltnp 2>/dev/null | grep -E ":3080 |:3128 " || echo "(ss: none)"\n'
        'echo "--- /proc/net/tcp ---"\n'
        'grep -E ":0C08 |:0C38 " /proc/net/tcp /proc/net/tcp6 2>/dev/null || echo "(proc: none)"\n'
    )
    rc, out, _ = dc.wsl_run(script, timeout=20)
    log('[1.6] WSL diag:\n{}'.format(out.strip()))
    # resolve inode -> pid
    inodes = re.findall(r'\s(\w+)\s+\w+\s+\w+:\w+', out) if out else []
    for ino in inodes:
        script = (
            'for p in /proc/[0-9]*; do\n'
            '  if ls -l "$p/fd" 2>/dev/null | grep -q "socket:[{}]"; then\n'
            '    echo "{} -> $(basename $p) $(cat $p/comm 2>/dev/null)"\n'
            '  fi\n'
            'done\n'
        ).format(ino, ino)
        _, o2, _ = dc.wsl_run(script, timeout=20)
        if o2.strip():
            log('[1.6] WSL inode {}: {}'.format(ino, o2.strip()))
    log('[1.6] wsl diag done')


def restore_windows():
    for attempt in range(1, 4):
        try:
            r = dc.start_dsh()
            log('[7] restore Win (try {}) : {}'.format(attempt, r))
            if r.get('ok'):
                return True
        except Exception as exc:
            log('[7] restore error: {}'.format(exc))
        time.sleep(5)
        try:
            dc.stop_dsh()
        except Exception:
            pass
        time.sleep(3)
    return False


def main():
    with open(LOG, 'w', encoding='utf-8') as f:
        f.write('=== WSL flow test v2 start ===\n')
    try:
        r = dc.stop_dsh()
        log('[1] stop Windows : {}'.format(r))
        time.sleep(2)

        diag_windows()
        diag_wsl()

        r = dc.wsl_start_dsh()
        log('[2] wsl start    : {}'.format(r))

        r = dc.wsl_status()
        log('[3] wsl status   : {}'.format(r))

        r = dc.wsl_stop_dsh()
        log('[4] wsl stop     : {}'.format(r))

        time.sleep(4)
        r = dc.wsl_get_pid()
        log('[5] wsl pid aft  : {}'.format(r))
    except Exception:
        log('ERROR: {}'.format(traceback.format_exc()))
    finally:
        ok = restore_windows()
        log('=== test end (windows restored: {}) ==='.format(ok))


if __name__ == '__main__':
    main()
