# -*- coding: utf-8 -*-
"""
test_wsl_flow3.py - TIME_WAIT hypothesis verification (v3).

After stopping the Windows dsh, wait for the Windows-side TIME_WAIT
sockets on :3080 to expire (default 240s) BEFORE starting the WSL dsh.
If the WSL dsh then starts successfully, the mirrored-loopback
TIME_WAIT-blocking hypothesis is confirmed.
"""

import os
import re
import time
import traceback

import dsh_control as dc

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'test-result.log')


def log(msg):
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write('[{}] {}\n'.format(time.strftime('%H:%M:%S'), msg))


def win_tw_count():
    """Count Windows-side TIME_WAIT rows touching :3080."""
    rc, out, _ = dc.run_capture(['netstat', '-ano'])
    n = 0
    for line in (out or '').splitlines():
        if ':3080 ' in line and 'TIME_WAIT' in line:
            n += 1
    return n


def wait_tw_clear(max_wait=250):
    """Poll until no TIME_WAIT remains on :3080 (or timeout)."""
    waited = 0
    while waited < max_wait:
        n = win_tw_count()
        log('[1.5] TIME_WAIT on 3080: {} (waited {}s)'.format(n, waited))
        if n == 0:
            return True
        time.sleep(20)
        waited += 20
    return False


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
        f.write('=== WSL flow test v3 (TIME_WAIT wait) start ===\n')
    try:
        r = dc.stop_dsh()
        log('[1] stop Windows : {}'.format(r))
        time.sleep(2)
        log('[1.5] initial TIME_WAIT count: {}'.format(win_tw_count()))

        cleared = wait_tw_clear(250)
        log('[1.5] TW cleared: {}'.format(cleared))

        r = dc.wsl_start_dsh()
        log('[2] wsl start    : {}'.format(r))

        r = dc.wsl_status()
        log('[3] wsl status   : {}'.format(r))

        r = dc.wsl_get_pid()
        log('[3.5] wsl pid    : {}'.format(r))

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
