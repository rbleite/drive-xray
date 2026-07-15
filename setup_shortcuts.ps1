# setup_shortcuts.ps1 — create launch "buttons" for drive-xray on Windows:
# Desktop + Start Menu shortcuts, and (optionally) launch-at-login.
# Also creates the same shortcuts for media-catalog when it is found.
#
# Usage (PowerShell, from the drive-xray folder):
#   .\setup_shortcuts.ps1                    # Desktop + Start Menu shortcuts
#   .\setup_shortcuts.ps1 -Startup           # also start automatically at login
#   .\setup_shortcuts.ps1 -MediaCatalog "C:\Users\you\tools\media-catalog"
#   .\setup_shortcuts.ps1 -Remove            # remove every shortcut this script created
#
# If -MediaCatalog is not given, sibling folders named "media-catalog" /
# "media_catalog" next to this project (and under the same parent) are tried.
# A found app is launched via its own run.bat/start.bat when it has one;
# otherwise as a Streamlit app on port 8502 (drive-xray keeps 8501).

param(
    [switch]$Startup,
    [switch]$Remove,
    [string]$MediaCatalog = ""
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$desktop   = [Environment]::GetFolderPath("Desktop")
$startMenu = [Environment]::GetFolderPath("Programs")
$startupDir = [Environment]::GetFolderPath("Startup")

$shell = New-Object -ComObject WScript.Shell

function New-AppShortcut {
    param($Name, $TargetPath, $Arguments, $WorkDir, $Folder)
    $lnkPath = Join-Path $Folder "$Name.lnk"
    $lnk = $shell.CreateShortcut($lnkPath)
    $lnk.TargetPath = $TargetPath
    if ($Arguments) { $lnk.Arguments = $Arguments }
    $lnk.WorkingDirectory = $WorkDir
    $lnk.WindowStyle = 7    # start minimized — the browser tab is the UI
    $ico = Join-Path $WorkDir "assets\icon.ico"
    if (Test-Path $ico) { $lnk.IconLocation = $ico }
    $lnk.Save()
    Write-Host "  criado: $lnkPath"
}

function Remove-AppShortcut {
    param($Name, $Folder)
    $lnkPath = Join-Path $Folder "$Name.lnk"
    if (Test-Path $lnkPath) {
        Remove-Item $lnkPath
        Write-Host "  removido: $lnkPath"
    }
}

# How to launch an app folder: its own .bat if present, otherwise Streamlit
# (venv streamlit.exe when it exists, else python -m streamlit) on $Port.
function Get-LaunchSpec {
    param($Dir, $Port)
    # start.bat first: in drive-xray it bootstraps the venv on first run
    # (media-catalog's run.bat bootstraps by itself).
    foreach ($bat in @("start.bat", "run.bat")) {
        $p = Join-Path $Dir $bat
        if (Test-Path $p) {
            return @{ Target = $p; Args = "" }
        }
    }
    if (Test-Path (Join-Path $Dir "app.py")) {
        $venvStreamlit = Join-Path $Dir ".venv\Scripts\streamlit.exe"
        if (Test-Path $venvStreamlit) {
            return @{ Target = $venvStreamlit; Args = "run app.py --server.port $Port" }
        }
        return @{ Target = "python"; Args = "-m streamlit run app.py --server.port $Port" }
    }
    return $null
}

# ---- collect the apps to handle -------------------------------------------
$apps = @()
$apps += @{ Name = "drive-xray"; Dir = $here; Port = 8501 }

$mcDir = $null
if ($MediaCatalog) {
    if (Test-Path $MediaCatalog) { $mcDir = (Resolve-Path $MediaCatalog).Path }
    else { Write-Warning "media-catalog não encontrado em: $MediaCatalog" }
} else {
    $parent = Split-Path -Parent $here
    foreach ($cand in @("media-catalog", "media_catalog", "MediaCatalog")) {
        $p = Join-Path $parent $cand
        if (Test-Path $p) { $mcDir = $p; break }
    }
}
if ($mcDir) {
    $apps += @{ Name = "media-catalog"; Dir = $mcDir; Port = 8502 }
} elseif (-not $Remove) {
    Write-Host "  (media-catalog não encontrado — usa -MediaCatalog <pasta> para o incluir)"
}

# ---- create / remove -------------------------------------------------------
foreach ($app in $apps) {
    if ($Remove) {
        foreach ($folder in @($desktop, $startMenu, $startupDir)) {
            Remove-AppShortcut -Name $app.Name -Folder $folder
        }
        continue
    }
    $spec = Get-LaunchSpec -Dir $app.Dir -Port $app.Port
    if (-not $spec) {
        Write-Warning "$($app.Name): sem run.bat/start.bat/app.py em $($app.Dir) — ignorado"
        continue
    }
    Write-Host "$($app.Name)  →  $($spec.Target) $($spec.Args)"
    New-AppShortcut -Name $app.Name -TargetPath $spec.Target -Arguments $spec.Args `
                    -WorkDir $app.Dir -Folder $desktop
    New-AppShortcut -Name $app.Name -TargetPath $spec.Target -Arguments $spec.Args `
                    -WorkDir $app.Dir -Folder $startMenu
    if ($Startup) {
        New-AppShortcut -Name $app.Name -TargetPath $spec.Target -Arguments $spec.Args `
                        -WorkDir $app.Dir -Folder $startupDir
        Write-Host "  ($($app.Name) vai arrancar automaticamente no login)"
    }
}

if (-not $Remove) {
    Write-Host ""
    Write-Host "Feito. Atalhos no Ambiente de Trabalho e Menu Iniciar$(if ($Startup) { ' + arranque no login' })."
    Write-Host "Para desfazer:  .\setup_shortcuts.ps1 -Remove"
}
