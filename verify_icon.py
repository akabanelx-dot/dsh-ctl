# -*- coding: utf-8 -*-
"""
verify_icon.py - verify the built dsh-ctl.exe icon is NOT modified.

Icon protection gate (called by build.bat BEFORE the desktop copy):
  1. Icon pixels: extract the exe's associated icon (32x32) and compare it
     pixel-by-pixel against icon-check.png — the authoritative reference
     extracted from the last verified build. 0 differing bytes = MATCH.
     (Comparing PNG bytes is meaningless: encoders change between PyInstaller
     versions; pixels are what the user sees.)
  2. Bundled assets: list the onefile archive with PyInstaller's own
     archive_viewer and require both protected assets to be present.

The old PWA comparison is gone: the Chrome PWA icon is a live, re-generated
asset (single 256x256 frame) and scaling it for comparison is not a stable
reference. The exe icon reference is icon-check.png, period.

Exit code 0 = verification passed, 1 = failed.
"""

import os
import subprocess
import sys

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
REF_PNG = os.path.join(HERE, 'icon-check.png')     # authoritative 32x32 pixels
EXE = os.path.join(HERE, 'dist', 'dsh-ctl.exe')
TMP_PNG = os.path.join(HERE, 'build-icon-extract.png')
ASSETS = ('DeepSeek Harness.ico', 'app_icon.png')

PY = sys.executable


def extract_exe_icon(exe, out_png):
    """Extract the exe's associated icon to a PNG via System.Drawing."""
    ps = (
        "Add-Type -AssemblyName System.Drawing;"
        "$i = [System.Drawing.Icon]::ExtractAssociatedIcon('{exe}');"
        "$i.ToBitmap().Save('{out}', [System.Drawing.Imaging.ImageFormat]::Png)"
    ).format(exe=exe.replace("'", "''"), out=out_png.replace("'", "''"))
    p = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                       capture_output=True, timeout=60)
    return p.returncode == 0 and os.path.isfile(out_png)


def archive_has(exe, name):
    """True when the onefile CArchive contains <name> (archive_viewer)."""
    p = subprocess.run(
        [PY, '-m', 'PyInstaller.utils.cliutils.archive_viewer', '-l', exe],
        capture_output=True, text=True, timeout=120)
    return name in (p.stdout or '')


def main():
    ok = True

    # 1) icon pixels vs the authoritative reference
    if not os.path.isfile(REF_PNG):
        print('icon reference missing: {}'.format(REF_PNG))
        return 1
    if not extract_exe_icon(EXE, TMP_PNG):
        print('FAIL: cannot extract icon from {}'.format(EXE))
        return 1
    a = Image.open(TMP_PNG).convert('RGBA').resize((32, 32), Image.LANCZOS)
    b = Image.open(REF_PNG).convert('RGBA')
    data_a, data_b = a.tobytes(), b.tobytes()
    if a.size != b.size:
        diff = len(data_a)
    else:
        diff = sum(1 for x, y in zip(data_a, data_b) if x != y)
    match = diff == 0
    ok = ok and match
    print('exe icon 32x32 vs reference icon-check.png: %d differing bytes / %d total -> %s'
          % (diff, len(data_a), 'MATCH' if match else 'DIFFER'))

    # 2) bundled protected assets inside the exe archive
    for name in ASSETS:
        present = archive_has(EXE, name)
        ok = ok and present
        print('bundled %s: %s' % (name, 'True' if present else 'False'))

    print('RESULT: %s' % ('PASS' if ok else 'FAIL'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
