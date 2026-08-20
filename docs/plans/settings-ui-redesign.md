# Settings UI redesign — E1–E7

**The complaint:** the settings are still confusing. Names read like variable names, several
settings look like duplicates of each other, some demand that you know a text format nobody has
written down, and nothing on screen tells you whether the level you are configuring will
actually run on your machine.

**The shape asked for:** keep the bundle overview and the build/log column. Above the settings,
the handful of things a person must actually supply — levels folder, BeamNG folder (both
auto-detected), folder name, level title — and the Build button. Below that, the settings as
**sections down a sidebar**: what you can see, collision, textures, navigation, and a
build/advanced section holding everything no ordinary user should touch. A sidebar rather than
tabs because more sections are coming (§1). Trenches and tunnels stop being their own setting and
become part of roads. Tree and prop density absorb their own on/off switches. Every free-text
box that requires knowing a format is replaced by a control that does not. And a **live estimate
of what the level will cost your machine** stays on screen the whole time.

This plan is the design and the build order for that. No code yet.

---

## 1. What the research says

Four findings from current practice, and what each one decides here.

**A sidebar, not tabs — because this list will grow.** Tabs work when the content divides into a
few mutually exclusive, roughly equal groups, and they fail as the group count grows: a tab row
has a fixed width, so every new section makes every existing label smaller or pushes it off the
end. A sidebar lists sections vertically, shows all of them at once, and costs nothing to extend
([Eleken, Tabs UX](https://www.eleken.co/blog-posts/tabs-ux); [Design for Ducks, alternatives to
tabs](https://designforducks.com/alternative-for-tabs-in-ui-design/)). Five sections is inside
tab range today, but the roadmap has more coming — traffic, weather, audio, a mod-export section
— and switching navigation patterns later is a rewrite of the shell plus the retraining of
everyone who learned the old one. **Decision: sidebar from the start**, laid out from data so a
sixth section is one list entry (§2.1).

**A sidebar past a handful of sections needs search.** Both of the settings UIs that carry
hundreds of options put a search field at the top of the sidebar and treat it as a first-class
route rather than a fallback: VS Code groups settings but leads with a filtering search bar, and
in macOS System Settings search is the *only* practical way to reach some deeply-placed panes
([VS Code settings](https://code.visualstudio.com/docs/getstarted/settings);
[macOS System Settings](https://www.geekhampton.com/tips-and-tricks/2023/9/27/frustrated-by-system-settings-use-the-view-menu-or-search)).
With 79 settings and more later, "which section is that in?" becomes the commonest question a
person has. **Decision:** a search field at the top of the sidebar, filtering across every
section by name and description and showing each hit under its section name.

**Progressive disclosure means levels, not hiding.** The pattern is to show the essential first
and let the interested go deeper, and its documented failure mode is nesting so many levels that
finding anything becomes the task ([IxDF](https://ixdf.org/literature/topics/progressive-disclosure);
[UXPin](https://www.uxpin.com/studio/blog/what-is-progressive-disclosure/);
[LogRocket](https://blog.logrocket.com/ux-design/progressive-disclosure-ux-types-use-cases/)).
The current UI has three levels — accordion, then *Show advanced*, then *why* — which is exactly
that failure. **Decision:** exactly two levels. Level one is the basics strip plus the four
level sections. Level two is the Advanced section. Nothing else collapses, nothing else expands.

**A settings screen with a live cost meter is a solved pattern — in games.** GTA V put a video
memory bar under the graphics options that fills as you change settings and turns red past the
card's capacity; Cyberpunk 2077, Hogwarts Legacy and Monster Hunter Wilds all ship a live VRAM
bar; Doom Eternal goes further and refuses settings that exceed the card
([GTA V's bar](https://gamefaqs.gamespot.com/boards/805606-grand-theft-auto-v/71629355);
[Doom Eternal's limit](https://www.tomshardware.com/features/how-to-play-doom-eternal-on-integrated-graphics)).
This app is a graphics settings menu whose "frame" takes two hours to render, so the feedback
matters *more* here, not less. **Decision:** build the meter (§4), and warn rather than refuse —
we are not the engine, and a user may legitimately build a level for a machine that is not this
one.

**A free-text box is a design failure whenever the set of valid answers is known.** Three of
ours are: furniture categories are exactly `lane`, `fence`, `cable`; a region is three numbers;
a fast-travel point is a name and a position. **Decision:** §3 replaces all of them.

---

## 2. The screen

```
┌─ Bundle ────────────────────────────────────────────┐  ┌─ Build ─────────────┐
│  aaBillinghame · ripped 20 Aug 09:35                │  │ [ Build level ]     │
│  1,276,060 instances · 5.13 GiB                     │  │ progress, ETA       │
│  bundle folder [.....................][ ⤢ ]         │  │ stage list          │
│  ✓ Found your CS2 export                            │  └─────────────────────┘
└─────────────────────────────────────────────────────┘  ┌─ Log ───────────────┐
┌─ Level ─────────────────────────────────────────────┐  │                     │
│  Levels folder [..................][ ⤢ ]          ✓ │  │                     │
│  BeamNG        [..................][ ⤢ ]          ✓ │  │  (both unchanged)   │
│  Folder name   [cs2_city.........]  Title [CS2 City] │  │                     │
│  Area          ( whole city | part of it )           │  │                     │
│                                                      │  │                     │
│  ── What this will cost ───────────────────────────  │  │                     │
│  Video memory ███████████░░░░ 4.1 / 5.2 GB  ✓        │  │                     │
│  Drawn detail ██████░░░░░░░░░ 1.8M triangles ✓       │  │                     │
│  Build ~1 h 40 m  ·  Disk 5.2 GB                     │  │                     │
└──────────────────────────────────────────────────────┘  │                     │
┌─ Settings ───────────────────────────────────────────┐  │                     │
│ [ Ultra | High | Performance | Medium | Low ]         │  │                     │
│ ┌────────────────┬──────────────────────────────────┐│  │                     │
│ │ [ search…    ] │  World                           ││  │                     │
│ │                │                                  ││  │                     │
│ │ LEVEL          │  ☑ Buildings                     ││  │                     │
│ │ › World     ●3 │  ☑ Roads and rails               ││  │                     │
│ │   Collision    │  Trees      ▁▃▅▇ 100 %           ││  │                     │
│ │   Textures     │  Props      ▁▃▅▇ 100 %           ││  │                     │
│ │   Navigation   │  ☑ Road markings                 ││  │                     │
│ │                │  …                               ││  │                     │
│ │ BUILD          │                                  ││  │                     │
│ │   Advanced  ●1 │                                  ││  │                     │
│ │                │                                  ││  │                     │
│ │ Reference ↗    │                                  ││  └─────────────────────┘
│ └────────────────┴──────────────────────────────────┘│
└──────────────────────────────────────────────────────┘
```

The meter sits **above** the settings panel, not inside a section, because it is the consequence
of all of them. The preset row sits directly above the sidebar because a preset is a shortcut
for what is inside it.

**The layout rebalances.** The settings panel now needs room for a nav column plus its content,
so the two columns go from 5fr / 7fr to **7fr / 5fr** — the log is a scrolling text pane and
gives up width more cheaply than a form does. At the default 1280-wide window that is roughly
700 px for settings (170 nav + ~500 content) against 500 for build and log.

### 2.1 The sidebar, specified

Built from a `SECTIONS` list so that adding one is a data change, never a layout change:

* **Grouped.** Headed groups — `LEVEL` (World, Collision, Textures, Navigation) and `BUILD`
  (Advanced) today. A group with one entry still renders its heading, so the future sections
  land in an established shape rather than reorganising the list when they arrive.
* **Badges.** A dot and a count on any section holding settings that differ from the preset's
  values. This is the third answer to "the presets don't seem to do anything": after applying
  one, the sidebar shows *where* it changed things, and after hand-editing, it shows what you
  have moved off the preset.
* **A warning marker** on a section that is pushing the meter over budget, so "why is the bar
  red" has a visible answer one click away — this is what the meter's *What should I change?*
  link points at.
* **Search** at the top, filtering by label and description across every section. Results are
  listed with their section as a subheading; the search field is focused by `/` or `ctrl+F`, and
  `Esc` clears it. This is the part that keeps the pattern working at 150 settings.
* **Keyboard**: up/down moves the selection, the content pane scrolls independently, and the
  selection is remembered between sessions (`ui_section`, replacing `ui_advanced`).
* **Narrow windows**: under about 900 px of panel width the nav column collapses to a horizontal
  row of section chips above the content — the tab layout, in other words, as the *degraded*
  form rather than the design.
* **A section is a heading, then a flat list.** No accordions inside a section, ever. That was
  the last redesign's mistake and it does not come back with a sidebar attached.

---

## 3. Every setting, and where it goes

Rules: every one of the 79 keys lands in exactly one place, no key is renamed or removed (the
CLI's `--set` names are a public interface), and the test suite checks both. "Not shown"
means the setting exists for `--set` and the docs but no longer has a control — either because
another control now drives it, or because it is a diagnostic.

**The mapping below was run against `SCHEMA` before this plan was written**: 79 assigned, 79 in
the schema, nothing duplicated, nothing orphaned, nothing invented —

    basics 6 · World 14 · Collision 9 · Textures 10 · Navigation 4 · Advanced 30 · not shown 6

### The basics strip (always visible)

| Control | Key | Notes |
| --- | --- | --- |
| Bundle folder | `bundle` | in the Bundle card, with its detection line |
| Levels folder | `output` | auto-detected |
| BeamNG | `beamng_exe` | auto-detected; **promoted** out of Advanced because the user named it as a basic |
| Folder name | `level_name` | |
| Level title | `level_title` | |
| Area | `region` | "The whole city" / "Part of it" → the map picker (§3.6) |

### Section — **World** ("what you will see")

| Control | Key(s) | Change |
| --- | --- | --- |
| Buildings | `buildings` | |
| Roads and rails | `networks`, **`terrain_cut`** | one switch. `terrain_cut` is not a thing a person wants separately — it is how trenches and tunnel mouths get their ground. It follows `networks` and leaves the UI. |
| Trees | `foliage_density`, **`trees`** | one slider, 0–100 %. **0 turns trees off** and writes `trees: false`; above 0 writes `trees: true`. |
| Props and fences | `prop_density`, **`props`** | same |
| Road markings | `markings` | |
| Painted surfaces | `areas` | |
| Building foundations | `skirts` | |
| Roadside furniture | `lane_furniture` + `lane_furniture_categories` | switch, plus three checkboxes (§3.5) shown only when it is on |
| Street and building lights | `lights` | `lights_max` → Advanced |
| Water | `water` | |
| Grass | `groundcover` | |
| How far you can see | `view_distance` | slider; the single biggest performance lever, and it belongs where its effect is |
| Time of day | `tod_hour` | a real user-facing choice, currently buried in "How much" |

### Section — **Collision**

`collision_preset` (Exact / Simplified / Off) as the header choice, then the per-kind switches
it gates: `collision_buildings`, `collision_props`, `collision_trees`, `collision_furniture`,
`collision_skirts`, `collision_paint`, `collision_lights`, and `collision_min_footprint`
("Ignore objects narrower than…").

### Section — **Textures**

| Control | Key(s) |
| --- | --- |
| Texture detail | `texture_max` |
| Surface bumpiness detail | `texture_max_normal` |
| Shine and roughness detail | `texture_max_data` |
| Colour-variant detail | `texture_max_variant` |
| Pre-compress textures | `texture_dds` |
| Ground detail | `terrain_upsample` |
| Ground brightness | `terrain_brightness` |
| Colour variety — trees / props | `paint_variants_tree`, `paint_variants_prop`, `paint_kinds` |

These are grouped here because they are what the meter's texture half responds to, so this is
the section where the bar moves most.

### Section — **Navigation**

| Control | Key(s) |
| --- | --- |
| Minimap and routing | `ai_roads` |
| Include airport taxiways | `navgraph_taxiways` |
| Fast-travel points | `pois`, `poi_max` |
| **The fast-travel point editor** | replaces `poi_file` (§3.7) |

### Section — **Advanced** (build and tuning)

Everything else, in three labelled blocks, with one sentence at the top of the section saying these
have measured defaults and are here for people building for a particular machine:

* **Detail and performance** — `generate_lods`, `tile_lod`, `imposters`, `imposter_min_metres`,
  `lod_detail_scale`, `lod_detail_scale_prop`, `lod_switch_error_px`, `lod0_max_error`,
  `lod0_min_triangles`, `forest_min_items`, `lane_max_tris`, `paint_factor_max_error`,
  `foliage_road_clearance`, `forest_shadows`, `tree_wind`, `tile_size`, `terrain_holes`,
  `terrain_blend_sharpen`, `lights_max`, `navgraph_max_width`
* **The machine this level is for** — `gpu_vram_mib`, `render_width`, `render_height` (the meter
  reads these; they default to what was detected)
* **Build process** — `jobs`, `cook`, `rebuild`, `keep_partial`, `sweep_temp`, `keep_dae`,
  `ui_browser`
* The sidebar's footer link — *Full settings reference* → `docs/settings.md` — is always visible,
  not only here

**Not shown anywhere:** `quality_preset` (it is the preset row), `terrain_cut` (driven by
networks), `trees` / `props` (driven by the sliders), `poi_file` (driven by the editor),
`ui_advanced` (retired — replaced by `ui_section`, remembering the last section).

### 3.5 Roadside furniture categories

`lane_furniture_categories` is a comma-separated string today (`"fence,cable,lane"`) and there
is nowhere in the UI that says what may go in it. The valid set is fixed and small — the bake
declares exactly three: `lane`, `fence`, `cable`. **Three checkboxes**, labelled Fences and
hedges / Overhead cables / Kerbside details, written back as the same string the CLI takes.

### 3.6 Area — a map, not three numbers

`region` is `"X Z R"` in bundle metres. Nobody knows their city's coordinates.

The bundle already ships everything needed to draw the city: `terrain/heightmap.png` plus a
`rasters` block giving its resolution, size and world origin — so pixel ↔ world is a documented
conversion the reader already performs. **The control is a map**: the heightmap as a grey
relief, road centrelines over it (the same lane data the navgraph stage reads), a draggable
circle, and the numbers shown underneath for anyone who wants to type them. "The whole city"
is the default and the circle only appears on "Part of it".

The same map view is reused twice more (§3.7, §4), which is what justifies building it.

**ONE map component, built once (decided 2026-08-20).** The region picker and the fast-travel
editor are the same drawing surface with two different things on top of it, and they are to be
built as one — the heightmap relief, the road centrelines, and a layer of markers over it. The
region picker's marker is a draggable centre point with its radius drawn round it, and both the
centre and the radius read back as numbers underneath; the fast-travel editor's markers are the
points, snapped to the nearest road. **Neither ships before the other**: §3.6 waits for §3.7 so
that the map is written once, for both, rather than a circle widget now and a point widget
later that share nothing. Until then `region` stays the three labelled numbers it is today.

### 3.7 Fast-travel points — an editor, not a JSON path

`poi_file` currently expects a path to a file the user writes by hand, in a format documented
only in the setting's help text. It leaves the UI. In its place, in the Navigation section:

* a **list** of points, each with a name, a position and a description;
* **Add** drops a point at the middle of the map view and lets you drag it onto a road (the
  existing snap already puts it on the nearest road);
* the derived points (`poi_max`, from the densest road cells) are shown greyed in the same list
  as "suggested", and *Use this* promotes one into your own list;
* the list is stored in the app's own folder and passed to the build as `poi_file`, so the CLI
  contract does not change at all.

---

## 4. "Will this run on my machine?" — the live meter

The centrepiece, and the part with real engineering behind it. It must never be a guess dressed
as a measurement.

### 4.1 What we can honestly say

| Figure | How it is obtained | Confidence |
| --- | --- | --- |
| **Video memory** — textures | **Computed.** Every texture's dimensions come from the PNG header (33 bytes per file); the cap is the setting; the format per role is fixed (BC7 colour, BC5 normal, BC4 data) and mips add a third. This is the same arithmetic the DDS writer performs — not a model of it. | exact |
| **Video memory** — meshes | **Calibrated.** Which shapes ship follows from the kind switches, the density sliders and the variant counts, all of which the census already counts. Bytes per shipped shape come from the bundle's own mesh sizes times a ratio measured from real builds. | calibrated |
| **Drawn detail** — triangles at once | **Calibrated**, per kind, against view distance and the LOD settings. | calibrated, coarse |
| **Build time** | Already exists: census unit counts × the per-stage rates in `history.json`. | calibrated |
| **Disk** | Computed from the same two halves. | good |
| **System RAM** | **We have never measured it.** See §4.4. | — |

### 4.2 The calibration loop

`history.json` already does exactly this for build *time*: ship defaults measured on the
reference city, then blend in the user's own results after each build. The size estimator gets
the same treatment, in the same file:

1. Ship coefficients measured on the reference city.
2. After every build, `cs2beamng budget` already measures the level's real resident bytes and
   drawn triangles — feed those, with the settings that produced them, back into the EMA.
3. The build report and the UI both show **estimated vs actual** afterwards
   ("estimated 4.1 GB, actual 4.27 GB"). That line is what keeps the estimator honest: an
   estimator nobody scores is an estimator nobody can trust.

### 4.3 What it looks like

A three-row block, always visible, updating on every settings change (debounced like the census):

```
Video memory  ███████████████░░░░░  4.1 GB      your card fits 5.2 GB     ✓
Drawn detail  ████████░░░░░░░░░░░░  1.8M triangles                         ✓
Build ~1 h 40 m  ·  Disk 5.2 GB  ·  estimated for your GeForce RTX 3070 at 2560×1440
```

* the budget mark is `resident_budget_mib()` for the detected card and resolution — the same
  function the validator uses, so the meter and the build's own verdict cannot disagree;
* three bands: fits / close / over. Over turns the bar red and adds one sentence in plain words
  ("This is more than your card can hold. The level will stutter and get worse the longer you
  play.") plus a *What should I change?* link that jumps to the section with the biggest lever, which
  the sidebar has already marked;
* it **warns, never blocks** — unlike Doom Eternal, because a user may deliberately be building
  for a different machine, which is exactly what the three "machine" settings in Advanced are
  for;
* a small caption states the basis: *computed*, *from your last 4 builds*, or *from the
  reference city*. Never a bare number.

### 4.4 The RAM question — the one thing asked for that this plan does not promise

Nothing in the app has ever measured BeamNG's system-memory use, and there is no honest way to
put a number on screen without one. Two steps, in order:

1. **Find out whether it can be read.** BeamNG's own log is already parsed for the card and the
   render resolution; the first task is to check whether it also reports process memory. If it
   does, record it per build and the meter gains a real third bar.
2. **If it does not**, the meter ships with *Disk* in that slot and says nothing about RAM,
   because "resident assets × 1.4" would be an invented number and this project does not ship
   those. The estimate would be indistinguishable from a measurement on screen and wrong in a
   way nobody could catch.

Video memory is the number that decides whether a level runs — it is what the audit's failure
threshold is expressed in and what every game's meter shows — so the meter is useful from day
one either way.

---

## 5. Plain language

Every user-facing string is rewritten. The rules:

* **No identifiers.** Not `LevelInfo.visibleDistance`, `m_MinLod`, `colorSet`, `DecalRoad`,
  `bb_autobillboard`, `.cdae`, `TSStatic`.
* **No references to our own research.** No section numbers, no "MEASURED over 1.16M
  placements", no load numbers, no "DECLARED TUNING", no dates.
* **Say what changes in the world, then what it costs.** "Distant objects keep their detail for
  longer. Sharper, and heavier on your graphics card."
* **One line, ≤ 100 characters.** If it needs more, the setting belongs in the Advanced section
  and the detail belongs in the docs.
* **The label never repeats itself in its own description** (the rule already built for this
  stays, but should have nothing left to catch).

| Setting | Now | Becomes |
| --- | --- | --- |
| `view_distance` | "LevelInfo.visibleDistance: the global far cull in metres" | How far you can see, in metres. The biggest single thing you can change for speed. |
| `ai_roads` | "The invisible road network the AI, the minimap and route-finding use. Off = no minimap and no routes" | Lets the minimap, route markers and traffic use the roads. Turning this off leaves the map blank. |
| `lod_switch_error_px` | "How many pixels of surface error a generated LOD may show when it takes over…" | How fussy the level is about distant detail. Lower looks better and runs slower. |
| `imposters` | "Engine-generated billboard imposters (bb_autobillboard)" | Draw far-away objects as flat pictures. Much faster, slightly flatter. |
| `terrain_upsample` | "Terrain grid: 2 = 8192 at 1.75 m (the game's own near-camera detail); 1 = 4096 at 3.5 m, lighter" | Ground detail. Higher is sharper underfoot and uses more video memory. |
| `texture_dds` | "Ship textures pre-compressed as DDS so BeamNG never cooks them on first load; off = PNG" | Prepare textures during the build instead of the first time you play. Leave this on. |
| `cook` | "Compile the shapes with BeamNG's own compiler after the build, so the first load is fast" | Prepare the level for BeamNG after building. Adds minutes now, saves many later. |
| `foliage_road_clearance` | "Declared deviation: drop a tree standing this close to a lane centre so the city can be driven. 0 = off" | Clear trees this close to the road so you can drive past them. |
| `paint_variants_tree` | "How many baked colour variants a TREE prefab gets. Each one is another shape and material set" | How much colour variety the trees have. More is prettier and uses more memory. |

**Where the real explanation goes.** The measured arguments — what was tried, what it cost, why
the default is the default — are not deleted and are not softened. They move out of the UI
entirely and live in `docs/settings.md`, which is already generated from `DETAILS` and is
already gated by CI. The *why* disclosure disappears from the settings panel; the sidebar
carries one permanent link to that page. Someone tuning a build wants a page they can read and search, not 79 popovers.

---

## 6. Behaviour changes this implies

These are real changes, not presentation, so each is called out:

1. **`terrain_cut` follows `networks`.** With roads off, the ground is no longer cut open for
   trenches and tunnel mouths — which is correct, since there is nothing in the hole. The CLI
   can still set them independently.
2. **Density 0 means off.** `foliage_density: 0` writes `trees: false` too, so the stage skips
   rather than processing 376,187 items and keeping none. Needs a check that 0 is handled the
   same way everywhere it is read.
3. **`beamng_exe` is promoted** from the Advanced section to the basics strip.
4. **`poi_file` becomes app-managed.** The app writes the file; the setting still points at it.
5. **`ui_advanced` retires**, replaced by `ui_section`.
6. **The `why` disclosure is removed from the UI.** `DETAILS` stays, and stays generated into
   the docs.

---

## 7. Build order and gates

| Step | What | Gate |
| --- | --- | --- |
| **E1** ✅ | Settings metadata: `SECTIONS` (id, group, label, keys), and per key a plain `label`, a plain `blurb` and a control kind. This becomes the UI's source of truth; `GROUPS` stays for the docs. | Test: every key in exactly one section; every key has a label and a blurb; blurb ≤ 100 chars; no blurb contains an identifier, a `§`, or a research figure (a word-list check, run in CI) |
| **E2** ✅ | The shell: basics strip, cost meter, preset row, the data-driven sidebar (groups, badges, search, keyboard, the narrow-window fallback), flat lists, no accordions. The 7fr/5fr rebalance. | The screen is looked at; a section added to `SECTIONS` in a scratch branch appears with no other edit |
| **E3** | Real controls: furniture checkboxes, the map view, the region picker, the fast-travel editor. | A region picked on the map builds the same level as the equivalent `--set region=` |
| **E4** | The estimator (`estimate.py`) and the meter. | `tools/test_estimate.py`: the texture half is exact against a real build's DDS bytes; the mesh half is within a stated tolerance of the reference city's measured resident bytes; and every figure carries a basis |
| **E5** | The plain-language rewrite; `DETAILS` out of the UI and into the docs. | The word-list check from E1, plus `gen_docs --check` |
| **E6** | The behaviour changes of §6. | A build with `foliage_density=0` ships no forest and skips the stage; a build with `networks=false` has no cut |
| **E7** | Docs and site: `docs/settings.md` regenerated with the new labels, the site's command builder updated. | CI |

E1 and E2 are one sitting. E4 is the biggest piece and is independent of E3 — it can land first
if the meter matters more than the map.

### E1 and E2 — BUILT 2026-08-20

`SECTIONS`, `BASICS`, `DRIVEN_BY`, `LABELS`, `BLURBS`, `CONTROLS`, `RANGES` in `settings.py`;
the shell, sidebar, search, badges, controls and meter frame in `ui/`. The partition came out
exactly as this plan predicted it — basics 6 · World 14 · Collision 9 · Textures 10 ·
Navigation 4 · Advanced 30 · driven 6 = 79 — and `tools/test_app_shell.py` gates all of it
(178 checks): the partition, the ≤ 100-character blurb, the label-echo rule, and a mechanical
**word-list check** that rejects an identifier, a file extension, a section mark, a research
word or a figure in any label or blurb.

**What a build found that the plan did not predict.** The sidebar's badge counts settings that
differ from the preset last applied — and on a *fresh install* it showed **one**. The shipped
default for `lod_switch_error_px` is 8.0 and the `high` preset set 6.0, so the app had always
opened claiming a preset it was not on. 8.0 is the value the reference city was built and
validated at through loads #11–#17; 6.0 was never built. The preset now says 8.0, and a new
gate asserts *the shipped defaults ARE the preset `quality_preset` names*.

**Seven decisions this plan left open, and what was decided:**

1. **"Custom" is derived, not stored.** `quality_preset` stays the name of the preset last
   applied and the payload carries `presetChanged`, the keys that have moved off it. That is one
   less piece of state to keep in step, and it is what the badges count — without it a preset
   the user has hand-edited has no baseline to compare against at all.
2. **The reference is a pane, not a link.** §5 puts one permanent link to `docs/settings.md` in
   the sidebar foot. A frozen build ships no `docs/`, so the link would be dead in exactly the
   distribution most people get. The foot instead opens an in-app **Every setting** pane built
   from the same metadata the controls are — key, plain label, blurb, default, and the long
   form out of `DETAILS`. It cannot go stale against the controls, and `docs/settings.md`
   is still generated and still gated.
3. **The `why` disclosure came out in E2, not E5**, because building that pane is what replaces
   it. E5 keeps the other half: the docs regeneration and the CI word-list gate.
4. **The narrow-window fallback triggers at 1,000 px of window**, not the 900 px of *panel*
   §2.1 names — at the default 1,280-wide window the panel is ~700 px, which this plan
   elsewhere describes as the normal layout, so a 900 px panel threshold would have collapsed
   the nav essentially always. 1,000 px of window is where the whole layout already stacks.
5. **`ui_advanced` is gone** and `ui_section` replaces it, with its choices *derived from
   `SECTIONS`* so that adding a section really is one data change. The test adds a `weather`
   section at runtime and reads it back out — E2's gate, minus the scratch branch.
6. **The meter draws only the rows it can fill.** Today that is the expected build time and the
   card read-out; the bars arrive with E4's `estimate` block, which the renderer already reads.
   An empty bar would be a promise the app cannot keep. Build time now comes from one function
   the meter and the Build card share, after they were caught printing "unknown" and "3m 35s"
   at the same moment from two different sources.
7. **`tod_hour` and `lane_furniture_categories` got their real controls early** (a rip-time /
   pick-a-time choice, and the three checkboxes of §3.5) because both were free once the
   control-kind indirection existed. §3.5 is therefore **done**; §3.6 and §3.7 are still E3.

### E1 and E2, second pass — reviewed 2026-08-20

Eleven changes off a read of the built screen. Six of them change what this plan said:

1. **`region` leaves the basics strip and heads the World section**, which §8 flagged as worth
   revisiting the moment the screen existed. It is a decision about the level, not a path the
   app needs from you, and the strip is for the latter. Its default is and stays the whole city.
2. **A subset is drawn UNDER the thing it belongs to**, indented behind a rule line, and
   switching the last of a parent's switches off switches the parent off. `SUBSET_OF` in the
   metadata drives the indent, the greying and that rule; `NEEDS` is the weaker relation, for a
   pair that gate each other from different sections (`lights_max` under `lights`) and so can
   grey out but cannot indent. Clearing all three roadside-furniture kinds now turns Roadside
   furniture off; clearing all seven collision switches sets collision to Nothing solid. It
   runs in the UI, where the boxes are — `--set` stays literal, so a headless build still does
   exactly and only what it was told.
3. **Custom is always on the preset row, and always remembered.** `~/.cs2beamng/custom.json`
   holds the user's own values and the preset they came from, so looking at what Performance
   does costs nothing and loses nothing. That file is also what makes the badges work after a
   hand-edit: once `quality_preset` reads "custom" there is no base to count against, and the
   base is now kept beside the values rather than in place of them. This supersedes the
   first pass's decision 1 — "custom" is stored again, but so is what it came from.
4. **Every quality setting change puts the row on Custom**, not only the ones a preset happens
   to name. Before this, changing `terrain_holes` or `tile_size` left the row reading "High"
   while the level was no longer what High builds. The basics strip is excluded: naming your
   level folder is not a change of quality.
5. **Advanced keeps its jargon, on purpose.** §5's "no identifiers" rule was applied to all
   seventy-nine settings and it made the Advanced section worse, not better: "Only for things
   seen beyond" says less than *Imposter minimum cull distance*, and somebody changing an LOD
   switch budget needs to be told that is what they are changing. The section carries
   `"technical": True` and the plain-language gate is scoped to the other four. The length
   rule, the sentence rule and the label-echo rule still apply there.
6. **No description may refer to another control.** "Zero uses the number above", "The same
   again for benches and signs" and "Follows the roads switch" all read fine in a list and mean
   nothing when the setting is reached through search — which this redesign made a first-class
   route into the settings. A new gate rejects thirteen such phrases across every section.

And five that this plan asked for and had not been done: **every number says what it is measured
in** (`UNITS`, and a gate that every number control outside the basics strip has one);
**`paint_kinds` became checkboxes** drawn from the bundle's own record kinds, which was the last
free-text box left where the valid answers are a fixed set, and a gate now forbids another;
**the cost meter was rebuilt** as a titled block of stat tiles with the target machine in its
header and the basis under every figure, in place of one grey run-on line; **`beamng_exe` is
filled in when it is found**, like the bundle and the levels folder, instead of leaving an empty
box beside a line saying it had been found; and **§3.6's map is deferred to §3.7's**, above.

---

## 8. Risks and open questions

* **The estimator is the risk.** A meter that says "fits" and then stutters is worse than no
  meter, because it is a promise. Mitigations: the texture half is computed rather than
  modelled; the mesh half is calibrated from the user's own builds; every figure states its
  basis; estimated-vs-actual is shown after every build; the CI gate scores it against a real
  measurement. If the mesh half cannot be made to hold within tolerance, ship the texture bar
  and the build time alone and say so — a smaller honest meter beats a complete dishonest one.
* **The map view is the scope risk.** It is a heightmap, a set of centrelines and a circle, but
  it is also the first drawing surface in this UI. If E3 runs long, the region picker can ship
  as three labelled number fields with the map following later; the fast-travel editor cannot —
  a list of named points with coordinates is still far better than a JSON path.
* **The sidebar has to earn its width.** It costs ~170 px of a panel that was already tight, which is why the layout rebalances to 7fr/5fr and why the narrow-window fallback exists. If the content pane still feels cramped in practice, the next move is the three-column layout (bundle/level left, settings centre, build/log right) rather than taking the nav away.
* **Does turning trees off at 0 % surprise anyone?** It is what was asked for and it is what a
  slider at zero should mean. The label reads "Trees — off" at 0 rather than "0 %".
* **`region` on the basics strip or in the World section?** It is on the strip in this plan because it
  changes the build more than any other single input, and because the meter needs it. Worth
  revisiting once the map exists.

---

## Sources

* [Tabs UX Best Practices: Examples and Common Mistakes — Eleken](https://www.eleken.co/blog-posts/tabs-ux)
* [Alternatives for tabs in UI design — Design for Ducks](https://designforducks.com/alternative-for-tabs-in-ui-design/)
* [User and workspace settings (the Settings editor's search-led model) — VS Code](https://code.visualstudio.com/docs/getstarted/settings)
* [Frustrated by System Settings? Use the View Menu or Search — GeekHampton](https://www.geekhampton.com/tips-and-tricks/2023/9/27/frustrated-by-system-settings-use-the-view-menu-or-search)
* [What is Progressive Disclosure? — Interaction Design Foundation](https://ixdf.org/literature/topics/progressive-disclosure)
* [What Is Progressive Disclosure in UX? — UXPin](https://www.uxpin.com/studio/blog/what-is-progressive-disclosure/)
* [Progressive disclosure in UX design: types and use cases — LogRocket](https://blog.logrocket.com/ux-design/progressive-disclosure-ux-types-use-cases/)
* [That VRAM usage bar on the graphics settings menu is genius — GTA V board](https://gamefaqs.gamespot.com/boards/805606-grand-theft-auto-v/71629355)
* [How to Play Doom Eternal on Integrated Graphics — Tom's Hardware](https://www.tomshardware.com/features/how-to-play-doom-eternal-on-integrated-graphics)
