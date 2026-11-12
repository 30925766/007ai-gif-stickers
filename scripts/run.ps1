param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RemainingArgs
)
$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$skillRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $skillRoot ".runtime"
New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null

function Find-Python {
    $candidates = @()
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $resolved = & $py.Source -3 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0) { $candidates += [string]$resolved }
        } catch {}
    }
    foreach ($name in @("python", "python3")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command -and $command.Source -notmatch "WindowsApps") { $candidates += $command.Source }
    }
    $localPython = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (Test-Path -LiteralPath $localPython) {
        $candidates += @(Get-ChildItem -LiteralPath $localPython -Filter python.exe -Recurse -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | Select-Object -ExpandProperty FullName)
    }
    foreach ($candidate in $candidates) {
        try {
            & $candidate -c "import sys, ssl, venv; sys.exit(0 if sys.version_info >= (3,10) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { return $candidate }
        } catch {}
    }
    return $null
}

$pythonExe = Find-Python
if (-not $pythonExe) {
    [Console]::Error.WriteLine("Installing Python for the GIF sticker skill...")
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        & $winget.Source install --id Python.Python.3.12 --exact --scope user --silent --accept-package-agreements --accept-source-agreements | ForEach-Object { [Console]::Error.WriteLine($_) }
        $pythonExe = Find-Python
    }
    if (-not $pythonExe) {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $cpu = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
        $suffix = if ($cpu -eq "ARM64") { "-arm64" } elseif ($cpu -eq "AMD64") { "-amd64" } else { "" }
        $installer = Join-Path $runtimeDir "python-3.12.10$suffix.exe"
        Invoke-WebRequest -UseBasicParsing -Uri "https://www.python.org/ftp/python/3.12.10/python-3.12.10$suffix.exe" -OutFile $installer
        $signature = Get-AuthenticodeSignature -LiteralPath $installer
        if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notmatch "Python Software Foundation") {
            throw "Python installer signature verification failed."
        }
        $process = Start-Process -FilePath $installer -ArgumentList @("/quiet", "InstallAllUsers=0", "PrependPath=0", "Include_test=0") -WindowStyle Hidden -Wait -PassThru
        if ($process.ExitCode -notin @(0, 3010)) { throw "Python installer failed with code $($process.ExitCode)." }
        $pythonExe = Find-Python
    }
}
if (-not $pythonExe) { throw "Python 3.10+ is unavailable. Install Python, then run this skill again." }
& $pythonExe (Join-Path $PSScriptRoot "bootstrap.py") @RemainingArgs
exit $LASTEXITCODE
