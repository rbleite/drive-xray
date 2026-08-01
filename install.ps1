# install.ps1 -- one-step setup for drive-xray + media-catalog on Windows.
#
# Written for someone starting from a clean machine with NOTHING installed:
# no Python, no git. It installs what is missing, fetches both apps, sets up
# their virtual environments, and creates desktop buttons -- so the first
# thing the user has to understand is "double-click this", not "clone a repo".
#
# Usage -- there are two ways in.
#
#   1. From a clean machine, paste this into PowerShell (no download needed):
#        irm https://raw.githubusercontent.com/rbleite/drive-xray/main/install.ps1 | iex
#
#   2. From an existing checkout, double-click install.bat, or:
#        .\install.ps1                        # install/update both apps
#        .\install.ps1 -Path "D:\apps"        # somewhere other than ~\tools
#        .\install.ps1 -SkipRustEngine        # do not fetch dx.exe
#        .\install.ps1 -SkipShortcuts         # no desktop/start-menu buttons
#
# Re-running is safe and is how you update: existing checkouts are pulled
# rather than re-cloned, and anything already installed is left alone.
#
# NOTE: keep this file pure ASCII. Windows PowerShell 5.1 reads BOM-less
# .ps1 files as ANSI, and mangled UTF-8 punctuation (curly quotes,
# em-dashes) can terminate strings mid-line and break parsing.

param(
    [string]$Path = "",
    [switch]$SkipRustEngine,
    [switch]$SkipShortcuts,
    [switch]$Startup
)

$ErrorActionPreference = "Stop"
# Invoke-WebRequest is an order of magnitude slower with the progress bar on,
# and PowerShell 5.1 still defaults to TLS 1.0 on some builds, which github.com
# refuses outright.
$ProgressPreference = "SilentlyContinue"
try {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

$REPOS = @(
    @{ Name = "drive-xray";    Url = "https://github.com/rbleite/drive-xray.git" },
    @{ Name = "media-catalog"; Url = "https://github.com/rbleite/media-catalog.git" }
)
$MIN_PY = [Version]"3.10"

function Write-Step { param($m) Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Write-Ok   { param($m) Write-Host "    OK: $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "    !! $m" -ForegroundColor Yellow }


function Update-SessionPath {
    # winget and the .exe installers update the registry, not this process.
    # Without this refresh, a tool installed above is still "missing" below.
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user    = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ";"
}


function Test-RealPython {
    param([string]$Exe)
    # Windows ships a python.exe stub under WindowsApps that only opens the
    # Microsoft Store. It answers Get-Command and even `--version` on some
    # builds, so the only reliable test is asking it to actually run code.
    if ($Exe -like "*\WindowsApps\*") { return $null }
    try {
        $out = & $Exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    } catch { return $null }
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    try { return [Version]$out.Trim() } catch { return $null }
}


function Find-Python {
    # Prefer the py launcher: it finds real installs even when PATH is a mess.
    foreach ($cand in @("py", "python", "python3")) {
        $cmd = Get-Command $cand -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        $exe = $cmd.Source
        if ($cand -eq "py") {
            # ask the launcher for the newest interpreter it knows about
            try { $exe = (& py -3 -c "import sys; print(sys.executable)" 2>$null) } catch { continue }
            if (-not $exe) { continue }
            $exe = $exe.Trim()
        }
        $ver = Test-RealPython $exe
        if ($ver -and $ver -ge $MIN_PY) { return @{ Exe = $exe; Version = $ver } }
    }
    return $null
}


function Install-ViaWinget {
    param([string]$Id, [string]$Label)
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { return $false }
    Write-Host "    installing $Label via winget..."
    # --scope user avoids the UAC prompt; some packages ignore it and fall back
    # to machine scope, which is fine when the shell is already elevated.
    & winget install --id $Id -e --source winget `
        --accept-package-agreements --accept-source-agreements `
        --disable-interactivity --scope user 2>&1 | Out-Null
    Update-SessionPath
    return $true
}


function Install-Python {
    Write-Step "Python $MIN_PY or newer is required"
    if (Install-ViaWinget "Python.Python.3.12" "Python 3.12") {
        $py = Find-Python
        if ($py) { return $py }
        Write-Warn "winget finished but Python is still not on PATH; trying the installer"
    }
    # Fallback: the official installer, per-user and silent. PrependPath=1 is
    # the checkbox everybody forgets, and its absence is the single most common
    # reason these instructions fail.
    $ver = "3.12.8"
    $url = "https://www.python.org/ftp/python/$ver/python-$ver-amd64.exe"
    $exe = Join-Path $env:TEMP "python-$ver-amd64.exe"
    Write-Host "    downloading $url"
    Invoke-WebRequest -Uri $url -OutFile $exe -UseBasicParsing
    Write-Host "    running the installer (this takes a minute)..."
    Start-Process -FilePath $exe -Wait -ArgumentList @(
        "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_pip=1",
        "Include_launcher=1", "SimpleInstall=1")
    Remove-Item $exe -ErrorAction SilentlyContinue
    Update-SessionPath
    $py = Find-Python
    if (-not $py) {
        throw "Python still not found after installing. Close this window, open a new PowerShell, and run this script again."
    }
    return $py
}


function Install-Git {
    Write-Step "git is required (the apps update themselves with it)"
    if (Install-ViaWinget "Git.Git" "Git for Windows") {
        if (Get-Command git -ErrorAction SilentlyContinue) { return }
        Write-Warn "winget finished but git is still not on PATH; trying the installer"
    }
    # Fallback: resolve the current Git for Windows installer from the API,
    # so this does not rot every time they cut a release.
    $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/git-for-windows/git/releases/latest" -UseBasicParsing
    $asset = $rel.assets | Where-Object { $_.name -like "*-64-bit.exe" } | Select-Object -First 1
    if (-not $asset) { throw "Could not find a Git for Windows installer to download." }
    $exe = Join-Path $env:TEMP $asset.name
    Write-Host "    downloading $($asset.name)"
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $exe -UseBasicParsing
    Write-Host "    running the installer..."
    Start-Process -FilePath $exe -Wait -ArgumentList @("/VERYSILENT", "/NORESTART", "/NOCANCEL")
    Remove-Item $exe -ErrorAction SilentlyContinue
    Update-SessionPath
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "git still not found after installing. Close this window, open a new PowerShell, and run this script again."
    }
}


function Sync-Repo {
    param([string]$Url, [string]$Dest, [string]$Name)
    if (Test-Path (Join-Path $Dest ".git")) {
        Write-Host "    updating $Name..."
        # A local edit must never be silently discarded, so this only
        # fast-forwards; anything else is reported and left for the user.
        & git -C $Dest pull --ff-only 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "$Name has local changes or a diverged branch -- left untouched."
        } else {
            Write-Ok "$Name up to date"
        }
    } else {
        Write-Host "    downloading $Name..."
        & git clone --quiet $Url $Dest
        if ($LASTEXITCODE -ne 0) { throw "Could not download $Name from $Url" }
        Write-Ok "$Name downloaded to $Dest"
    }
}


function Initialize-Venv {
    param([string]$Root, [string]$PythonExe, [string]$Name)
    $venvPy = Join-Path $Root ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPy)) {
        Write-Host "    creating the $Name environment..."
        & $PythonExe -m venv (Join-Path $Root ".venv")
        if ($LASTEXITCODE -ne 0) { throw "Could not create a virtual environment in $Root" }
    }
    $req = Join-Path $Root "requirements.txt"
    if (Test-Path $req) {
        Write-Host "    installing $Name dependencies..."
        & $venvPy -m pip install --quiet --upgrade pip 2>&1 | Out-Null
        & $venvPy -m pip install --quiet -r $req
        if ($LASTEXITCODE -ne 0) { throw "Could not install dependencies for $Name" }
    }
    Write-Ok "$Name ready"
}


function Get-RustEngine {
    param([string]$Root)
    # Optional: the pure-Python engine indexes everything on its own. This
    # binary is just faster on very large drives, so a failure here is a
    # warning, never fatal.
    if ($env:PROCESSOR_ARCHITECTURE -notmatch "64") {
        Write-Warn "skipping the fast engine (needs a 64-bit system)"
        return
    }
    if (Test-Path (Join-Path $Root "dx.exe")) { Write-Ok "fast engine already present"; return }
    try {
        $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/rbleite/drive-xray/releases/latest" -UseBasicParsing
        $asset = $rel.assets | Where-Object { $_.name -like "*windows-x86_64.zip" } | Select-Object -First 1
        if (-not $asset) { Write-Warn "no Windows engine in the latest release; using the Python engine"; return }
        $zip = Join-Path $env:TEMP $asset.name
        $tmp = Join-Path $env:TEMP ("dx-" + [Guid]::NewGuid().ToString("N"))
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -UseBasicParsing
        Expand-Archive -Path $zip -DestinationPath $tmp -Force
        $dx = Get-ChildItem -Path $tmp -Filter "dx.exe" -Recurse | Select-Object -First 1
        if ($dx) {
            Copy-Item $dx.FullName (Join-Path $Root "dx.exe") -Force
            Write-Ok "fast engine installed ($($rel.tag_name))"
        }
        Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue
    } catch {
        Write-Warn "could not fetch the fast engine ($($_.Exception.Message)); the Python engine works fine"
    }
}


# --------------------------------------------------------------------------

Write-Host ""
Write-Host "  drive-xray + media-catalog -- Windows setup" -ForegroundColor White
Write-Host "  Installs anything missing, then creates desktop buttons."

# Default to ~\tools because that is where both projects already look for each
# other: media-catalog finds drive-xray's engine at ~\tools\drive-xray, and
# the shortcut script pairs them as siblings.
if (-not $Path) { $Path = Join-Path $env:USERPROFILE "tools" }
New-Item -ItemType Directory -Force -Path $Path | Out-Null
Write-Step "Installing into $Path"

$py = Find-Python
if ($py) {
    Write-Ok "found Python $($py.Version)"
} else {
    $py = Install-Python
    Write-Ok "installed Python $($py.Version)"
}

if (Get-Command git -ErrorAction SilentlyContinue) {
    Write-Ok "found git"
} else {
    Install-Git
    Write-Ok "installed git"
}

Write-Step "Getting the apps"
foreach ($repo in $REPOS) {
    Sync-Repo -Url $repo.Url -Dest (Join-Path $Path $repo.Name) -Name $repo.Name
}

Write-Step "Setting up the environments"
foreach ($repo in $REPOS) {
    Initialize-Venv -Root (Join-Path $Path $repo.Name) -PythonExe $py.Exe -Name $repo.Name
}

$dxRoot = Join-Path $Path "drive-xray"
if (-not $SkipRustEngine) {
    Write-Step "Fast indexing engine (optional)"
    Get-RustEngine -Root $dxRoot
}

if (-not $SkipShortcuts) {
    Write-Step "Creating desktop buttons"
    $shortcuts = Join-Path $dxRoot "setup_shortcuts.ps1"
    if (Test-Path $shortcuts) {
        # NB: not $args -- that is an automatic variable in PowerShell
        $scArgs = @{ MediaCatalog = (Join-Path $Path "media-catalog") }
        if ($Startup) { $scArgs["Startup"] = $true }
        & $shortcuts @scArgs
    } else {
        Write-Warn "setup_shortcuts.ps1 not found -- skipping"
    }
}

Write-Host ""
Write-Host "  Done." -ForegroundColor Green
Write-Host ""
Write-Host "  Look for 'drive-xray' on your Desktop and double-click it."
Write-Host "  Index a drive there first -- media-catalog reads what it produces,"
Write-Host "  so it has nothing to show until drive-xray has scanned something."
Write-Host ""
Write-Host "  Installed in: $Path"
Write-Host "  To update later, run this script again."
Write-Host ""
