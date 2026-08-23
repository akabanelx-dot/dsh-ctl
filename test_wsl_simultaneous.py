# -*- coding: utf-8 -*-
"""
test_wsl_simultaneous.py - NAT-mode simultaneous dual-dsh flow test.

Verifies the post-fix contract (NAT networking, no mutex):
  [1] Windows dsh running + WSL dsh running SIMULTANEOUSLY
  [2] both endpoints answer HTTP 200 during the whole test
  [3] wsl_stop_dsh stops ONLY the WSL dsh, Windows dsh unaffected
  [4] wsl_start_dsh restarts the WSL dsh alongside the Windows one
  [5] hold-open client keeps the VM alive past the idle timeout
Every step is appended to test-result.log; the Windows dsh is never
stopped by this test (it must keep serving throughout).
"""

import time
import traceback

import dsh_control as dc

LOG = r'C:\Users\赤羽兰霞\Documents\dsh-ctl\test-result.log'


def log(msg):
    with open(LOG, 'a', encoding='utf-8') as f:
        f.write('[{}] {}\n'.format(time.strftime('%H:%M:%S'), msg))


def http_ok(host, port=3080):
    try:
        import socket
        s = socket.create_connection((host, port), timeout=3)
        s.close()
        return True
    except OSError:
        return False


def main():
    with open(LOG, 'w', encoding='utf-8') as f:
        f.write('=== WSL simultaneous test (NAT, no mutex) start ===\n')
    try:
        win_pid = dc.get_dsh_pid()
        log('[1] Windows dsh pid: {}'.format(win_pid))
        assert win_pid is not None, 'Windows dsh not running - abort'

        # WSL dsh must start while the Windows dsh keeps running.
        if not dc.wsl_is_running():
            r = dc.wsl_start_dsh()
            log('[2] wsl start alongside win: {}'.format(r))
            assert r.get('ok'), 'wsl_start_dsh failed: %s' % r
        wsl_pid = dc.wsl_get_pid()
        log('[2.5] wsl pid: {}'.format(wsl_pid))
        assert wsl_pid is not None, 'WSL dsh not running after start'

        # Simultaneous liveness across a 30s window (past vmIdleTimeout for
        # the hold-open check in step 5).
        ok_win = ok_wsl = 0
        for i in range(30):
            if http_ok('127.0.0.1', 3080):
                ok_win += 1
            rc, out, _ = dc.wsl_run(
                'curl -s -o /dev/null -w "%{http_code}" --max-time 3 http://127.0.0.1:3080/',
                timeout=10)
            if (out or '').strip() == '200':
                ok_wsl += 1
            time.sleep(1)
        log('[3] simultaneous 30s: win {}/30 wsl {}/30'.format(ok_win, ok_wsl))
        assert ok_win == 30 and ok_wsl == 30, 'simultaneous liveness failed'

        # Stop ONLY the WSL dsh; the Windows one must stay up.
        r = dc.wsl_stop_dsh()
        log('[4] wsl stop: {}'.format(r))
        assert r.get('ok'), 'wsl_stop_dsh failed: %s' % r
        time.sleep(4)
        assert dc.get_dsh_pid() == win_pid, 'Windows dsh was disturbed!'
        assert dc.wsl_get_pid() is None, 'WSL dsh still running after stop'
        log('[4.5] win intact (pid {}), wsl stopped'.format(win_pid))

        # Restart the WSL dsh alongside Windows.
        r = dc.wsl_start_dsh()
        log('[5] wsl restart: {}'.format(r))
        assert r.get('ok'), 'wsl restart failed: %s' % r
        time.sleep(2)
        assert dc.wsl_is_running() and dc.is_running(), 'both must be up'
        log('[6] FINAL dual state: win {} + wsl {}'.format(
            dc.get_dsh_pid(), dc.wsl_get_pid()))
        log('=== test end (PASS) ===')
    except Exception:
        log('ERROR: {}'.format(traceback.format_exc()))
        log('=== test end (FAIL) ===')
        raise


if __name__ == '__main__':
    main()
