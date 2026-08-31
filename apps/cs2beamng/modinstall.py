"""Where Cities: Skylines II keeps its local mods, and how the ripper gets there.

The app already knows the CS2 user folder — `csiibundle.bake.locate` walks it to find the
bundle, because the bundle is written into `<user folder>/ModsData/Carto/`. The mods live one
level up in `<user folder>/Mods/<name>/`, which is what the modding toolchain deploys to and
what the game scans at startup.

So the app can install the producer for the user: `cs2beamng.exe` ships the mod zip beside
itself (`mod/CartoRipper-*.zip`, put there by the release build), and `install()` verifies it
and unpacks it into that folder. One download instead of two, and the mod and the app that
reads its bundle are then a matched pair by construction.

Nothing here reaches the network. The zip is a file on disk that shipped with the app; if it is
absent — a source checkout, or a wheel install — `available()` is False and the UI offers the
download link instead.
"""

import hashlib
import os
import shutil
import sys
import zipfile

MOD_FOLDER_NAME = "Carto"
# What a complete install looks like. The three `Carto_<platform>_*` files are the Burst-compiled
# jobs; a "mod" that is only `Carto.dll` runs every collector off Burst and is not the mod.
REQUIRED_FILES = ("Carto.dll", "Carto_win_x86_64.dll")


def app_root():
    """The folder the application is running out of — the exe's folder when frozen."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def cs2_user_dir():
    """The Cities: Skylines II user folder on this machine, or None.

    The Windows path is the real one; under Proton the same tree lives inside the prefix, and
    the bundle locator already enumerates those prefixes, so the mods folder is derived from
    whichever candidate actually exists rather than guessed a second time.
    """
    from csiibundle.bake.locate import candidate_bundle_dirs

    for bundle_dir in candidate_bundle_dirs():
        # <user>/ModsData/Carto  ->  <user>
        user = os.path.dirname(os.path.dirname(os.path.abspath(bundle_dir)))
        if os.path.isdir(user):
            return user
    return None


def mods_dir():
    """`<CS2 user folder>/Mods`, or None when CS2 was never run on this machine."""
    user = cs2_user_dir()
    return os.path.join(user, "Mods") if user else None


def install_dir():
    """Where the ripper goes: `<CS2 user folder>/Mods/Carto`, or None."""
    mods = mods_dir()
    return os.path.join(mods, MOD_FOLDER_NAME) if mods else None


def installed_version():
    """The version of the ripper already installed, `"unknown"` if it is there without a
    version stamp, or None if it is not installed."""
    folder = install_dir()
    if not folder or not os.path.isfile(os.path.join(folder, "Carto.dll")):
        return None
    stamp = os.path.join(folder, "cs2beamng_mod_version.txt")
    if os.path.isfile(stamp):
        try:
            with open(stamp, encoding="utf-8") as handle:
                return handle.read().strip() or "unknown"
        except OSError:
            pass
    return "unknown"


def bundled_zip():
    """The mod zip shipped beside the application, or None (a source checkout has none)."""
    folder = os.path.join(app_root(), "mod")
    if not os.path.isdir(folder):
        return None
    zips = sorted(f for f in os.listdir(folder) if f.lower().endswith(".zip"))
    return os.path.join(folder, zips[-1]) if zips else None


def bundled_version():
    """The version stamped into the bundled zip's name, e.g. `CartoRipper-1.0.16-…zip` -> 1.0.16."""
    path = bundled_zip()
    if not path:
        return None
    parts = os.path.basename(path).split("-")
    return parts[1] if len(parts) > 1 else "unknown"


def available():
    """True when this build can install the mod without a download."""
    return bundled_zip() is not None and mods_dir() is not None


def _verify(path):
    """The zip must contain the mod folder with at least its DLL and its Burst library, and its
    sha256 must match the `.sha256` beside it when the release build wrote one."""
    digest_file = path + ".sha256"
    if os.path.isfile(digest_file):
        want = ""
        with open(digest_file, encoding="utf-8") as handle:
            want = handle.read().split()[0].strip().lower()
        got = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                got.update(chunk)
        if got.hexdigest() != want:
            raise ValueError(f"{os.path.basename(path)} does not match its sha256 - re-download it")
    with zipfile.ZipFile(path) as archive:
        names = {n.replace("\\", "/") for n in archive.namelist()}
        for required in REQUIRED_FILES:
            if f"{MOD_FOLDER_NAME}/{required}" not in names:
                raise ValueError(
                    f"{os.path.basename(path)} has no {MOD_FOLDER_NAME}/{required} in it"
                )


def member_dest(mods_dir, rel):
    """Where an archive member lands - inside `mods_dir`, or a `ValueError`. `Carto/../../x`
    passes the folder-prefix test the caller makes, and a user-supplied archive carries no sha
    to vouch for it (validation pass 2026-08-31, B13)."""
    root = os.path.abspath(mods_dir)
    dest = os.path.abspath(os.path.join(root, *rel.split("/")))
    if os.path.commonpath([root, dest]) != root:
        raise ValueError(f"mod archive member escapes the mods folder: {rel}")
    return dest


def install(zip_path=None, version=None):
    """Unpack the mod into the CS2 mods folder. Returns the folder it wrote.

    Replaces the folder's contents rather than merging into them: a stale Burst library from an
    older build beside a new `Carto.dll` is the one failure mode that produces a mod which loads
    and then behaves like neither version.
    """
    path = zip_path or bundled_zip()
    if not path:
        raise FileNotFoundError(
            "no mod zip shipped with this build - download it from the "
            "releases page and pass it here"
        )
    target = install_dir()
    if not target:
        raise FileNotFoundError(
            "no Cities: Skylines II user folder on this machine - run the game once, then try again"
        )
    _verify(path)
    if os.path.isdir(target):
        shutil.rmtree(target)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with zipfile.ZipFile(path) as archive:
        for member in archive.namelist():
            rel = member.replace("\\", "/")
            if not rel.startswith(MOD_FOLDER_NAME + "/") or rel.endswith("/"):
                continue
            dest = member_dest(os.path.dirname(target), rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with archive.open(member) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
    stamp = version or bundled_version() or "unknown"
    with open(
        os.path.join(target, "cs2beamng_mod_version.txt"), "w", encoding="utf-8", newline="\n"
    ) as handle:
        handle.write(stamp + "\n")
    return target
