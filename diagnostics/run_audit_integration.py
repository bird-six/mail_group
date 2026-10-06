"""Run Vue/FastAPI integration with synthetic addresses and blocked SMTP."""
from pathlib import Path
import os
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        raise SystemExit("Install Node.js/npm on the development machine first")
    environment = {**os.environ, "MAIL_GROUP_TEST_PYTHON": sys.executable}
    return subprocess.run([npm, "exec", "--", "vitest", "run", "--config", "integration.config.ts"],
                          cwd=ROOT / "mail_group_vue3", env=environment).returncode


if __name__ == "__main__":
    sys.exit(main())
