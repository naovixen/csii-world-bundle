<p align="center">
    <picture>
        <source media="(prefers-color-scheme: dark)" srcset="./Documentation/Banner-Dark.png">
        <source media="(prefers-color-scheme: light)" srcset="./Documentation/Banner-Bright.png">
        <img alt="Carto Logo" src="./Documentation/Banner-Bright.png" width=250>
    </picture>
</p>

# Carto — Cities: Skylines II world ripper

**Rip your Cities: Skylines II city — the real meshes, materials, terrain and every one of its
1.2 million objects — and build it into a BeamNG.drive level you can drive.**

This is a fork of [Carto](https://github.com/taipei-native/Carto), whose vector/raster geodata
export it keeps intact (see [Geodata export](#geodata-export-upstream-carto) below).

## Get it

Two downloads, or one — the app can install the mod for you.

| | |
| --- | --- |
| **[⬇ The app](../../releases/latest)** | `cs2beamng-<version>-windows-x64.zip`. Unzip, run `cs2beamng.exe`. No Python, no install. |
| **[⬇ The ripper mod](../../releases/latest)** | `CartoRipper-<version>-cs2-<gameversion>.zip`. Unzip the `Carto` folder into `…\AppData\LocalLow\Colossal Order\Cities Skylines II\Mods\` — or let `cs2beamng.exe` do it (below). |

Windows will say *"Windows protected your PC"* the first time, because the build is not
code-signed: **More info → Run anyway**. Each asset ships a `.sha256` beside it.

### Three steps

1. **Install the ripper mod** — `cs2beamng.exe` ships this zip beside itself and offers an
   **Install the ripper mod** button in its Bundle card, until the mod is there. That button
   exists in the downloaded app only: running from a source checkout or a `pip install` there is
   no zip to unpack, and the app says so and links here instead. Either way, unzipping the
   `Carto` folder yourself does the same job.
2. **Rip your city** — in the game: **Options → Carto → Ripper → Rip World**. About 28 minutes for
   a full city; untick *Rip Objects* and *Rip Terrain* for a ~5 minute roads-only pass. The bundle
   is written to the game's own `ModsData\Carto\` folder.
3. **Build the level** — open the app, check it found your bundle and your BeamNG folder, pick a
   quality preset, press **Build level**. Then start BeamNG and pick your city.

`cs2beamng doctor` (or the app's own detection panel) says what it found on your machine and what
is missing, with the fix for each.

### Command line

Same settings, same stages, same level:

```
pipx install csii-world-tools            # or: pip install csii-world-tools
cs2beamng doctor
cs2beamng build [bundle] --out <levels>/<name> [--preset high] [--region "X Z R"] [--set key=value ...]
cs2beamng budget <level_dir>             # what a built level costs: resident bytes, triangles drawn
```

Every setting, its default and what was measured to arrive at it:
[`docs/settings.md`](docs/settings.md). The app shows **nine** of them by default and hides the
other seventy behind *Show advanced*.

### What you need

| Thing | Why |
| --- | --- |
| Cities: Skylines II 1.6+ | Your city, and the mod that rips it. |
| BeamNG.drive | Where the level goes. Turn on **Advanced Lighting** if you want street lights — Basic Lighting shades no point or spot light at all. |
| ~30 GB of disk | The bundle, its bake cache, and the level. |
| 8 GB of VRAM | For a full city at the `performance` preset. Less than that: `medium`, `low`, or build one district with `--region`. |

## World Ripper (this fork)

This fork extends Carto from a vector-geodata exporter into a **full world ripper**: beside the
existing GeoJSON/Shapefile/GeoTIFF output, it walks the loaded city in-engine and writes real
meshes, materials/textures, per-instance transforms and full attributes into a single,
versioned, engine-neutral **CS2 World Bundle**.

* **Writing a consumer? Start at [`docs/consumer-guide.md`](docs/consumer-guide.md)** — the route
  through the layers, the pipeline order, the must-implement tiers, and a worked example tracing
  one instance from manifest to pixels. Then
  [`docs/consumer-caveats.md`](docs/consumer-caveats.md) for the things that are true and
  surprising, and [`docs/bundle-changelog.md`](docs/bundle-changelog.md) for telling which
  vintage of bundle you have.
* The bundle contract is frozen and documented in **[`docs/bundle-schema.md`](docs/bundle-schema.md)**,
  with a JSON Schema per layer in [`docs/schema/`](docs/schema/) (fifteen) and a complete minimal
  example bundle in [`docs/example-bundle/`](docs/example-bundle/). A consumer can be built
  against those alone — no game, no mod source.
* An engine-neutral reference reader lives in [`tools/csiibundle/`](tools/csiibundle/) — standard
  library plus numpy, never imports `bpy`. See [`tools/README.md`](tools/README.md).
* Validate any bundle with `python tools/validate_bundle.py <bundle_dir>` (standard library only).
* Downstream consumers read the bundle through its documented contract and are never coupled
  into the mod. Two live in this repo beside it: the Blender adapter
  (`tools/blender_import_bundle.py`) and the **`cs2beamng` BeamNG.drive level builder**
  ([`apps/cs2beamng/`](apps/cs2beamng/README.md)), both on the engine-neutral `csiibundle`
  package and its offline bake (`csiibundle.bake`, contract §13). `pip install -e .` installs
  both.
* **Local and personal use only.** The ripper extracts assets from your own copy of the game for
  interoperability with software you own. Do not publish, upload or redistribute ripped meshes
  or textures, and never commit them to a repository.

Implementation follows the phased plan in [`PLAN.md`](PLAN.md). The export path is complete: a
one-button **Rip World** on the Options → Carto ripper tab walks the loaded city (progress
appears in the game's notification area) and writes the full bundle to
`…\AppData\LocalLow\Colossal Order\Cities Skylines II\ModsData\Carto\`. Vanilla Carto's
vector/raster export is unaffected. A reference consumer lives at
[`tools/blender_import_bundle.py`](tools/blender_import_bundle.py) (Blender import of a full
bundle); it doubles as the readable reference implementation of the contract's consumer-side
algorithms.

## Geodata export (upstream Carto)

The fork keeps every bit of upstream Carto's vector and raster export, unchanged and beside the
ripper:

* **Vector data** – buildings, districts, map tiles, pathways, POIs, roads, routes, tracks,
  zoning cells and more, as GeoJSON or ESRI Shapefile.
* **Raster data** – terrain elevation and water depth, as GeoTIFF.
* **Attribute-rich output** – names, direction and other metadata on every feature.
* **Projection support** – WGS84 / UTM (EPSG 326xx / 327xx) by default; custom Transverse
  Mercator projections too.

**Only want the geodata?** Install *upstream* Carto from
[Paradox Mods](https://mods.paradoxplaza.com/mods/87428/Windows) — through the in-game mod
browser, their website, or [Skyve](https://github.com/JadHajjar/Skyve). That mod page is
upstream's; this fork is distributed from [its own releases](../../releases/latest) and is not on
Paradox Mods.

### Using it

1. Load a save, start a new game, or open the editor.
2. Open **Options** and select **Carto** from the sidebar.
3. In the **General** tab, choose file format and output name.
4. Select data types to export in the **Feature** tab.
5. Configure geometry and attributes in the **Properties** tab.
6. Set your projection in the **Projection** tab.
7. Click **Export Files** in the **General** tab.
8. Your data will be saved to: `C:\Users\<YourName>\AppData\LocalLow\Colossal Order\Cities Skylines II\ModsData\Carto`

See the [Tutorials](https://github.com/taipei-native/Carto/wiki/Tutorial) for making maps with QGIS, and the [User Manual](https://github.com/taipei-native/Carto/wiki) for detailed documentation.

## Building from source

You do not need this to use either half — [the releases](../../releases/latest) carry both, and
`pipx install csii-world-tools` gives you the command line. This is for working on it.

**The app** (Python ≥ 3.11):

```
pip install -e .              # csiibundle (reader + bake) and the cs2beamng app
pip install -e ".[all]"       # + native window, DDS textures, fast LOD decimation
pip install -e ".[build]"     # + PyInstaller, for the standalone build

cs2beamng                     # the UI: native window, or your browser at http://127.0.0.1:<port>/?t=…
cs2beamng gui --browser       # always the browser
python -m cs2beamng           # the same, without the console script on PATH

python tools/build_exe.py     # dist/cs2beamng-<version>-<platform>.zip
```

The frozen app is `dist/cs2beamng/cs2beamng.exe`. PyInstaller also leaves an identically named
bootloader stub in its scratch directory, `build/cs2beamng/` — that one has no `_internal/`
beside it and fails with *"Failed to load Python DLL"*. `build_exe.py` prints the path to run.

The level is written to `%LOCALAPPDATA%\BeamNG\BeamNG.drive\current\levels\<level_name>\`
(Linux/macOS paths are detected too) with a `cs2beamng_report.json` beside it. The first build
also bakes the bundle's `derived/` cache (≈ 15 min for a full city); later builds re-bake only
what changed. Architecture and stage detail: [`apps/cs2beamng/README.md`](apps/cs2beamng/README.md).

**The mod** needs the [CS2 Modding Toolchain](https://cs2.paradoxwikis.com/Modding) — it compiles
against the game's own assemblies, which is also why no CI can build it:

```
dotnet build -c Release              # Mod.targets deploys it to your CS2 Mods folder
pwsh tools/release_mod.ps1 -Tag v0.2.0 -Upload    # build, package and attach it to a release
```

**Releasing:** tag `vX.Y.Z` and push. `.github/workflows/release.yml` builds the app for Windows
and Linux, the wheel and the sdist, and opens a **draft** release. Then run `release_mod.ps1` to
attach the mod, or `python tools/build_exe.py --mod <zip>` to ship the mod *inside* the app so
there is only one download.

## API Integration

Carto exposes a public API at `Carto.IO.IO.Export(Carto.IO.Options)` so other mods can drive exports on their demand. For the detailed documentation, please refer to the [API guide](https://github.com/taipei-native/Carto/wiki/Api).

## Credits

Since April 2024, when I started to develop Carto, I’ve learned from and been inspired by the open-source work of amazing Cities: Skylines II modders including **algernon**, **Guo**, **krzychu124**, **TDW**, and **yenyang** — a big shout-out to them!

Special thanks also to beta testers like **Allegretic**, **Excellent Guy**, **jefferyharrell**, **LightLight**, and members of the *Cities: Skylines Modding* and *Cities: Skylines Taiwan Assets* Discord servers for their feedback and support.

Carto also includes C# adaptations of parts of [PROJ](https://github.com/OSGeo/PROJ) and [PROJ4JS](https://github.com/proj4js/proj4js), optimized for Unity’s Burst compiler. License details are available in the `Licenses` directory.

Finally, sincere thanks to everyone who contributed translations on [Crowdin](https://crowdin.com/project/carto):

* Dutch: **GeraspteGatenKaas**
* French: **Morgan · Toverux** and **Wateir**

## Contact

You can reach out via:

* [Paradox Forum](https://forum.paradoxplaza.com/forum/threads/carto.1699089/) (English / Chinese)
* [Cities: Skylines Modding Discord](https://discord.gg/HTav7ARPs2) (English)
* [Cities: Skylines Taiwan Assets Discord](https://discord.gg/Gz4K66jT64) (Chinese preferred)

For bug reports or in-depth discussions, please use [GitHub Issues](https://github.com/taipei-native/Carto/issues) or [Discussions](https://github.com/taipei-native/Carto/discussions) to keep things organized.

## License

This project is licensed under the MIT License. See [LICENSE.txt](LICENSE.txt) for full details.
