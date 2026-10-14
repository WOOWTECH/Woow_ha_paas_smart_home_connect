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
    revocation = os.environ.get(
        'SHC_ACCESS_REVOCATION_SOURCE', str(root / 'access_revocation.py'))
    with tempfile.TemporaryDirectory(prefix='shc-watchdog-tests-') as directory:
        targets = []
        for name in ('test_cloudflared_watchdog.py', 'test_access_revocation.py'):
            target = Path(directory) / name
            shutil.copyfile(root / 'watchdog_tests' / name, target)
            targets.append(str(target))
        environment = dict(
            os.environ, SHC_MANAGER_SOURCE=source,
            SHC_ACCESS_REVOCATION_SOURCE=revocation,
        )
        return subprocess.call(
            [sys.executable, '-m', 'pytest', '-q', *targets],
            cwd=directory, env=environment,
        )


if __name__ == '__main__':
    raise SystemExit(main())
