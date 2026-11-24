<#
.SYNOPSIS
    Set up godot2splat (g2s) on a Windows PC with an NVIDIA GPU, and check everything a scan needs.

.DESCRIPTION
    Run from anywhere:   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1

    1. uv: uses the one on PATH. If there is none, installs it for this user only
       (astral.sh installer, into %USERPROFILE%\.local\bin), and says so first.
    2. Python environment: uv sync --python 3.12 --extra gpu --extra dev (.venv in the repository).
       torch comes from the pytorch-cu128 index set in pyproject.toml: RTX 50-series (Blackwell,
       sm_120) needs that CUDA 12.8 build.
    3. Checks: torch sees the GPU, gsplat imports, the CUDA Toolkit and Visual Studio 2022 C++
       compiler that gsplat compiles its kernels with on first use, Godot (G2S_GODOT),
       Unreal Engine 5.8, and the .NET Framework 4.8 SDK that Unreal's build tool needs.
    4. Compiles gsplat's kernels for this GPU and tests them (tests/test_gsplat_gpu.py; a few
       minutes the first time, cached in tools\torch_extensions).

    Nothing is installed system-wide. Whatever is missing is listed at the end with the command
    that installs it; you run those yourself.

.PARAMETER DryRun
    Only check. Does not install uv, does not run uv sync, does not compile gsplat.
    The Python checks use the existing .venv if there is one.

.PARAMETER SkipGsplatBuild
    Skip step 4.

.PARAMETER UnrealVersion
    Unreal Engine version to look for (default 5.8).

.PARAMETER NoUnreal
    Do not check Unreal Engine and its build tools (Godot scans only).
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$SkipGsplatBuild,
    [string]$UnrealVersion = "5.8",
    [switch]$NoUnreal
)

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$Missing = New-Object System.Collections.ArrayList   # each: @{ What; Fix }
$Warnings = New-Object System.Collections.ArrayList

function Ok([string]$msg)   { Write-Host "  [ok]      $msg" -ForegroundColor Green }
function Bad([string]$msg)  { Write-Host "  [missing] $msg" -ForegroundColor Red }
function Warn([string]$msg) { Write-Host "  [warning] $msg" -ForegroundColor Yellow; [void]$Warnings.Add($msg) }
function Info([string]$msg) { Write-Host "  $msg" }
function Step([string]$msg) { Write-Host ""; Write-Host "== $msg" -ForegroundColor Cyan }
function Need([string]$what, [string]$fix) {
    Bad $what
    [void]$Missing.Add(@{ What = $what; Fix = $fix })
}

Write-Host "godot2splat setup: $Root"
if ($DryRun) { Write-Host "Dry run: checking only, nothing is installed or changed." -ForegroundColor Yellow }

# --------------------------------------------------------------------------- uv
Step "uv (Python package manager)"
$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    $userUv = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path $userUv) { $uv = Get-Item $userUv }
}
if ($uv) {
    $uvPath = if ($uv.Source) { $uv.Source } else { $uv.FullName }
    Ok "$(& $uvPath --version) ($uvPath)"
} elseif ($DryRun) {
    Need "uv is not installed" 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"    # per user, no admin; or: winget install --id astral-sh.uv -e'
} else {
    Info "uv not found. Installing it for this user only (into $env:USERPROFILE\.local\bin, no admin rights):"
    Info '  irm https://astral.sh/uv/install.ps1 | iex'
    try {
        Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Write-Host "  uv install failed: $_" -ForegroundColor Red
    }
    $uvPath = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path $uvPath) {
        $env:Path = (Split-Path $uvPath) + ";" + $env:Path
        Ok "installed $(& $uvPath --version) ($uvPath)"
        Info "Open a new terminal afterwards so that 'uv' is on PATH there too."
    } else {
        $uvPath = $null
        Need "uv could not be installed" 'winget install --id astral-sh.uv -e'
    }
}

# --------------------------------------------------------------------------- NVIDIA driver
Step "NVIDIA GPU and driver"
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if ($smi) {
    $gpus = & nvidia-smi --query-gpu=name,driver_version,memory.total,compute_cap --format=csv,noheader 2>$null
    foreach ($line in @($gpus)) {
        $f = $line -split ",\s*"
        Ok "$($f[0]), driver $($f[1]), $($f[2]), compute capability $($f[3])"
        $major = 0
        [void][int]::TryParse(($f[1] -split "\.")[0], [ref]$major)
        if ($major -gt 0 -and $major -lt 570) {
            Need "NVIDIA driver $($f[1]) is too old for CUDA 12.8 (needs 570 or newer)" "Install the latest Game Ready or Studio driver from https://www.nvidia.com/Download/index.aspx (or the NVIDIA App)"
        }
    }
} else {
    Need "nvidia-smi not found: no NVIDIA driver" "Install the NVIDIA driver (570 or newer) from https://www.nvidia.com/Download/index.aspx"
}

# --------------------------------------------------------------------------- Python environment
Step "Python environment (.venv)"
if ($DryRun) {
    Info "skipped: uv sync --python 3.12 --extra gpu --extra dev"
} elseif ($uvPath) {
    Info "uv sync --python 3.12 --extra gpu --extra dev   (torch from the pytorch-cu128 index, several GB the first time)"
    Push-Location $Root
    try {
        & $uvPath sync --python 3.12 --extra gpu --extra dev
        $syncCode = $LASTEXITCODE
    } finally { Pop-Location }
    if ($syncCode -eq 0) { Ok "uv sync done" }
    else { Need "uv sync failed (exit code $syncCode, see its output above)" "cd `"$Root`"; uv sync --python 3.12 --extra gpu --extra dev" }
}

$py = $null
if (Test-Path $VenvPython) {
    $checkPy = @'
import json
r = {}
try:
    import torch
    r["torch"] = torch.__version__
    r["torch_cuda"] = torch.version.cuda
    r["cuda_ok"] = torch.cuda.is_available()
    if r["cuda_ok"]:
        major, minor = torch.cuda.get_device_capability(0)
        r["gpu"] = torch.cuda.get_device_name(0)
        r["arch"] = "sm_%d%d" % (major, minor)
        r["arch_list"] = torch.cuda.get_arch_list()
        # A kernel built for sm_XY runs on any GPU of the same major version and an equal or
        # higher minor (sm_86 runs on an RTX 40-series, sm_89, which torch does not list);
        # one with a suffix (sm_90a) runs only on exactly that GPU. PTX (compute_XY) is
        # compiled by the driver for any GPU at or above it.
        import re
        def runs_here(entry):
            m = re.fullmatch(r"(sm|compute)_(\d+)(\d)([a-z]?)", entry)
            if not m:
                return False
            built = (int(m.group(2)), int(m.group(3)))
            if m.group(1) == "compute":
                return built <= (major, minor)
            if m.group(4):
                return built == (major, minor)
            return built[0] == major and built[1] <= minor
        r["arch_ok"] = any(runs_here(a) for a in r["arch_list"])
        r["vram_gb"] = round(torch.cuda.get_device_properties(0).total_memory / 2**30, 1)
except Exception as e:
    r["torch_error"] = repr(e)
try:
    import gsplat
    r["gsplat"] = gsplat.__version__
except Exception as e:
    r["gsplat_error"] = repr(e)
try:
    from g2s import gsplat_compat
    r["vcvars"] = gsplat_compat.find_vcvars()
    r["cuda_toolkit"] = gsplat_compat.find_cuda_toolkit(r.get("torch_cuda"))
except Exception as e:
    r["g2s_error"] = repr(e)
print("G2S_JSON " + json.dumps(r))
'@
    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) "g2s_setup_check.py"
    Set-Content -Path $tmp -Value $checkPy -Encoding ASCII
    $out = & $VenvPython -I $tmp 2>&1 | ForEach-Object { "$_" }
    Remove-Item $tmp -ErrorAction SilentlyContinue
    $jsonLine = $out | Where-Object { $_ -like "G2S_JSON *" } | Select-Object -Last 1
    if ($jsonLine) { $py = $jsonLine.Substring(9) | ConvertFrom-Json }
    else { Need "the Python check did not run: $($out -join ' ')" "cd `"$Root`"; uv sync --python 3.12 --extra gpu --extra dev" }
} else {
    Need "no .venv in $Root" "cd `"$Root`"; uv sync --python 3.12 --extra gpu --extra dev    # or run this script without -DryRun"
}

$syncFix = "cd `"$Root`"; uv sync --python 3.12 --extra gpu --extra dev"
if ($py) {
    if ($py.torch_error) {
        Need "torch does not import: $($py.torch_error)" $syncFix
    } elseif (-not $py.torch_cuda) {
        Need "torch $($py.torch) is a CPU-only build" "$syncFix    # --extra gpu takes torch from the pytorch-cu128 index"
    } elseif (-not $py.cuda_ok) {
        Need "torch $($py.torch) (CUDA $($py.torch_cuda)) cannot use the GPU: torch.cuda.is_available() is False" "Update the NVIDIA driver (570 or newer), then run: $syncFix"
    } else {
        Ok "torch $($py.torch), CUDA $($py.torch_cuda), $($py.gpu) ($($py.arch), $($py.vram_gb) GB)"
        if ($py.arch_list -and -not $py.arch_ok) {
            Need "this torch build has no kernels that run on $($py.arch) (it has: $($py.arch_list -join ', '))" "$syncFix    # RTX 50-series needs the cu128 build (pytorch-cu128 index in pyproject.toml)"
        }
    }
    if ($py.gsplat_error) { Need "gsplat does not import: $($py.gsplat_error)" $syncFix }
    elseif ($py.gsplat) { Ok "gsplat $($py.gsplat) imports" }

    # gsplat compiles its CUDA kernels on first use: needs the CUDA Toolkit torch was built for, and MSVC 2022.
    if ($py.g2s_error) {
        Need "g2s does not import: $($py.g2s_error)" $syncFix
    } else {
        if ($py.cuda_toolkit) { Ok "CUDA Toolkit for gsplat's kernels: $($py.cuda_toolkit)" }
        elseif ($py.torch_cuda) {
            Need "CUDA Toolkit $($py.torch_cuda) (gsplat compiles its kernels with it on first use)" "winget install --exact --id Nvidia.CUDA --version $($py.torch_cuda)    # system-wide, installs next to other CUDA versions"
        }
    }
}

# --------------------------------------------------------------------------- Visual Studio 2022 C++ and .NET Framework 4.8 SDK
Step "Visual Studio 2022 C++ toolchain (gsplat kernels; Unreal plugin build)"
$vswhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
$vsInstaller = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\setup.exe"
$vcComp = "Microsoft.VisualStudio.Component.VC.Tools.x86.x64"
$netComp = "Microsoft.Net.Component.4.8.SDK"
$freshVs = 'winget install --id Microsoft.VisualStudio.2022.BuildTools -e --override "--wait --passive --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --add Microsoft.Net.Component.4.8.SDK"    # system-wide, about 6 GB, asks for admin'
$vs2022 = @()
if (Test-Path $vswhere) {
    $vs2022 = @(& $vswhere -version "[17.0,18.0)" -products * -property installationPath)
}
if ($vs2022.Count -eq 0) {
    Need "Visual Studio 2022 (or its Build Tools) with the C++ workload and the .NET Framework 4.8 SDK" $freshVs
} else {
    $vsPath = $vs2022[0]
    $withVc = @(& $vswhere -version "[17.0,18.0)" -products * -requires $vcComp -property installationPath)
    if ($withVc.Count -gt 0) {
        $vsPath = $withVc[0]
        Ok "MSVC C++ x64 tools: $vsPath"
    } else {
        Need "the C++ workload of Visual Studio 2022 at $vsPath" "& `"$vsInstaller`" modify --installPath `"$vsPath`" --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --passive    # system-wide, asks for admin"
    }
    if (-not $NoUnreal) {
        $withNet = @(& $vswhere -version "[17.0,18.0)" -products * -requires $netComp -property installationPath)
        $netSdkDir = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\NETFXSDK\4.8"
        if ($withNet.Count -gt 0 -or (Test-Path $netSdkDir)) {
            Ok ".NET Framework 4.8 SDK (Unreal's build tool needs it)"
        } else {
            Need ".NET Framework 4.8 SDK (Unreal's build tool needs it)" "& `"$vsInstaller`" modify --installPath `"$vsPath`" --add Microsoft.Net.Component.4.8.SDK --passive    # system-wide, asks for admin"
        }
    }
    $winSdk = Join-Path ${env:ProgramFiles(x86)} "Windows Kits\10\Include"
    if (Test-Path $winSdk) { Ok "Windows SDK: $((Get-ChildItem $winSdk -Directory | Sort-Object Name | Select-Object -Last 1).Name)" }
    else { Need "Windows 10/11 SDK" "& `"$vsInstaller`" modify --installPath `"$vsPath`" --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended --passive    # system-wide, asks for admin" }
}
$newerVs = @()
if (Test-Path $vswhere) { $newerVs = @(& $vswhere -version "[18.0," -products * -property installationPath) }
if ($newerVs.Count -gt 0 -and $vs2022.Count -eq 0) {
    Info "Found a newer Visual Studio ($($newerVs[0])). CUDA 12.8 does not accept it: 2022 Build Tools are still needed, next to it."
}

# --------------------------------------------------------------------------- Godot
Step "Godot 4.3 or newer (G2S_GODOT)"
function Find-Godot {
    if ($env:G2S_GODOT) { return @{ Path = $env:G2S_GODOT; From = "G2S_GODOT" } }
    foreach ($name in "godot", "godot4") {
        $c = Get-Command $name -ErrorAction SilentlyContinue
        if ($c) { return @{ Path = $c.Source; From = "PATH ($name)" } }
    }
    $cands = @()
    $cands += Get-Command "Godot*.exe" -ErrorAction SilentlyContinue | ForEach-Object { $_.Source }
    $dirs = @(
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"),
        (Join-Path $env:USERPROFILE "Desktop"),
        (Join-Path $env:USERPROFILE "Downloads"),
        (Join-Path $env:USERPROFILE "scoop\apps\godot\current"),
        $env:ProgramFiles,
        (Join-Path $env:ProgramFiles "Godot")
    )
    foreach ($d in $dirs) {
        if ($d -and (Test-Path $d)) {
            $cands += Get-ChildItem -Path $d -Filter "Godot*.exe" -Recurse -Depth 2 -File -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }
        }
    }
    $cands = @($cands | Where-Object { $_ -notmatch "_console\.exe$" } | Select-Object -Unique)
    if ($cands.Count -gt 0) { return @{ Path = ($cands | Sort-Object | Select-Object -Last 1); From = "found, G2S_GODOT not set" } }
    return $null
}
$godot = Find-Godot
if (-not $godot) {
    Need "Godot 4.3+ (not on PATH, G2S_GODOT not set)" 'winget install --id GodotEngine.GodotEngine -e    # per user; then: setx G2S_GODOT "<path to the Godot .exe>"'
} elseif (-not (Test-Path $godot.Path) -and -not (Get-Command $godot.Path -ErrorAction SilentlyContinue)) {
    Need "G2S_GODOT points at $($godot.Path), which does not exist" 'setx G2S_GODOT "<path to the Godot .exe>"    # then open a new terminal'
} else {
    $ver = (& $godot.Path --version 2>$null | Select-Object -Last 1)
    $ok = $false
    if ("$ver" -match "^(\d+)\.(\d+)") { $ok = ([int]$Matches[1] -gt 4) -or ([int]$Matches[1] -eq 4 -and [int]$Matches[2] -ge 3) }
    if (-not $ok) {
        Need "Godot at $($godot.Path) reports version '$ver'; 4.3 or newer is needed" 'winget install --id GodotEngine.GodotEngine -e'
    } else {
        Ok "Godot $ver ($($godot.Path); $($godot.From))"
        if ($godot.From -ne "G2S_GODOT" -and $godot.From -notlike "PATH (godot*") {
            Need "G2S_GODOT is not set, and 'godot' is not on PATH (g2s looks for those)" "setx G2S_GODOT `"$($godot.Path)`"    # per user; open a new terminal afterwards"
        }
    }
}

# --------------------------------------------------------------------------- Unreal Engine
if (-not $NoUnreal) {
    Step "Unreal Engine $UnrealVersion"
    $ue = $null
    if ($env:G2S_UNREAL) {
        if (Test-Path $env:G2S_UNREAL) { Ok "G2S_UNREAL: $env:G2S_UNREAL" ; $ue = "set" }
        else { Need "G2S_UNREAL points at $env:G2S_UNREAL, which does not exist" "setx G2S_UNREAL `"<path to UnrealEditor-Cmd.exe>`"    # or remove it" }
    }
    if (-not $ue) {
        $cands = @()
        $reg = Get-ItemProperty "HKLM:\SOFTWARE\EpicGames\Unreal Engine\$UnrealVersion" -ErrorAction SilentlyContinue
        if ($reg -and $reg.InstalledDirectory) { $cands += $reg.InstalledDirectory }
        $cands += Join-Path $env:ProgramFiles "Epic Games\UE_$UnrealVersion"
        $cands += Join-Path $env:USERPROFILE "UnrealEngine\UE_$UnrealVersion"
        foreach ($c in $cands) {
            $exe = Join-Path $c "Engine\Binaries\Win64\UnrealEditor-Cmd.exe"
            if (Test-Path $exe) { $ue = $c; break }
        }
        if ($ue) {
            Ok "Unreal Engine $UnrealVersion at $ue"
            $uat = Join-Path $ue "Engine\Build\BatchFiles\RunUAT.bat"
            if (-not (Test-Path $uat)) { Need "RunUAT.bat missing in $ue (g2s install compiles the plugin with it)" "Epic Games Launcher > Unreal Engine > Library > $UnrealVersion > Verify" }
        } else {
            Need "Unreal Engine $UnrealVersion (looked in: $($cands -join '; '))" "Epic Games Launcher > Unreal Engine > Library > + > $UnrealVersion > Install    (or skip with -NoUnreal for Godot-only scans)"
        }
    }
}

# --------------------------------------------------------------------------- gsplat kernels
Step "gsplat kernels for this GPU"
$canBuild = $py -and $py.cuda_ok -and $py.gsplat -and $py.cuda_toolkit -and $py.vcvars
if ($DryRun) {
    Info "skipped (dry run): .venv\Scripts\python.exe -m pytest tests\test_gsplat_gpu.py"
} elseif ($SkipGsplatBuild) {
    Info "skipped (-SkipGsplatBuild). The first g2s build compiles them instead."
} elseif (-not $canBuild) {
    Info "skipped: fix the items above first, then run this script again."
} else {
    Info "Compiling (a few minutes the first time) and testing against a plain torch renderer:"
    Push-Location $Root
    try {
        & $VenvPython -m pytest tests\test_gsplat_gpu.py -q
        $testCode = $LASTEXITCODE
    } finally { Pop-Location }
    if ($testCode -eq 0) { Ok "gsplat kernels compiled and tested" }
    else { Need "gsplat kernel build or test failed (exit code $testCode, see above)" "cd `"$Root`"; .venv\Scripts\python.exe -m pytest tests\test_gsplat_gpu.py -q    # read the first error" }
}

# --------------------------------------------------------------------------- summary
Write-Host ""
if ($Missing.Count -eq 0) {
    Write-Host "Ready. Next:" -ForegroundColor Green
    Write-Host "  .venv\Scripts\Activate.ps1"
    Write-Host "  g2s install `"path\to\MyGame.uproject`"       # compiles the Unreal plugin on this machine"
    Write-Host "  g2s install path\to\godot_project"
    Write-Host "  g2s build `"path\to\MyGame.uproject`" /Game/Maps/MyLevel --godot-project path\to\godot_project"
    exit 0
}
Write-Host "Missing ($($Missing.Count)). Fix each, then run this script again:" -ForegroundColor Red
$i = 1
foreach ($m in $Missing) {
    Write-Host ""
    Write-Host "$i. $($m.What)" -ForegroundColor Red
    Write-Host "   $($m.Fix)"
    $i++
}
exit 1
