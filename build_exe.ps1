$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
$candidates = [System.Collections.Generic.List[string]]::new()
if (-not [string]::IsNullOrWhiteSpace($env:HAMSHMAREH_PYTHON)) {
  $candidates.Add($env:HAMSHMAREH_PYTHON)
}
$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -ne $python -and $python.CommandType -eq 'Application') {
  $candidates.Add($python.Source)
}
$python3 = Get-Command python3 -ErrorAction SilentlyContinue
if ($null -ne $python3 -and $python3.CommandType -eq 'Application') {
  $candidates.Add($python3.Source)
}
$candidates.Add('D:\python312\python.exe')
$candidates.Add((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'))
$candidates.Add((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'))

$pythonCommand = $null
foreach ($candidate in $candidates | Select-Object -Unique) {
  try {
    if ((Test-Path -LiteralPath $candidate) -or $candidate -eq 'python') {
      & $candidate -c 'import PyInstaller' 2>$null
      if ($LASTEXITCODE -eq 0) {
        $pythonCommand = $candidate
        break
      }
    }
  } catch {
    # Try the next installed Python runtime.
  }
}
if ([string]::IsNullOrWhiteSpace($pythonCommand)) {
  throw 'Python 3 with PyInstaller is required. Set HAMSHMAREH_PYTHON or install Python 3 and PyInstaller.'
}
& $pythonCommand -m PyInstaller --clean --noconfirm .\build.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE." }
Write-Output "Built: $PSScriptRoot\dist\HamshmarehExtractor.exe"
