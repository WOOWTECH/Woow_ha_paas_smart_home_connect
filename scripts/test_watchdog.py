"""Run OS lifecycle tests without importing the entire HA integration package."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    source = os.environ.get('SHC_MANAGER_SOURCE', str(root / 'cloudflared_manager.py'))
    with tempfile.TemporaryDirectory(prefix='shc-watchdog-tests-') as directory:
        target = Path(directory) / 'test_cloudflared_watchdog.py'
        shutil.copyfile(root / 'watchdog_tests' / target.name, target)
        environment = dict(os.environ, SHC_MANAGER_SOURCE=source)
        return subprocess.call(
            [sys.executable, '-m', 'pytest', '-q', str(target)],
            cwd=directory, env=environment,
        )


if __name__ == '__main__':
    raise SystemExit(main())
