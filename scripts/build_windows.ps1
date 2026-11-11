param(
    [string]$InnoSetupCompiler
)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if ($env:OS -ne 'Windows_NT') {
    throw 'This script must run on native Windows; cross-compilation is not supported.'
}
$Uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $Uv) {
    throw 'uv was not found. Install uv for this Windows user, then rerun this script.'
}
$env:UV_CACHE_DIR = if ($env:UV_CACHE_DIR) { $env:UV_CACHE_DIR } else { Join-Path $Root '.uv-cache' }
$env:PYINSTALLER_CONFIG_DIR = if ($env:PYINSTALLER_CONFIG_DIR) { $env:PYINSTALLER_CONFIG_DIR } else { Join-Path $Root 'build\pyinstaller-cache' }
Push-Location $Root
try {
    & $Uv.Source sync --python 3.13 --locked --group dev
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed with exit code $LASTEXITCODE" }
    $Python = Join-Path $Root '.venv\Scripts\python.exe'
    $PythonVersion = & $Python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
    if ($LASTEXITCODE -ne 0 -or $PythonVersion -ne '3.13') { throw "Expected Python 3.13 in the project environment, got $PythonVersion" }
    & (Join-Path $Root '.venv\Scripts\pyinstaller.exe') --noconfirm --clean `
        --distpath (Join-Path $Root 'dist\windows') --workpath (Join-Path $Root 'build\windows') `
        (Join-Path $Root 'packaging\nova-mic-switch.spec')
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
    $Bundle = Join-Path $Root 'dist\windows\NovaMicSwitch'
    Write-Output "Windows bundle created at $Bundle"

    $Compiler = $InnoSetupCompiler
    if (-not $Compiler) {
        $Found = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if ($Found) { $Compiler = $Found.Source }
    }
    if ($Compiler) {
        if (-not (Test-Path -LiteralPath $Compiler -PathType Leaf)) {
            throw "Inno Setup compiler not found: $Compiler"
        }
        $CompilerVersion = (Get-Item -LiteralPath $Compiler).VersionInfo.ProductVersion
        if (-not $CompilerVersion) { $CompilerVersion = (Get-Item -LiteralPath $Compiler).VersionInfo.FileVersion }
        if ($CompilerVersion -and $CompilerVersion -notmatch '^6\.7\.4(?:\.|$)') {
            throw "Expected Inno Setup compiler 6.7.4, got $CompilerVersion"
        }
        $Version = & $Python -c 'import tomllib; print(tomllib.loads(open("pyproject.toml", "rb").read().decode())["project"]["version"])'
        if ($LASTEXITCODE -ne 0 -or $Version -notmatch '^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$') {
            throw "Could not read a safe release version from pyproject.toml: $Version"
        }
        New-Item -ItemType Directory -Force -Path (Join-Path $Root 'dist\installers') | Out-Null
        & $Compiler "/DAppVersion=$Version" (Join-Path $Root 'packaging\windows\installer.iss')
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }
        $Installer = Join-Path $Root "dist\installers\NovaMicSwitch-Setup-$Version.exe"
        if (-not (Test-Path -LiteralPath $Installer -PathType Leaf)) { throw "Expected installer was not created: $Installer" }
        $Hash = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant()
        $HashLine = "$Hash  $(Split-Path -Leaf $Installer)"
        [System.IO.File]::WriteAllText("$Installer.sha256", "$HashLine`n", [System.Text.Encoding]::ASCII)
        [System.IO.File]::WriteAllText((Join-Path $Root 'dist\installers\SHA256SUMS'), "$HashLine`n", [System.Text.Encoding]::ASCII)
        Write-Output "Installer created under $(Join-Path $Root 'dist\installers')"
    } else {
        Write-Warning 'Inno Setup was not found; only the portable onedir bundle was built. Install Inno Setup 6.7+ and rerun with -InnoSetupCompiler <ISCC.exe> to create the installer.'
    }
} finally {
    Pop-Location
}
