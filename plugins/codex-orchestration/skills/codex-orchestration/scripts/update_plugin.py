#!/usr/bin/env python3
"""Update Codex-Orchestration from its canonical Git marketplace.

This wrapper deliberately delegates mutation to Codex's native plugin CLI. It
does not remove the plugin, rewrite config, inspect credentials, or touch routing
and session state.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
from typing import Any, Callable, NamedTuple
from urllib.parse import urlsplit


PLUGIN_NAME = "codex-orchestration"
MARKETPLACE_NAME = "codex-orchestration"
PLUGIN_ID = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
REPOSITORY_URL = "https://github.com/Cjbuilds/Codex-Orchestration"
COMMAND_TIMEOUT_SECONDS = 120
MAX_COMMAND_OUTPUT = 1_000_000
MAX_MANIFEST_BYTES = 128_000


class UpdateError(RuntimeError):
    """The installed source or native update result failed validation."""


class SemVer(NamedTuple):
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: object) -> "SemVer":
        if type(value) is not str:
            raise UpdateError("plugin version is not a string")
        match = re.fullmatch(
            r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
            r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
            r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
            value,
        )
        if match is None:
            raise UpdateError(f"invalid plugin version: {value!r}")
        prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
        if any(
            part.isdigit() and len(part) > 1 and part.startswith("0")
            for part in prerelease
        ):
            raise UpdateError(f"invalid plugin version: {value!r}")
        return cls(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            prerelease,
        )

    def compare(self, other: "SemVer") -> int:
        left = (self.major, self.minor, self.patch)
        right = (other.major, other.minor, other.patch)
        if left != right:
            return -1 if left < right else 1
        if not self.prerelease and not other.prerelease:
            return 0
        if not self.prerelease:
            return 1
        if not other.prerelease:
            return -1
        for left_part, right_part in zip(self.prerelease, other.prerelease):
            if left_part == right_part:
                continue
            left_numeric = left_part.isdigit()
            right_numeric = right_part.isdigit()
            if left_numeric and right_numeric:
                return -1 if int(left_part) < int(right_part) else 1
            if left_numeric != right_numeric:
                return -1 if left_numeric else 1
            return -1 if left_part < right_part else 1
        if len(self.prerelease) == len(other.prerelease):
            return 0
        return -1 if len(self.prerelease) < len(other.prerelease) else 1


Runner = Callable[[Path, list[str], dict[str, str]], object]


def _no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise UpdateError(f"Codex JSON contains duplicate key {key!r}")
        value[key] = item
    return value


def _load_json(value: str, *, label: str) -> object:
    try:
        return json.loads(value, object_pairs_hook=_no_duplicate_object)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise UpdateError(f"{label} did not return strict JSON") from exc


def _safe_environment(codex_home: Path) -> dict[str, str]:
    """Keep host essentials while withholding credentials and Git code hooks."""

    environment: dict[str, str] = {}
    sensitive_fragments = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")
    blocked_exact = {
        "ANTHROPIC_AUTH_TOKEN",
        "BASH_ENV",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_VERTEX",
        "ENV",
        "NODE_OPTIONS",
        "NODE_PATH",
        "PERL5OPT",
        "PYTHONHOME",
        "PYTHONPATH",
        "RUBYOPT",
    }
    for key, value in os.environ.items():
        upper = key.upper()
        if (
            upper in blocked_exact
            or upper.startswith("GIT_")
            or upper.startswith("SSH_")
            or upper.startswith("DYLD_")
            or upper.startswith("LD_PRELOAD")
            or any(fragment in upper for fragment in sensitive_fragments)
        ):
            continue
        environment[key] = value
    environment["CODEX_HOME"] = str(codex_home)
    return environment


def _run_json(binary: Path, arguments: list[str], environment: dict[str, str]) -> object:
    codex_home = environment.get("CODEX_HOME")
    if not codex_home:
        raise UpdateError("Codex plugin command is missing its isolated CODEX_HOME")
    try:
        result = subprocess.run(
            [str(binary), *arguments],
            cwd=codex_home,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise UpdateError(f"could not run Codex plugin command: {exc}") from exc
    if len(result.stdout) > MAX_COMMAND_OUTPUT or len(result.stderr) > MAX_COMMAND_OUTPUT:
        raise UpdateError("Codex plugin command returned excessive output")
    if result.returncode != 0:
        action = " ".join(arguments[:4])
        raise UpdateError(
            f"Codex {action} failed with exit {result.returncode}; no command output "
            "was echoed because it may contain private local diagnostics"
        )
    return _load_json(result.stdout, label="Codex plugin command")


def resolve_binary(value: str) -> Path:
    candidate = Path(value).expanduser()
    if candidate.parent != Path(".") or os.sep in value:
        if not candidate.is_file():
            raise UpdateError(f"Codex binary does not exist: {candidate}")
        return candidate.resolve()
    found = shutil.which(value)
    if not found:
        raise UpdateError(f"Codex binary is not on PATH: {value}")
    return Path(found).resolve()


def resolve_codex_home(value: Path | None) -> Path:
    selected = value or Path(os.environ.get("CODEX_HOME", "~/.codex"))
    expanded = selected.expanduser()
    if not expanded.is_dir():
        raise UpdateError(f"CODEX_HOME does not exist: {expanded}")
    return expanded.resolve()


def _is_canonical_repository(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() != "github.com"
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        return False
    path = parsed.path.rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    return path.lower() == "/cjbuilds/codex-orchestration"


def _plugin_entry(payload: object) -> dict[str, Any]:
    if type(payload) is not dict or set(payload) != {"installed", "available"}:
        raise UpdateError("Codex plugin list returned an unsupported shape")
    installed = payload.get("installed")
    if type(installed) is not list:
        raise UpdateError("Codex plugin list did not return installed plugins")
    matches = [
        entry
        for entry in installed
        if type(entry) is dict and entry.get("pluginId") == PLUGIN_ID
    ]
    if len(matches) != 1:
        raise UpdateError(f"expected exactly one installed {PLUGIN_ID} entry")
    return matches[0]


def _validate_entry(entry: dict[str, Any], codex_home: Path) -> tuple[str, bool, Path]:
    if (
        entry.get("name") != PLUGIN_NAME
        or entry.get("marketplaceName") != MARKETPLACE_NAME
        or entry.get("installed") is not True
        or type(entry.get("enabled")) is not bool
    ):
        raise UpdateError("installed plugin identity or state is invalid")
    version = entry.get("version")
    SemVer.parse(version)
    assert isinstance(version, str)

    marketplace = entry.get("marketplaceSource")
    if (
        type(marketplace) is not dict
        or marketplace.get("sourceType") != "git"
        or not _is_canonical_repository(marketplace.get("source"))
    ):
        raise UpdateError(
            "the plugin is not installed from the canonical Git marketplace; "
            "automatic update is refused"
        )

    source = entry.get("source")
    source_path = source.get("path") if type(source) is dict else None
    if type(source_path) is not str or not source_path:
        raise UpdateError("installed plugin source path is unavailable")
    expected = (
        codex_home
        / ".tmp"
        / "marketplaces"
        / MARKETPLACE_NAME
        / "plugins"
        / PLUGIN_NAME
    )
    candidate = Path(source_path).expanduser()
    if not candidate.is_absolute() or candidate != expected:
        raise UpdateError("installed plugin source path is outside the trusted marketplace")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise UpdateError("installed plugin source path is unavailable") from exc
    if resolved != expected:
        raise UpdateError("installed plugin source path is outside the trusted marketplace")
    current = codex_home
    for segment in candidate.relative_to(codex_home).parts:
        current = current / segment
        if current.is_symlink():
            raise UpdateError("installed plugin source path contains a symlink")
    if not resolved.is_dir():
        raise UpdateError("installed plugin source path is not a directory")
    return version, entry["enabled"], resolved


def _validate_upgrade(payload: object, codex_home: Path) -> None:
    if type(payload) is not dict:
        raise UpdateError("marketplace upgrade returned an invalid result")
    if payload.get("selectedMarketplaces") != [MARKETPLACE_NAME]:
        raise UpdateError("marketplace upgrade targeted an unexpected marketplace")
    if payload.get("errors") != []:
        raise UpdateError("marketplace upgrade reported errors")
    roots = payload.get("upgradedRoots")
    expected = codex_home / ".tmp" / "marketplaces" / MARKETPLACE_NAME
    if type(roots) is not list:
        raise UpdateError("marketplace upgrade omitted its updated root")
    trusted_roots: set[Path] = set()
    for root in roots:
        if type(root) is not str:
            raise UpdateError("marketplace upgrade returned an invalid root")
        candidate = Path(root).expanduser()
        if not candidate.is_absolute() or candidate != expected:
            raise UpdateError("marketplace upgrade returned an unexpected root")
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise UpdateError("marketplace upgrade returned a missing root") from exc
        if resolved != expected or not resolved.is_dir():
            raise UpdateError("marketplace upgrade returned an unsafe root")
        current = codex_home
        for segment in candidate.relative_to(codex_home).parts:
            current = current / segment
            if current.is_symlink():
                raise UpdateError("marketplace upgrade root contains a symlink")
        trusted_roots.add(resolved)
    if expected not in trusted_roots:
        raise UpdateError("marketplace upgrade did not refresh the trusted snapshot")


def _candidate_version(plugin_root: Path) -> str:
    manifest = plugin_root / ".codex-plugin" / "plugin.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise UpdateError("candidate plugin manifest is missing or symlinked")
    details = manifest.stat()
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
        or details.st_size > MAX_MANIFEST_BYTES
    ):
        raise UpdateError("candidate plugin manifest has unsafe metadata")
    try:
        payload = _load_json(manifest.read_text(encoding="utf-8"), label="plugin manifest")
    except (OSError, UnicodeError) as exc:
        raise UpdateError("candidate plugin manifest cannot be read") from exc
    if type(payload) is not dict:
        raise UpdateError("candidate plugin manifest is not an object")
    if (
        payload.get("name") != PLUGIN_NAME
        or not _is_canonical_repository(payload.get("repository"))
    ):
        raise UpdateError("candidate plugin manifest identity is invalid")
    version = payload.get("version")
    SemVer.parse(version)
    assert isinstance(version, str)
    return version


def perform_update(
    binary: Path,
    codex_home: Path,
    *,
    runner: Runner = _run_json,
    environment: dict[str, str] | None = None,
) -> tuple[str, str, bool]:
    """Refresh, validate, install, and verify one canonical plugin update."""

    codex_home = codex_home.expanduser().resolve()
    effective_environment = (
        dict(environment) if environment is not None else _safe_environment(codex_home)
    )
    effective_environment["CODEX_HOME"] = str(codex_home)

    before_entry = _plugin_entry(
        runner(binary, ["plugin", "list", "--json"], effective_environment)
    )
    current_version, was_enabled, plugin_root = _validate_entry(
        before_entry, codex_home
    )
    upgrade = runner(
        binary,
        [
            "plugin",
            "marketplace",
            "upgrade",
            MARKETPLACE_NAME,
            "--json",
        ],
        effective_environment,
    )
    _validate_upgrade(upgrade, codex_home)
    candidate_version = _candidate_version(plugin_root)
    precedence = SemVer.parse(candidate_version).compare(SemVer.parse(current_version))
    if precedence < 0:
        raise UpdateError(
            f"candidate {candidate_version} is a downgrade from {current_version}; refused"
        )
    if precedence == 0:
        unchanged_entry = _plugin_entry(
            runner(binary, ["plugin", "list", "--json"], effective_environment)
        )
        unchanged_version, unchanged_enabled, unchanged_root = _validate_entry(
            unchanged_entry, codex_home
        )
        if (
            unchanged_version != current_version
            or unchanged_enabled is not was_enabled
            or unchanged_root != plugin_root
        ):
            raise UpdateError(
                "post-refresh verification found version, enabled-state, or source drift"
            )
        return current_version, candidate_version, False

    installed = runner(
        binary,
        ["plugin", "add", PLUGIN_ID, "--json"],
        effective_environment,
    )
    if type(installed) is not dict or installed.get("version") != candidate_version:
        raise UpdateError("Codex plugin install returned an unexpected version")

    after_entry = _plugin_entry(
        runner(binary, ["plugin", "list", "--json"], effective_environment)
    )
    after_version, after_enabled, after_root = _validate_entry(after_entry, codex_home)
    if (
        after_version != candidate_version
        or after_enabled is not was_enabled
        or after_root != plugin_root
    ):
        raise UpdateError(
            "post-install verification found version, enabled-state, or source drift"
        )
    return current_version, candidate_version, True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Update Codex-Orchestration from its canonical Git marketplace without "
            "removing the plugin or changing routing, credentials, or sessions."
        )
    )
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--codex-home", type=Path)
    return parser.parse_args()


def main() -> int:
    try:
        args = parse_args()
        binary = resolve_binary(args.codex_bin)
        codex_home = resolve_codex_home(args.codex_home)
        previous, current, changed = perform_update(binary, codex_home)
        if changed:
            print(f"Codex-Orchestration updated: {previous} -> {current}")
            print(
                "Restart Codex Desktop and start a new task to load the updated plugin. "
                "Existing chats, credentials, and routing state were not touched."
            )
        else:
            print(f"Codex-Orchestration is already current at {current}.")
        return 0
    except UpdateError as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
