# Distribution & first-run UX — D1–D5

**Goal:** a person who has never opened this repo can (1) download the ripper mod from a GitHub
release and drop it in, (2) download one app file and double-click it, (3) see the settings that
matter and nothing else, and press **Build level**.

**Scope:** packaging, releases, and the settings surface. No pipeline behaviour changes — every
one of the 78 settings keeps its meaning and its `--set` name; this plan changes what is *shown*,
not what is *available*. Nothing here touches `Systems/`, `IO/` or `Domain/`, so it cannot make
the bundle on disk stale.

---

## 0. What is actually in the way (measured 2026-08-20)

Facts, not guesses; each one is why a step below exists.

### The mod

* `Carto.csproj` imports `Mod.props`/`Mod.targets` from `$CSII_TOOLPATH` and references `Game`,
  `Colossal.*`, `Unity.*` with `<Private>False</Private>`. A GitHub runner has neither the
  toolchain nor the game's assemblies, and those assemblies are not redistributable.
  **Therefore CI can never build the DLL.** Any release of the mod is a *locally built* artifact
  that a script uploads. This is the single constraint that shapes D4.
* The deployed mod folder is `…\Cities Skylines II\Mods\Carto\`, six files, 4.8 MiB:
  `Carto.dll`, `Carto.pdb`, `Carto_win_x86_64.dll` (+ `.pdb`), `Carto_linux_x86_64.so`,
  `Carto_mac_x86_64.bundle`. The three `Carto_<platform>_*` files are the **Burst-compiled
  jobs** — a release shipping `Carto.dll` alone ships a mod whose collectors fall off Burst.
  The zip must be that folder, whole.
* `README.md` § Installation still points at **Paradox Mods id 87428**, which is *upstream
  Carto* and contains no ripper. `Properties/PublishConfiguration.xml` still carries upstream's
  `ModId`, `DisplayName` and description. Publishing from this repo as-is would push a fork over
  someone else's mod page. GitHub Releases is the correct channel for this fork; PDX is out of
  scope and that config must not be fired by accident.

### The app

Install today is `git clone` + `pip install -e .`. That is the "difficult to know how to
execute" complaint, and it is also the only install that *works*, because of three defects:

1. `stages/cook.py:56-63` imports top-level **`level_budget`**, which lives in `tools/` and is in
   no wheel (`pyproject.toml` maps only `csiibundle` and `cs2beamng`). Its fallback climbs four
   `dirname`s to a `tools/` that exists only in a checkout. A non-editable `pip install .`
   therefore raises `ImportError` when the stages package is imported.
2. Nothing calls `multiprocessing.freeze_support()`. `pipeline/runner.py:69` and four stages
   (`derived`, `materials`, `networks`, and the bake pool) use `get_context("spawn")`; under a
   frozen exe every spawned child **re-executes the exe**, and without `freeze_support()` that is
   an unbounded fork of app windows.
3. **Pillow is an undeclared dependency.** `beamng/materials.py:92` and `stages/terrain.py:508`
   import `PIL`; `ci.yml` installs `pillow` by hand, `pyproject.toml` does not list it.

Only two `__file__` uses exist in the whole app (`server.py:52` for `ui/`, and the cook climb
above), so once (1) is fixed the app is relocatable. That is the good news for the exe.

### The settings

* 78 keys, rendered as six `<details>` groups with only the first open (`ui/app.js:180-190`).
  Every key is rendered, always.
* Every control gets its full help string as an inline `div.help` (`ui/app.js:257`).
  **22 of the 78 help strings are over 200 characters; the longest is 2,320**
  (`lod_switch_error_px`) — they are audit notes ("MEASURED over 1.16M placements …"), correct
  and irreplaceable for the person tuning a build, and a wall of text for the person who wants a
  city. The median is 68 chars.
* Auto-detection **already exists and is good**: `settings.detected()` returns bundles that
  actually hold a `bundle.json`, existing BeamNG userfolders, and the BeamNG exe found through
  Steam's `libraryfolders.vdf`, on all three OSes (`csiibundle/bake/locate.py`,
  `settings.candidate_userfolders` / `candidate_beamng_exes`). The UI hides it in a
  `<select class="detected" hidden>` beside a text box. **The work is presentation, not
  detection.**

---

## 1. Verdict on the three delivery routes

| Route | Verdict | Why |
| --- | --- | --- |
| **Deploy the page to a GitHub URL** | **No**, for the builder | The UI is a thin client over a local server, and the *build* needs the local filesystem (a multi-GiB bundle), spawned worker processes, write access to BeamNG's userfolder, and it launches `BeamNG.drive.exe -compilemeshes`. A tab on `github.io` can do none of those. Making it work would mean uploading the user's ripped city to a server — which the licence terms in `CLAUDE.md` forbid outright. |
| **An exe on the Releases tab** | **Yes — primary** | One file, no Python, double-click. It is what the audience (CS2/BeamNG players) expects. |
| **Command line** | **Yes — secondary** | For scripted and Linux use: `pipx install csii-world-tools` → `cs2beamng`. Already the CLI's shape; it just needs to exist on PyPI. |
| GitHub Pages as a **front door** | Yes, small | A static docs site with screenshots, the download buttons, and a "copy the command line" widget for headless users. Documentation, not the application. |

---

## D1 — Packaging fixes (prerequisite for everything else)

Small, mechanical, and each one is a defect today.

1. **`level_budget`**: move `tools/level_budget.py` to `csiibundle/level_budget.py` (it is
   engine-neutral analysis and both consumers may want it), leave a two-line shim at
   `tools/level_budget.py` re-exporting it so `docs/plans/a8-performance.md`'s command lines and
   the `tools/test_*.py` scripts keep working, and change `stages/cook.py` to
   `from csiibundle import level_budget`. Delete the `__file__` climb.
2. **`freeze_support()`**: the first statement of `cli.main()`, before any other import work.
   Harmless unfrozen, mandatory frozen.
3. **`pillow`** into `[project].dependencies` — it is not optional in practice (a colour-type-2
   PNG and a palette PNG both need it).
4. **Make `requires-python` match reality**: CI runs 3.11 and 3.13; keep `>=3.10` only if 3.10 is
   actually tested, else raise it.
5. **New CI gate**, the one that would have caught (1) and (3): in a clean venv, `pip install .`
   (**not** `-e`), change to a scratch directory outside the checkout, and run
   `cs2beamng build docs/example-bundle --out …`. Add it to `ci.yml` beside the existing headless
   build.

**Gate D1:** the new CI job is green on all three OSes.

---

## D2 — Settings: basic by default, everything else behind "Show advanced"

### D2.1 Tier every key

Add a `BASIC` list beside `GROUPS` (one source of truth, checked by `test_app_shell.py` exactly
as `GROUPS` is today at line 61).

**Basic — the whole default surface, ten controls:**

| Control | Key(s) | Presentation |
| --- | --- | --- |
| Bundle folder | `bundle` | text + **Detected** line + Browse (already in the Bundle card) |
| Levels folder | `output` | same |
| Level name / title | `level_name`, `level_title` | one row; title derived from the name until edited |
| Area | `region` | "Whole city" / "Part of it" radio; the `X Z R` box appears only for the second |
| Quality | `quality_preset` | the existing segmented control, plus one line of prose per preset |
| Street lights | `lights` | a real user choice with a real cost; the preset sets it, this exposes it |
| Collision | `collision_preset` | exact / simplified / off |
| Compile shapes now | `cook` | "Faster first load (needs BeamNG closed)" |
| Target machine | `gpu_vram_mib`, `render_width/height` | **read-only summary** of what was detected, with an "override" link into advanced |

Everything else (68 keys) is **advanced**, in today's six groups, unchanged, behind a single
`Show advanced` toggle remembered in settings (`ui_advanced`, a new bool). The advanced panel
keeps the `<details>` accordion; the basic panel is flat — no dropdown to open before you can see
anything.

> Nothing is removed and nothing is renamed. `--set lod_switch_error_px=6` keeps working, and the
> tuning knobs the audit sections argue about stay exactly where the audit points at them.

### D2.2 Two-part help

Split help into `help` (one sentence, ≤ 120 chars, always shown) and `detail` (the rest of
today's text, behind a "why" disclosure on the row). Keep the audit prose verbatim, in a parallel
`DETAILS = {key: "…"}` dict rather than inline in `SCHEMA` — that keeps `SCHEMA` readable and
loses nothing. `schema_for_ui()` ships both.

A test asserts every `help` is ≤ 120 chars. That is what stops the essays growing back.

### D2.3 Make detection visible

`settings.detected()` already returns everything needed. Replace the hidden `<select>` with:

* a **green check + path** under each path field when the detected value is in use ("Found your
  CS2 export — 4.2 GiB, ripped 2026-08-19"; the census already computes the second half),
* an amber line with a **Use this** button when a different valid candidate exists,
* a red line naming what was looked for when there is none ("No bundle found. Rip one in CS2:
  Options → Carto → Ripper → Rip World") — and, once D4.3 lands, an **Install the mod** button
  right there,
* a **Rescan** button; detection currently runs only at server start.

### D2.4 Presets that explain themselves

`PRESETS` has five entries; the UI renders five bare words. Give each a one-line consequence
("**High** — everything, 512 px textures. ~1.5 h, ~5 GiB. The default.") and, when the VRAM and
render size are detected, mark the one that fits the machine as *Recommended*. The numbers exist
— `beamng/validate_level.resident_budget_mib` and `pipeline/history.py` — so this is a read, not
an estimate.

### D2.5 CLI parity

`cs2beamng build --preset high --region "2994 1853 200"` as first-class flags (they are
`--set quality_preset=` / `--set region=` today), and a `cs2beamng doctor` that prints what was
detected, what is missing, and the exact command to fix each. That is the CLI's answer to
"difficult to know how to execute".

**Gate D2:** `test_app_shell.py` extended — every key in exactly one group *and* one tier, every
`help` ≤ 120 chars, every basic key rendered in the basic panel. Then Naomi opens the app and
confirms the default screen is those ten controls.

---

## D3 — The exe

**Shape:** PyInstaller **onedir**, zipped. Not onefile: onefile re-extracts ~120 MiB to `%TEMP%`
on every launch *and on every spawned worker*, and this app spawns workers by design.

* Entry point `cs2beamng.exe` → `cli.main()` → native window (pywebview/WebView2, present on
  Windows 11 by default; the browser fallback already exists in `window.py`).
* Bundle the optional native deps so the good path is the default path: **etcpak** (DDS — without
  it every texture ships as PNG and BeamNG cooks it on first load), **pyfqmr** (LOD decimation,
  else the numpy fallback), **pywebview**, **numpy**, **pillow**.
* `--add-data` for `cs2beamng/ui/*` (already declared as `package-data`).
* Console handling: ship it **windowed** and verify that `cs2beamng.exe build …` still attaches
  to the parent console when launched from one. If that attach proves fragile, emit two exes from
  one spec (`cs2beamng.exe` windowed, `cs2beamng-cli.exe` console) rather than making the GUI open
  a black window.
* **Estimated size:** 100–150 MiB zipped (numpy + PIL dominate). Well inside GitHub's 2 GiB asset
  limit.

**Known friction to document, not to fix:** an unsigned exe raises SmartScreen ("Windows
protected your PC" → More info → Run anyway). Code signing is ~$200/yr and out of scope, but the
README and the Pages site must show that dialog and say what to click, or a large share of the
audience stops there.

**Linux:** the same spec produces a directory plus launcher; worth building, because CS2 runs
under Proton and the bundle-path detection already knows the Proton prefix. **macOS:** CS2 has no
macOS build, so the only mac user is one copying a bundle across — ship them the PyPI package and
skip the `.app` (notarisation is a project of its own).

**Gate D3:** on a Windows account with **no Python installed**: unzip, double-click, build
`docs/example-bundle` end to end, then a real city. Watch the worker spawn — if the exe opens two
windows, `freeze_support()` is wrong.

---

## D4 — Releases

### D4.1 `release.yml` (tag `v*`) — the app half

CI-buildable, so fully automatic: build the wheel and sdist, build the Windows and Linux onedir
zips, checksum everything, create the GitHub release with generated notes, attach the assets.

### D4.2 `tools/release_mod.ps1` — the mod half

Not CI-buildable (§0). A local script, run on the machine that has the toolchain:

1. refuse to run on a dirty tree or off the tag,
2. `dotnet build -c Release`,
3. collect the **six** deployed files — take the deploy list from `Mod.targets` rather than
   hard-coding it, so a toolchain change is picked up instead of silently dropping a Burst lib,
4. zip as `CartoRipper-<ModVersion>-cs2-<GameVersion>.zip` + sha256,
5. `gh release upload <tag>` onto the release D4.1 made.

The zip's internal layout is `Carto/…`, so "extract into your Mods folder" is literally true.
Include a `HOW-TO-INSTALL.txt` with the destination path for Windows, the Proton prefix, and
macOS.

**Version discipline:** `Carto.csproj`'s `AssemblyVersion`/`Version`, `PublishConfiguration.xml`'s
`ModVersion` and the git tag must agree — check it in the script. Put `GameVersion` (currently
`1.6.*`) in the release notes; a mod built against the wrong game version is the first support
question you will get.

**Do not touch `PublishConfiguration.xml`'s upstream `ModId`.** If PDX publishing is ever wanted,
that is a separate decision with a new mod id and a new name.

### D4.3 The step that removes the mod install entirely (recommended)

The app already knows where CS2's `LocalLow` root is (`locate.py` walks it to find the bundle).
Give the "No bundle found" state an **Install the ripper mod** button: take the mod zip
**bundled inside the exe** (offline, no runtime network — which fits the project's habits better
than a download), verify its sha256, extract to `…\Cities Skylines II\Mods\Carto\`, and say "now
start CS2 and press Rip World".

That collapses the first half of the user journey to *one download*. It is the highest-value item
in this plan. Bundling the zip also ships the mod and the app that reads its bundle as **one
matched pair**, which is the versioning story the bundle schema wants anyway.

**Gate D4:** on a machine that has never seen this repo — download two files (or one, with
D4.3), rip a small city, build it, drive it.

### D4.4 PyPI

Publish `csii-world-tools` via GitHub trusted publishing (OIDC, no stored token) from the same
tag workflow, so `pipx install csii-world-tools` works. Check the name is free; if not,
`cs2beamng` is the obvious alternative and matches the console script.

---

## D5 — Docs and the front door

1. **Rewrite the README's opening.** Today it opens on upstream Carto's feature list and buries
   the ripper. The first screen should carry: what this does (a screenshot of a CS2 city in
   BeamNG), the download links, and the three steps.
2. **`docs/` → GitHub Pages** (`pages.yml`): the same three steps with screenshots, the
   SmartScreen dialog, a troubleshooting list (no bundle found / BeamNG running during the cook /
   Advanced Lighting needed for `lights` / VRAM), and a small **command builder** — a static
   settings picker that emits a `cs2beamng build --set …` line. That is the only sound use of a
   hosted page here, and it costs one HTML file.
3. **Move the reference material** (architecture, and all 78 settings with their full audit prose)
   into `docs/`. Generate that page from the `DETAILS` dict of D2.2, so the settings reference and
   the UI can never disagree.
4. **Delete `pip install -e .` from the front page.** It stays in a "Building from source" section
   for contributors.

---

## Sequencing

| Step | Depends on | Rough size |
| --- | --- | --- |
| D1 packaging fixes + CI gate | — | small |
| D2 settings tiering, help split, detection UI | — (parallel with D1) | medium |
| D3 exe spec + first manual build | D1 | medium |
| D4.1 / D4.2 release workflow + mod script | D3 | small |
| D4.3 in-app mod installer | D4.2 | small–medium |
| D4.4 PyPI | D1 | small |
| D5 README + Pages | D3, D4 | medium (mostly writing + screenshots) |

D1 and D2 are independent and can go in either order. Nothing here blocks the A8 work.

---

## Risks and declared unknowns

* **The frozen worker.** `spawn` under PyInstaller is the one real technical risk (D1.2, D3).
  It is well-trodden and `freeze_support()` is the documented answer, but it must be *tested*, not
  assumed — a build that forks endlessly on a user's machine is the worst possible first
  impression.
* **Console attach for `cs2beamng.exe build`.** Unverified; the fallback (two exes) is cheap.
* **Antivirus false positives** on unsigned PyInstaller binaries are common. Mitigation: publish
  the sha256 and keep the pip/pipx route an equal citizen.
* **The mod zip is only as good as the machine that built it** — the same class of hazard as the
  staleness rule: the released DLL is a snapshot of one local build. The clean-tree and tag checks
  are what make it reproducible, and the release notes must name the commit.
* **Settings are load-bearing for the audit.** The tiering must not delete or rename a key; every
  `docs/cs2-api-audit.md` reference to a setting name has to keep resolving.
