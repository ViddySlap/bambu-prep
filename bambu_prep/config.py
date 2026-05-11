"""Config loading for bambu-prep.

The config file is a TOML document. Resolution order for the path:

1. Explicit argument to ``load_config(path=...)``
2. ``BAMBU_PREP_CONFIG`` env var
3. ``%APPDATA%/bambu-prep/config.toml`` on Windows
4. ``$XDG_CONFIG_HOME/bambu-prep/config.toml`` (or ``~/.config/bambu-prep/config.toml``)

A missing config file is not an error — the defaults are usable as-is on a
standard Windows + Bambu Studio install. Secrets (printer IP, access code,
serial) are pulled in from env vars via the ``[secret_refs]`` table so the
committed config never carries credential values.
"""

from __future__ import annotations

import os
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path


DEFAULT_BAMBU_STUDIO_EXE = Path("C:/Program Files/Bambu Studio/bambu-studio.exe")
DEFAULT_BAMBU_RESOURCES_DIR = Path("C:/Program Files/Bambu Studio/resources")


@dataclass(frozen=True)
class Paths:
    bambu_studio_exe: Path = DEFAULT_BAMBU_STUDIO_EXE
    bambu_resources_dir: Path = DEFAULT_BAMBU_RESOURCES_DIR
    bambu_user_dir: Path | None = None
    temp_scratch_dir: Path = field(
        default_factory=lambda: Path(tempfile.gettempdir()) / "bambu-prep"
    )


@dataclass(frozen=True)
class Printer:
    ip: str = ""
    access_code: str = ""
    serial: str = ""


@dataclass(frozen=True)
class Behavior:
    allow_rotations: bool = False
    ensure_on_bed: bool = True
    allow_mix_temp: bool = False


@dataclass(frozen=True)
class Config:
    paths: Paths = field(default_factory=Paths)
    printer: Printer = field(default_factory=Printer)
    behavior: Behavior = field(default_factory=Behavior)
    source_path: Path | None = None


class ConfigError(ValueError):
    """Raised when the config file is present but malformed."""


def default_config_path() -> Path:
    """Return the platform-appropriate default config path.

    The returned path may not exist.
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "bambu-prep" / "config.toml"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "bambu-prep" / "config.toml"
    return Path.home() / ".config" / "bambu-prep" / "config.toml"


def resolve_config_path(explicit: Path | None = None, env: dict[str, str] | None = None) -> Path:
    env = env if env is not None else os.environ
    if explicit is not None:
        return Path(explicit)
    env_val = env.get("BAMBU_PREP_CONFIG")
    if env_val:
        return Path(env_val)
    return default_config_path()


def load_config(
    path: Path | None = None,
    env: dict[str, str] | None = None,
) -> Config:
    """Load and resolve a Config, or return defaults if no file is present."""
    env = env if env is not None else os.environ
    resolved_path = resolve_config_path(path, env)
    if not resolved_path.is_file():
        return Config(source_path=None)

    try:
        with resolved_path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"failed to parse {resolved_path}: {e}") from e

    return _build_config(raw, source_path=resolved_path, env=env)


def _build_config(raw: dict, *, source_path: Path | None, env: dict[str, str]) -> Config:
    paths = _build_paths(raw.get("paths", {}))
    printer = _build_printer(raw.get("printer", {}))
    behavior = _build_behavior(raw.get("behavior", {}))

    secret_refs = raw.get("secret_refs", {})
    if secret_refs:
        printer = _apply_secret_refs(printer, secret_refs, env)

    return Config(paths=paths, printer=printer, behavior=behavior, source_path=source_path)


def _build_paths(section: dict) -> Paths:
    defaults = Paths()
    return Paths(
        bambu_studio_exe=_path_or(section.get("bambu_studio_exe"), defaults.bambu_studio_exe),
        bambu_resources_dir=_path_or(
            section.get("bambu_resources_dir"), defaults.bambu_resources_dir
        ),
        bambu_user_dir=_optional_path(section.get("bambu_user_dir")),
        temp_scratch_dir=_path_or(section.get("temp_scratch_dir"), defaults.temp_scratch_dir),
    )


def _build_printer(section: dict) -> Printer:
    return Printer(
        ip=str(section.get("ip", "")),
        access_code=str(section.get("access_code", "")),
        serial=str(section.get("serial", "")),
    )


def _build_behavior(section: dict) -> Behavior:
    defaults = Behavior()
    return Behavior(
        allow_rotations=bool(section.get("allow_rotations", defaults.allow_rotations)),
        ensure_on_bed=bool(section.get("ensure_on_bed", defaults.ensure_on_bed)),
        allow_mix_temp=bool(section.get("allow_mix_temp", defaults.allow_mix_temp)),
    )


def _apply_secret_refs(printer: Printer, refs: dict, env: dict[str, str]) -> Printer:
    """For each "section.key" -> ENV_VAR mapping, fill the printer field from env if blank."""
    overrides: dict[str, str] = {}
    for dotted_key, env_var in refs.items():
        if not isinstance(env_var, str):
            continue
        section, _, key = dotted_key.partition(".")
        if section != "printer" or not key:
            continue
        if getattr(printer, key, None) != "":
            continue  # explicit toml value wins
        value = env.get(env_var, "")
        if value:
            overrides[key] = value
    return replace(printer, **overrides) if overrides else printer


def _path_or(value, default: Path) -> Path:
    if value in (None, ""):
        return default
    return Path(value)


def _optional_path(value) -> Path | None:
    if value in (None, ""):
        return None
    return Path(value)
