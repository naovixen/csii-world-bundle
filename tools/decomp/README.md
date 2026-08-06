# decomp — offline decompiler for CS2's managed assemblies

Answers "what does CS2 actually expose, and what does it actually do?" by decompiling
`%CSII_MANAGEDPATH%` to real C#. No game launch.

This is a **development tool, not part of the mod and not part of the bundle contract.** It is
excluded from `Carto.csproj`'s compile glob by `DefaultItemExcludes` in the repo-root
`Directory.Build.props`, and shielded from that same file's net48/Unity-BCL settings by the empty
`Directory.Build.props` beside this README. Both are load-bearing; see their comments.

```
cd tools/decomp && dotnet build -c Release        # needs ICSharpCode.Decompiler from NuGet once
./bin/Release/net8.0/decomp.exe <mode> <dllGlob> [args]
```

## Modes

| | |
|---|---|
| `type <dll> <FullTypeName>` | one type as C#, to stdout — the workhorse |
| `list <dll> <typeRegex>` | matching full type names |
| `all <dll> <outDir>` | every top-level type, one `.cs` per type — then use ripgrep |

`<dll>` is a glob against the Managed folder; `.dll` is optional. `CSII_MANAGEDPATH` is read from
the environment, falling back to the default Steam path. Nested types (`Job` structs and the like)
decompile with their parent — ask for the outer type.

```sh
decomp type Game Game.Rendering.BatchDataHelpers                  # the whole helper class
decomp type Colossal.IO.AssetDatabase Colossal.IO.AssetDatabase.SurfaceAsset
decomp all  Game /tmp/game-src                                    # once, then:
rg '_BaseColorMap|_ControlMask' /tmp/game-src/Game                #   string sweeps
rg 'm_Rotate' /tmp/game-src/Game                                  #   who reads/writes a field
rg 'ComponentType\.ReadWrite<Game\.Net\.Marker>' /tmp/game-src/Game   # who declares an archetype
```

## Why a decompiler, and not an IL walker

This tool replaced `mdump`, a `System.Reflection.Metadata` member/`#US`-heap/IL dumper. The IL
walker earned its keep for two audits — but on 2026-08-06 hand-reading the IL of
`CalculateNodeParameters` and `GenerateCompositionMesh` produced **two wrong readings** (a scale
factor and a matrix assignment) that the decompiled source settled in minutes, and the whole
network 1:1 validation was done from source. IL answers "what does this method call, in what
order"; anything with arithmetic, operand flow or compiler-generated structure needs source.
`all` + ripgrep covers the old `strings` and `find` modes: string literals and member references
both appear in the decompiled text, with the type name in the file path.

## Findings this line of work has produced (from the mdump era onward)

Recorded here because the finding outlives the tool that made it:

- **`VTSurfaceAsset` exists at all.** `SurfaceAsset` has a second virtual-texturing path —
  `hasVTSurfaceAsset`, `vtSurfaceAsset`, `LoadTexturesVT` — and `VTSurfaceAsset.Load()` hands back
  a Unity material with the decoded maps already bound. That is where every building's textures
  were.
- **`SurfaceAsset.GetPreProcessedTextureGuid(stack, layer)` is broken in the game**: it indexes
  the header with `stackIndex * layerIndex` instead of `stackIndex * 4 + layerIndex`, so every
  coordinate containing a zero returns the base colour. It had silently written the base colour
  into the normal and mask of 1,428 of 1,926 materials.
- **`SubMeshFlags.OutlineOnly` sub-meshes draw nothing in normal rendering**
  (`BatchInstanceJob.UpdateObjectInstances` masks their layers away) — the ripper had been
  shipping silhouette shells as world geometry.
- **`NetSectionFlags.Hidden*` hides nothing in the composition-mesh path**, `MeshLayer` selects
  batch groups on a baked mesh the producer never reads, and ground compositions carry no
  `NetPieceFlags.Bottom` piece to trim — three reasonable readings of field names, all wrong,
  all cheap to check against source.
- **`NetCompositionMeshData.m_IndexFactor` is indices per metre**, not a UV term — and it
  reproduces to five significant figures from the ripper's own meshes, which doubles as a check
  that the producer reads the geometry the game rasterises.
- **The whole network-assembly rule set** — matrices, `m_Rotate`, vertex-colour matrix selection,
  half cuts, corner rails, orphan caps — is recorded as a validation matrix in
  [`docs/cs2-api-audit.md`](../../docs/cs2-api-audit.md) §8.

Archetype questions read best as source too: `GetPrefabComponents` / `GetArchetypeComponents`
bodies are just `set.Add(ComponentType.ReadWrite<T>())` lines, and grepping the instantiation
over an `all` dump answers "who puts this component on an entity?" in one query. That is how the
buried water/sewage/cable node props were finally excluded — `Game.Net.Marker` has exactly one
source, so an owner carrying it *is* a marker net by construction.

## The other half of the same question

Some questions are about shipped *data*, not code. `Cities2_Data/Content/Game/*.cok` are plain
zip archives (stored, uncompressed) with entries named `<AssetName>_<guid>.<Ext>`, so
`python -m zipfile -l` reads them with no tooling at all, and `cache.db` is a flat index whose
extension histogram gives an instant census — that is how "2,443 `.VTSurface` files ship" was
established.

Beware the structs: `SurfaceAssetVTHeader` and `PerStackData` are `[ExplicitLayout]`, and parsing
their guid fields offline from assumed offsets produced plausible, distinct, **wrong** values that
matched no asset in any archive. Structural counts off these files have held up; per-field offsets
have not. Confirm those in the running game.

## Gotchas

- **A decompile is still a claim about code, not about data.** `attrs.elevation` was once
  "proved" clamped-at-zero from a correctly-decompiled method — true of that code path, false of
  the stored component (29,610 negative values in a real city). Read the shipped bundle before
  generalising any code reading to the data; see the `check-the-shipped-bundle-first` rule.
- `ilspycmd` (the official CLI) currently fails to install as a dotnet tool; the library works.
- `all` over `Game.dll` takes a few minutes and ~hundreds of MB; do it once per game version and
  keep the output directory for grepping.
