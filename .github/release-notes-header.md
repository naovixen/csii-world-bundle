## Two downloads, or one

**The application** — `cs2beamng-<version>-windows-x64.zip`. Unzip it anywhere and run
`cs2beamng.exe`. No Python, no install. (Windows will show "Windows protected your PC" the first
time, because the build is not code-signed: **More info → Run anyway**.) `cs2beamng-cli.exe` in
the same folder is the command-line half.

**The ripper mod** — `CartoRipper-<version>-cs2-<gameversion>.zip`. This is the Cities: Skylines II
mod that writes the world bundle the application reads. Unzip the `Carto` folder into

```
%USERPROFILE%\AppData\LocalLow\Colossal Order\Cities Skylines II\Mods\
```

or press **Install the ripper mod** in the application, which does exactly that.

Then: load your city in CS2 → **Options → Carto → Ripper → Rip World** (~28 minutes for a full
city) → open the application → **Build level**.

Verify a download against its `.sha256` if you want to: `sha256sum -c <file>.sha256`.

Everything below is generated from the commits.
