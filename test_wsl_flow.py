# -*- coding: utf-8 -*-
"""
test_wsl_flow.py - self-contained WSL dsh flow test.

Runs OUTSIDE the dsh process tree (started via Task Scheduler), because
stopping the Windows dsh kills every child of the dsh session tree -
including any process we spawn from inside the session. This script:
  1. stops the Windows dsh (frees port 3080)
  2. starts the WSL dsh, waits for readiness
  3. verifies status/pid
  4. stops the WSL dsh
  5. ALWAYS restores the Windows dsh (finally, with retries)
Every step is appended to test-result.log; restore failures are retried.
"""

import os
import sys
import time
import traceback

import dsh_control as dc

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'test-result.log')


def log(msg):
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write('[{}] {}\n'.format(time.strftime('%H:%M:%S'), msg))


def restore_windows():
    """Restore the Windows dsh with retries and cleanup between attempts."""
    for attempt in range(1, 4):
        try:
            r = dc.start_dsh()
            log('[7] restore Win (try {}) : {}'.format(attempt, r))
            if r.get('ok'):
                return True
        except Exception as exc:  # never let restore die silently
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
        f.write('=== WSL flow test start ===\n')
    try:
        r = dc.stop_dsh()
        log('[1] stop Windows : {}'.format(r))
        time.sleep(2)

        r = dc.wsl_start_dsh()
        log('[2] wsl start    : {}'.format(r))

        r = dc.wsl_status()
        log('[3] wsl status   : {}'.format(r))

        r = dc.wsl_get_pid()
        log('[4] wsl pid      : {}'.format(r))

        r = dc.wsl_stop_dsh()
        log('[5] wsl stop     : {}'.format(r))

        time.sleep(4)  # let the wsl status cache expire
        r = dc.wsl_get_pid()
        log('[6] wsl pid aft  : {}'.format(r))
    except Exception:
        log('ERROR: {}'.format(traceback.format_exc()))
    finally:
        ok = restore_windows()
        log('=== test end (windows restored: {}) ==='.format(ok))


if __name__ == '__main__':
    main()
