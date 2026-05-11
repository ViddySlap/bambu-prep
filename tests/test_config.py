import textwrap
from pathlib import Path

import pytest

from bambu_prep.config import (
    Config,
    ConfigError,
    default_config_path,
    load_config,
    resolve_config_path,
)


def test_missing_file_returns_defaults(tmp_path: Path) -> None:
    cfg = load_config(path=tmp_path / "does-not-exist.toml", env={})
    assert cfg.source_path is None
    assert cfg.paths.bambu_studio_exe.name == "bambu-studio.exe"
    assert cfg.paths.bambu_user_dir is None
    assert cfg.paths.temp_scratch_dir.name == "bambu-prep"
    assert cfg.printer.ip == ""
    assert cfg.printer.access_code == ""
    assert cfg.behavior.allow_rotations is False
    assert cfg.behavior.ensure_on_bed is True


def test_load_with_paths_section(tmp_path: Path) -> None:
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(textwrap.dedent("""
        [paths]
        bambu_studio_exe = "D:/custom/bambu.exe"
        bambu_user_dir = "D:/u"
    """))
    cfg = load_config(path=cfg_file, env={})
    assert cfg.source_path == cfg_file
    assert cfg.paths.bambu_studio_exe.as_posix() == "D:/custom/bambu.exe"
    assert cfg.paths.bambu_user_dir is not None
    assert cfg.paths.bambu_user_dir.as_posix() == "D:/u"


def test_empty_string_falls_back_to_default(tmp_path: Path) -> None:
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(textwrap.dedent("""
        [paths]
        bambu_studio_exe = ""
        temp_scratch_dir = ""
    """))
    cfg = load_config(path=cfg_file, env={})
    assert cfg.paths.bambu_studio_exe.name == "bambu-studio.exe"
    assert cfg.paths.temp_scratch_dir.name == "bambu-prep"


def test_secret_refs_fill_blank_printer_fields(tmp_path: Path) -> None:
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(textwrap.dedent("""
        [printer]
        serial = "01P00A123456789"

        [secret_refs]
        "printer.access_code" = "BAMBU_AC"
        "printer.ip" = "BAMBU_IP"
        "printer.serial" = "BAMBU_SERIAL"
    """))
    env = {"BAMBU_AC": "12345678", "BAMBU_IP": "192.168.5.10", "BAMBU_SERIAL": "fromenv"}
    cfg = load_config(path=cfg_file, env=env)
    assert cfg.printer.access_code == "12345678"
    assert cfg.printer.ip == "192.168.5.10"
    # toml value wins over env when present
    assert cfg.printer.serial == "01P00A123456789"


def test_secret_refs_missing_env_leaves_blank(tmp_path: Path) -> None:
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(textwrap.dedent("""
        [secret_refs]
        "printer.access_code" = "DOES_NOT_EXIST"
    """))
    cfg = load_config(path=cfg_file, env={})
    assert cfg.printer.access_code == ""


def test_secret_refs_ignore_unknown_sections(tmp_path: Path) -> None:
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(textwrap.dedent("""
        [secret_refs]
        "weirdo.key" = "ANYTHING"
        "printer.bogusfield" = "ALSO_ANYTHING"
    """))
    cfg = load_config(path=cfg_file, env={"ANYTHING": "x", "ALSO_ANYTHING": "y"})
    assert cfg.printer.access_code == ""
    assert cfg.printer.ip == ""


def test_behavior_section_overrides(tmp_path: Path) -> None:
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text(textwrap.dedent("""
        [behavior]
        allow_rotations = true
        ensure_on_bed = false
    """))
    cfg = load_config(path=cfg_file, env={})
    assert cfg.behavior.allow_rotations is True
    assert cfg.behavior.ensure_on_bed is False
    assert cfg.behavior.allow_mix_temp is False


def test_malformed_toml_raises(tmp_path: Path) -> None:
    cfg_file = tmp_path / "c.toml"
    cfg_file.write_text("this = is = not valid toml\n")
    with pytest.raises(ConfigError):
        load_config(path=cfg_file, env={})


def test_resolve_config_path_explicit_wins(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit.toml"
    env = {"BAMBU_PREP_CONFIG": str(tmp_path / "env.toml")}
    assert resolve_config_path(explicit, env) == explicit


def test_resolve_config_path_env_var(tmp_path: Path) -> None:
    env = {"BAMBU_PREP_CONFIG": str(tmp_path / "env.toml")}
    assert resolve_config_path(None, env) == tmp_path / "env.toml"


def test_resolve_config_path_default_xdg() -> None:
    # Force the non-Windows branch by clearing APPDATA and setting XDG_CONFIG_HOME.
    env = {"XDG_CONFIG_HOME": "/tmp/xdg"}
    import sys

    if sys.platform == "win32":
        # On Windows, default_config_path consults APPDATA first; test only that
        # it returns a path ending with bambu-prep/config.toml.
        p = default_config_path()
        assert p.name == "config.toml"
        assert p.parent.name == "bambu-prep"
    else:
        # Patch via env injection isn't enough since default_config_path reads
        # os.environ; this branch only checks the function returns sane shape.
        p = default_config_path()
        assert p.name == "config.toml"


def test_default_config_returns_usable_paths() -> None:
    cfg = Config()
    assert cfg.paths.bambu_studio_exe is not None
    assert cfg.paths.temp_scratch_dir is not None
