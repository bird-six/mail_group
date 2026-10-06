"""Exercise real NSIS installation with a synthetic payload and isolated registry ID.

Never launch the user's installed application or read their mailbox data. The
production include files are compiled unchanged; only the app identity, payload,
shortcut creation, and output paths are replaced for this test.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
import winreg

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / 'desktop'
RESULTS = ROOT / 'diagnostics/desktop-v3'
REG_ACCESS = winreg.KEY_READ | winreg.KEY_WOW64_64KEY


def run(command, log=None):
    return subprocess.run(list(map(str, command)), check=True, stdout=log,
                          stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)


def reg_value(key, name):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, REG_ACCESS) as handle:
            return winreg.QueryValueEx(handle, name)[0]
    except FileNotFoundError:
        return None


def fingerprint(directory):
    return {p.relative_to(directory).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob('*') if p.is_file()}


def main():
    RESULTS.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='mailgroup-installer-test-')).resolve()
    identity = str(uuid.uuid4())
    install_key = f'Software\\{identity}'
    uninstall_key = f'Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{identity}'
    installed = work / 'installed'
    payloads = {name: work / name for name in ('old-payload', 'new-payload')}
    app_name = '邮件群发助手.exe'
    source = work / 'Idle.cs'
    source.write_text('class Idle { static void Main() { System.Threading.Thread.Sleep(600000); } }')
    compiler = Path(os.environ['WINDIR']) / 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    for name, payload in payloads.items():
        payload.mkdir()
        (payload / 'resources').mkdir()
        run([compiler, '/nologo', '/target:winexe', f'/out:{payload / app_name}', source])
        (payload / f'{name}.txt').write_text(name)

    # Synthetic data includes a database, Unicode attachment names and binary files.
    profile = work / 'profile' / 'data'
    profile.mkdir(parents=True)
    with sqlite3.connect(profile / 'mail_group.sqlite3') as db:
        db.execute('CREATE TABLE preserved(kind TEXT, value TEXT)')
        db.executemany('INSERT INTO preserved VALUES (?, ?)', [
            ('sender', 'sender@example.com'), ('authorization', 'synthetic-placeholder'),
            ('recipient', 'recipient@example.com'), ('template', '合成模板'),
            ('task', 'paused'), ('mailbox', '合成收发邮件'),
        ])
    for folder in ('saved_attachments', 'task_attachments'):
        (profile / folder).mkdir()
        (profile / folder / '附件.txt').write_bytes(b'synthetic attachment\x00\xff')
    before = fingerprint(profile)
    cases = []
    running = None

    def build(kind, version, payload, include):
        # JSON is written to a file and parsed by node; no shell interpolation.
        options = {
            'appId': f'com.mailgroup.installer-test.{identity}',
            'directories': {'output': str(work / 'artifacts')},
            'extraMetadata': {'version': version, 'name': f'mailgroup-test-{identity}'},
            'nsis': {
                'guid': identity, 'include': include,
                'artifactName': 'mailgroup-test-' + kind + '.exe', 'createDesktopShortcut': False,
                'createStartMenuShortcut': False, 'runAfterFinish': False,
                'allowToChangeInstallationDirectory': kind != 'update',
            },
        }
        settings = work / (kind + '.json')
        settings.write_text(json.dumps(options), encoding='utf-8')
        config = work / (kind + '.cjs')
        config.write_text(
            f'const base = require({json.dumps(str(DESKTOP / "package.json"))}).build;\n'
            f'const patch = require({json.dumps(str(settings))});\n'
            'module.exports = {...base, ...patch, nsis: {...base.nsis, ...patch.nsis}};\n',
            encoding='utf-8')
        with (RESULTS / f'installer-{kind}-build.log').open('w', encoding='utf-8') as log:
            subprocess.run([shutil.which('node'), str(DESKTOP / 'node_modules/electron-builder/cli.js'),
                            '--win', 'nsis', '--x64', '--prepackaged', str(payload),
                            '--config', str(config)], cwd=DESKTOP, check=True,
                           stdout=log, stderr=subprocess.STDOUT,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        return work / 'artifacts' / ('mailgroup-test-' + kind + '.exe')

    def invoke(executable, expected=0, *args):
        process = subprocess.run([str(executable), '/S', '/currentuser', *map(str, args)],
                                 timeout=90, creationflags=subprocess.CREATE_NO_WINDOW)
        assert process.returncode == expected, f'{executable.name}: {process.returncode} != {expected}'
        assert fingerprint(profile) == before, 'Synthetic data changed during install/uninstall'

    def uninstall():
        # Only touch the random test registration and an installation inside work.
        location = reg_value(install_key, 'InstallLocation')
        if location is None:
            return
        assert Path(location).resolve() == installed.resolve() and installed.is_relative_to(work)
        command = reg_value(uninstall_key, 'UninstallString')
        assert command and command.startswith('"'), 'Unexpected test uninstall command'
        executable = Path(command.split('"')[1]).resolve()
        assert executable.is_relative_to(installed), 'Uninstaller is outside the test workspace'
        invoke(executable)
        for _ in range(100):
            if reg_value(install_key, 'InstallLocation') is None:
                return
            time.sleep(0.1)
        raise AssertionError('Test uninstaller did not remove its registration')

    try:
        setup = build('setup', '3.2.1', payloads['new-payload'], str(DESKTOP / 'installer-common.nsh'))
        update = build('update', '3.2.1', payloads['new-payload'], str(DESKTOP / 'installer-update.nsh'))
        legacy = build('legacy', '3.0.0', payloads['old-payload'], None)

        invoke(update, 2)
        cases.append('update rejects missing installation')
        invoke(setup, 0, f'/D={installed}')
        assert reg_value(uninstall_key, 'DisplayVersion') == '3.2.1'
        assert (installed / 'new-payload.txt').is_file()
        cases.append('fresh installation and data preservation')
        uninstall()
        cases.append('uninstall preserves data')

        invoke(legacy, 0, f'/D={installed}')
        assert reg_value(uninstall_key, 'DisplayVersion') == '3.0.0'
        running = subprocess.Popen([str(installed / app_name)], creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(0.5)
        invoke(update, 3)
        assert running.poll() is None, 'Installer terminated the running app'
        assert reg_value(uninstall_key, 'DisplayVersion') == '3.0.0'
        cases.append('running app blocks upgrade without termination')
        running.terminate()  # Only the synthetic process created above.
        running.wait(timeout=10)
        running = None

        service = work / 'mail-group-service.exe'
        shutil.copy2(installed / app_name, service)
        running = subprocess.Popen([str(service)], creationflags=subprocess.CREATE_NO_WINDOW)
        time.sleep(0.5)
        invoke(update, 3)
        assert running.poll() is None, 'Installer terminated the running service'
        assert reg_value(uninstall_key, 'DisplayVersion') == '3.0.0'
        cases.append('running service blocks upgrade without termination')
        running.terminate()
        running.wait(timeout=10)
        running = None

        invoke(update, 0, f'/D={work / "must-not-use"}')
        assert Path(reg_value(install_key, 'InstallLocation')).resolve() == installed
        assert reg_value(uninstall_key, 'DisplayVersion') == '3.2.1'
        assert (installed / 'new-payload.txt').is_file()
        assert not (installed / 'old-payload.txt').exists()
        assert not (work / 'must-not-use' / app_name).exists()
        cases.append('3.0 to 3.2.1 upgrade retains path and all data')
        invoke(update)
        cases.append('repeat update preserves data')

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, uninstall_key, 0,
                            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as key:
            winreg.SetValueEx(key, 'DisplayVersion', 0, winreg.REG_SZ, '9.0.0')
        invoke(update, 5)
        invoke(setup, 5)
        cases.append('downgrade rejected by both installers')
        uninstall()
        cases.append('upgraded uninstaller preserves data')
        result = {'passed': True, 'cases': cases, 'preserved_files': len(before),
                  'payload': 'synthetic', 'registry_identity': identity, 'real_smtp_calls': 0}
        (RESULTS / 'installer-upgrade.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False), flush=True)
    finally:
        if running is not None and running.poll() is None:
            running.terminate()
            running.wait(timeout=10)
        uninstall()
        # Leave the isolated fixtures for diagnosis. They contain no personal data.


if __name__ == '__main__':
    main()
