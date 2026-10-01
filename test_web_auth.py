# -*- coding: utf-8 -*-
"""
test_web_auth.py - checks for the DSH >= 0.1.5 Web entry-token adaptation.

These tests never start dsh; they only exercise the helpers that capture the
launch token, sanitise the child environment and clear dead writer locks.

Run:  python test_web_auth.py
"""

import os
import subprocess
import sys
import tempfile
import unittest

import dsh_control as dc


class TokenCaptureTest(unittest.TestCase):
    """`dsh web` prints the authenticated URL once to stdout -> WEB_LOG."""

    TOKEN = 'Ssx0pJPtC3R-myoqSfG3gYFR79j7A0JLsXQ8SOv9QDc'
    LINES = [
        '[qq-mode-console] active (namespace=qq-mode)',
        'dsh web: http://127.0.0.1:3080/?token=' + TOKEN,
        '',
    ]

    def _write_log(self, lines):
        fd, path = tempfile.mkstemp(prefix='dsh-web-test.', suffix='.log')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
        return path

    def test_finds_token_appended_after_offset(self):
        path = self._write_log(self.LINES)
        old, dc.WEB_LOG = dc.WEB_LOG, path
        try:
            # pretend the log already had this much before this launch
            offset = len(self.LINES[0]) + 1
            self.assertEqual(dc.ui_url_from_log(offset, attempts=1, delay=0),
                             'http://127.0.0.1:3080/?token=' + self.TOKEN)
        finally:
            dc.WEB_LOG = old
            os.remove(path)

    def test_ignores_bytes_before_offset(self):
        """A token from a previous run must never be reused."""
        path = self._write_log(self.LINES)
        old, dc.WEB_LOG = dc.WEB_LOG, path
        try:
            self.assertEqual(dc.ui_url_from_log(os.path.getsize(path),
                                                attempts=1, delay=0), '')
        finally:
            dc.WEB_LOG = old
            os.remove(path)

    def test_returns_empty_without_token(self):
        path = self._write_log(['no url here', 'plain: http://127.0.0.1:3080/'])
        old, dc.WEB_LOG = dc.WEB_LOG, path
        try:
            self.assertEqual(dc.ui_url_from_log(0, attempts=1, delay=0), '')
        finally:
            dc.WEB_LOG = old
            os.remove(path)

    def test_save_load_clear_roundtrip(self):
        url = 'http://127.0.0.1:3080/?token=' + self.TOKEN
        dc.save_ui_url(url)
        try:
            self.assertEqual(dc.load_ui_url(), url)
        finally:
            dc.clear_ui_url()
        self.assertEqual(dc.load_ui_url(), '')


class ChildEnvTest(unittest.TestCase):
    """The dsh child must get a normal user PATH and no inherited proxy."""

    def test_path_comes_from_registry(self):
        env = dc.child_env()
        self.assertIn('PATH', env)
        self.assertTrue(env['PATH'].strip(), 'PATH must not be empty')

    def test_proxy_vars_removed(self):
        saved = {k: 'http://127.0.0.1:1' for k in dc._PROXY_VARS}
        os.environ.update(saved)
        try:
            env = dc.child_env()
            for name in dc._PROXY_VARS:
                self.assertNotIn(name, env)
        finally:
            for k in saved:
                os.environ.pop(k, None)

    def test_node_options_removed(self):
        os.environ['NODE_OPTIONS'] = '--require shim.js'
        try:
            self.assertNotIn('NODE_OPTIONS', dc.child_env())
        finally:
            os.environ.pop('NODE_OPTIONS', None)


class StaleLockTest(unittest.TestCase):
    """Only dead-owner PID locks may be deleted; JSON locks must survive."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='dsh-lock-test.')
        self.old = dc.STALE_LOCK_DIRS
        dc.STALE_LOCK_DIRS = (self.tmp,)

    def tearDown(self):
        dc.STALE_LOCK_DIRS = self.old
        for name in os.listdir(self.tmp):
            try:
                os.remove(os.path.join(self.tmp, name))
            except OSError:
                pass
        os.rmdir(self.tmp)

    def _put(self, name, content):
        with open(os.path.join(self.tmp, name), 'w', encoding='utf-8') as f:
            f.write(content)

    def test_dead_pid_lock_is_removed(self):
        self._put('node_modules.lock', '999999\n')
        self.assertEqual(dc.clean_stale_locks(), ['node_modules.lock'])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'node_modules.lock')))

    def test_live_pid_lock_is_kept(self):
        self._put('node_modules.lock', str(os.getpid()))
        self.assertEqual(dc.clean_stale_locks(), [])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, 'node_modules.lock')))

    def test_json_lock_is_kept(self):
        """task-board/ledger-v2.lock holds JSON - deleting it would be wrong."""
        self._put('ledger-v2.lock', '{"pid":%d,"token":"x"}' % os.getpid())
        self.assertEqual(dc.clean_stale_locks(), [])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, 'ledger-v2.lock')))

    def test_non_lock_files_untouched(self):
        self._put('settings.yaml', 'agent-default-model:\n')
        self.assertEqual(dc.clean_stale_locks(), [])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, 'settings.yaml')))


class DiscoveryTest(unittest.TestCase):
    def test_node_found(self):
        node = dc.find_node()
        self.assertTrue(node and os.path.isfile(node), 'node.exe not found')

    def test_chrome_found(self):
        chrome = dc.find_chrome()
        self.assertTrue(chrome and os.path.isfile(chrome), 'chrome.exe not found')

    def test_chrome_profile_matches_pwa(self):
        """open_ui() must seed the cookie in the PWA's own profile."""
        self.assertTrue(dc.chrome_profile().strip())


class PathTest(unittest.TestCase):
    """Paths must stay absolute even when environment variables are missing."""

    def test_core_paths_are_absolute(self):
        names = ('DSH_DIR', 'WEB_LOG', 'ERR_LOG', 'CTL_LOG',
                 'NPM_DIR', 'NODE_EXE', 'DSH_BIN', 'UI_URL_FILE',
                 'DSH_HOME', 'PWA_WINDOWS_LNK')
        for name in names:
            value = getattr(dc, name)
            self.assertTrue(os.path.isabs(value),
                            '{} is relative: {!r}'.format(name, value))

    def test_env_dir_falls_back_when_unset(self):
        """APPDATA missing used to turn DSH_DIR into 'DeepSeekHarness'."""
        saved = os.environ.pop('APPDATA', None)
        try:
            self.assertEqual(dc._env_dir('APPDATA', r'C:\x\AppData\Roaming'),
                             r'C:\x\AppData\Roaming')
        finally:
            if saved is not None:
                os.environ['APPDATA'] = saved

    def test_stale_lock_dirs_are_absolute(self):
        for root in dc.STALE_LOCK_DIRS:
            self.assertTrue(os.path.isabs(root), repr(root))


class LiveTokenTest(unittest.TestCase):
    """Integration-flavoured: talks to a running dsh on :3080."""

    def setUp(self):
        if not dc.is_running():
            self.skipTest('dsh is not running on :3080')

    def test_bare_origin_is_rejected(self):
        """The whole point of the bug fix."""
        self.assertFalse(dc._token_is_live('http://127.0.0.1:{}/'.format(dc.PORT)))

    def test_bogus_token_is_rejected(self):
        self.assertFalse(dc._token_is_live(
            'http://127.0.0.1:{}/?token={}'.format(dc.PORT, 'x' * 43)))

    def test_token_from_log_is_accepted(self):
        url = dc.live_ui_url()
        if not url:
            self.skipTest('no live token in WEB_LOG (dsh started before this build?)')
        self.assertIn('token=', url)
        self.assertTrue(dc._token_is_live(url))


if __name__ == '__main__':
    unittest.main(verbosity=2)
