<#
.SYNOPSIS
    Copy this repository to another machine: a zip, or a folder on a share or USB drive.

.DESCRIPTION
    Leaves out what each machine makes for itself:
      .venv\                  Python environment (setup_windows.ps1 rebuilds it)
      tools\                  compiled gsplat kernels and other local toolchain (per GPU)
      .g2s\, workspace*\      scan and training caches
      __pycache__\, .pytest_cache\, .godot\   Python and Godot caches
      tests\unreal_fixture\{Content,Plugins,Saved,Intermediate,DerivedDataCache,Binaries}
                              the Unreal test project's generated files
    Keeps .git, so the copy can still pull and commit.

    Examples:
      scripts\pack.ps1                              # zip next to the repository: ..\godot2splat-YYYYMMDD-HHMM.zip
      scripts\pack.ps1 D:\transfer\g2s.zip          # zip at that path
      scripts\pack.ps1 E:\godot2splat               # copy into that folder (USB drive)
      scripts\pack.ps1 \\other-pc\share\godot2splat # copy into a network share
      scripts\pack.ps1 E:\godot2splat -DryRun       # list what would be copied, write nothing

    A destination ending in .zip is written as a zip; anything else is a folder. Copying into an
    existing folder updates it (robocopy /E): files there that are not in the repository stay.

.PARAMETER Destination
    Zip file or folder. Default: a dated zip in the folder that contains the repository.

.PARAMETER DryRun
    Print the files that would be copied and their total size; write nothing.

.PARAMETER Force
    Overwrite an existing zip.
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)][string]$Destination,
    [switch]$DryRun,
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RepoName = Split-Path $Root -Leaf

# Directories left out by name, anywhere in the tree (robocopy /XD accepts wildcards).
$ExcludeNames = @(".venv", ".g2s", "__pycache__", ".pytest_cache", ".godot", "workspace*")
# Directories left out by full path.
$ExcludePaths = @("tools") + @("Content", "Plugins", "Saved", "Intermediate", "DerivedDataCache", "Binaries" |
    ForEach-Object { "tests\unreal_fixture\$_" })
$ExcludePaths = $ExcludePaths | ForEach-Object { Join-Path $Root $_ }
$ExcludeFiles = @("*.pyc")

if (-not $Destination) {
    $Destination = Join-Path (Split-Path $Root -Parent) ("{0}-{1}.zip" -f $RepoName.ToLower(), (Get-Date -Format "yyyyMMdd-HHmm"))
}
$AsZip = $Destination -match "\.zip$"
$DestFull = [System.IO.Path]::GetFullPath($Destination)

if ($DestFull.TrimEnd("\").StartsWith($Root.TrimEnd("\") + "\", [System.StringComparison]::OrdinalIgnoreCase) -and -not $AsZip) {
    throw "Destination $DestFull is inside the repository; pick a folder outside it."
}

function Invoke-Robocopy([string]$from, [string]$to, [switch]$ListOnly) {
    $rcArgs = @($from, $to, "/E", "/R:1", "/W:1", "/NP", "/NDL", "/NJH", "/NJS", "/BYTES",
              "/XD") + $ExcludeNames + $ExcludePaths + @("/XF") + $ExcludeFiles
    if ($ListOnly) { $rcArgs += "/L" } else { $rcArgs += "/MT:8" }
    $out = & robocopy @rcArgs
    # robocopy: 0-7 success (bit 1 = files copied), 8+ = failures
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed (exit code $LASTEXITCODE):`n$($out -join "`n")" }
    return $out
}

Write-Host "Repository: $Root"
Write-Host "Left out:   $($ExcludeNames -join ', '), tools\, tests\unreal_fixture\{Content,Plugins,Saved,Intermediate,DerivedDataCache,Binaries}"

if ($DryRun) {
    $probe = Join-Path ([System.IO.Path]::GetTempPath()) "g2s_pack_dryrun_does_not_exist"
    $lines = Invoke-Robocopy $Root $probe -ListOnly
    $files = @()
    $bytes = [int64]0
    foreach ($l in $lines) {
        if ($l -match "^\s*\S.*?\s+(\d+)\s+(\S.*)$") {
            $bytes += [int64]$Matches[1]
            $files += $Matches[2].Trim().Substring($Root.Length).TrimStart("\")
        }
    }
    $files | Sort-Object | ForEach-Object { Write-Host "  $_" }
    Write-Host ("Dry run: {0} files, {1:N1} MB would go to {2} ({3})." -f $files.Count, ($bytes / 1MB), $DestFull, $(if ($AsZip) { "zip" } else { "folder" }))
    exit 0
}

if ($AsZip) {
    if (Test-Path $DestFull) {
        if (-not $Force) { throw "$DestFull exists. Pass -Force to overwrite it." }
        Remove-Item $DestFull
    }
    $parent = Split-Path $DestFull -Parent
    if (-not (Test-Path $parent)) { New-Item -ItemType Directory -Path $parent | Out-Null }
    $stage = Join-Path ([System.IO.Path]::GetTempPath()) ("g2s_pack_" + [guid]::NewGuid().ToString("N"))
    $stageRepo = Join-Path $stage $RepoName
    try {
        Invoke-Robocopy $Root $stageRepo | Out-Null
        Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
        # One top-level folder named after the repository. Entries are added one by one because
        # ZipFile.CreateFromDirectory under Windows PowerShell 5.1 writes backslashes into entry
        # names, which other unzip tools turn into file names instead of folders.
        $zip = [System.IO.Compression.ZipFile]::Open($DestFull, [System.IO.Compression.ZipArchiveMode]::Create)
        try {
            foreach ($f in Get-ChildItem -Path $stageRepo -Recurse -File -Force) {
                $entry = $RepoName + "/" + $f.FullName.Substring($stageRepo.Length + 1).Replace("\", "/")
                [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $f.FullName, $entry,
                    [System.IO.Compression.CompressionLevel]::Optimal)
            }
        } finally { $zip.Dispose() }
    } finally {
        if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
    }
    Write-Host ("Wrote {0} ({1:N1} MB)." -f $DestFull, ((Get-Item $DestFull).Length / 1MB)) -ForegroundColor Green
    Write-Host "On the other PC: unzip it, then run  powershell -ExecutionPolicy Bypass -File $RepoName\scripts\setup_windows.ps1"
} else {
    Invoke-Robocopy $Root $DestFull | Out-Null
    Write-Host "Copied to $DestFull." -ForegroundColor Green
    Write-Host "On the other PC: powershell -ExecutionPolicy Bypass -File `"<that folder>\scripts\setup_windows.ps1`""
}
