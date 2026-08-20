# A7 — navigation: the minimap, the spawn, and fast travel

> **STATUS — CLOSED, VALIDATED IN GAME 2026-08-20.** All seven items were built (audit §84), and
> the loads after that found two more — both fixed and measured: `map.lua` crashed on a triangle
> we shipped (**§85**, A7.5), and the route ran onto an overpass with no ramp (**§86**, A7.6).
> Naomi loaded the 2026-08-19 22:15 build and reported the navigation good.
>
> **The two PRODUCER changes are still "built, awaiting in-game re-rip"**, and no consumer load
> can settle them: `leftHandTraffic` and `districts.jsonl` need a fresh rip, not a fresh build.
> They are the only thing this plan leaves open.
>
> **Read "AS BUILT" at the foot of this file before this text** — the junction rule W1 proposes
> was built, measured and replaced, and three properties of the lane layer this plan did not know
> about (`SideConnection`, `Invert`, longitudinal chaining) are what the implementation actually
> turns on.

Three symptoms reported on the 2026-08-19 load of `cs2_city_fine`:

1. **The minimap does not work** — it draws nothing.
2. **The spawn puts us inside a building.** — **RETRACTED by Naomi, same day: not a defect.**
   It was the building rotation (§78), now fixed. §A7.3 records the mis-diagnosis rather than
   deleting it.
3. **We cannot set a waypoint and have it route along the roads**, and there are no
   fast-travel POIs for the places worth going to (the keys bottom-left, the east-city
   beach, the town left of the lake).

**Symptoms 1 and 3 are one defect.** BeamNG's minimap app, its big-map route line and its
ground-marker arrows all read the same structure — `map.getMap().nodes`, the AI navgraph —
and **this level ships no navgraph at all**. Symptom 2 turned out not to be a defect at all.
The fast-travel points are a second, additive piece with an exact vanilla contract.

Everything below is measured against BeamNG's own source in
`G:\SteamLibrary\steamapps\common\BeamNG.drive\lua\ge\` and its shipped levels, and against
the 2026-08-19 16:28 bundle. **Nothing here invents a consumer-side rule**; every emitted
field is one a vanilla level already emits.

---

## A7.0 The root cause — the level has no navgraph

### What is measured

`cs2_city_fine/main/**/items.level.json`, every class in the level:

| class | count |
|---|---|
| SpotLight | 25,000 |
| TSStatic | 19,593 |
| WaterBlock | 190 |
| SimGroup | 114 |
| WaterPlane / TimeOfDay / TerrainBlock / SpawnSphere / ScatterSky / LevelInfo / Forest | 1 each |

**No `DecalRoad`. No `BeamNGWaypoint`. No `map.json`.** Those are the only three things
`lua/ge/map.lua` reads (`loadJsonDecalMap`, lines 567–915):

* `scenetree.findClassObjects('BeamNGWaypoint')` → manual nodes;
* `scenetree.findClassObjects('DecalRoad')`, kept when `road.drivability > 0` → road polylines;
* `<levelDir>/map.json` → manual segments over waypoints that must **already exist as scene
  objects** (`"the manual waypoint (beamNGWaypoint) … does not exist"`, map.lua:881).

Our roads are `TSStatic` meshes in `Networks/`. `map.lua` has never looked at a TSStatic and
never will. The graph is empty, so:

* `ui/apps/minimap/roads.lua:17` — `local mapNodes = map.getMap().nodes`, iterates
  `n.links`, builds the kd-tree, `hasRoads = lIdx > 1`. Empty graph → **nothing drawn**. The
  minimap app draws roads as *vectors from the navgraph*, not from an image; that is why no
  amount of texture work would have fixed it.
* `core/groundMarkers.lua:283` — `if not map.getMap().nodes[w]`. No nodes → no route, so a
  waypoint can never "travel along the roads".
* `freeroam/bigMapMode.lua:239` — `local mapData = map.getMap()`.

### Scale, for calibration

`west_coast_usa` (2,048 m square) ships **12,752 `DecalRoad`s / 59,557 nodes**, of which
**1,299 are pure AI helpers** on `material: "road_invisible"` (mostly in
`MissionGroup/AIWaypointsGroup/`, alongside 826 `BeamNGWaypoint`s and a 21.7 kB `map.json`
for the overpasses and tunnels). So a navgraph of a few thousand invisible roads is not a
deviation — it is exactly what a BeamNG level is.

---

## A7.1 The navgraph — the rule is BeamNG's, and it is `DecalRoad` + `road_invisible`

### The rule, verbatim from `west_coast_usa/main/MissionGroup/AIWaypointsGroup/items.level.json`

```json
{"class":"DecalRoad","__parent":"AIWaypointsGroup",
 "position":[-295.67,544.82,74.79],
 "nodes":[[-295.67,544.82,74.79,7.5],[-272.68,532.09,74.80,15.0]],
 "material":"road_invisible","drivability":1,"improvedSpline":true,
 "oneWay":true,"lanesRight":0,"autoJunction":false,
 "useSubdivisions":false,"renderPriority":1,"detail":0.2,
 "distanceFade":[200,100],"startEndFade":[20,20],"textureLength":100}
```

A node is `[x, y, z, width]`. `road_invisible` is a level-local material
(`art/road/main.materials.json`) over a transparent DDS with
`"alphaTest": true, "alphaRef": 127, "castShadows": false`.

Four readings from `map.lua` that decide our whole design:

1. **`useSubdivisions: false` makes our polyline the navgraph, 1:1.** In that branch
   (map.lua:727–748) each `DecalRoad` node becomes exactly one map node at
   `road:getNodePosition(i)` with `radius = road:getNodeWidth(i) * 0.5`. With subdivisions ON
   the engine instead resamples and Ramer–Douglas–Peuckers the road's *edge* geometry
   (map.lua:641–690), which we do not want to be at the mercy of.
2. **`speedLimit` is in m/s, not km/h.** `createSpeedLimits` uses `baseValue = 19.444 -- 70
   km/h`, and vanilla `DecalRoad`s carry `"speedLimit":"11.18"` (= 25 mph). CS2's
   `attrs.limit` is km/h → **divide by 3.6**. `<= 0` means "auto-calculate" (map.lua:613).
3. **`autoLanes: false` + `lanesLeft`/`lanesRight` give an explicit lane string** and BeamNG
   derives `oneWay` from it, flipping direction if needed (map.lua:626–639). It applies
   `rules.rightHandDrive` itself, so we ship left/right counts and let it choose.
4. **The node merge is 3-D and radius-sized, and that is the trap.**
   `mergeOverlappingNodes` (map.lua:1188) broad-phases in 2-D but tests
   `n1.pos:squaredDistance(n2.pos) < square(max(r1, r2))` in **3-D**. A 24 m highway gives
   radius 12 m, so a bridge deck 8 m over a street would silently weld into it. That is
   exactly why `west_coast_usa`'s overpasses and tunnels are `autoJunction: false` in its
   `map.json`. `autoJunction: false` sets `noMerge`, which excludes a road's *interior* nodes
   from the fuzzy merge but still lets its two `endNode`s merge — first exactly
   (`mergeOverlappingNodes(true)`, tolerance 0.1 m and matching radii), then by radius.

### What the bundle gives us — measured on the 2026-08-19 16:28 rip

`networks.jsonl`, 8,259 segments:

| category | segments |
|---|---|
| road | 5,725 |
| highway | 943 |
| cable | 693 |
| train | 313 |
| pathway | 248 |
| pipe | 105 |
| taxiway | 91 |
| waterway | 81 |
| tram | 60 |

Every segment carries `attrs`: `name` (the street name, on 6,732), `category`, `lanes`,
`form`, `direction` (`oneway`/`twoway`), `limit` (km/h, on 7,461), `width`, `length`, and
`elevation`/`elevationStart`/`elevationEnd` on 2,103. **But only 970 records carry a
`centerline`** — the 7,289 with a mesh do not. The geometry has to come from `lanes.jsonl`.

`lanes.jsonl`, 680,629 records. The relevant ones:

* **131,618 `car` lanes.** 52,974 join a `road`/`highway` segment through `owner`
  (48,884 road + 4,090 highway, over **6,667 distinct segments**); 907 join a non-drivable
  network; and **77,737 (59.1 %) have no network owner at all** — these are the node-owned
  junction interiors and the lot-owned driveways.
* **269,759 `connection` lanes, and they are NOT the road junctions.** Every one of them is
  node-owned, and of 3,000 sampled road car-lane endpoints, **0.0 % had a `connection`
  endpoint within 5 cm** (0.2 % within 0.5 m). Sampled `connection` lanes sit at y ≈ 721–1612 m
  against the roads' 508–594 m: they are air corridors and transit links.
  **`lanegraph.py`'s docstring calls `connection` "the turning movements THROUGH a junction"
  — on this bundle that reading is wrong, and the module comment must be corrected.**

**The junctions are `car` lanes.** Of 2,000 sampled road car-lane endpoints, **62.1 % have
another `car` lane endpoint from a *different* owner within 5 cm** — against 3.9 % for
`other`, 1.9 % for `pedestrian`, 0.0 % for `connection`, `track` and `parking`. So the drivable
graph welds `car`-to-`car`, and the node-owned car lanes are the bridge across each junction.

And that is why segment centerlines alone will not do: derived per segment (car lanes oriented
to agree, resampled by arclength, averaged) and measured endpoint-to-nearest-other-endpoint,
the **median gap is 17.97 m** — one junction's width — with only 9.3 % inside 0.5 m.

### The work

**W1 — `tools/csiibundle/navgraph.py`, a new shared module** (both consumers; a route graph is
not a BeamNG idea). Bundle-frame in, bundle-frame out. It builds a *road-centerline* graph, not
a lane graph, because that is what BeamNG's navgraph is:

* **Edges.** For each of the 6,667 `road`/`highway` segments with car lanes: orient the
  segment's car lanes to a common direction, resample each by arclength, average → one
  centerline polyline. Widths from `attrs.width`.
* **Nodes.** Weld car-lane endpoints at a tolerance (sweep 1/5/10/25 cm and **report the
  welded fraction** — 62.1 % at 5 cm is the floor, not the target). A junction hub is the
  centroid of the endpoints welded into one cluster; extend each incident segment centerline to
  its hub so **every road meeting at a junction shares one exact coordinate**, and BeamNG's
  exact-merge pass does the rest without relying on the radius merge.
* **Counted approximations** (CLAUDE.md: declared and counted, in code and in the report):
  averaging opposing carriageways of a divided highway into one centerline; hub collapse, which
  discards turn restrictions; any lane whose endpoint welds to nothing.

**W2 — `apps/cs2beamng/stages/navgraph.py`, a new stage** behind the existing
`ai_roads` setting (declared in `settings.py:45` as "experimental" and **read by nothing today**
— `scene.py` lists it in `SETTINGS` and never uses it). Default it **on**. It emits, into a
`Navgraph` SimGroup:

| field | source |
|---|---|
| `nodes` | `[x, y, z, width]`, frame via `frame.to_beamng` |
| `material` | `road_invisible` (we ship the material + a 4×4 transparent DDS) |
| `drivability` | `road` → 1.0, `highway` → 1.0; `taxiway` behind a setting at 0.3; `pathway`/`tram`/`train`/`waterway`/`cable`/`pipe` excluded |
| `speedLimit` | `attrs.limit / 3.6` (km/h → m/s), omitted where absent |
| `oneWay`, `lanesLeft`, `lanesRight`, `autoLanes: false` | `attrs.direction` + `attrs.lanes` |
| `autoJunction` | `false` on any segment with `attrs.elevation*`, `true` elsewhere |
| `useSubdivisions` | `false`, always — see reading (1) |
| `improvedSpline`, `renderPriority`, `detail`, `distanceFade`, `startEndFade`, `textureLength` | copied from the vanilla AI-helper road verbatim |

Node width is the open knob: CS2's `attrs.width` is the faithful value but gives a 12 m merge
radius on a 5-lane highway. Ship `min(attrs.width, navgraph_max_width)` with the clamp
**declared and counted**, and gate the number on how many false welds appear (below).

**W3 — traffic side.** `Instance.City.leftHandTraffic` exists in the producer
(`Systems/NetworkSystem.cs:896`) and **is not in `bundle.json`**. Add it to the manifest —
a one-bool producer change, so it lands on the next rip. Until then the consumer infers it from
which side of the centerline a segment's forward lanes lie and **counts the inference**.

**W4 — `map.json` is deliberately not used in this phase.** It buys exact topology and costs a
`BeamNGWaypoint` scene object per node (`west_coast_usa` uses it for 826). Hold it in reserve
for stacked interchanges if W1's hubs plus `autoJunction: false` do not settle them.

### The gate

1. `beamng.log`: `map.lua` reports its node/edge counts on load. **Non-zero, and within 20 % of
   the offline count `navgraph.py` reports.** Zero means the roads were not seen at all.
2. **No `Road … centerline to edge distance exceeding 20m` warnings** (map.lua:656).
3. The minimap draws the road network, and it matches the roads under the car when driving.
4. **`tools/check_navgraph_vs_game.py`** — the harness this repo's convention asks for
   (cf. `check_terrain_vs_game.py`, `check_instances_vs_game.py`). Sample N points on CS2 car
   lanes, ask whether the emitted navgraph has an edge within tolerance, and report the miss
   rate by category. It is the offline oracle; the load is the confirmation.
5. **False-weld check:** for every pair of navgraph nodes the merge radius would join, report
   those whose vertical separation exceeds 3 m. Target zero; any survivor names a bridge that
   needs `autoJunction: false`.

---

## A7.2 The minimap image — secondary, and a different mechanism

The *minimap app* needs no image (A7.0). But the old dashboard/navigation map does:
`ui/uiNavi.lua:354` — `minimapFromTerrainBlock()` reads **`TerrainBlock.minimapImage`** and
builds `{size: [blockSize, blockSize, maxHeight], offset: [pos.x, pos.y + blockSize, 0], file}`.
`west_coast_usa` ships `"minimapImage":"levels/west_coast_usa/westcoast_minimap.png"`, a
**2048×2048 PNG**. `core/levels.lua:170` allows an `info.json` `minimap` array to override it —
and **no shipped level uses that**, so port the vanilla mechanism, not the override.

**The work:** the `scene` stage already renders `<name>_preview.png` at 512 px from the
heightmap and water raster (`_write_preview`, `scene.py:160`). Render a second, 2048 px
top-down image — terrain colour from the splat/albedo path the terrain stage already has,
water tinted, and the navgraph's roads drawn over it — write it as
`<name>_minimap.png`, and set `minimapImage` on the `TerrainBlock` in
`stages/terrain.py`. `offset` follows uiNavi's own formula; our TerrainBlock sits at
`[-7167.125, -7167.125, 0]` with a 14,336 m block.

**Gate:** the field resolves (no `beamng.log` texture error), and the image's north-up
orientation matches the car's position on it — the `+ blockSize` in uiNavi's `offset` is a
top-left convention and is the obvious place to get a flip.

---

## A7.3 The spawn — NOT A DEFECT, and this section is a retraction

**Naomi, 2026-08-19: the spawn is fine. The clipping was the building rotation — audit §78,
`rotationMatrix` is basis vectors and we were writing the transpose — which is already fixed
(in `beamng/frame.py`, uncommitted at the time of writing).**

This plan's first draft called the spawn a defect and it was wrong. What it measured is true and
what it concluded from it is not:

* True: the spawn sits on lane owner `1126631`, which is **not in `networks.jsonl`** — one of
  the 77,737 master `car` lanes (59.1 %) with no network segment behind it. `_pick_spawn`
  (`scene.py:201`) filters on `kind == "car" and not secondary` and nothing else.
* True: within 40 m the bundle holds 156 instances, nearest `TrashContainerEmpty01` at 3.0 m,
  `Trashbag04` at 3.1 m, `NA_MailboxStand03` at 6.2 m, `WallLight03` at 8.4 m.
* **False: that this is "a service courtyard between buildings".** Trash containers, mailboxes,
  wall lights, `GardenBench02`, `Parasol03` and `TableSet01` are exactly the dressing of a
  normal urban street. The reading was the artifact talking, not the data.

The car appeared to be inside a building because **the buildings were 14.24° off their own
roads** and one of them was standing over the street. Fix the rotation and the spawn is on a
street, which is where `_pick_spawn` put it.

**No work. Do not "harden" the spawn picker.** A stricter candidate set would be a consumer-side
rule invented to close an artifact that had a different cause — the exact failure mode CLAUDE.md
names. If a future spawn genuinely lands somewhere wrong, the fix starts from that measurement,
not from this one.

**What survives into A7.4:** the *snapping* rule a POI needs (put a named point on a road) is
still needed, and it is written there. It is a placement rule for new points, not a correction
to the existing spawn.

---

## A7.4 Fast-travel POIs — the contract is exact, and we already emit half of it

### The rule, from `core/levels.lua:525–553`

`onGetRawPoiListForLevel` walks `levelInfo.spawnPoints`, and for each entry with an
`objectname` that `scenetree.findObject` resolves, emits a POI with
`icon = "poi_fasttravel_round_orange"` and
`quickTravelPosRotFunction = getSpawnPointPosRot`. `freeroam/bigMapPoiProvider.lua:275` groups
them under **`type_spawnPoint = {label = "Quicktravel Points", icon = "fastTravel"}`**. That is
the feature asked for, and it is driven entirely by data a level ships.

`west_coast_usa` ships nine of them:

```json
{"translationId":"levels.west_coast_usa.spawnpoints.industrial",
 "description":"levels.west_coast_usa.spawnpoints.industrial.description",
 "logbookEntry":"…","objectname":"spawns_industrial","preview":"spawns_industrial.jpg"}
```

`objectname` must name a real scene object — a `SpawnSphere` — and the quick-travel rotation is
`quat(0,0,1,0) * spawnPoint:getRotation()`, the same convention `frame.heading_matrix` already
encodes for `spawn_city`.

We ship **one** spawn point today. Nothing else is missing: no new file format, no Lua.

### The work, in three tiers

**Tier 1 — a user-authored POI list, and it is the one that answers the actual request.**
The places named ("the keys bottom-left", "the east city beach", "the town left of the lake")
are Naomi's names for her own city; no algorithm will produce them. Add a setting
`poi_file` pointing at a small JSON list:

```json
[{"name": "Vice City Beach", "at": [4820, 1660], "description": "East city beach"}]
```

`at` is a bundle-frame or BeamNG-frame XY (declare which; BeamNG-frame is what the user can read
off the big map). The stage snaps each point to the nearest lane owned by a `road` network
segment — the join A7.1's W1 already builds, which is what keeps a POI off a driveway —
emits a `SpawnSphere` named `spawn_<slug>` and an `info.json` `spawnPoints` entry. Ten of these
is ten lines of JSON and it is the whole feature.

**Tier 2 — derive a default set, so a fresh build is not empty.** Cluster the drivable network
by connectivity and distance; keep clusters over a size threshold; name each by the **most
common `attrs.name`** among its segments (6,732 segments carry a street name) or the strongest
`attrs.brand`/`attrs.zone` among nearby `instances.jsonl` buildings — those records carry
`address`, `brand`, `zone` and `zoning` already. Cap the count (vanilla ships 3–9) and pick the
seeds by settlement size, so the result is "Hyacinth Street", "Pebblebrook Highway" — legible,
if not evocative.

**Tier 3 — districts, and this is the faithful answer, but it needs a rip.** CS2 has real,
player-named districts, and **Carto already reads them** for its vector export:
`Systems/AreaSystem.cs` uses `District`, `CurrentDistrict` and `GetLabelName` (lines 129–1073).
They do not reach the bundle — `areas.jsonl`'s 27,156 records carry only `prefab`, `nodes` and
`triangles`, no `kind` and no `name`. Emit `districts.jsonl` (name + polygon + centroid) from
the producer, take schema to 3 for the new layer, and Tier 2's naming becomes the city's own
district names.

**Do not build Tier 3 first.** It is a producer change, so it is a description of code that has
not been ripped yet; Tiers 1 and 2 ship against the bundle on disk today.

### The gate

`validate_level.py` already checks `info.json`'s `defaultSpawnPointName` against the
`SpawnSphere` set (line 430). Extend it: **every** `spawnPoints[].objectname` must resolve to a
`SpawnSphere`, and every `preview` must exist — `levels.lua:147` silently substitutes the level
preview when it does not, so a broken path shows as a plausible-looking wrong thumbnail rather
than an error. In game: the big map's "Quicktravel Points" group lists every POI, and selecting
one puts the car on the road there facing along it.

---

## A7.5 Routing to a waypoint — it falls out of A7.1, and it is worth gating separately

`core/groundMarkers.lua` builds its route from `map.getMap().nodes` and nothing else, so A7.1
delivers this. But "a route exists" and "the route is drivable" are different claims, and the
second is the one that was asked for. Gate it in game:

1. Set a waypoint on the big map across town. A line is drawn, and it follows roads rather than
   cutting across terrain.
2. The ground-marker arrows appear and stay on the carriageway through junctions.
3. Follow it end to end: no route through a building, no route down a bridge's side into the
   street beneath it (that is the 3-D merge of reading (4), and it is the failure to watch for).
4. Spawn traffic (`gameplay/traffic.lua` also runs on the navgraph) and check the AI keeps to
   its side. Wrong side across the whole city means the W3 traffic-side inference is inverted.

---

## Order, and what each step needs

| # | Item | Needs a re-rip? | Blocks |
|---|---|---|---|
| 1 | A7.1 W1 — `csiibundle/navgraph.py` + `check_navgraph_vs_game.py` | no | everything |
| 2 | A7.1 W2 — the `navgraph` stage, `ai_roads` on by default | no | minimap, routing |
| 3 | A7.4 Tier 1 — `poi_file` | no | — |
| 4 | A7.2 — `minimapImage` | no | — |
| 5 | A7.4 Tier 2 — derived POI names | no | — |
| 6 | A7.1 W3 — `leftHandTraffic` in the manifest | **yes** | correct lane sides |
| 7 | A7.4 Tier 3 — `districts.jsonl` | **yes** | good POI names |

Items 1–5 are consumer-only and testable against the bundle on disk. **6 and 7 are producer
changes: they are "built, awaiting in-game re-rip" the moment they compile, and no result from
the current bundle can validate them.**

Per `rebuild-the-level-before-committing`: after each consumer change, rebuild `cs2_city_fine`
in place with cook on (~19 min at the current settings, `cs2beamng_report.json` holds them) and
let Naomi validate in game **before** the commit.

### A correction this plan owes the codebase

`tools/csiibundle/lanegraph.py`'s module docstring says `connection` lanes are "the turning
movements THROUGH a junction — the connections that make the graph a graph". Measured above:
on this bundle 0.0 % of road car-lane endpoints meet a `connection` endpoint within 5 cm, and
62.1 % meet another `car` endpoint. **The junctions are `car` lanes**; `connection` is
something else (its sampled members sit 200–1,000 m above the roads). Fix the docstring in
step 1 — a wrong claim in the module a new consumer reads first is how the next five days get
spent.

---

## AS BUILT — 2026-08-19, all seven items (audit §84)

Every item in the order table above is built. What follows records where the plan was right,
where it was wrong, and what the code actually does, so the plan and the repo do not drift.

### 1–2. The navgraph — `csiibundle/navgraph.py` + the `navgraph` stage

Built as planned in shape, **not** in the junction rule.

**W1's hub collapse is not in the code.** The plan said: weld car-lane endpoints, take the
centroid of each cluster as a hub, extend every incident centerline into it. It was built first
and measured out. Three findings, in the order they arrived:

1. Matching a segment's averaged **centerline endpoint** to a junction node matched **7.9 %** of
   segment ends. A centerline endpoint is in the middle of the carriageway; a junction lane's
   endpoint is on a lane. Median gap 4.51 m. The match has to be per LANE — and then it is
   **90.8 %** at 5 cm and flat out to 50 cm, so the tolerance sweep the plan asked for found
   nothing to tune.
2. Clustering by shared junction *owner* over-merges: hubs chain into blobs with a **median
   horizontal extent of 33 m and a maximum of 896 m**, and 1,422 of them over 60 m. One motorway
   interchange becomes a star of half-kilometre straight lines. Bridging per LANE instead is
   better but still leaves a 33 m median and an 896 m maximum.
3. So there is no hub at all. Each **junction lane** becomes its own connector road whose two
   terminal nodes are set to the two segments' own centerline ends — the join is exact by
   construction, and **CS2's turn restrictions survive as graph topology** instead of being
   collapsed away, which the plan listed as an accepted approximation and is now not one.

**Three things the plan did not know about the lane layer**, each of which would have produced a
plausible wrong graph, and each answered by a flag the game itself sets:

* **`SideConnection`** — 25,811 of the 52,974 segment-owned car lanes are driveway stubs. The plan
  would have averaged them into the carriageway. The flag is the game's answer; a length threshold
  would have been ours.
* **`Invert`** — a lane's direction relative to its segment. It is what makes `lanesLeft` /
  `lanesRight` a read rather than a guess, and what orients the spine.
* **Longitudinal chaining** — a segment's lanes are split at interior nodes as well as stacked
  laterally. A 120 m 4-lane road ships twelve lanes: six lateral offsets across one midpoint
  split. The centerline is built from the longest **chain** of welded lanes plus one lateral
  offset, not from averaging polylines.

**Direct joints**, which the plan did not anticipate: 1,383 places where two segments abut with no
turning movement between them (an asset change, a bridge abutment). Without them the graph falls
into 133 components instead of 35.

Emitted: **6,667 segment roads + 9,569 connectors + 1,383 joints = 17,619 `DecalRoad`s, 56,012
nodes, 96.65 % in one connected component.** Every vanilla field copied verbatim from
`west_coast_usa`'s AI-helper roads, `useSubdivisions: false`, `speedLimit` in m/s, and
`autoJunction: false` on every elevated or tunnel segment.

**Node width is not the open knob the plan expected.** It is measured from the lateral spread of
the segment's own carriageway lanes plus one half-lane, rather than taken from `attrs.width` (which
includes the pavements). `navgraph_max_width` exists and defaults to **0 — no clamp** — because the
false-weld check finds nothing to clamp: 2,936 node pairs sit inside a merge radius and **none**
across more than 3 m of height.

### 3 + 7. Fast travel — `poi` stage, and the tiers reordered

Tier 1 (`poi_file`) and tier 2 (derived) as planned. **Tier 3 shipped too**, because item 7's
producer change is in the same commit: where a rip carries `districts.jsonl` the stage uses CS2's
own district names ahead of anything derived, and where it does not it says so in its report
rather than quietly falling back.

The derived tier names a point after the **street name the `navgraph` stage rode along on the
road** as a Torque dynamic field (`cs2Street`), not after the road's own `seg_<id>` name. A
fast-travel point called "seg_2331850" is worse than none.

### 4. The minimap image — moved into the `terrain` stage

The plan put it in `scene`. It belongs in `terrain`: that stage already holds the heightmap, the
dominant-ground index and the water raster, and it owns the `TerrainBlock` the `minimapImage` field
goes on. A later stage mutating another stage's object dict would be lost the moment the terrain
stage was skipped and replayed. 2048 px, ground colour by each ground texture's mean albedo,
hillshaded, water tinted.

### 6. `leftHandTraffic` — shipped, and the consumer no longer needs it

`bundle.json` now carries it (**built, awaiting in-game re-rip**). But the consumer-side inference
the plan called a fallback turned out to be a **measurement**, not a guess: the lanes running with
a two-way segment sit to one side of the ones running against it, with the sign convention
calibrated against CS2's own `TurnRight`/`TurnLeft` flags. The vote is 5,735 to 1. A first attempt
that inferred it from turn *lengths* was discarded on measurement (9.88 m against 9.82 m — no
signal).

### The correction the plan owed the codebase, corrected

The plan said `lanegraph.py`'s module docstring calls `connection` lanes the turning movements.
It does not — it says *node-owned* lanes are, which is correct and is now confirmed (40,708
node-owned `car` lanes bridge two road segments within 5 cm). The wrong claim was the comment on
`DRIVABLE_KINDS`. Both have been rewritten, and the docstring now states the owner/kind
distinction explicitly instead of leaving it to be re-derived.

### Versioning — the plan was wrong and §12 already said so

The plan said `districts.jsonl` "needs schema 3". It does not. §12: *"Within `schema: 1`, the
producer may … add a new layer key, or add a new optional manifest key that a v1 consumer can
safely ignore."* Both `layers.districts` and `leftHandTraffic` are exactly those cases. Recorded in
`docs/bundle-schema.md` §12.3.

### What is measured, and what still needs a load

Measured offline, against the bundle on disk:

| gate | result |
|---|---|
| `check_navgraph_vs_game.py` coverage | 4,000/4,000 sampled drivable lane points have an edge within half their road |
| connectivity | 17,028/17,619 roads in one component (96.65 %) |
| false welds across height | 0 of 2,936 in-radius node pairs |
| direction / speed vs CS2 | 6,664/6,667 and 6,667/6,667 |
| `tools/test_app_shell.py` | 133/133 |
| `tools/validate_bundle.py` | VALID, 1 known warning |

**VALIDATED IN GAME 2026-08-20** — everything this paragraph asked for was seen and reported
good, on the 2026-08-19 22:15 build (through the two defects A7.5 and A7.6 fixed on the way).
What it asked for: the minimap actually
drawing and matching the road under the car; a big-map waypoint routing along roads with the
ground-marker arrows staying on the carriageway through junctions and never dropping off a bridge
into the street below; the "Quicktravel Points" group listing the POIs and putting the car on the
road facing along it; and traffic keeping to its side. `beamng.log` should report a non-zero
`map.lua` node count within 20 % of 56,012, and no `Road … centerline to edge distance exceeding
20m` warnings (that message only comes from the `useSubdivisions` branch, which we never take).

**Neither producer change can be validated by any of the above.** `leftHandTraffic` and
`districts.jsonl` are "built, awaiting in-game re-rip".

---

## A7.5 — the load, and the crash it found (2026-08-19)

The load happened, and it ended before the level opened:

```
*** FATAL LUA ERROR: [string "lua/ge/map.lua"]:459: attempt to compare nil with string
```

Full analysis in **audit §85**. In short: `optimizeNodes` composes a replacement edge without a
`lanes` field and hands it to `edgeCompare`, whose `lanes` test is the one of ten carrying no
guard. It only gets there through a degree-2 node whose two neighbours are already linked — a
triangle — and we shipped 331 of them, 9 tying on all nine guarded fields.

Two of ours caused it, and the first also fragmented the graph:

1. a connector's terminal nodes carried the *pair's minimum* width, so they never merged with the
   segment end they sat exactly on (`map.lua:1211` compares radii as well as positions): 9,738
   wrong-width terminal nodes, and the graph BeamNG had at optimiser time stood in 692 pieces;
2. the emitted set was not a simple graph: 874 self-loop connectors, 1,245 zero-length joints,
   962 connector/joint pairs across one node pair.

Both are fixed in `csiibundle.navgraph` (terminal widths, then `_simple_graph`), which is where
they belong — the rule is about the graph, not about BeamNG. 17,619 roads → 15,393; triangles
331 → **0**; components at optimiser time 692 → 35, largest 13.8 % → 96.5 %; lane coverage
unchanged at 4,000/4,000.

| gate | result |
|---|---|
| `tools/test_navgraph_map.py` (new) | 10/10, including 0 triangles on the emitted `DecalRoad`s |
| `check_navgraph_vs_game.py` | 6/6 questions OK, the sixth being map.lua's own build |
| `validate_level.py` | now runs the port over the built level and fails the build on a triangle |

**The connectivity number in the A7.1 table above was measured on the wrong node identity**
(coordinate equality, not the consumer's merge) and read 96.65 % while BeamNG's own graph was in
692 pieces. `_components` now uses `_terminal_clusters`, the same identity as the rule.

**Seen on the 2026-08-20 load**: everything A7.1's paragraph lists, none of which this crash had
allowed to be seen.

**BUILT AND REBUILT 2026-08-19 21:30.** `cs2_city_fine` in place, 14m55s, `status: done`,
validator 0 problems, cook 7,883/7,883. The validator now reads the port over the level's OWN
`DecalRoad`s: `{"decalRoads": 15393, "mapNodes": 32494, "endNodesMerged": 18703, "chords": 0,
"crashRisks": 0}`. Resident 3,381.7 MiB, unchanged from A8. `lights` off, `groundcover` on,
`foliage_density` 60 / `prop_density` 100.


---

## A7.6 - the route onto an overpass (2026-08-19)

The load worked; the navigation put the route on a viaduct above the car, with no ramp. **Not a
false edge** - every link-creating pass in `map.lua` was ported and measured first
(`mergeOverlappingNodes`, `resolveTJunctions`, `resolveXJunctions`, `mergeNodesToLines`) and
between them they made 2 vertical joins, both between a segment and its own connectors.

It was `startPosLinks` (map.lua:2871): an edge whose RADIUS covers the car costs exactly zero, so
the `wZ = 4` height bias never runs and a wide carriageway's 13 m radius reaches a deck 7 m up.
31 of 112,030 sampled road points, 3 sites, all 26 m roads under 7-8 m of clearance.

Fixed by `_clearance_clamp`: a segment's emitted half-width is capped at the distance to the
nearest road at a different height. 19 segments clamped, worst 13.32 m of width, stacked coverage
31 -> 0, connectivity and lane coverage unchanged. Full analysis in audit 86.

---

## CLOSED — 2026-08-20

Naomi loaded the 2026-08-19 22:15 build of `cs2_city_fine` and reported the navigation good. That
closes A7.0-A7.5 as planned, plus the crash and the overpass route the loads found: the minimap
draws, a waypoint routes along the roads, and the quicktravel points put the car on a road facing
along it.

Left open, and not by anything a load can decide: **`leftHandTraffic` and `districts.jsonl` are
producer changes awaiting a re-rip**. Until one happens the level is built from a bundle that
predates them, so the flag reads its default and the POI names come from the fallback.
