<#
Sets up the Outpost app on Windows (the app only - a Windows PC can't be a
mesh node). Run it from the outpost folder, in PowerShell:

    powershell -ExecutionPolicy Bypass -File deploy\windows\setup.ps1

To also copy the mesh's config from an existing node (asks for that node's
password once):

    powershell -ExecutionPolicy Bypass -File deploy\windows\setup.ps1 -FromNode pi@192.168.1.20

It makes a Python environment in .venv, installs Outpost and the web UI,
adds the Opus audio codec (opus.dll), and sets up config\outpost.yaml.
Running it again is safe; it updates what's already there.
Then start Outpost with deploy\windows\run-outpost.cmd.

Needs Windows 10 or later. Uses Python 3.10-3.13 (64-bit) if it's installed;
otherwise it installs Python 3.12 from python.org for this user.
#>
param(
    [string]$FromNode = ""
)

# "Continue", not "Stop": Windows PowerShell 5.1 turns a native command's
# stderr into fatal errors under "Stop". Exit codes are checked instead.
$ErrorActionPreference = "Continue"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root

function Say($text) { Write-Host "== $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "!! $text" -ForegroundColor Red; exit 1 }

# ------------------------------------------------------------------ Python
# The "py" launcher (installed with python.org Python) picks a version;
# plain "python" may be the Microsoft Store placeholder, so check what it is.
function Find-Python {
    foreach ($v in "3.13", "3.12", "3.11", "3.10") {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            $exe = & py "-$v" -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $ok = & python -c "import sys; print(sys.executable if (3, 10) <= sys.version_info[:2] <= (3, 13) else '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $ok) { return $ok.Trim() }
    }
    return $null
}

# No usable Python: install python.org's 64-bit Python for this user only
# (no admin needed). The installer is checked for the Python Software
# Foundation's code signature before it's run.
$PyVersion = "3.12.10"
function Install-Python {
    $exe = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (Test-Path $exe) { return $exe }
    Say "Python not found - installing Python $PyVersion (64-bit) from python.org"
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $url = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-amd64.exe"
    $installer = Join-Path ([IO.Path]::GetTempPath()) "python-$PyVersion-amd64.exe"
    try {
        $ProgressPreference = "SilentlyContinue"   # PowerShell 5.1's progress bar makes downloads crawl
        Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing -ErrorAction Stop
    } catch {
        Write-Host "   couldn't download $url ($($_.Exception.Message))"
        return $null
    }
    try {
        $sig = Get-AuthenticodeSignature $installer
        if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notmatch "Python Software Foundation") {
            Write-Host "   the downloaded installer isn't signed by the Python Software Foundation - not running it"
            return $null
        }
        $p = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList "/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1", "InstallLauncherAllUsers=0", "Include_test=0"
        if ($p.ExitCode -ne 0) {
            Write-Host "   the Python installer failed (exit code $($p.ExitCode))"
            return $null
        }
    } finally { Remove-Item -Force $installer -ErrorAction SilentlyContinue }
    if (Test-Path $exe) { return $exe }
    return $null
}

$Venv = Join-Path $Root ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"

if (-not (Test-Path $VenvPy)) {
    $py = Find-Python
    if (-not $py) { $py = Install-Python }
    if (-not $py) {
        Fail "Python 3.10-3.13 not found. Install it from https://www.python.org/downloads/windows/ (64-bit), then run this again."
    }
    Say "making a Python environment in .venv (using $py)"
    & $py -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Fail "couldn't make the Python environment" }
}

$arch = (& $VenvPy -c "import platform; print(platform.machine())").Trim()
if ($arch -ne "AMD64") {
    Fail "this Python is $arch; Outpost's Opus library is 64-bit x86 only. Install the 64-bit (x64) Python from python.org, delete the .venv folder, and run this again."
}

Say "installing Outpost and its Python packages"
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet -e ".[web]"
if ($LASTEXITCODE -ne 0) { Fail "pip install failed (see above)" }

# ------------------------------------------------------------------- Opus
# Windows has no system libopus. The PyOgg package on PyPI ships a 64-bit
# opus.dll; take just that file (checked against a known hash) and put it
# next to the environment's python.exe, where outpost\codec.py looks.
$OpusDll = Join-Path $Venv "Scripts\opus.dll"
$PyOggWheel = "PyOgg-0.6.14a1-py2.py3-none-win_amd64.whl"
$PyOggSha256 = "40F79B288B3A667309890885F4CF53371163B7DAE17EB17567FB24AB467ECA26"

if (-not (Test-Path $OpusDll)) {
    Say "adding the Opus audio codec (opus.dll)"
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ("outpost-opus-" + [Guid]::NewGuid())
    New-Item -ItemType Directory -Path $tmp -ErrorAction Stop | Out-Null
    try {
        & $VenvPy -m pip download --quiet "PyOgg==0.6.14a1" --platform win_amd64 --only-binary=:all: --no-deps -d $tmp
        $whl = Join-Path $tmp $PyOggWheel
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path $whl)) { Fail "couldn't download $PyOggWheel" }
        if ((Get-FileHash -Algorithm SHA256 $whl).Hash -ne $PyOggSha256) { Fail "$PyOggWheel doesn't match its expected checksum - not using it" }
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [IO.Compression.ZipFile]::OpenRead($whl)
        try {
            $entry = $zip.Entries | Where-Object { $_.FullName -eq "pyogg/opus.dll" }
            if (-not $entry) { Fail "opus.dll not found in $PyOggWheel" }
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $OpusDll, $true)
        } finally { $zip.Dispose() }
    } finally { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
}

& $VenvPy -c "from outpost.codec import OpusDecoder; OpusDecoder()"
if ($LASTEXITCODE -ne 0) { Fail "Opus didn't load - delete .venv\Scripts\opus.dll and run this again" }
Write-Host "   Opus audio codec: OK"

# ------------------------------------------------------------------ config
$Config = Join-Path $Root "config\outpost.yaml"

if ($FromNode) {
    if (-not (Get-Command scp -ErrorAction SilentlyContinue)) {
        Fail "scp not found - turn on Windows' OpenSSH Client (Settings > System > Optional features)"
    }
    Say "copying the mesh's config from $FromNode (you may be asked for its password, and to trust its key)"
    $new = "$Config.new"
    & scp -q -o ConnectTimeout=10 "${FromNode}:outpost/config/outpost.yaml" $new
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $new)) { Fail "couldn't copy ~/outpost/config/outpost.yaml from $FromNode" }
    if (Test-Path $Config) {
        Copy-Item -Force $Config "$Config.bak" -ErrorAction Stop
        Write-Host "   kept the old config as config\outpost.yaml.bak"
    }
    Move-Item -Force $new $Config -ErrorAction Stop
    Write-Host "   Outpost config: copied"
} elseif (-not (Test-Path $Config)) {
    Copy-Item (Join-Path $Root "config\outpost.example.yaml") $Config -ErrorAction Stop
    Write-Host ""
    Write-Host "   config\outpost.yaml is a fresh example config. To join your mesh, run this"
    Write-Host "   again with -FromNode user@address (an existing node), or copy that node's"
    Write-Host "   ~/outpost/config/outpost.yaml here by hand."
}

Write-Host ""
Say "done. Start Outpost with:  deploy\windows\run-outpost.cmd"
Write-Host "   The first time, Windows Firewall asks about Python: allow it on private"
Write-Host "   networks, or Outpost can't hear other nodes. Your Wi-Fi/Ethernet network"
Write-Host "   needs to be set to Private in Windows' network settings, too."
