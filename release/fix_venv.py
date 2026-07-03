"""修复 release venv，使其在任意电脑上可运行（不依赖 pyvenv.cfg 中的硬编码路径）"""
import os, shutil, sys

RELEASE = r"f:\PythonFiles\mail_group\release\邮件群发系统_v2\backend"
SYSPY = r"C:\Users\23014\AppData\Local\Programs\Python\Python312"

pyvenv_cfg = os.path.join(RELEASE, ".venv", "pyvenv.cfg")
if os.path.exists(pyvenv_cfg):
    os.remove(pyvenv_cfg)
    print("[1/5] Deleted pyvenv.cfg")
else:
    print("[1/5] pyvenv.cfg already gone")

scripts = os.path.join(RELEASE, ".venv", "Scripts")
os.makedirs(scripts, exist_ok=True)

# 复制真实解释器和 DLL
for f in ["python.exe", "pythonw.exe", "python312.dll", "python3.dll"]:
    src = os.path.join(SYSPY, f)
    dst = os.path.join(scripts, f)
    if os.path.exists(src):
        shutil.copy2(src, dst)
        print(f"[2/5] Copied {f}")

# 合并标准库
src_lib = os.path.join(SYSPY, "Lib")
dst_lib = os.path.join(RELEASE, ".venv", "Lib")
os.makedirs(dst_lib, exist_ok=True)
copied = 0
for root, dirs, files in os.walk(src_lib):
    if "site-packages" in root.replace("\\", "/").split("/"):
        continue
    rel = os.path.relpath(root, src_lib)
    target_dir = os.path.join(dst_lib, rel)
    os.makedirs(target_dir, exist_ok=True)
    for f in files:
        tgt = os.path.join(target_dir, f)
        if not os.path.exists(tgt):
            shutil.copy2(os.path.join(root, f), tgt)
            copied += 1
print(f"[3/5] Merged stdlib: {copied} files")

# 复制 tcl/tk 等运行时目录（如果存在）
for sub in ["tcl", "DLLs"]:
    src = os.path.join(SYSPY, sub)
    if os.path.isdir(src):
        dst = os.path.join(RELEASE, ".venv", sub)
        if not os.path.exists(dst):
            shutil.copytree(src, dst)
            print(f"[4/5] Copied {sub}/")

print("[5/5] Fix complete — venv is now portable")
