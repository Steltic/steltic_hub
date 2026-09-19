<#
  Steltic — first-run bootstrap and launcher (Windows).

  Everything heavy is fetched on first run, so the thing you ship is small:
    1. uv          (~15 MB, private to Steltic — the machine's PATH is not touched)
    2. CPython     (uv downloads it; the hub itself is version-agnostic)
    3. steltic-hub (a few hundred KB)
    4. modules     (installed from the app's Modules tab, each into its own environment)

  Subsequent launches skip straight to step 4's result and open the window.

  Why an explicit --python on every uv call: uv ignores a package's upper Python bound, which is
  how installs land on 3.13 where openseespy cannot load. The hub pins the interpreter per module
  instead of hoping.
#>
param(
  [switch]$Reinstall,      # rebuild the hub environment from scratch
  [switch]$Restart,        # stop a hub that is already running and start a fresh one (new code on disk)
  [switch]$NoWindow,       # start the server only; do not open a window
  [switch]$BootstrapOnly,  # fetch uv + Python + the hub, then exit without starting anything (Tauri shell)
  [switch]$Console,        # keep the console visible (for diagnosing a failed start)
  [int]$Port = 8300
)

$ErrorActionPreference = 'Stop'
# ONE data root for the launcher and the hub. Without this the launcher's private uv lands in
# a different folder from the one the hub searches, and the hub quietly downloads its own copy.
$Root    = Join-Path $env:LOCALAPPDATA 'Steltic'
$env:STELTIC_HUB_DATA = $Root
$HubEnv  = Join-Path $Root 'hubenv'
$BinDir  = Join-Path $Root 'bin'
$HubPy   = Join-Path $HubEnv 'Scripts\python.exe'
$UvExe   = Join-Path $BinDir 'uv.exe'
$LogFile = Join-Path $Root 'launcher.log'

New-Item -ItemType Directory -Force -Path $Root, $BinDir | Out-Null

function Say($msg, $colour = 'Gray') {
  Write-Host $msg -ForegroundColor $colour
  Add-Content -Path $LogFile -Value ("[{0}] {1}" -f (Get-Date -Format s), $msg)
}

function Fail($msg) {
  Say $msg 'Red'
  Say "Full log: $LogFile"
  # Steltic.bat runs this script with a HIDDEN window: a Read-Host here would wait forever where
  # nobody can see it. Show a message box instead, and open the log so the reason is one click away.
  try {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show("$msg`n`nThe full log is in:`n$LogFile", 'Steltic could not start',
      'OK', 'Error') | Out-Null
  } catch { if ($Console) { Read-Host "`nPress Enter to close" } }
  try { Start-Process notepad.exe $LogFile } catch { }
  exit 1
}

# ---------------------------------------------------------------- 1. uv
if (-not (Test-Path $UvExe)) {
  Say 'First run: fetching uv (one time, ~15 MB)…' 'Cyan'
  try {
    $env:UV_INSTALL_DIR   = $BinDir
    $env:UV_NO_MODIFY_PATH = '1'
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
  } catch { Fail "Could not download uv: $_" }
  if (-not (Test-Path $UvExe)) {
    $found = Get-ChildItem -Path $BinDir -Filter uv.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) { Copy-Item $found.FullName $UvExe -Force } else { Fail 'uv did not install.' }
  }
  Say 'uv ready.' 'Green'
}

# ---------------------------------------------------------------- 2 + 3. hub environment
if ($Reinstall -and (Test-Path $HubEnv)) {
  Say 'Removing the existing hub environment…'
  Remove-Item -Recurse -Force $HubEnv
}

if (-not (Test-Path $HubPy)) {
  Say 'Creating the Steltic environment (downloads Python if this machine has none)…' 'Cyan'
  & $UvExe venv --python 3.12 $HubEnv
  if ($LASTEXITCODE -ne 0) { Fail 'Could not create the Steltic environment.' }
}

$local = Join-Path $PSScriptRoot '..'
$HubExe = Join-Path $HubEnv 'Scripts\steltic-hub.exe'
$Stamp  = Join-Path $HubEnv 'steltic-hub.installed'     # when pip last ran for the hub itself
$PyProj = Join-Path $local 'pyproject.toml'
$needInstall = $Reinstall -or -not (Test-Path $HubExe)
if (-not $needInstall -and (Test-Path $PyProj)) {
  # Running from a checkout (an editable install): code changes are live, but a dependency added to
  # pyproject.toml is not until pip runs again. Cheap when nothing changed.
  if (-not (Test-Path $Stamp) -or ((Get-Item $PyProj).LastWriteTimeUtc -gt (Get-Item $Stamp).LastWriteTimeUtc)) {
    Say 'pyproject.toml is newer than the installed hub — refreshing the hub environment…' 'Cyan'
    $needInstall = $true
  }
}
if ($needInstall) {
  Say 'Installing the Steltic hub…' 'Cyan'
  if (Test-Path $PyProj) {
    & $UvExe pip install --python $HubPy -e $local          # running from a checkout
  } else {
    & $UvExe pip install --python $HubPy steltic-hub        # running from a release
  }
  if ($LASTEXITCODE -ne 0) { Fail 'Could not install the Steltic hub.' }
  Set-Content -Path $Stamp -Value (Get-Date -Format s)
  Say 'Hub installed.' 'Green'
}
if ($BootstrapOnly) { Say 'Bootstrap complete.' 'Green'; exit 0 }

function Open-Window($u) {
  # A chromeless app window with no shell to ship: Edge is on every supported Windows build, and
  # --app= is the same WebView2 engine a Tauri build would use. Falls back to the default browser.
  $edge = @(
    "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
  ) | Where-Object { Test-Path $_ } | Select-Object -First 1
  if ($edge) {
    Start-Process $edge -ArgumentList "--app=$u", "--window-size=1400,900",
      "--user-data-dir=$(Join-Path $Root 'window')"
  } else {
    Start-Process $u
  }
}

# ---------------------------------------------------------------- 4. run
$env:STELTIC_HUB_PORT = $Port
$env:STELTIC_HUB_UV   = $UvExe        # the hub uses the same uv the launcher fetched
$url = "http://127.0.0.1:$Port"
$UrlFile = Join-Path $Root 'hub.url'  # written by the hub with the URL it ACTUALLY bound

function Hub-Info($u) {
  # Only a Steltic hub answers /healthz with a module count -- another app squatting on the port does not.
  try { $p = Invoke-RestMethod "$u/healthz" -TimeoutSec 2; if ($p.ok -and ($null -ne $p.modules)) { return $p } } catch { }
  return $null
}
function Hub-Alive($u) { return ($null -ne (Hub-Info $u)) }

function Stop-Hub($u, $info) {
  # Graceful first (the hub stops its module servers and retires hub.url on the way out); a hub too
  # old to have the route is stopped by pid -- the one listening on the port.
  $stopped = $false
  try { Invoke-RestMethod "$u/api/hub/shutdown" -Method Post -TimeoutSec 5 | Out-Null; $stopped = $true } catch { }
  if (-not $stopped) {
    $p = [int]([uri]$u).Port
    $pids = @()
    try { $pids = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique } catch { }
    foreach ($spid in $pids) { if ($spid -and $spid -ne $PID) { Say "  stopping the old hub by pid $spid (it predates /api/hub/shutdown)"; Stop-Process -Id $spid -Force -ErrorAction SilentlyContinue } }
  }
  $deadline = (Get-Date).AddSeconds(45)
  while ((Get-Date) -lt $deadline -and (Hub-Alive $u)) { Start-Sleep -Milliseconds 300 }
  if (Hub-Alive $u) { Fail "The hub on $u did not stop. Close it (Task Manager: pythonw.exe) and run Steltic again." }
  Start-Sleep -Milliseconds 800     # let the port close
}

# Already running? Raise a window at it (on the port it chose, which may differ from $Port) -- unless
# the code on disk moved on since it started (a git pull, an edit in this checkout): the running hub
# is then the OLD version, and it is stopped and replaced instead. -Restart forces that.
foreach ($candidate in @($url, $(if (Test-Path $UrlFile) { (Get-Content $UrlFile -Raw).Trim() }))) {
  if (-not $candidate) { continue }
  $info = Hub-Info $candidate
  if ($null -eq $info) { continue }
  $old = ($null -eq $info.stale) -or [bool]$info.stale      # a hub without the field predates the check: treat as stale
  if ($Restart -or $old) {
    $why = if ($Restart) { 'restart requested' } elseif ($null -eq $info.stale) { 'an older hub without a freshness check' } else { 'its source files changed since it started' }
    Say "Steltic is running on $candidate (pid $($info.pid)) -- $why; restarting it." 'Cyan'
    Stop-Hub $candidate $info
    break
  }
  Say "Steltic is already running on $candidate." 'Green'
  if (-not $NoWindow) { Open-Window $candidate }
  exit 0
}
Remove-Item $UrlFile -ErrorAction SilentlyContinue

Say "Starting Steltic on $url" 'Cyan'
$hubArgs = @('-m', 'steltic_hub.cli', '--port', "$Port", '--no-browser')
if ($Console) {
  Start-Process -FilePath $HubPy -ArgumentList $hubArgs -NoNewWindow
} else {
  $pinfo = New-Object System.Diagnostics.ProcessStartInfo
  $pinfo.FileName = (Join-Path $HubEnv 'Scripts\pythonw.exe')
  if (-not (Test-Path $pinfo.FileName)) { $pinfo.FileName = $HubPy }
  $pinfo.Arguments = ($hubArgs -join ' ')
  $pinfo.WindowStyle = 'Hidden'
  $pinfo.UseShellExecute = $false
  [System.Diagnostics.Process]::Start($pinfo) | Out-Null
}

# wait for health -- the hub falls back to a free port when $Port is taken and records it in hub.url
$deadline = (Get-Date).AddSeconds(90)
$ready = $false
while ((Get-Date) -lt $deadline) {
  if (Test-Path $UrlFile) {
    $u = (Get-Content $UrlFile -Raw -ErrorAction SilentlyContinue)
    if ($u) { $url = $u.Trim() }
  }
  if (Hub-Alive $url) { $ready = $true; break }
  Start-Sleep -Milliseconds 400
}
if (-not $ready) { Fail "Steltic did not start within 90 s. See $LogFile and $Root\logs\hub.log." }
Say "Steltic is running on $url." 'Green'

if ($NoWindow) { exit 0 }


Open-Window $url
