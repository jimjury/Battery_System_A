param([string]$PythonPath = '', [int]$Port = 8765, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$candidates = @()
if ($PythonPath) { $candidates += $PythonPath }
if ($env:AB_PYTHON) { $candidates += $env:AB_PYTHON }
$localConfig = Join-Path $projectRoot '.local\launcher.json'
if (Test-Path -LiteralPath $localConfig) { $candidates += (Get-Content -LiteralPath $localConfig -Raw | ConvertFrom-Json).python }
$portableRuntime = Join-Path $projectRoot 'runtime\python.exe'
if (Test-Path -LiteralPath $portableRuntime) { $candidates += $portableRuntime }
if ($env:CONDA_PREFIX) { $candidates += (Join-Path $env:CONDA_PREFIX 'python.exe') }
foreach ($command in @('python.exe', 'python3.exe')) {
  $found = Get-Command $command -ErrorAction SilentlyContinue
  if ($found) { $candidates += $found.Source }
}
foreach ($registryRoot in @('HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore')) {
  if (Test-Path $registryRoot) {
    Get-ChildItem $registryRoot | ForEach-Object {
      $install = Get-ItemProperty ($_.PSPath + '\InstallPath') -ErrorAction SilentlyContinue
      if ($install -and $install.'(default)') { $candidates += (Join-Path $install.'(default)' 'python.exe') }
    }
  }
}
$selected = $null
foreach ($candidate in ($candidates | Where-Object { $_ } | Select-Object -Unique)) {
  if ($candidate -like '*\WindowsApps\*' -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
  & $candidate -I -c 'import sys; assert sys.version_info >= (3, 10)' 2>$null
  if ($LASTEXITCODE -eq 0) { $selected = $candidate; break }
}
if (-not $selected) { throw 'No compatible existing Python found. Run Start_B.cmd -PythonPath <existing python.exe>. No installation was attempted.' }
Set-Location -LiteralPath $projectRoot
$launchArgs = @('-m', 'server_b.portable', '--port', $Port)
if (-not $NoBrowser) { $launchArgs += '--open' }
& $selected @launchArgs
exit $LASTEXITCODE
