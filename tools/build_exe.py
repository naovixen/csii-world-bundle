"""Build the standalone application: `python tools/build_exe.py [--mod <CartoRipper.zip>]`.

Produces `dist/cs2beamng/` (a folder with `cs2beamng.exe`, `cs2beamng-cli.exe` and their
libraries) and zips it as `dist/cs2beamng-<version>-<platform>.zip` with a `.sha256` beside it —
the two assets a release attaches.

`--mod` copies the ripper mod's zip into the build so the app can install the producer itself
(`cs2beamng.modinstall`), which is what makes it ONE download rather than two. Without it the app
still builds and simply offers the download link instead.

Needs `pip install -e .[build]` (PyInstaller). Cross-building is not a thing: run this on the OS
you are shipping for.
"""

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(ROOT, "packaging", "cs2beamng.spec")
MOD_DIR = os.path.join(ROOT, "packaging", "mod")
DIST = os.path.join(ROOT, "dist")
BUILD = os.path.join(ROOT, "build")


def version():
    sys.path.insert(0, os.path.join(ROOT, "apps"))
    from cs2beamng import __version__

    return __version__


def platform_tag():
    system = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}.get(platform.system(), platform.system().lower())
    machine = {"AMD64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine(), platform.machine().lower())
    return f"{system}-{machine}"


def stage_mod(path):
    """Put the mod zip (and its checksum, if there is one) where the spec looks for it."""
    if os.path.isdir(MOD_DIR):
        shutil.rmtree(MOD_DIR)
    if not path:
        return None
    os.makedirs(MOD_DIR, exist_ok=True)
    shutil.copy2(path, os.path.join(MOD_DIR, os.path.basename(path)))
    if os.path.isfile(path + ".sha256"):
        shutil.copy2(path + ".sha256", os.path.join(MOD_DIR, os.path.basename(path) + ".sha256"))
    return os.path.basename(path)


def run_pyinstaller(clean):
    args = [sys.executable, "-m", "PyInstaller", SPEC, "--distpath", DIST, "--workpath", BUILD,
            "--noconfirm"]
    if clean:
        args.append("--clean")
    print("$ " + " ".join(args), flush=True)
    return subprocess.call(args, cwd=ROOT)


def zip_dist(folder, out_path):
    """Zip with the folder itself as the top entry, so unzipping makes one tidy directory."""
    base = os.path.dirname(folder)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for root, _dirs, files in os.walk(folder):
            for name in files:
                full = os.path.join(root, name)
                archive.write(full, os.path.relpath(full, base))
    digest = hashlib.sha256()
    with open(out_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    with open(out_path + ".sha256", "w", encoding="utf-8", newline="\n") as handle:
        handle.write(f"{digest.hexdigest()}  {os.path.basename(out_path)}\n")
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="build_exe", description=__doc__.split("\n\n")[0])
    parser.add_argument("--mod", default=None, help="ripper mod zip to ship inside the app")
    parser.add_argument("--no-clean", action="store_true", help="reuse PyInstaller's work folder")
    parser.add_argument("--no-zip", action="store_true", help="leave dist/cs2beamng/ unzipped")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is not installed: pip install -e .[build]", file=sys.stderr)
        return 2

    if args.mod and not os.path.isfile(args.mod):
        print(f"no such mod zip: {args.mod}", file=sys.stderr)
        return 2
    staged = stage_mod(args.mod)
    print(f"mod: {staged or 'none - the app will offer the download link instead'}")

    code = run_pyinstaller(not args.no_clean)
    if code != 0:
        return code
    folder = os.path.join(DIST, "cs2beamng")
    if not os.path.isdir(folder):
        print(f"PyInstaller produced no {folder}", file=sys.stderr)
        return 1

    size = sum(os.path.getsize(os.path.join(r, f))
               for r, _d, fs in os.walk(folder) for f in fs)
    exe = os.path.join(folder, "cs2beamng" + (".exe" if sys.platform.startswith("win") else ""))
    print(f"built {folder}  ({size / 1048576:.0f} MiB)")
    # PyInstaller leaves an IDENTICALLY NAMED bootloader stub in its work directory, and that
    # one is not runnable: `_internal/` is only ever assembled in the dist folder, so running it
    # gets "Failed to load Python DLL .../build/cs2beamng/_internal/python313.dll". Print the
    # one to run, and name the other, or the two folders are told apart by trial and error.
    print(f"run it: {exe}")
    print(f"        (not the one in {BUILD}{os.sep}cs2beamng - that is PyInstaller's scratch "
          f"copy with no _internal beside it, and it fails with 'Failed to load Python DLL')")
    if args.no_zip:
        return 0
    out = os.path.join(DIST, f"cs2beamng-{version()}-{platform_tag()}.zip")
    digest = zip_dist(folder, out)
    print(f"{out}  ({os.path.getsize(out) / 1048576:.0f} MiB)\nsha256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
