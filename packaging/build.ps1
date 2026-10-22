<#
.SYNOPSIS
Build the Pixheet desktop app for Windows and test the result.

.DESCRIPTION
The script does these steps:
  1. Install the locked dependencies with uv: the gui extra and the build group.
  2. Build dist\Pixheet.exe with PyInstaller from packaging\pixheet.spec. Two builds of
     one commit with the same tools give the same exe.
  3. Test the exe with packaging\check_exe.py. The exe must pass the smoke test (--smoke)
     and convert a test image correctly. It must also convert images of many formats to
     the same sheets as pixheet in Python. check_exe.py stops the exe and all processes
     that it started if the exe does not stop in time.
  4. Write dist\Pixheet.exe.sha256.
The script stops with exit code 1 when a step fails. It runs on Windows PowerShell 5.1.

.PARAMETER OneDir
Build the folder dist\Pixheet\ in place of one file.

.EXAMPLE
powershell -ExecutionPolicy Bypass -File packaging\build.ps1
#>
param(
    [switch]$OneDir
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Invoke-Step {
    param([string]$Name, [scriptblock]$Command)
    Write-Host "==> $Name"
    # uv and PyInstaller write their log to stderr. When the output goes to a file or a
    # pipe, PowerShell 5.1 makes each stderr line an error. With 'Stop', the first line
    # stops the script. The exit code tells if the step failed.
    $ErrorActionPreference = 'Continue'
    & $Command
    if ($LASTEXITCODE -ne 0) {
        Write-Host "build.ps1: the step '$Name' failed with exit code $LASTEXITCODE."
        exit 1
    }
}

# uv.lock pins the exact versions of PyInstaller, pyinstaller-hooks-contrib and
# setuptools. The tests of the excludes and of the Tcl and Tk trim in the spec used these
# versions. After an update of the lock, the spec guards and check_exe.py find most
# problems. Also test the app by hand.
Invoke-Step 'Install the dependencies' {
    uv sync --locked --extra gui --group build
}

# With --clean, PyInstaller deletes its cache folder. By default, all PyInstaller builds
# of the user share that folder, %LOCALAPPDATA%\pyinstaller. A build with UPX keeps
# processed DLLs there, and a build at the same time can delete them. Thus this build
# uses its own folder in the git-ignored build folder. This spec does not use UPX or
# strip, so PyInstaller keeps no files in it.
$env:PYINSTALLER_CONFIG_DIR = Join-Path $root 'build\pyinstaller-config'

$specArgs = @()
if ($OneDir) {
    $specArgs = @('--', '--onedir')
}
# Make the build reproducible. Two builds of one commit with the same tools then give the
# same exe, byte for byte. PYTHONHASHSEED=0 keeps the files in base_library.zip in the same
# order. PyInstaller writes SOURCE_DATE_EPOCH as the time stamp in the PE header of the exe.
# The script uses the time of the commit, or a value that the user sets.
$epoch = $env:SOURCE_DATE_EPOCH
if (-not $epoch) {
    try {
        $epoch = & git -C $root log -1 --format=%ct 2>$null
    } catch {
        $epoch = $null
    }
}
if ("$epoch" -notmatch '^\d+$') {
    # There is no git or no commit. Use 1980-01-01, the first date that a zip file can keep.
    $epoch = '315532800'
}
$oldHashSeed = $env:PYTHONHASHSEED
$oldEpoch = $env:SOURCE_DATE_EPOCH
$env:PYTHONHASHSEED = '0'
$env:SOURCE_DATE_EPOCH = "$epoch"
try {
    Invoke-Step 'Build the exe' {
        uv run --locked --extra gui --group build pyinstaller --noconfirm --clean packaging\pixheet.spec @specArgs
    }
} finally {
    # The exe check and the exe run without these values.
    $env:PYTHONHASHSEED = $oldHashSeed
    $env:SOURCE_DATE_EPOCH = $oldEpoch
}

if ($OneDir) {
    $exe = Join-Path $root 'dist\Pixheet\Pixheet.exe'
} else {
    $exe = Join-Path $root 'dist\Pixheet.exe'
}
if (-not (Test-Path -LiteralPath $exe -PathType Leaf)) {
    Write-Host "build.ps1: $exe does not exist."
    exit 1
}

# A windowed exe that shows an error dialog does not stop. check_exe.py has a time limit,
# and then it stops the whole process tree. Thus the build cannot hang here.
Invoke-Step 'Test the exe: smoke test and conversion' {
    uv run --locked --extra gui --group build python packaging\check_exe.py $exe
}

$hash = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant()
$name = Split-Path -Leaf $exe
$hashFile = "$exe.sha256"
# The format of sha256sum: "<hash> *<name>". Test it with: sha256sum -c Pixheet.exe.sha256
[System.IO.File]::WriteAllText($hashFile, "$hash *$name`n", [System.Text.Encoding]::ASCII)

$size = (Get-Item -LiteralPath $exe).Length / 1MB
Write-Host ('build.ps1: built {0} ({1:N1} MB)' -f $exe, $size)
Write-Host "build.ps1: SHA256 $hash"
exit 0
