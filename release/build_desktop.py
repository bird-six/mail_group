"""Build self-contained Windows x64 desktop packages without copying user data."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'release/desktop-v3'


def run(args, cwd=ROOT):
    print('>', ' '.join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), cwd=cwd, check=True)


def prepare_electron():
    package = ROOT / 'desktop/node_modules/electron'
    version = json.loads((package / 'package.json').read_text(encoding='utf-8'))['version']
    archive_name = f'electron-v{version}-win32-x64.zip'
    checksum = json.loads((package / 'checksums.json').read_text())[archive_name]
    archive = ROOT / 'build' / archive_name
    archive.parent.mkdir(parents=True, exist_ok=True)
    if not archive.exists() or hashlib.file_digest(archive.open('rb'), 'sha256').hexdigest() != checksum:
        url = f'https://github.com/electron/electron/releases/download/v{version}/{archive_name}'
        print(f'Downloading official Electron {version} ...', flush=True)
        with urllib.request.urlopen(url, timeout=90) as response, archive.open('wb') as target:
            shutil.copyfileobj(response, target)
    with archive.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != checksum:
            raise RuntimeError('Electron SHA256 mismatch; refusing to package')
    destination = (package / 'dist').resolve()
    with zipfile.ZipFile(archive) as bundle:
        for item in bundle.infolist():
            if not (destination / item.filename).resolve().is_relative_to(destination):
                raise RuntimeError('Unsafe Electron archive path')
        bundle.extractall(destination)
    (package / 'path.txt').write_text('electron.exe', encoding='utf-8')


def write_delivery_files():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'release/桌面版使用说明.txt', OUTPUT / '使用说明.txt')
    entries = []
    version = json.loads((ROOT / 'desktop/package.json').read_text(encoding='utf-8'))['version']
    for kind in ('nsis', 'update', 'portable'):
        path = OUTPUT / f'MailGroup-{version}-x64-{kind}.exe'
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        entries.append(f'{digest}  {path.name}')
    (OUTPUT / 'SHA256SUMS.txt').write_text('\n'.join(entries) + '\n', encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--skip-install', action='store_true', help='Reuse existing build dependencies')
    parser.add_argument('--verify', action='store_true', help='Run isolated backend and frontend tests')
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('Build the Windows distribution on Windows x64 with Python 3.12+ and Node 24+')
    python = ROOT / '.venv/Scripts/python.exe'
    npm = shutil.which('npm.cmd')
    if not npm:
        raise SystemExit('Node.js / npm is required on the build machine only')
    if not python.exists():
        run([sys.executable, '-m', 'venv', ROOT / '.venv'])
    if not args.skip_install:
        run([python, '-m', 'pip', 'install', '-r', ROOT / 'release/requirements-build.txt'])
        run([npm, 'ci'], ROOT / 'mail_group_vue3')
        run([npm, 'ci'], ROOT / 'desktop')
    prepare_electron()
    run([npm, 'run', 'build'], ROOT / 'mail_group_vue3')
    if args.verify:
        run([python, '-B', '-m', 'pytest', 'backend', '-q', '-p', 'no:cacheprovider'])
        run([npm, 'exec', '--', 'vitest', 'run'], ROOT / 'mail_group_vue3')
        run([npm, 'exec', '--', 'vitest', 'run', '--config', 'audit.config.ts'], ROOT / 'mail_group_vue3')
        run([python, '-B', ROOT / 'diagnostics/run_audit_integration.py'])
    run([python, '-B', '-m', 'PyInstaller', '--noconfirm', '--distpath', ROOT / 'build/backend',
         '--workpath', ROOT / 'build/pyinstaller', ROOT / 'backend/desktop-service.spec'])
    run([npm, 'run', 'dist'], ROOT / 'desktop')
    run([npm, 'run', 'dist:update'], ROOT / 'desktop')
    write_delivery_files()
    if args.verify:
        run([python, '-B', ROOT / 'diagnostics/installer_upgrade_smoke.py'])
    print(f'Ready: {OUTPUT}', flush=True)


if __name__ == '__main__':
    main()
