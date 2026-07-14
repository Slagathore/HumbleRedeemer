"""Where user data lives, and why an update can't take it with it.

The installer owns the program folder: it replaces HumbleRedeemer.exe there
on every update. So the one thing that must never be true is "the database,
the cookies or the logs live in the program folder". These tests pin that
down for the installed layout, keep the portable layout portable, and read
packaging/installer.iss to check the installer really only writes the exe.
"""
import os

import pytest

from redeemer import paths


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("HUMBLEREDEEMER_DATA", raising=False)


def _installed(tmp_path, monkeypatch):
    """A program folder that looks like an Inno Setup install."""
    program = tmp_path / "Programs" / "HumbleRedeemer"
    program.mkdir(parents=True)
    (program / "HumbleRedeemer.exe").write_text("exe")
    (program / "unins000.exe").write_text("uninstaller")  # Inno's fingerprint
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable",
                        str(program / "HumbleRedeemer.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    return program


def _portable(tmp_path, monkeypatch):
    program = tmp_path / "unzipped"
    program.mkdir()
    (program / "HumbleRedeemer.exe").write_text("exe")
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    monkeypatch.setattr(paths.sys, "executable",
                        str(program / "HumbleRedeemer.exe"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    return program


def test_installed_data_dir_is_outside_the_program_folder(tmp_path, monkeypatch):
    program = _installed(tmp_path, monkeypatch)
    data = paths.data_dir()

    assert paths.is_installed(str(program))
    assert os.path.normcase(data) != os.path.normcase(str(program))
    # the check that matters: nothing the installer overwrites can reach it
    assert not os.path.normcase(data).startswith(
        os.path.normcase(str(program)) + os.sep)
    assert data == os.path.join(str(tmp_path / "AppData" / "Local"),
                                "HumbleRedeemer")


def test_installed_update_cache_is_outside_the_program_folder(tmp_path, monkeypatch):
    program = _installed(tmp_path, monkeypatch)
    assert not os.path.normcase(paths.cache_dir()).startswith(
        os.path.normcase(str(program)) + os.sep)


def test_portable_build_keeps_its_data_next_to_the_exe(tmp_path, monkeypatch):
    program = _portable(tmp_path, monkeypatch)
    assert not paths.is_installed(str(program))
    assert paths.data_dir() == str(program)


def test_source_run_uses_the_repo(monkeypatch):
    monkeypatch.setattr(paths.sys, "frozen", False, raising=False)
    root = os.path.dirname(os.path.dirname(os.path.abspath(paths.__file__)))
    assert paths.program_dir() == root
    assert paths.data_dir() == root


def test_env_override_wins(tmp_path, monkeypatch):
    _installed(tmp_path, monkeypatch)
    monkeypatch.setenv("HUMBLEREDEEMER_DATA", str(tmp_path / "elsewhere"))
    assert paths.data_dir() == str(tmp_path / "elsewhere")


def test_migration_moves_old_exe_adjacent_data(tmp_path, monkeypatch):
    program = _installed(tmp_path, monkeypatch)
    (program / "redeemer.db").write_text("old db")
    (program / ".humblecookies").write_text("[]")
    (program / "master_humble_keys_2026.csv").write_text("id,name\n")
    target = tmp_path / "data"

    moved = paths.migrate_from(str(program), str(target))

    assert set(moved) >= {"redeemer.db", ".humblecookies",
                          "master_humble_keys_2026.csv"}
    assert (target / "redeemer.db").read_text() == "old db"
    assert not (program / "redeemer.db").exists()
    # the exe itself is never data
    assert (program / "HumbleRedeemer.exe").exists()


def test_migration_never_overwrites_newer_data(tmp_path, monkeypatch):
    program = _installed(tmp_path, monkeypatch)
    (program / "redeemer.db").write_text("stale copy left by an old build")
    target = tmp_path / "data"
    target.mkdir()
    (target / "redeemer.db").write_text("the real one")

    paths.migrate_from(str(program), str(target))

    assert (target / "redeemer.db").read_text() == "the real one"


def test_installer_only_writes_the_exe_into_the_program_folder():
    """Read the actual Inno script: if a [Files] entry ever starts shipping
    something into {app} that the app also writes, an update would clobber
    it. Nothing but the exe is allowed there."""
    iss = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(paths.__file__))),
        "packaging", "installer.iss")
    with open(iss, encoding="utf-8") as f:
        lines = f.read().splitlines()

    section, files = "", []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped.lower()
            continue
        if section == "[files]" and stripped and not stripped.startswith(";"):
            files.append(stripped)

    assert files, "installer.iss has no [Files] section any more"
    for entry in files:
        assert "DestDir: \"{app}\"" in entry
        assert "HumbleRedeemer.exe" in entry
        for data_file in paths.DATA_FILES:
            assert data_file not in entry, (
                f"installer.iss ships {data_file} into the program folder; "
                "an update would overwrite the user's own copy")
