"""Read-only host launch settings for the selected user-owned host."""

from __future__ import annotations

import getopt
import os
import shlex
import shutil
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnigent._platform import resolve_cli_binary
from omnigent.config import load_global_config
from omnigent.harness_aliases import canonicalize_harness
from omnigent.harness_startup_config import resolve_harness_config

SUPPORTED_HARNESSES = {"claude-native", "codex-native"}


class HarnessEnvironment(BaseModel):
    model_config = ConfigDict(strict=True)

    inherit: bool
    variables: dict[str, str]
    unset: list[str]


class HarnessStartup(BaseModel):
    """Launch metadata returned through the owner-only host API."""

    model_config = ConfigDict(strict=True)

    command: str
    resolved_path: str | None
    command_source: Literal["env", "config", "default"]
    arg_count: int = Field(ge=0)
    # None identifies an older host that reports only arg_count.
    args: list[str] | None = None
    configured_command: str | None = None
    configured_args: list[str] | None = None
    environment: HarnessEnvironment | None = None


def describe_harness_startup(harness: str) -> HarnessStartup:
    """Describe host defaults for web launches; workspace overrides may differ."""
    from omnigent.host.connect import _build_runner_env

    harness = canonicalize_harness(harness) or harness
    if harness not in SUPPORTED_HARNESSES:
        raise ValueError("launch settings are not reported for this harness")
    _, overrides = resolve_harness_config(load_global_config())
    entry = overrides.get(harness, {})
    env = _build_runner_env(
        os.environ, server_url="", runner_id="", binding_token="", workspace="", parent_pid=0
    )
    name = harness.removesuffix("-native")
    path = env.get("PATH", os.defpath)
    override = env.get(f"OMNIGENT_{name.upper()}_PATH", "").strip()
    command = entry.get("command", name)
    source: Literal["env", "config", "default"] = "config" if "command" in entry else "default"
    # Claude uses env before config; Codex uses config before a resolvable env override.
    if override and (
        harness == "claude-native"
        or (
            "command" not in entry
            and (
                shutil.which(override, path=path)
                or (os.path.isfile(override) and os.access(override, os.X_OK))
            )
        )
    ):
        command, source = override, "env"
    args = entry.get("args", [])
    environment = HarnessEnvironment(inherit=True, variables={}, unset=[])
    unwrapped = _unwrap_env(command, args, path)
    if unwrapped is not None:
        command, args, path, environment = unwrapped
        # env searches only its effective PATH, never our extra install directories.
        resolved = shutil.which(command, path=path)
    else:
        if Path(command).name == "env":
            environment = None
        resolved = resolve_cli_binary(command, which=lambda cmd: shutil.which(cmd, path=path))
    return HarnessStartup(
        command=command,
        resolved_path=resolved,
        command_source=source,
        arg_count=len(args),
        args=args,
        configured_command=entry.get("command"),
        configured_args=entry.get("args", []),
        environment=environment,
    )


def env_wrapper_environment(command: str, args: list[str]) -> HarnessEnvironment | None:
    """
    Return the environment changes an ``env`` wrapper launch applies.

    :param command: Configured harness command, e.g. ``"env"`` or ``"claude"``.
    :param args: Arguments passed to *command*, e.g.
        ``["CLAUDE_CONFIG_DIR=/srv/claude", "claude"]``.
    :returns: The wrapper's ``-i``/``-``/``-u``/``-S``/assignment changes, or
        ``None`` when *command* is not a parseable ``env`` wrapper (e.g. it
        uses ``--chdir``).
    """
    try:
        normalized = _normalize_env_wrapper_args(args)
    except ValueError:
        return None
    unwrapped = _unwrap_env(command, normalized, os.defpath)
    return unwrapped[3] if unwrapped is not None else None


def _normalize_env_wrapper_args(args: list[str]) -> list[str]:
    """
    Rewrite env's legacy ``-`` and ``-S`` split strings into the modeled form.

    Split strings use POSIX shell quoting, which matches GNU ``env -S`` when
    the string has no ``${VAR}`` expansion, backslash escape, or ``#`` comment.

    :param args: ``env`` arguments, e.g. ``["-S", "A=1 claude"]``.
    :returns: Equivalent arguments, e.g. ``["A=1", "claude"]``.
    :raises ValueError: If a split string has unbalanced quotes or uses
        syntax that POSIX quoting would read differently from ``env``.
    """
    normalized: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        if arg == "-":
            normalized.append("-i")  # env's legacy spelling of --ignore-environment
        elif arg in ("-S", "--split-string") and index + 1 < len(args):
            normalized.extend(_split_env_string(args[index + 1]))
            index += 1
        elif arg.startswith("--split-string="):
            normalized.extend(_split_env_string(arg.partition("=")[2]))
        elif arg.startswith("-S") and len(arg) > 2:
            normalized.extend(_split_env_string(arg[2:]))
        elif arg in ("-u", "--unset") and index + 1 < len(args):
            normalized.extend(args[index : index + 2])
            index += 1
        elif arg.startswith("-") and arg != "--":
            normalized.append(arg)
        else:
            normalized.extend(args[index:])
            break
        index += 1
    return normalized


def _split_env_string(value: str) -> list[str]:
    """
    Split an ``env -S`` string, rejecting syntax POSIX quoting would misread.

    :param value: Split string, e.g. ``"CLAUDE_CONFIG_DIR=/srv/claude claude"``.
    :returns: The arguments, e.g. ``["CLAUDE_CONFIG_DIR=/srv/claude", "claude"]``.
    :raises ValueError: If *value* uses ``$`` expansion, a backslash escape, a
        ``#`` comment, or unbalanced quotes.
    """
    if "$" in value or "\\" in value or any(word.startswith("#") for word in value.split()):
        raise ValueError(f"unsupported env -S syntax: {value!r}")
    return shlex.split(value)


def _unwrap_env(
    command: str, args: list[str], path: str
) -> tuple[str, list[str], str, HarnessEnvironment] | None:
    """Separate env assignments and options from the wrapped command."""
    if Path(command).name != "env":
        return None
    try:
        options, remaining = getopt.getopt(args, "iu:", ["ignore-environment", "unset="])
    except getopt.GetoptError:
        return None
    if remaining[:1] == ["-"]:
        return None  # env's legacy -i spelling; keep this wrapper opaque.
    environment = HarnessEnvironment(inherit=True, variables={}, unset=[])
    for option, value in options:
        if option in ("-i", "--ignore-environment"):
            environment.inherit = False
        else:
            environment.unset.append(value)
        if option in ("-i", "--ignore-environment") or value == "PATH":
            path = os.defpath
    for index, arg in enumerate(remaining):
        key, separator, value = arg.partition("=")
        if not separator:
            return arg, remaining[index + 1 :], path, environment
        if not key:
            return None
        environment.variables[key] = value
        if key == "PATH":
            path = value
    return None
