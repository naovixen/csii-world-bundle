"""`cs2beamng doctor` — what this machine offers, what is missing, and the exact fix for each.

The app detects everything it needs (the bundle, the BeamNG user folder, the executable, the
card and the render size) and until now said so only inside the UI. `doctor` is the same
detection printed, so a person on the command line — or one reporting a problem — can see in one
screen why a build would not start, or why it would be slower or uglier than it should be.

Exit code 0 when a build could run right now, 1 when something required is missing.
"""

import json
import os
import sys

from . import __version__
from . import settings as settings_mod

OK, WARN, BAD = "ok", "warn", "missing"
MARK = {OK: "  OK  ", WARN: " NOTE ", BAD: " MISS "}


def collect():
    """Every fact `report` prints, as plain data (also what `--json` emits)."""
    values = settings_mod.load()
    detected = settings_mod.detected(values)
    out = {
        "version": __version__,
        "python": sys.version.split()[0],
        "frozen": bool(getattr(sys, "frozen", False)),
        "platform": sys.platform,
        "appDir": detected.get("appDir"),
        "checks": [],
    }

    def add(name, status, detail, fix=None):
        out["checks"].append({"name": name, "status": status, "detail": detail, "fix": fix})

    # -- the bundle ----------------------------------------------------------------------------
    bundle = values.get("bundle")
    manifest = os.path.join(bundle or "", "bundle.json")
    if bundle and os.path.isfile(manifest):
        info = {}
        try:
            with open(manifest, encoding="utf-8") as handle:
                info = json.load(handle)
        except (OSError, ValueError):
            pass
        city = info.get("city") or "?"
        when = (info.get("generatedAt") or "")[:16].replace("T", " ")
        add(
            "CS2 bundle",
            OK,
            f"{bundle}\n         {city}, ripped {when}, schema {info.get('schema', '?')}",
        )
    else:
        fix = "In CS2: Options -> Carto -> Ripper -> Rip World."
        from . import modinstall

        if modinstall.installed_version() is None and modinstall.mods_dir():
            # only the packaged app ships the zip, so only it can offer the button; anywhere
            # else this must say the thing the reader can actually do
            how = (
                "Run the app and press 'Install the ripper mod', or unzip it into "
                if modinstall.available()
                else "Download CartoRipper-*.zip from the releases page and unzip it into "
            )
            fix = "The ripper mod is not installed. " + how + str(modinstall.install_dir()) + "."
        # `bundle.json` is the manifest and the ripper writes it LAST, so a folder full of layer
        # files without one is a rip that is still running or was interrupted - which is a
        # different thing to say than "you have never ripped".
        partial = (
            bundle
            and os.path.isdir(bundle)
            and any(
                os.path.isfile(os.path.join(bundle, name))
                for name in ("instances.jsonl", "networks.jsonl", "terrain.json", "materials.json")
            )
        )
        if partial:
            add(
                "CS2 bundle",
                BAD,
                f"{bundle}\n         layers are there but bundle.json is not - "
                f"a rip is still running, or one was interrupted",
                "let the rip finish (the manifest is written last), then run this again",
            )
        else:
            add("CS2 bundle", BAD, f"none at {bundle or '(nowhere to look)'}", fix)
    others = [p for p in detected.get("bundles", []) if p != bundle]
    if others:
        add("other bundles", WARN, "; ".join(others), "pick one in the app, or pass it to `build`")

    # -- the ripper mod ------------------------------------------------------------------------
    from . import modinstall

    installed = modinstall.installed_version()
    if installed:
        add("ripper mod", OK, f"{modinstall.install_dir()} (version {installed})")
    elif modinstall.mods_dir():
        add(
            "ripper mod",
            WARN if bundle and os.path.isfile(manifest) else BAD,
            f"not installed in {modinstall.mods_dir()}",
            "run the app and press 'Install the ripper mod'"
            if modinstall.available()
            else "download CartoRipper-*.zip from the releases page and unzip it there",
        )
    else:
        add(
            "ripper mod",
            WARN,
            "no Cities: Skylines II user folder found",
            "run CS2 once so it creates its user folder",
        )

    # -- where the level goes ------------------------------------------------------------------
    output = values.get("output")
    if output and os.path.isdir(os.path.dirname(output) or output):
        add("BeamNG levels folder", OK, output)
    else:
        add(
            "BeamNG levels folder",
            WARN,
            f"{output} (does not exist yet; it will be created)",
            "run BeamNG.drive once, or set the folder in the app",
        )

    # -- the cook ------------------------------------------------------------------------------
    exe = detected.get("beamngExe")
    if exe:
        add(
            "BeamNG executable",
            OK,
            f"{exe}\n         shapes are compiled after the build (fast first load)",
        )
    else:
        add(
            "BeamNG executable",
            WARN,
            "not found - the level will ship uncooked .dae sources",
            "set `beamng_exe` in the app, or `--set beamng_exe=<path to BeamNG.drive.exe>`",
        )

    # -- the machine the level is for ----------------------------------------------------------
    try:
        from .beamng import validate_level

        vram, pixels, source = validate_level.render_target(values)
        budget = validate_level.resident_budget_mib(vram, pixels)
        add(
            "target machine",
            OK if "fallback" not in source else WARN,
            f"{vram:,} MiB card at {pixels / 1.0e6:.2f} Mpixel ({source}); "
            f"a level may mount about {budget:,.0f} MiB",
            None
            if "fallback" not in source
            else "run BeamNG once so it logs the card and resolution, or set gpu_vram_mib/render_width/render_height",
        )
        profile, where = validate_level.light_manager()
        if profile and "basic" in profile.lower():
            add(
                "BeamNG lighting",
                WARN,
                f"{profile} ({where}) - point and spot lights are never shaded",
                "turn on Advanced Lighting in BeamNG, or leave `lights` off",
            )
        elif profile:
            add("BeamNG lighting", OK, f"{profile} ({where})")
    except Exception as exc:
        add("target machine", WARN, f"could not read it ({exc})")

    # -- optional accelerators -----------------------------------------------------------------
    for module, what, why in (
        (
            "etcpak",
            "DDS textures",
            "without it every texture ships as PNG and BeamNG cooks it on first load",
        ),
        ("pyfqmr", "LOD decimation", "without it a slower numpy vertex-clustering fallback runs"),
        ("webview", "native window", "without it the UI opens in your browser"),
        ("PIL", "PNG decoding", "REQUIRED - some CS2 textures cannot be read without it"),
        ("numpy", "the whole pipeline", "REQUIRED"),
    ):
        required = why.startswith("REQUIRED")
        try:
            __import__(module)
            add(what, OK, f"{module} is installed")
        except ImportError:
            add(
                what,
                BAD if required else WARN,
                f"{module} is not installed - {why}",
                f"pip install {'pillow' if module == 'PIL' else module}",
            )
        except Exception as exc:
            # Installed but unusable — which is a different problem with a different fix, and it
            # must not take the whole `doctor` command down with it. (etcpak importing archspec
            # in a frozen build without archspec's data files raises FileNotFoundError here.)
            add(
                what,
                BAD if required else WARN,
                f"{module} is installed but will not import: {type(exc).__name__}: {exc}",
                "reinstall it, or report this with the line above",
            )

    out["ready"] = not any(c["status"] == BAD for c in out["checks"])
    return out


def report(as_json=False):
    facts = collect()
    if as_json:
        print(json.dumps(facts, indent=1))
        return 0 if facts["ready"] else 1
    where = "frozen build" if facts["frozen"] else f"python {facts['python']}"
    print(f"cs2beamng {facts['version']}  ({where}, {facts['platform']})")
    print(f"settings: {os.path.join(facts['appDir'] or '', 'settings.json')}")
    print()
    for check in facts["checks"]:
        print(f"[{MARK[check['status']]}] {check['name']}: {check['detail']}")
        if check.get("fix"):
            print(f"          -> {check['fix']}")
    print()
    print("Ready to build." if facts["ready"] else "Not ready - see the MISS lines above.")
    return 0 if facts["ready"] else 1
