# The backend runtime includes Python and static UI, never application data.
from pathlib import Path
root = Path(SPECPATH).parent
a = Analysis([str(root / 'backend/main.py')], pathex=[str(root / 'backend')],
    binaries=[], datas=[(str(root / 'backend/dist'), 'dist'), (str(root / 'release/mail.ico'), '.')],
    hiddenimports=['uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto',
        'uvicorn.protocols.http.h11_impl', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on', 'email_validator'],
    hookspath=[], runtime_hooks=[], excludes=['tkinter','pytest','IPython','numpy','pandas','matplotlib'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='mail-group-service',
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='mail-group-service')
