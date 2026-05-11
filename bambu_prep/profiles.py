"""Bambu Studio preset profile discovery and name resolution.

A "preset" is one of three kinds of JSON files Bambu Studio ships:

- ``machine``  — printer hardware (``Bambu Lab A1 0.4 nozzle``)
- ``process``  — slicing parameters (``0.20mm Standard @BBL A1``)
- ``filament`` — material parameters (``Bambu PLA Matte @BBL A1``)

Two layered sources:

1. Vendor (shipped with Bambu Studio) at
   ``<bambu_resources_dir>/profiles/BBL/{kind}/*.json``. Always present.
2. User (saved customizations) at ``<bambu_user_dir>/{kind}/*.json``. Optional.

When a name collides between user and vendor, the user copy wins — mirroring
how Bambu Studio itself inherits.

Filenames mirror preset names: ``{preset_name}.json``. We resolve by reading
the file's ``name`` field, so a future filename-vs-name divergence remains
correct, but filename lookup is the fast path.

The resources/profiles tree contains plenty of non-preset files (cover PNGs,
``*template*.json`` g-code snippets, ``@base`` parent definitions). Resolution
filters them out by requiring the JSON's ``type`` to match the requested kind
and ``instantiation`` to be ``"true"``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from bambu_prep.config import Config

ProfileKind = Literal["machine", "process", "filament"]
_KINDS: tuple[ProfileKind, ...] = ("machine", "process", "filament")


class ProfileError(LookupError):
    """Raised when a preset can't be resolved or doesn't validate."""


@dataclass(frozen=True)
class Profile:
    kind: ProfileKind
    name: str
    path: Path
    source: Literal["user", "vendor"]


def _vendor_dir(config: Config, kind: ProfileKind) -> Path:
    return config.paths.bambu_resources_dir / "profiles" / "BBL" / kind


def _user_dir(config: Config, kind: ProfileKind) -> Path | None:
    base = config.paths.bambu_user_dir
    if base is None:
        return None
    return base / kind


def _looks_like_preset_filename(path: Path) -> bool:
    """Skip obvious non-preset files (templates, base definitions, hidden)."""
    name = path.name
    if name.startswith("."):
        return False
    if "template" in name.lower():
        return False
    if "@base" in name:
        return False
    return True


def _candidate_paths(directory: Path) -> Iterable[Path]:
    if not directory.is_dir():
        return ()
    return (p for p in directory.iterdir() if p.suffix == ".json" and _looks_like_preset_filename(p))


def _load_if_preset(path: Path, kind: ProfileKind) -> str | None:
    """Return the preset's ``name`` if the file is a real instantiable preset of ``kind``."""
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("type") != kind:
        return None
    if str(data.get("instantiation", "")).lower() != "true":
        return None
    name = data.get("name")
    if not isinstance(name, str) or not name:
        return None
    return name


def list_profiles(config: Config, kind: ProfileKind) -> list[Profile]:
    """List all instantiable presets of ``kind``, user entries layered over vendor."""
    if kind not in _KINDS:
        raise ValueError(f"unknown profile kind: {kind!r}")

    by_name: dict[str, Profile] = {}

    for path in _candidate_paths(_vendor_dir(config, kind)):
        name = _load_if_preset(path, kind)
        if name is not None:
            by_name[name] = Profile(kind=kind, name=name, path=path, source="vendor")

    user_dir = _user_dir(config, kind)
    if user_dir is not None:
        for path in _candidate_paths(user_dir):
            name = _load_if_preset(path, kind)
            if name is not None:
                by_name[name] = Profile(kind=kind, name=name, path=path, source="user")

    return sorted(by_name.values(), key=lambda p: p.name)


def resolve(config: Config, kind: ProfileKind, name: str) -> Profile:
    """Resolve a preset name to its full Profile record. Raise ProfileError on miss."""
    if kind not in _KINDS:
        raise ValueError(f"unknown profile kind: {kind!r}")

    user_dir = _user_dir(config, kind)
    if user_dir is not None:
        hit = _try_resolve_in(user_dir, kind, name, source="user")
        if hit is not None:
            return hit

    hit = _try_resolve_in(_vendor_dir(config, kind), kind, name, source="vendor")
    if hit is not None:
        return hit

    raise ProfileError(f"no {kind} preset named {name!r} in vendor or user profiles")


def _try_resolve_in(
    directory: Path,
    kind: ProfileKind,
    name: str,
    source: Literal["user", "vendor"],
) -> Profile | None:
    """Fast path: try ``{directory}/{name}.json`` first; fall back to a directory scan."""
    if not directory.is_dir():
        return None

    fast_path = directory / f"{name}.json"
    if fast_path.is_file():
        loaded_name = _load_if_preset(fast_path, kind)
        if loaded_name == name:
            return Profile(kind=kind, name=name, path=fast_path, source=source)

    for candidate in _candidate_paths(directory):
        loaded_name = _load_if_preset(candidate, kind)
        if loaded_name == name:
            return Profile(kind=kind, name=name, path=candidate, source=source)

    return None
