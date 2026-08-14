# Consumer caveats

Things that are true about the bundle and surprising, collected because each one has already
cost somebody a wrong city. `docs/consumer-guide.md` is the route; this is the list of
potholes. Every entry names the contract section that governs it.

---

## The bundle is one moment of one save

**It is the dry, built world.** No weather, no wetness, no waves, no vehicles, no pedestrians,
no simulation state. Both rip queries drop `Marker`, `Creature` and `Vehicle` entities
deliberately. If you want traffic, you generate it — `lanes.jsonl` (§5A) is the graph to
generate it on.

**`grading.json` is a snapshot of one hour in one weather.** A rip at dusk records dusk. It is
weather-dependent by construction and says so (`weatherDependent`, and `lighting.time` carries
the in-game clock). A reference screenshot taken at another hour is not describing this
grading. (§8)

**A bundle can be mixed-vintage.** The producer runs eight stages and files whose stage did not
run are left alone, so a partial rip leaves untouched layers at their previous vintage rather
than deleting them. If two layers disagree about the same entity, that is the likely reason.
The validator's "an area's owner resolves to no instance" warning is the symptom to watch.

---

## Placement

**`attrs.elevation` is not the ground's height.** It is how far the object stands *above* the
ground, and it is **signed**: on the 2026-08-12 city, 970 of 483,351 records carrying it are
negative, down to **−86.3 m** (underground station parts, buried props and decals). Ground level
is `pos[1] − elevation`. Reading it as terrain height gives a city where everything sits at a
couple of metres. (§4.2)

**A `.glb` is one STATE of a prefab, not the prefab.** `attrs.treeState` and `attrs.fullness`
select a derived mesh — `__teen`, `__elderly`, `__dead`, `__stump`, `__partial1`,
`__partial2`, `__full`. The step *set* is per-prefab, so walk down the candidates until a file
exists rather than demanding the exact one. There is no `__child`: the game has no Child case —
a child is a Teen below growth 128, scaled. (§6.1)

**`attrs.growthScale` is not in `scale`.** A tree record has no `scale` field at all;
`growthScale` is a uniform multiplier you fold in yourself. On the reference city **299,972
instances carry one — all of them trees — and 130,880 of those are below 1.0**, so ignoring it
draws every one of those saplings at full height. (§4.2)

**`attrs.state == "construction"` draws NOTHING.** `BatchInstanceSystem` skips the building
entirely; the crane and the site fence are separate records the bundle already carries. Draw
the building and you put a finished tower on a building site. (§4.2)

**`parent` is advisory, not a foreign key.** Owners the bundle does not carry — utility-net
edges, net nodes — leave a `parent` that resolves to nothing: 20,959 records over 4,190
distinct owners on the reference city. Use it to group when it resolves; never require it. (§4)

**Stack sections share one id.** Every `__stack_first` / `__stack_mid` / `__stack_last` /
`__stack_rest` record of one stacked instance carries that instance's id. It is the only
sanctioned repetition of an `id` in the file, and the bare prefab is never referenced directly —
so its mesh, and its skirt, are unreferenced by construction. (§4.1)

**A missing mesh may be declared.** `warnings.jsonl` (§10) is how the producer says "I could
not deliver this asset" without dropping the placement, which §4.1 requires to outlive the
geometry. Check `reader.read_declared_gaps()` before calling one a defect; substitute a
stand-in and the objects still land in the right places. An **undeclared** missing mesh is a
real error.

---

## Materials and colour

**The `.glb` carries no metallic-roughness map.** Every glTF material hard-codes
`roughnessFactor` 0.9. The real values are per-texel in `materials.json`'s `mask`: **R
metallic, A smoothness**, so `roughness = 1 − A`. Getting the smoothness/roughness sense
backwards is silent and makes everything either too shiny or too flat. (§7.1)

**`mask.G` and `mask.B` are advisory.** The producer reads them as ambient occlusion and a
detail/emissive mask; the game's own asset-authoring documentation describes them as a glossy
coat and an unused channel. CS2 ships its shaders compiled, so the disagreement is recorded
rather than resolved. Both readings agree on R and A, which are the two that matter.

**Some materials have no textures and are not broken.** CS2 draws water and glass with shaders,
so `template:WaterGeneric` (pools) and `DefaultGlassDoubleSided` (windows) serialise with no
maps at all: **218 of 4,784 materials carry no texture in any slot**, of which
`csiibundle.materials.shader_only_kind` names 206 (109 glass, 51 water, 40 foliage, 4 overlay,
2 placeholder) and leaves 12 unclassified. A glTF loader falls back to `baseColorFactor`
`[1,1,1,1]`, which is why every pool renders as a white rectangle. Branch on the
`template:<name>` flag. (§7.1)

**The `template:` flag is also the only evidence a material is not drawn at all.** An invisible
shader binds none of the properties the producer reads, so it serialises with no maps, no tint
and no alphaMode — indistinguishable from a plain material unless you read the template. A
consumer that draws every material renders invisible taxiways and paths as opaque surfaces.

**`colorMask` is not `mask`.** `colorMask` selects between the three `colorSet` channels per
texel; `mask` is the PBR packing. Confusing the two is how this layer once shipped a palette
nothing could apply. (§7.1, §7.2)

**The tint is a masked MULTIPLY, not a replacement.** `lerp(c, c × colorSet[i].rgb,
colorMask[i])`. Reading "selects" as *replace the albedo with the channel colour* flattens
every surface to a solid tone. (§7.2)

**Vegetation is authored desaturated.** Skip `colorSet` and every tree, bush and hedge is
grey-brown. The companion trap is alpha: leaf canopies are alpha-tested cards
(`alphaMode: "mask"`), so a loader ignoring `alphaMode` draws each canopy as solid grey
rectangles. (§7.2)

**`colorMask.A` polarity is advisory.** Documented at the freeze as "snow coverage (white = no
snow)"; measured over 731 masks, every vegetation mask is all-black, which under that reading
means permanently snow-covered foliage. The polarity is now recorded as the opposite — white =
snow may accumulate — but it is not recoverable from managed code. Validate it against the game
before building weather on it. (§7.1)

**`colors.json` is the palette, not the paint.** It is the prefab's *possible* variations with
their weights; what a given object was actually drawn with is its own `attrs.colorSet`. Without
`colorSet`, every copy of a prefab renders identically where the city shows a street of
different colours. (§7.2)

**A §7.2 key may match no mesh.** 184 of 3,620 palette keys and 86 of 1,186 light keys on the
reference city describe geometry the bundle deliberately excludes — tree `*Child*` states and
vehicles. Absence of a lookup result is normal; do not treat it as corruption.

---

## Lights

**One file, two record shapes, placed by opposite rules.** A **procedural** entry (no `kind`)
is emissive material state — a neon sign's lit colour, a traffic light's red — and genuinely
has no position: it binds to a material `slot`, so place it at the centroid of the primitive
using that material. A **punctual** entry (`kind: "punctual"`) is a real HDRP light and carries
a position; use it, and do not fall back to the centroid rule, which would move every
streetlamp to the middle of its pole. Treating them uniformly makes 2,532 lights or none. (§7.2)

**`parentMesh` is a colour selector, not a parent.** *(Corrected 2026-08-14.)* A sweep of every
reader of the game's `Effect.m_ParentMesh` finds three and none of them places a light: it
picks which sub-mesh's colour the light takes. A punctual light's `position`/`rotation` are in
the **object frame**. The field is also overloaded — a value above the prefab's sub-mesh count
is a procedural-bone connection id, and negatives other than −1 are unresolvable authored
values (−1001 occurs on a workshop asset). Bounds-check before indexing anything with it.

**`boneIndex`, when present, changes what `position` means.** The game rewrites the position
into a procedural bone's bind-pose space when it resolves one, so an entry carrying `boneIndex`
is not object-frame. At most 4 of 654 punctual lights on the reference city. Skipping them is
defensible; placing them in the object frame and believing the result is not.

**Read `intensityUnit`.** The reference city reads `Lux` on all 654 punctual lights while the
contract's own worked example shows `Lumen`. A Lux spot and a Lumen point are different
quantities.

---

## Roads, lanes and markings

**`lanes.jsonl` (§5A) and `lanemeshes.jsonl` (§5B) are different layers.** The first is the
drivable graph; the second is geometry drawn *along* lanes — cables, fences, hedges, road
markings, wear tint. They share a curve concept and nothing else.

**`lanes.jsonl`'s `owner` is not a foreign key.** *(Corrected 2026-08-14.)* It is the generic
ECS owner: 21.7% resolve into `networks.jsonl`, 19.1% into `instances.jsonl` and **59.2% into
neither**, because the commonest owner is a net node and the bundle emits no node records at
all. (§5A, §5.1)

**There are no `kind: "node"` network records, and that is deliberate.** CS2 keeps no junction
geometry on the node; the flare where an edge meets an intersection is stored on the *edge*.
Emitting node records would invent geometry the game does not have. (§5.1)

**Port `GetTileCount` verbatim.** `N = L / max(1, size.z)` quantised through the clip window,
with the `−0.0001` epsilon. Rounding differently gives a different fence from the game on every
short span — and `tilingCount == 0` means the part draws **nothing**. (§5B)

**Never reason from a decal's box scale to its UVs.** The game draws a marking as one instanced
projector box; the box stretches but the texture repeats `N = L / meshLength`. (§5B)

**Use `decalRole`, not the assetId.** The producer classifies marking vs wear from the part
material's tint alpha. The substring heuristic it replaced disagrees on 7 of 43 decal parts,
all one way, demoting real markings to the wear tint's lower lift — and it fails outright on
mods. An absent `decalRole` means unknown, not either class. (§5B)

**`attrs.decalLayers` keeps a road marking off the wall behind it.** A decal projects only onto
the surfaces it names. (§4.2a)

**Never generate collision from a decal or an area.** They are paint. Take collision from the
network meshes. (§4.2a, §8b)

---

## Terrain and water

**The heightmap contains the ground that covers every tunnel.** The game does not carve it — a
per-frame stencil pass discards those pixels on screen. `clipvolumes.jsonl` is what opens them,
and a consumer that meshes `heightmap.png` alone walls off every underground network. It is the
single most visible thing to get wrong in §8. (§8, §8c)

**Only a DEEP clip band may carve; a shallow one flattens and never deletes.** And delete a
face only when a volume contains the **whole face** — testing corners against the *union* of
volumes reads as contained at a junction where four corners are covered by four different
volumes while the interior escapes them all, and tears a hole. Report deleted area against
clipped area; the ratio must never exceed 1.00. (§8c)

**The clip band is relative to the lane surface, and the lane surface is a reference, not the
road.** The producer holds one `laneY` per quad of arc, so on a superelevated carriageway the
band hangs level while the road leans, by half the cross-fall. Want the road surface itself?
Interpolate `left.y` → `right.y` across the strip. (§8c)

**Area clips are NOT open-topped.** They carry a real `top` since 2026-08-09; terrain above it
is not clipped. An absent `top` means unbounded, which is a different statement. (§8c)

**The splatmap is not a partition.** Composite over the base in channel order; do not
normalise, or every meadow goes half brown. Rock is the residual of **`R` alone**. (§8)

**`B` and `A` are bit-packed: two 4-bit weights each.** Normalise a nibble by **15**, not 255,
and sample the packed channels with **point/nearest** filtering — halfway between `0x0F` and
`0x10` decodes to a nibble pair unrelated to either neighbour. Reading either channel as one
0–255 opacity merges two grounds and drops two more; it is what made a city's painted beaches
render as forest floor. `R` and `G` are genuine continuous weights and should be sampled
smoothly. (§8)

**`R` is dirt and `G` is grass.** A bundle declaring `"R": "grass"` predates 2026-08-10 and its
map is inverted. Do not derive the assignment from slope: both soil weights fall with slope
together. Read `rasters.splatmap.channels`; do not calibrate it. (§8)

**Do not treat a roadbed as dirt.** The blend has no roadbed response at all — it weights the
same ground under roadbeds as under lawns. The dirt look beside roads is §5B wear decals and
§8b surface areas. (§8)

**`seaLevel` is a datum, not a water surface.** `rasters.water.seaLevel` is what the simulation
resets ocean water to and what the renderer measures camera depth against. On the reference
city it reads 15.0 while the mapped water measures 508.6–528.3 m. **`terrain/water_level.png`
is the authoritative surface.** (§8)

**An area triangle is a footprint, not a surface.** The game draws a prism per triangle and
projects the material onto whatever ground is inside it. The terrain rises through the flat
triangle on 26.6% of triangles and 61.4% of areas — 11 cm at p95, 55 cm at p99. Drape it.
Nothing flattens the ground underneath. (§8b)

**`heightRanges` is a guide, not a substitute for the heightmap.** The game's band contains an
independent recomputation on 83.3% of triangles, but 0.52% come out narrower, most plausibly
because the game recomputes only when the area changes and can lag a later terrain edit. If the
surface must sit on the ground, sample the ground. (§8b)

**`rendererPriority` is the overlap draw order.** Without it you have no rule for which of two
overlapping areas paints on top, and you z-fight every overlap. (§8b)

**An `unresolved: true` area prefab is a session fact, not a producer bug.** The save references
a surface asset that was not loaded — an asset pack, in practice — and the game itself drew
those areas with its grey Missing Area fallback. Surface it by `name`; the cure is enabling the
pack and re-ripping, not consumer-side invention. (§8b)

**`worldTerrain`'s absence is a fact about the session.** The backdrop heightmap is a GPU
resource the game keeps resident only when it needs it. The rip report says
`World terrain: no backdrop heightmap resident; layer omitted`. Treat it as §1 says: absent key,
absent feature. **`worldWater` will never appear** — backdrop water is a permanent non-goal;
draw a plane at `seaLevel`. (§8)

**A cut cell's `surface_index.png` id is meaningless** — the cell is not drawn at all. (§8c)

---

## Colour grading

**Apply it as a display transform; never bake it into the shipped textures.** The textures are
the faithful rip. A consumer that wants raw albedo ignores the block entirely. (§8)

**Check `diagnostics` before trusting a single number in it.** `stackEvaluated: false` or
`volumesFound: 0` means the capture read an unevaluated HDRP stack, and every value in the file
is a default rather than the game's grade. The first cut of this layer shipped exactly that.

**`fog` is not part of the colour chain.** It is a function of a pixel's distance from the
camera, so it belongs in your atmosphere, not in your textures. Its `tint` is HDR and may
exceed 1.

**`vignette` is not part of the chain either** — it is a function of where a pixel is, not what
colour it is. It matters only because it is why a screenshot's corners read darker than its
centre, which will mislead you if you sample a reference colour off-centre.

**`lighting` is why replaying the grading alone does not match.** Measured against three
in-game surfaces the chain lands within a few percent on red and green and leaves a consistent
deficit on blue; a near-neutral pavement (albedo linear 0.1019/0.1018/0.1010) still renders warm
beige in game. No property of an albedo can do that. Light your scene with the shipped sun and
sky, not white.

**`mode: External` with `hasLut: true` means the block is incomplete.** The LUT is a GPU 3D
render texture and is not exported. Every rip so far has read `ACES` because the game's LUT gate
was off, but a bundle recording only the mode could not tell you whether it got lucky.

**`diagnostics.unread` is the producer's own blind-spot report.** It names volume components
some volume in the scene overrode and the writer does not capture, with their overridden values.
It exists because `Fog` went unnoticed for two months, and it earned itself on its first rip by
naming `SplitToning`. Read it before concluding the colour chain is complete.

---

## Things that are deliberately not in the bundle

Each of these is a decision with a reason, not an omission to work around.

| not shipped | why |
|---|---|
| vehicles, pedestrians, animals | the bundle is the built environment; both rip queries exclude `Marker`, `Creature`, `Vehicle` |
| waves, wetness, weather, water flow | transient simulation state — the dry world only |
| backdrop water | seeded to `seaLevel` and then simulated; the surface and the ground under it are already in the bundle twice over |
| the tonemapping LUT | a GPU 3D render texture; reading it back needs a compute dispatch |
| `RequireEditor` parts | the map editor draws them; a bundle is always the played world (§4.3) |
| `lodBias` on an area prefab | the game never applies it to rendering, only to a per-triangle distance cull |
| node junction meshes | CS2 keeps no geometry on the node; the flare lives on the edge (§5.1) |
| animation curves, bone histories | referenced by index where they matter (`animation`, `boneIndex`) and not carried |
