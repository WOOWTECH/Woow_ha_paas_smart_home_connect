"""Offline packaging regression against an explicitly supplied HACS checkout.

Run with Python 3.13 and scripts/requirements-hacs.txt. Actual HACS repository
classes, JSON loaders, schemas and file selection run unchanged. GitHub tree /
content responses and HA service imports are isolated; no HA is started and no
files are installed. This does not prove live HACS installation or upgrade.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib
import importlib.metadata
import json
import logging
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import unittest

import voluptuous as vol

DOMAIN = "woow_paas_smart_home"
MIRROR = "WOOWTECH/Woow_ha_paas_smart_home_connect"


def unavailable(*args, **kwargs):
    raise AssertionError("HA services and network access are outside this offline test")


def load_hacs(source: Path):
    """Import real HACS modules, bypassing integration bootstrap only."""
    for name in ("homeassistant", "homeassistant.core", "homeassistant.helpers",
                 "homeassistant.helpers.device_registry", "homeassistant.helpers.issue_registry",
                 "homeassistant.helpers.config_validation", "homeassistant.helpers.json",
                 "homeassistant.helpers.storage", "homeassistant.loader", "homeassistant.util",
                 "homeassistant.util.json"):
        sys.modules[name] = ModuleType(name)
    sys.modules["homeassistant.core"].HomeAssistant = unavailable
    sys.modules["homeassistant.helpers.storage"].Store = type(
        "UnavailableStore", (), {"__init__": unavailable}
    )
    sys.modules["homeassistant.helpers.issue_registry"].IssueSeverity = SimpleNamespace(WARNING="warning")
    sys.modules["homeassistant.helpers.issue_registry"].async_create_issue = unavailable
    sys.modules["homeassistant.loader"].async_get_custom_components = unavailable
    sys.modules["homeassistant.helpers.config_validation"].url = vol.Url()
    sys.modules["homeassistant.helpers.json"].JSONEncoder = json.JSONEncoder
    sys.modules["homeassistant.util.json"].json_loads = json.loads
    # Empty package shells prevent HACS/HA startup; their submodules are the
    # actual files from source, including repositories/base.py and integration.py.
    for suffix in ("", ".repositories", ".utils", ".validate"):
        name = "hacs_offline" + suffix
        module = ModuleType(name)
        module.__path__ = [str(source.joinpath(*suffix.lstrip(".").split(".")))] if suffix else [str(source)]
        sys.modules[name] = module
    return (
        importlib.import_module("hacs_offline.repositories.integration").HacsIntegrationRepository,
        importlib.import_module("hacs_offline.utils.validate"),
        importlib.import_module("hacs_offline.validate.integration_manifest").Validator,
        importlib.import_module("hacs_offline.exceptions").HacsException,
    )


def read_tree(root: Path) -> dict[str, bytes]:
    names = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=root
    ).decode().split("\0")
    return {name: (root / name).read_bytes() for name in sorted(set(names)) if name and (root / name).is_file()}


def repository_fixture(files):
    """Replace GitHub I/O, retaining HACS common_validate and manifest loaders."""
    requests = []

    async def github_content(*, method, repository, path, params, **kwargs):
        assert repository == MIRROR
        assert params == {"ref": "main"}
        requests.append(str(path))
        return SimpleNamespace(data=SimpleNamespace(content=base64.b64encode(files[str(path)]).decode()))

    async def raw_content(url, **kwargs):
        prefix = f"https://raw.githubusercontent.com/{MIRROR}/main/"
        assert url.startswith(prefix)
        path = url.removeprefix(prefix)
        requests.append(path)
        return files[path].decode()

    hacs = SimpleNamespace(
        core=SimpleNamespace(config_path="/offline-ha-config"),
        system=SimpleNamespace(action=False), status=SimpleNamespace(startup=False),
        log=logging.getLogger("hacs-offline"),
        githubapi=SimpleNamespace(repos=SimpleNamespace(contents=SimpleNamespace(get=unavailable))),
        async_github_api_method=github_content, async_download_file=raw_content,
    )
    repository = Repository(hacs, MIRROR)

    async def supply_tree(**kwargs):
        # Boundary replacing remote registration/ETag/tree fetching only.
        repository.ref = repository.data.default_branch = "main"
        directories = {str(parent) for name in files for parent in Path(name).parents if str(parent) != "."}
        repository.tree = [SimpleNamespace(
            full_path=name, filename=Path(name).name, path=str(Path(name).parent) if "/" in name else "",
            is_directory=name in directories,
            download_url=f"https://raw.githubusercontent.com/{MIRROR}/main/{name}",
        ) for name in sorted(set(files) | directories)]
        repository.treefiles = sorted(files)

    repository.common_update_data = supply_tree
    return repository, requests


class MetadataTests(unittest.TestCase):
    def test_root_repository_loads_and_selects_runtime_files(self):
        async def scenario():
            repository, requests = repository_fixture(FILES)
            self.assertTrue(await repository.validate_repository())
            self.assertEqual(repository.content.path.remote, "")
            self.assertEqual(repository.content.path.local, f"/offline-ha-config/custom_components/{DOMAIN}")
            self.assertEqual(repository.data.domain, DOMAIN)
            self.assertEqual(requests, ["hacs.json", "manifest.json"])
            await ManifestValidator(repository).async_validate()
            selected = {item.path for item in repository.gather_files_to_download()}
            for name in ("__init__.py", "manifest.json", "config_flow.py", "coordinator.py",
                         "cloudflared_manager.py", "services.yaml", "strings.json", "translations/en.json"):
                self.assertIn(name, selected)
            self.assertEqual(selected, set(FILES))
            self.assertFalse(any(name.startswith("custom_components/") for name in selected))
            OBSERVATIONS["selected_files"] = sorted(selected)
            OBSERVATIONS["local_install_path"] = repository.content.path.local
            OBSERVATIONS["content_requests"] = requests
        asyncio.run(scenario())

    def test_hacs_manifest_schema_and_readme(self):
        metadata = json.loads(FILES["hacs.json"])
        Schemas.HACS_MANIFEST_JSON_SCHEMA(metadata)
        self.assertEqual(metadata["name"], "Woow PaaS Smart Home")
        self.assertIs(metadata["content_in_root"], True)
        self.assertIs(metadata["render_readme"], True)
        self.assertIn("README.md", FILES)
        self.assertFalse(metadata.get("zip_release", False))

    def test_integration_manifest_schema_and_identity(self):
        manifest = json.loads(FILES["manifest.json"])
        Schemas.INTEGRATION_MANIFEST_JSON_SCHEMA(manifest)
        self.assertEqual(manifest["domain"], DOMAIN)
        if ARGS.expected_version:
            self.assertEqual(manifest["version"], ARGS.expected_version)
        self.assertEqual(manifest["documentation"], f"https://github.com/{MIRROR}")
        self.assertEqual(manifest["issue_tracker"], f"https://github.com/{MIRROR}/issues")

    def test_missing_or_false_root_flag_is_rejected_by_real_loader(self):
        for mode in ("missing", "false"):
            with self.subTest(mode=mode):
                files = dict(FILES)
                files.pop("hacs.json", None)
                if mode == "false":
                    files["hacs.json"] = json.dumps({"name": "fixture", "content_in_root": False}).encode()
                repository, _ = repository_fixture(files)
                with self.assertRaisesRegex(HacsException, "Repository structure.*not compliant"):
                    asyncio.run(repository.validate_repository())

    def test_missing_issue_tracker_is_rejected_by_hacs_schema(self):
        manifest = json.loads(FILES["manifest.json"])
        manifest.pop("issue_tracker", None)
        with self.assertRaisesRegex(vol.Invalid, "issue_tracker"):
            Schemas.INTEGRATION_MANIFEST_JSON_SCHEMA(manifest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hacs-source", type=Path, required=True, help="Directory containing HACS manifest.json and repositories/")
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--expected-version", help="Optional proof that an existing version was preserved")
    parser.add_argument("--report", type=Path, help="Write JSON evidence outside the integration checkout")
    ARGS = parser.parse_args()
    ARGS.hacs_source = ARGS.hacs_source.resolve()
    sys.addaudithook(lambda event, args: unavailable() if event == "socket.connect" else None)
    Repository, Schemas, ManifestValidator, HacsException = load_hacs(ARGS.hacs_source)
    FILES = read_tree(ARGS.repository)
    OBSERVATIONS = {}
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(MetadataTests))
    if ARGS.report:
        modules = {str(Path(module.__file__).relative_to(ARGS.hacs_source)): hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                   for name, module in sys.modules.items() if name.startswith("hacs_offline.") and getattr(module, "__file__", None)}
        ARGS.report.write_text(json.dumps({
            "offline_only": True, "native_ha_started": False, "live_github_tested": False,
            "hacs_version": json.loads((ARGS.hacs_source / "manifest.json").read_text())["version"],
            "python": sys.version, "tests_run": result.testsRun,
            "failures": len(result.failures), "errors": len(result.errors), "passed": result.wasSuccessful(),
            "hacs_source": str(ARGS.hacs_source), "hacs_module_sha256": modules,
            "candidate_file_sha256": {name: hashlib.sha256(data).hexdigest() for name, data in FILES.items()},
            "dependencies": {name: importlib.metadata.version(name) for name in ("aiogithubapi", "attrs", "awesomeversion", "voluptuous")},
            "boundaries": ["HA import services unavailable; JSON uses stdlib; HA URL validator uses voluptuous.Url",
                           "GitHub tree registration replaced by local git inventory; API/raw responses use local bytes",
                           "No HACS bootstrap, download writes, HA discovery or upgrade lifecycle executed"],
            **OBSERVATIONS,
        }, indent=2) + "\n")
    raise SystemExit(0 if result.wasSuccessful() else 1)
