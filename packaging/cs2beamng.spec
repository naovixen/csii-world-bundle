# PyInstaller spec — the standalone application. Built by `python tools/build_exe.py`.
#
# TWO executables out of ONE analysis, sharing one folder of libraries:
#
#   cs2beamng.exe      windowed - what a double-click opens
#   cs2beamng-cli.exe  console  - `cs2beamng-cli build <bundle> --out ...`
#
# Why two: a windowed build on Windows has no console at all, so the headless `build` command
# would print nothing and read nothing; a console build pops a black window behind the UI. They
# share every byte of the library folder, so the second exe costs about a megabyte.
#
# ONEDIR, not onefile. Onefile re-extracts the whole ~120 MiB payload to a temp folder on every
# launch - and this application spawns worker PROCESSES, each of which re-executes the exe.

import os

# `.spec` files are exec'd with SPECPATH set to the folder holding this file
ROOT = os.path.dirname(SPECPATH)  # noqa: F821 - injected by PyInstaller
APPS = os.path.join(ROOT, "apps")
TOOLS = os.path.join(ROOT, "tools")

# Modules reached by name at runtime, which the import graph therefore does not see:
#   stages/__init__.py `__import__(f"{__name__}.{name}")` over the stage list
#   csiibundle/bake/__init__.py `importlib.import_module` over PRODUCT_MODULES
HIDDEN = [
    "cs2beamng.stages." + name for name in (
        "bake", "terrain", "water", "scene", "shapes", "networks", "derived", "instances",
        "lights", "navgraph", "poi", "groundcover", "materials", "cook", "validate")
] + [
    "csiibundle.bake." + name for name in (
        "materials", "lods", "markings", "lanegeometry", "areas", "skirts", "terrainmask",
        "terraincut", "ground", "roads", "tiles", "manifest", "runner", "locate", "progress")
] + [
    # the optional accelerators, bundled so the good path is the default path
    "etcpak", "pyfqmr",
    # pywebview picks its backend at runtime
    "webview", "webview.platforms.winforms", "webview.platforms.edgechromium",
    "clr_loader", "pythonnet",
    "PIL.Image", "PIL.ImagePalette",
    # cKDTree: the navgraph, the POI snap and map_lua's node merge
    "scipy.spatial", "scipy.spatial.cKDTree",
    # multiprocessing's spawn bootstrap
    "multiprocessing.spawn", "multiprocessing.popen_spawn_win32", "multiprocessing.pool",
]

from PyInstaller.utils.hooks import collect_data_files

DATAS = [
    (os.path.join(APPS, "cs2beamng", "ui"), os.path.join("cs2beamng", "ui")),
]
# `etcpak` imports `archspec` to pick its SIMD path, and archspec reads its CPU database out of
# `archspec/json/`. Without the data file, importing etcpak raises FileNotFoundError - and the
# frozen build would silently fall back to PNG textures, i.e. BeamNG cooking every one of them
# on first load. Found by running the frozen exe, not by building it.
DATAS += collect_data_files("archspec")
# The ripper mod, when the release build put it there: `mod/CartoRipper-<version>.zip` beside the
# exe is what lets the app install the producer for the user (one download instead of two).
MOD_DIR = os.path.join(ROOT, "packaging", "mod")
if os.path.isdir(MOD_DIR) and any(f.endswith(".zip") for f in os.listdir(MOD_DIR)):
    DATAS.append((MOD_DIR, "mod"))

# Trim only what nothing imports. NOT `unittest`: numpy.testing imports it, scipy imports
# numpy.testing, and the navgraph imports scipy - excluding it cost the navgraph stage in the
# first frozen build. Every entry here was checked against a real frozen build, not guessed.
EXCLUDES = ["tkinter", "matplotlib", "pytest", "IPython", "notebook", "pandas",
            "PyQt5", "PyQt6", "PySide2", "PySide6"]

analysis = Analysis(  # noqa: F821
    [os.path.join(SPECPATH, "cs2beamng_launcher.py")],  # noqa: F821
    pathex=[APPS, TOOLS],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN,
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(analysis.pure)  # noqa: F821

ICON = os.path.join(SPECPATH, "cs2beamng.ico")  # noqa: F821
icon = ICON if os.path.isfile(ICON) else None

gui = EXE(  # noqa: F821
    pyz, analysis.scripts, [],
    exclude_binaries=True,
    name="cs2beamng",
    console=False,
    icon=icon,
)
cli = EXE(  # noqa: F821
    pyz, analysis.scripts, [],
    exclude_binaries=True,
    name="cs2beamng-cli",
    console=True,
    icon=icon,
)
COLLECT(  # noqa: F821
    gui, cli, analysis.binaries, analysis.datas,
    strip=False,
    upx=False,             # UPX-packed binaries are a reliable way to be called a virus
    name="cs2beamng",
)
