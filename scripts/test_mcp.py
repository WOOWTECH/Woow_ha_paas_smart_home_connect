"""Run MCP installer/URL regressions inside an environment with HA installed."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="woow-mcp-tests-") as directory:
        scratch = Path(directory)
        components = scratch / "custom_components"
        components.mkdir()
        (components / "woow_paas_smart_home").symlink_to(root, target_is_directory=True)
        targets = []
        for name in ("test_native_mcp_setup.py", "test_native_mcp_url.py"):
            target = scratch / name
            shutil.copyfile(root / "watchdog_tests" / name, target)
            targets.append(str(target))
        return subprocess.call(
            [sys.executable, "-m", "pytest", "-q", *targets],
            cwd=scratch,
            env={**os.environ, "PYTHONPATH": str(scratch), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        )


if __name__ == "__main__":
    raise SystemExit(main())
