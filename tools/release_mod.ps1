<#
.SYNOPSIS
    Build the ripper mod and package it as a GitHub release asset.

.DESCRIPTION
    The mod CANNOT be built by CI: Carto.csproj imports the Cities: Skylines II modding
    toolchain from $env:CSII_TOOLPATH and references the game's own assemblies, and neither
    exists on a GitHub runner (nor may they be redistributed). So the mod half of a release is
    built HERE, on a machine that has the game and the toolchain, and uploaded onto the release
    the CI workflow made.

    That makes this script the reproducibility story for the DLL, which is why it refuses to run
    on a dirty tree and checks that Carto.csproj, PublishConfiguration.xml and the git tag all
    agree about the version.

    What ships is exactly what the toolchain deploys - `Mod.targets` copies bin\Release\net48\**
    to <CS2 user folder>\Mods\Carto, so the zip is that folder. All six files: dropping the
    three Carto_<platform>_* Burst libraries would ship a mod whose collectors silently fall
    back off Burst.

.EXAMPLE
    pwsh tools/release_mod.ps1 -Tag v0.2.0 -Upload
    pwsh tools/release_mod.ps1 -AllowDirty          # local test build, no upload
#>
[CmdletBinding()]
param(
    [string]$Tag,
    [switch]$Upload,
    [switch]$AllowDirty,
    [string]$OutDir
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
if (-not $OutDir) { $OutDir = Join-Path $root "dist" }

function Fail($message) { Write-Error $message; exit 1 }

# -- 1. the tree ---------------------------------------------------------------------------------
Push-Location $root
try {
    $status = git status --porcelain
    if ($status -and -not $AllowDirty) {
        Fail "the working tree is dirty - a released DLL must be reproducible from a commit.`n$status`nPass -AllowDirty for a local test build."
    }
    $commit = (git rev-parse --short HEAD).Trim()

    # -- 2. the versions -------------------------------------------------------------------------
    [xml]$csproj = Get-Content (Join-Path $root "Carto.csproj")
    $version = ($csproj.Project.PropertyGroup | Where-Object { $_.Version }).Version | Select-Object -First 1
    [xml]$publish = Get-Content (Join-Path $root "Properties\PublishConfiguration.xml")
    $modVersion = $publish.Publish.ModVersion.Value
    $gameVersion = $publish.Publish.GameVersion.Value
    if ($version -ne $modVersion) {
        Fail "Carto.csproj says $version and PublishConfiguration.xml says $modVersion - pick one."
    }
    if ($Tag) {
        $tagVersion = $Tag -replace '^v', ''
        if ($tagVersion -ne $version) { Fail "tag $Tag does not match version $version" }
    }
    Write-Host "Carto $version for CS2 $gameVersion  ($commit)" -ForegroundColor Cyan

    # -- 3. the toolchain ------------------------------------------------------------------------
    $toolpath = [Environment]::GetEnvironmentVariable("CSII_TOOLPATH", "User")
    if (-not $toolpath -or -not (Test-Path $toolpath)) {
        Fail "CSII_TOOLPATH is not set or does not exist - install the CS2 Modding Toolchain first."
    }

    # -- 4. build --------------------------------------------------------------------------------
    Write-Host "dotnet build -c Release" -ForegroundColor Cyan
    dotnet build -c Release --nologo
    if ($LASTEXITCODE -ne 0) { Fail "the build failed" }

    $outDirBin = Join-Path $root "bin\Release\net48"
    if (-not (Test-Path $outDirBin)) { Fail "no build output at $outDirBin" }

    # The Burst libraries are the reason this list is checked rather than globbed and hoped for.
    $required = @("Carto.dll", "Carto_win_x86_64.dll", "Carto_linux_x86_64.so", "Carto_mac_x86_64.bundle")
    foreach ($name in $required) {
        if (-not (Test-Path (Join-Path $outDirBin $name))) {
            Fail "$name is missing from $outDirBin - a mod without its Burst libraries is not the mod"
        }
    }

    # -- 5. stage --------------------------------------------------------------------------------
    $stage = Join-Path ([System.IO.Path]::GetTempPath()) ("cartorip_" + [Guid]::NewGuid().ToString("N"))
    $modFolder = Join-Path $stage "Carto"
    New-Item -ItemType Directory -Path $modFolder -Force | Out-Null
    Copy-Item (Join-Path $outDirBin "*") $modFolder -Recurse -Force

    Set-Content -Path (Join-Path $modFolder "cs2beamng_mod_version.txt") -Value $version -Encoding utf8 -NoNewline
    $howto = @"
Carto World Ripper $version  (built from $commit, for Cities: Skylines II $gameVersion)

INSTALL
  Unzip the "Carto" folder from this archive into your Cities: Skylines II mods folder:

    Windows   %USERPROFILE%\AppData\LocalLow\Colossal Order\Cities Skylines II\Mods\
    Linux     <steam>/steamapps/compatdata/949230/pfx/drive_c/users/steamuser/AppData/
              LocalLow/Colossal Order/Cities Skylines II/Mods/   (CS2 runs under Proton)

  You should end up with .../Mods/Carto/Carto.dll and five files beside it. Replace the whole
  folder when you update - a new Carto.dll beside an old Carto_win_x86_64.dll is neither version.

USE
  Start the game, load a city (or the map editor), then Options -> Carto -> Ripper -> Rip World.
  A full city takes about 28 minutes. The bundle is written to

    ...\AppData\LocalLow\Colossal Order\Cities Skylines II\ModsData\Carto\

  which is where the cs2beamng application looks for it by default.

  Untick "Rip Objects" and "Rip Terrain" for a ~5 minute networks-only rip.

This mod only READS the game's data and only WRITES into its own ModsData folder. It is for
local, personal use: do not publish or redistribute the meshes and textures it extracts.
"@
    Set-Content -Path (Join-Path $modFolder "HOW-TO-INSTALL.txt") -Value $howto -Encoding utf8

    # -- 6. zip ----------------------------------------------------------------------------------
    New-Item -ItemType Directory -Path $OutDir -Force | Out-Null
    $gameTag = ($gameVersion -replace '\*', '' -replace '\.$', '')
    $zip = Join-Path $OutDir "CartoRipper-$version-cs2-$gameTag.zip"
    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path $modFolder -DestinationPath $zip -CompressionLevel Optimal
    $hash = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
    Set-Content -Path "$zip.sha256" -Value "$hash  $(Split-Path -Leaf $zip)" -Encoding utf8
    Remove-Item $stage -Recurse -Force

    $sizeMiB = [math]::Round((Get-Item $zip).Length / 1MB, 2)
    Write-Host "$zip  ($sizeMiB MiB)" -ForegroundColor Green
    Write-Host "sha256 $hash"

    # -- 7. upload -------------------------------------------------------------------------------
    if ($Upload) {
        if (-not $Tag) { Fail "-Upload needs -Tag: the release to attach this to" }
        if (-not (Get-Command gh -ErrorAction SilentlyContinue)) { Fail "gh is not installed" }
        Write-Host "uploading to release $Tag" -ForegroundColor Cyan
        gh release upload $Tag $zip "$zip.sha256" --clobber
        if ($LASTEXITCODE -ne 0) { Fail "gh release upload failed" }
        Write-Host "uploaded" -ForegroundColor Green
    } else {
        Write-Host "not uploaded (pass -Upload -Tag <tag> to attach it to a release)"
        Write-Host "to ship it INSIDE the app:  python tools/build_exe.py --mod `"$zip`""
    }
} finally {
    Pop-Location
}
