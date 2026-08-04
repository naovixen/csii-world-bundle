# mdump — offline inspector for CS2's managed assemblies

Answers "what does CS2 actually expose, and what does it actually do?" by reading
`%CSII_MANAGEDPATH%` directly. No decompiler, no ILSpy, no launching the game.

This is a **development tool, not part of the mod and not part of the bundle contract.** It is
excluded from `Carto.csproj`'s compile glob by `DefaultItemExcludes` in the repo-root
`Directory.Build.props`, and shielded from that same file's net48/Unity-BCL settings by the empty
`Directory.Build.props` beside this README. Both are load-bearing; see their comments.

```
cd tools/mdump && dotnet build -c Release
./bin/Release/net8.0/mdump.exe <mode> <dllGlob> [args]
```

`System.Reflection.Metadata` is in-box for .NET 8, so there is nothing to restore and no network
access — same rules as the Python tools alongside it.

## Modes

| | |
|---|---|
| `types <dll> <typeRx>` | list matching type names |
| `members <dll> <typeRx> [memberRx]` | fields, properties and methods with decoded signatures |
| `strings <dll> <rx>` | the `#US` heap — `ldstr` literals a member dump cannot see |
| `il <dll> <typeRx> <methodRx>` | crude method body dump |
| `find <dll> <rx>` | every method whose IL mentions a matching member or string |

`<dll>` is a glob against the Managed folder; `.dll` is optional. `CSII_MANAGEDPATH` is read from
the environment, falling back to the default Steam path.

```sh
mdump members Colossal.IO.AssetDatabase '^Colossal\.IO\.AssetDatabase\.SurfaceAsset$'
mdump strings Game '_BaseColorMap|_MaskMap'          # how the shader property names were found
mdump il      Colossal.IO.AssetDatabase VTSurfaceAsset '^Load$'
mdump find    Colossal.IO.AssetDatabase 'SurfaceAsset::m_VTSurfaceAsset'
```

## What it has been worth

Two texture-layer defects, both invisible from inside the running game:

- **`VTSurfaceAsset` exists at all.** `members` on `SurfaceAsset` showed a whole second
  virtual-texturing path — `hasVTSurfaceAsset`, `vtSurfaceAsset`, `LoadTexturesVT` — that the
  ripper had never touched, and `il` on `VTSurfaceAsset::Load` showed it hands back a Unity
  material with the decoded maps already bound. That is where every building's textures were.
- **`SurfaceAsset.GetPreProcessedTextureGuid(stack, layer)` is broken in the game.** `il` showed it
  indexing the header with `stackIndex * layerIndex` instead of `stackIndex * 4 + layerIndex`, so
  every coordinate containing a zero returns the base colour. It had been silently writing the base
  colour into the normal and mask of 1,428 of 1,926 materials.

## Gotchas

- **The IL walker is crude on purpose** — it decodes opcodes and prints only `ldstr` / `call` /
  `ldfld`-style operands. Get the operand-size table wrong and the walk desynchronises and prints
  confident nonsense. It is enough to answer "what does this method call, and in what order"; it is
  not a decompiler.
- **By default `il` hides operand-less opcodes**, which hides arithmetic. The `mul` above is
  invisible in the default output. Set `MDUMP_ALL=1` to print every opcode — do that before
  concluding anything about what a method *computes* rather than what it calls.
- `MetadataReader` has no `UserStrings` collection; the `#US` heap is walked with `GetNextHandle`
  from a nil handle. That is why `strings` is its own mode.

## The other half of the same question

Some questions are about shipped *data*, not code. `Cities2_Data/Content/Game/*.cok` are plain zip
archives (stored, uncompressed) with entries named `<AssetName>_<guid>.<Ext>`, so `python -m
zipfile -l` reads them with no tooling at all, and `cache.db` is a flat index whose extension
histogram gives an instant census — that is how "2,443 `.VTSurface` files ship" was established.

Beware the structs: `SurfaceAssetVTHeader` and `PerStackData` are `[ExplicitLayout]`, and parsing
their guid fields offline from assumed offsets produced plausible, distinct, **wrong** values that
matched no asset in any archive. Structural counts off these files have held up; per-field offsets
have not. Confirm those in the running game.
