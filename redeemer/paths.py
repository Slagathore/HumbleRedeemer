"""Where the app keeps its own data, and why that is not always next to the exe.

Two layouts, because the app ships two ways:

  portable — the bare HumbleRedeemer.exe from the zip, or a source checkout.
             Data stays next to the app, exactly as the README promises: the
             folder you unzipped is the whole app, delete it and it's gone.

  installed — the Inno Setup build. Its program folder is owned by the
             installer, so an update can replace it. Data therefore lives in
             %LOCALAPPDATA%\\HumbleRedeemer instead, where no install or
             uninstall step ever writes, and the first run after installing
             moves any exe-adjacent data there so nothing is left behind.

The installed layout is detected by the uninstaller Inno always drops next to
the program (unins000.exe). Set HUMBLEREDEEMER_DATA to force a location.
"""
import glob
import os
import shutil
import sys

APP_DIR_NAME = "HumbleRedeemer"

# Everything the app writes with a relative path (it chdirs into the data dir
# at startup, so these names are resolved there).
DATA_FILES = (
    "redeemer.db",
    ".humblecookies",
    ".steamcookies",
    ".gogcookies",
    "app.log",
    "redeemed.csv",
    "already_owned.csv",
    "errored.csv",
)
DATA_GLOBS = ("master_*.csv",)

# Inno Setup writes these into the program folder; nothing else does.
_INSTALL_MARKERS = ("unins000.exe", "unins000.dat")


def frozen():
    return bool(getattr(sys, "frozen", False))


def program_dir():
    """The folder the running app lives in (exe dir when frozen, repo root
    when running from source)."""
    if frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_installed(program_directory=None):
    """True when this copy was put here by the Inno Setup installer."""
    directory = program_directory or program_dir()
    return any(os.path.exists(os.path.join(directory, marker))
               for marker in _INSTALL_MARKERS)


def user_data_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, APP_DIR_NAME)


def data_dir(program_directory=None):
    """Where redeemer.db, the cookie files and app.log belong."""
    override = os.environ.get("HUMBLEREDEEMER_DATA")
    if override:
        return os.path.abspath(override)
    directory = program_directory or program_dir()
    if frozen() and is_installed(directory):
        return user_data_dir()
    return directory


def cache_dir():
    """Scratch space for downloaded update installers. Always under the user
    data dir, never inside the program folder."""
    return os.path.join(user_data_dir(), "updates")


def ensure(path):
    os.makedirs(path, exist_ok=True)
    return path


def migrate_from(program_directory, target):
    """Move data an older layout left next to the exe into the data dir.

    Never overwrites a file already in the target (the target is the newer
    truth), never raises: a file we can't move is left where it is and the
    app carries on with whatever it could take.
    """
    moved = []
    if os.path.abspath(program_directory) == os.path.abspath(target):
        return moved
    if not os.path.isdir(program_directory):
        return moved
    ensure(target)
    names = list(DATA_FILES)
    for pattern in DATA_GLOBS:
        names += [os.path.basename(p) for p in
                  glob.glob(os.path.join(program_directory, pattern))]
    for name in names:
        src = os.path.join(program_directory, name)
        dst = os.path.join(target, name)
        if not os.path.exists(src) or os.path.exists(dst):
            continue
        try:
            shutil.move(src, dst)
            moved.append(name)
        except OSError:
            try:
                shutil.copy2(src, dst)
                moved.append(name)
            except OSError:
                pass
    return moved
