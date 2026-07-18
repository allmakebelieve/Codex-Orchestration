from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    REPO_ROOT
    / "plugins"
    / "codex-orchestration"
    / "skills"
    / "codex-orchestration"
    / "scripts"
    / "update_plugin.py"
)
SPEC = importlib.util.spec_from_file_location("update_plugin", SCRIPT)
assert SPEC and SPEC.loader
UPDATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UPDATE)


class FakeCodex:
    def __init__(
        self,
        *,
        home: Path,
        current: str = "0.6.0",
        candidate: str = "0.7.0",
        source_url: str = "https://github.com/Cjbuilds/Codex-Orchestration.git",
        enabled: bool = True,
    ) -> None:
        self.home = home.resolve()
        self.current = current
        self.candidate = candidate
        self.source_url = source_url
        self.enabled = enabled
        self.installed = current
        self.calls: list[tuple[str, ...]] = []
        self.plugin_root = (
            self.home
            / ".tmp"
            / "marketplaces"
            / "codex-orchestration"
            / "plugins"
            / "codex-orchestration"
        )
        manifest = self.plugin_root / ".codex-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(
                {
                    "name": "codex-orchestration",
                    "version": current,
                    "repository": "https://github.com/Cjbuilds/Codex-Orchestration",
                }
            ),
            encoding="utf-8",
        )

    def entry(self) -> dict[str, object]:
        return {
            "pluginId": UPDATE.PLUGIN_ID,
            "name": UPDATE.PLUGIN_NAME,
            "marketplaceName": UPDATE.MARKETPLACE_NAME,
            "version": self.installed,
            "installed": True,
            "enabled": self.enabled,
            "source": {"source": "local", "path": str(self.plugin_root)},
            "marketplaceSource": {
                "sourceType": "git",
                "source": self.source_url,
            },
        }

    def __call__(
        self, _binary: Path, arguments: list[str], _environment: dict[str, str]
    ) -> object:
        command = tuple(arguments)
        self.calls.append(command)
        if command == ("plugin", "list", "--json"):
            return {"installed": [self.entry()], "available": []}
        if command == (
            "plugin",
            "marketplace",
            "upgrade",
            UPDATE.MARKETPLACE_NAME,
            "--json",
        ):
            manifest = self.plugin_root / ".codex-plugin" / "plugin.json"
            manifest.write_text(
                json.dumps(
                    {
                        "name": UPDATE.PLUGIN_NAME,
                        "version": self.candidate,
                        "repository": UPDATE.REPOSITORY_URL,
                    }
                ),
                encoding="utf-8",
            )
            return {
                "selectedMarketplaces": [UPDATE.MARKETPLACE_NAME],
                "upgradedRoots": [
                    str(self.home / ".tmp" / "marketplaces" / UPDATE.MARKETPLACE_NAME)
                ],
                "errors": [],
            }
        if command == ("plugin", "add", UPDATE.PLUGIN_ID, "--json"):
            self.installed = self.candidate
            return {"version": self.candidate, "installedPath": "/cache/new"}
        raise AssertionError(f"unexpected Codex call: {command!r}")


class PluginUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name) / "codex-home"
        self.home.mkdir()
        self.binary = Path(self.temporary.name) / "codex"
        self.binary.write_text("fake", encoding="utf-8")

    def test_successful_update_uses_only_native_marketplace_and_add_commands(self) -> None:
        fake = FakeCodex(home=self.home)
        routing = self.home / ".codex-orchestration-routing.json"
        auth = self.home / "auth.json"
        session = self.home / "sessions" / "keep.jsonl"
        session.parent.mkdir()
        routing.write_text("routing-sentinel", encoding="utf-8")
        auth.write_text("auth-sentinel", encoding="utf-8")
        session.write_text("chat-sentinel", encoding="utf-8")

        result = UPDATE.perform_update(
            self.binary, self.home, runner=fake, environment={"PATH": "/bin"}
        )

        self.assertEqual(result, ("0.6.0", "0.7.0", True))
        self.assertEqual(
            fake.calls,
            [
                ("plugin", "list", "--json"),
                (
                    "plugin",
                    "marketplace",
                    "upgrade",
                    UPDATE.MARKETPLACE_NAME,
                    "--json",
                ),
                ("plugin", "add", UPDATE.PLUGIN_ID, "--json"),
                ("plugin", "list", "--json"),
            ],
        )
        self.assertEqual(routing.read_text(encoding="utf-8"), "routing-sentinel")
        self.assertEqual(auth.read_text(encoding="utf-8"), "auth-sentinel")
        self.assertEqual(session.read_text(encoding="utf-8"), "chat-sentinel")

    def test_same_version_is_idempotent_and_skips_install(self) -> None:
        fake = FakeCodex(home=self.home, candidate="0.6.0")
        result = UPDATE.perform_update(
            self.binary, self.home, runner=fake, environment={}
        )
        self.assertEqual(result, ("0.6.0", "0.6.0", False))
        self.assertNotIn(("plugin", "add", UPDATE.PLUGIN_ID, "--json"), fake.calls)
        self.assertEqual(fake.calls.count(("plugin", "list", "--json")), 2)

    def test_untrusted_or_local_marketplace_fails_before_upgrade(self) -> None:
        for source_type, source in (
            ("git", "https://github.com/attacker/Codex-Orchestration.git"),
            ("local", str(self.home / "checkout")),
            ("git", "https://github.com/Cjbuilds/Codex-Orchestration.git?ref=evil"),
            ("git", "https://github.com:invalid/Cjbuilds/Codex-Orchestration.git"),
        ):
            with self.subTest(source_type=source_type, source=source):
                fake = FakeCodex(home=self.home, source_url=source)
                original_entry = fake.entry

                def entry() -> dict[str, object]:
                    value = original_entry()
                    value["marketplaceSource"] = {
                        "sourceType": source_type,
                        "source": source,
                    }
                    return value

                fake.entry = entry  # type: ignore[method-assign]
                with self.assertRaises(UPDATE.UpdateError):
                    UPDATE.perform_update(
                        self.binary, self.home, runner=fake, environment={}
                    )
                self.assertEqual(fake.calls, [("plugin", "list", "--json")])

    def test_downgrade_and_malformed_candidate_fail_before_install(self) -> None:
        fake = FakeCodex(home=self.home, current="0.7.0", candidate="0.6.0")
        with self.assertRaisesRegex(UPDATE.UpdateError, "downgrade"):
            UPDATE.perform_update(
                self.binary, self.home, runner=fake, environment={}
            )
        self.assertNotIn(("plugin", "add", UPDATE.PLUGIN_ID, "--json"), fake.calls)

        fake = FakeCodex(home=self.home)
        original = fake.__call__

        def malformed(
            binary: Path, arguments: list[str], environment: dict[str, str]
        ) -> object:
            result = original(binary, arguments, environment)
            if tuple(arguments[:3]) == ("plugin", "marketplace", "upgrade"):
                (fake.plugin_root / ".codex-plugin" / "plugin.json").write_text(
                    '{"name":"wrong","version":"0.7.0"}', encoding="utf-8"
                )
            return result

        with self.assertRaisesRegex(UPDATE.UpdateError, "manifest"):
            UPDATE.perform_update(
                self.binary, self.home, runner=malformed, environment={}
            )
        self.assertNotIn(("plugin", "add", UPDATE.PLUGIN_ID, "--json"), fake.calls)

    def test_post_install_version_or_enabled_drift_fails_closed(self) -> None:
        fake = FakeCodex(home=self.home)
        original = fake.__call__

        def drift(
            binary: Path, arguments: list[str], environment: dict[str, str]
        ) -> object:
            result = original(binary, arguments, environment)
            if tuple(arguments) == ("plugin", "add", UPDATE.PLUGIN_ID, "--json"):
                fake.enabled = False
            return result

        with self.assertRaisesRegex(UPDATE.UpdateError, "verification"):
            UPDATE.perform_update(
                self.binary, self.home, runner=drift, environment={}
            )

    def test_candidate_path_must_be_exact_non_symlinked_marketplace_path(self) -> None:
        fake = FakeCodex(home=self.home)
        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        original_entry = fake.entry

        def entry() -> dict[str, object]:
            value = original_entry()
            value["source"] = {"source": "local", "path": str(outside)}
            return value

        fake.entry = entry  # type: ignore[method-assign]
        with self.assertRaisesRegex(UPDATE.UpdateError, "source path"):
            UPDATE.perform_update(
                self.binary, self.home, runner=fake, environment={}
            )
        self.assertEqual(fake.calls, [("plugin", "list", "--json")])

        fake = FakeCodex(home=self.home)
        plugins = fake.plugin_root.parent
        real_plugins = plugins.with_name("real-plugins")
        plugins.rename(real_plugins)
        plugins.symlink_to(real_plugins, target_is_directory=True)
        with self.assertRaisesRegex(UPDATE.UpdateError, "source path"):
            UPDATE.perform_update(
                self.binary, self.home, runner=fake, environment={}
            )
        self.assertEqual(fake.calls, [("plugin", "list", "--json")])

    def test_semver_handles_prereleases_and_rejects_ambiguous_versions(self) -> None:
        self.assertLess(
            UPDATE.SemVer.parse("0.7.0-rc.1").compare(
                UPDATE.SemVer.parse("0.7.0")
            ),
            0,
        )
        self.assertGreater(
            UPDATE.SemVer.parse("0.7.0-beta.11").compare(
                UPDATE.SemVer.parse("0.7.0-beta.2")
            ),
            0,
        )
        for value in ("0.7", "00.7.0", "0.7.0-01", "0.7.0-a..b", True):
            with self.subTest(value=value), self.assertRaises(UPDATE.UpdateError):
                UPDATE.SemVer.parse(value)

    def test_child_environment_strips_credentials_and_code_injection_hooks(self) -> None:
        injected = {
            "PATH": "/safe/bin",
            "OPENROUTER_API_KEY": "secret",
            "GIT_CONFIG_COUNT": "1",
            "SSH_ASKPASS": "/tmp/askpass",
            "NODE_OPTIONS": "--require=/tmp/inject.js",
            "PYTHONPATH": "/tmp/inject",
            "DYLD_INSERT_LIBRARIES": "/tmp/inject.dylib",
            "LD_PRELOAD": "/tmp/inject.so",
        }
        with mock.patch.dict(UPDATE.os.environ, injected, clear=True):
            environment = UPDATE._safe_environment(self.home)
        self.assertEqual(environment["PATH"], "/safe/bin")
        self.assertEqual(environment["CODEX_HOME"], str(self.home))
        for key in injected.keys() - {"PATH"}:
            self.assertNotIn(key, environment)


if __name__ == "__main__":
    unittest.main()
