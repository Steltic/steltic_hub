<#
  Steltic Hub — Windows self-test.

  Runs the whole first-run path end to end and writes selftest-report.txt next to this repo.
  Everything it creates lives in .\selftest-data, so your real %LOCALAPPDATA%\Steltic (and any
  existing Steltic install) is untouched. Delete that folder to reset.
#>
param([switch]$KeepData, [int]$Port = 8399)

$ErrorActionPreference = 'Continue'
$Repo   = Split-Path -Parent $PSScriptRoot
$Data   = Join-Path $Repo 'selftest-data'
$Report = Join-Path $Repo 'selftest-report.txt'
$HubEnv = Join-Path $Data 'hubenv'
$BinDir = Join-Path $Data 'bin'
$UvExe  = Join-Path $BinDir 'uv.exe'
$HubPy  = Join-Path $HubEnv 'Scripts\python.exe'
$url    = "http://127.0.0.1:$Port"

$env:STELTIC_HUB_DATA = $Data
$env:STELTIC_HUB_UV   = $UvExe

$script:Pass = 0; $script:Fail = 0; $script:Lines = @()

function Log($s = '') { $script:Lines += $s; Write-Host $s }
function Step($n) { Log ''; Log ("=" * 72); Log "  $n"; Log ("=" * 72) }
function Check($name, $ok, $detail = '') {
  if ($ok) { $script:Pass++; Log "  [PASS] $name$(if($detail){" -- $detail"})" }
  else     { $script:Fail++; Log "  [FAIL] $name$(if($detail){" -- $detail"})" }
}
function Clean($text) {
  # Native tools writing to stderr become NativeCommandError records in PowerShell. That is not
  # an error, just noise wrapped around the real output -- strip the wrapper, keep the output.
  ($text -split "`r?`n") | Where-Object {
    $_ -ne '' -and
    $_ -notmatch '^\s*\+\s' -and
    $_ -notmatch 'CategoryInfo|FullyQualifiedErrorId|NativeCommandError' -and
    $_ -notmatch '^\s*At .*SelfTest\.ps1:\d+ char:\d+'
  }
}

function Run($exe, $arglist, $label) {
  Log "  `$ $exe $($arglist -join ' ')"
  $o = & $exe @arglist 2>&1 | Out-String
  $code = $LASTEXITCODE
  foreach ($l in (Clean $o | Select-Object -Last 25)) { Log "      $l" }
  Check $label ($code -eq 0) "exit $code"
  return $o
}

Log "Steltic Hub self-test"
Log "  when      : $(Get-Date -Format u)"
Log "  repo      : $Repo"
Log "  data      : $Data"
Log "  PSVersion : $($PSVersionTable.PSVersion)"
Log "  OS        : $([System.Environment]::OSVersion.VersionString)"
Log "  64-bit    : $([System.Environment]::Is64BitOperatingSystem)"

# ---------------------------------------------------------------- 0. syntax
Step "0. PowerShell syntax of the shipped launcher"
$errs = $null
[System.Management.Automation.Language.Parser]::ParseFile(
  (Join-Path $PSScriptRoot 'Steltic.ps1'), [ref]$null, [ref]$errs) | Out-Null
Check "Steltic.ps1 parses" ($errs.Count -eq 0) "$($errs.Count) parse error(s)"
foreach ($e in $errs) { Log "      $($e.Extent.StartLineNumber): $($e.Message)" }

# ---------------------------------------------------------------- 1. uv
Step "1. uv bootstrap (private copy, PATH not modified)"
New-Item -ItemType Directory -Force -Path $Data, $BinDir | Out-Null
if (-not (Test-Path $UvExe)) {
  try {
    $env:UV_INSTALL_DIR = $BinDir; $env:UV_NO_MODIFY_PATH = '1'
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
  } catch { Log "      download failed: $_" }
  if (-not (Test-Path $UvExe)) {
    $f = Get-ChildItem $BinDir -Filter uv.exe -Recurse -EA SilentlyContinue | Select-Object -First 1
    if ($f) { Copy-Item $f.FullName $UvExe -Force }
  }
}
Check "uv present" (Test-Path $UvExe) $UvExe
if (Test-Path $UvExe) { Run $UvExe @('--version') "uv runs" | Out-Null }

# ---------------------------------------------------------------- 2. python + hub
Step "2. Hub environment (uv provisions CPython)"
if (-not (Test-Path $HubPy)) { Run $UvExe @('venv', '--python', '3.12', $HubEnv) "create hub venv" | Out-Null }
Check "hub python present" (Test-Path $HubPy) $HubPy
if (Test-Path $HubPy) {
  $v = (& $HubPy -c "import sys;print('%d.%d.%d'%sys.version_info[:3])") | Out-String
  Log "      interpreter: $($v.Trim())"
}
Run $UvExe @('pip', 'install', '--python', $HubPy, '-e', $Repo) "install steltic-hub" | Out-Null
Run $UvExe @('pip', 'install', '--python', $HubPy, 'pytest', 'numpy') "install pytest (+ numpy for the bundled Probabilistic-analysis tests)" | Out-Null

# ---------------------------------------------------------------- 3. tests
Step "3. Test suite (on Windows)"
Push-Location $Repo
$o = & $HubPy -m pytest tests -q 2>&1 | Out-String
$code = $LASTEXITCODE
foreach ($l in ($o -split "`r?`n" | Where-Object { $_ -ne '' } | Select-Object -Last 15)) { Log "      $l" }
Check "pytest" ($code -eq 0) "exit $code"
Pop-Location

# ---------------------------------------------------------------- 4. doctor
Step "4. steltic-hub doctor"
$env:STELTIC_HUB_PORT = $Port
$o = & $HubPy -m steltic_hub.cli doctor 2>&1 | Out-String
foreach ($l in ($o -split "`r?`n")) { Log "      $l" }
Check "doctor ran" ($o -match 'Steltic Hub doctor')
Check "doctor found uv" ($o -notmatch 'uv\s+NOT FOUND')
Check "doctor found git" ($o -notmatch 'git\s+NOT FOUND')

# ---------------------------------------------------------------- 5. a throwaway smoke module
Step "5. Write, link and install a throwaway smoke module (no network, no clone)"
# The product ships no example module. The self-test writes its own into selftest-data: one
# manifest and one stdlib script that exercise field substitution, CLI flags, SSE streaming and
# artifact detection in a few seconds. Anything left over from an older self-test is forgotten.
& $HubPy -m steltic_hub.cli forget example_module 2>&1 | Out-Null
$ex = Join-Path $Data 'smoke_module'
New-Item -ItemType Directory -Force -Path $ex | Out-Null
@'
{
  "schema": 1, "id": "smoke_module", "name": "Smoke Module", "accent": "#7ee0c0", "order": 900,
  "blurb": "Written by the self-test. Not part of the product.",
  "source": { "url": "https://example.invalid/smoke_module" },
  "env": { "python": "3.12", "install": [] },
  "output": { "root": "{job_dir}" },
  "viewers": [ { "id": "demo", "label": "Smoke viewer", "accent": "#7ee0c0", "path": "smoke_viewer.html" } ],
  "tabs": [
    { "id": "run", "title": "Run", "kind": "form",
      "run": { "kind": "cli", "cwd": "{job_dir}", "label": "Run smoke test",
               "command": [ "{module_dir}/run.py", "--job", "{job}", "--note", "{f.note}" ] },
      "fields": [
        { "id": "job", "type": "project", "label": "Project", "required": true },
        { "id": "note", "type": "textarea", "rows": 3, "label": "Note" },
        { "id": "steps", "type": "number", "label": "Steps", "arg": "--steps", "default": 5 },
        { "id": "mode", "type": "select", "label": "Mode", "arg": "--mode", "default": "normal",
          "options": [ { "value": "normal", "label": "Succeed" }, { "value": "fail", "label": "Fail" } ] },
        { "id": "verbose", "type": "checkbox", "label": "Verbose", "arg": "--verbose", "arg_style": "flag-if-true" },
        { "id": "attach", "type": "file", "label": "Attach a file", "arg": "--attach" }
      ],
      "artifacts": [ { "label": "Smoke viewer", "path": "smoke_viewer.html" }, { "label": "Result", "path": "smoke_result.json" } ] },
    { "id": "viewers", "title": "Viewers", "kind": "viewers" },
    { "id": "files", "title": "Files", "kind": "files" }
  ]
}
'@ | ForEach-Object { [System.IO.File]::WriteAllText((Join-Path $ex 'steltic_module.json'), $_) }   # UTF-8, no BOM
@'
import argparse, json, sys, time
from pathlib import Path
ap = argparse.ArgumentParser()
ap.add_argument("--job", default="Project"); ap.add_argument("--note", default="")
ap.add_argument("--steps", type=int, default=5); ap.add_argument("--mode", default="normal")
ap.add_argument("--verbose", action="store_true"); ap.add_argument("--attach", default="")
a = ap.parse_args()
out = Path.cwd()
print(f"smoke module starting in {out}")
if a.verbose: print(f"  args: {vars(a)}")
for i in range(1, max(1, a.steps) + 1):
    if a.mode == "fail" and i == max(1, a.steps) // 2 + 1:
        print(f"step {i}: deliberate failure", file=sys.stderr); sys.exit(3)
    print(f"step {i} of {a.steps}"); time.sleep(0.1)
(out / "smoke_viewer.html").write_text(f"<!doctype html><title>smoke</title><h1>{a.job}</h1><p>{a.note}</p>", encoding="utf-8")
(out / "smoke_result.json").write_text(json.dumps(vars(a)), encoding="utf-8")
print("wrote smoke_viewer.html and smoke_result.json")
'@ | ForEach-Object { [System.IO.File]::WriteAllText((Join-Path $ex 'run.py'), $_) }
Run $HubPy @('-m', 'steltic_hub.cli', 'link', 'smoke_module', $ex) "link smoke_module" | Out-Null
Run $HubPy @('-m', 'steltic_hub.cli', 'install', 'smoke_module') "install smoke_module" | Out-Null

# ---------------------------------------------------------------- 6. start the hub hidden
Step "6. Start the hub the way the launcher does (pythonw, hidden)"
# A hub left running by a previous self-test would answer the health check and the new one would
# defer to it (single instance) -- the test would then exercise stale code. Clear the port first.
try {
  $stale = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
           Select-Object -ExpandProperty OwningProcess -Unique
  foreach ($spid in $stale) {
    if ($spid -and $spid -ne $PID) {
      Log "      stopping a previous hub still listening on port $Port (pid $spid)"
      Stop-Process -Id $spid -Force -ErrorAction SilentlyContinue
    }
  }
  if ($stale) { Start-Sleep -Seconds 2 }
} catch { Log "      (could not check port $Port for a stale hub: $_)" }
Remove-Item (Join-Path $Data 'hub.url') -ErrorAction SilentlyContinue
$pythonw = Join-Path $HubEnv 'Scripts\pythonw.exe'
Check "pythonw.exe exists" (Test-Path $pythonw) $pythonw
$exe = if (Test-Path $pythonw) { $pythonw } else { $HubPy }
$pinfo = New-Object System.Diagnostics.ProcessStartInfo
$pinfo.FileName = $exe
$pinfo.Arguments = "-m steltic_hub.cli --port $Port --no-browser"
$pinfo.WindowStyle = 'Hidden'; $pinfo.UseShellExecute = $false
$proc = [System.Diagnostics.Process]::Start($pinfo)
Log "      started pid $($proc.Id) via $(Split-Path -Leaf $exe)"

$deadline = (Get-Date).AddSeconds(60); $ready = $false; $health = $null
while ((Get-Date) -lt $deadline) {
  try { $health = Invoke-RestMethod "$url/healthz" -TimeoutSec 2; if ($health.ok) { $ready = $true; break } } catch { }
  Start-Sleep -Milliseconds 400
}
Check "hub answers /healthz" $ready ($(if($health){"modules=$($health.modules) version=$($health.version)"}else{"no response in 60s"}))
$hubPid = $proc.Id
if ($ready) {
  Check "hub reports its pid and is not stale" (($health.pid -eq $proc.Id) -and ($health.stale -eq $false)) "pid=$($health.pid) stale=$($health.stale)"

  # ---------------------------------------------------------------- 6b. restart in place
  Step "6b. Restart in place (/api/hub/restart -- the window's Restart button; the launcher uses it for stale code)"
  # The hub spawns its successor (detached pythonw, same port, --replace), then stops gracefully;
  # the successor waits for the port and takes over. This is what makes "new code on disk" a
  # one-click restart instead of a Task Manager hunt for pythonw.exe.
  $rs = try { Invoke-RestMethod "$url/api/hub/restart" -Method Post -TimeoutSec 10 } catch { $null }
  Check "restart accepted" ([bool]$rs.ok) "old pid $($rs.pid), successor pid $($rs.successor)"
  $deadline = (Get-Date).AddSeconds(90); $h2 = $null
  while ((Get-Date) -lt $deadline) {
    try { $h2 = Invoke-RestMethod "$url/healthz" -TimeoutSec 2; if ($h2.ok -and $h2.pid -ne $health.pid) { break } } catch { }
    $h2 = $null; Start-Sleep -Milliseconds 400
  }
  Check "a new hub answers on the same port" ($null -ne $h2) ($(if($h2){"pid $($h2.pid) (was $($health.pid))"}else{"no successor within 90 s"}))
  $deadline = (Get-Date).AddSeconds(20)
  while ((Get-Date) -lt $deadline -and -not $proc.HasExited) { Start-Sleep -Milliseconds 300 }
  Check "old hub process exited" $proc.HasExited
  if ($h2) { $hubPid = $h2.pid } else { $ready = $false }
}

if (-not $ready) {
  Log ''
  Log '  --- diagnosing the failed start ---'
  $alive = -not $proc.HasExited
  Log "      pythonw process still alive : $alive$(if(-not $alive){" (exit code $($proc.ExitCode))"})"
  if ($alive) { try { $proc.Kill() } catch { } }

  $hubLog = Join-Path $Data 'logs\hub.log'
  if (Test-Path $hubLog) {
    Log "      tail of $hubLog :"
    foreach ($l in (Get-Content $hubLog -Tail 40)) { Log "        $l" }
  } else {
    Log "      no $hubLog -- the process died before it could open one"
  }

  Log ''
  Log '      retrying with python.exe (console) to capture the error:'
  $tmpOut = Join-Path $Data 'start-stdout.txt'
  $tmpErr = Join-Path $Data 'start-stderr.txt'
  $p2 = Start-Process -FilePath $HubPy -ArgumentList "-m steltic_hub.cli --port $($Port + 1) --no-browser" -PassThru -NoNewWindow -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr
  Start-Sleep -Seconds 20
  if (-not $p2.HasExited) {
    try { $ping = Invoke-RestMethod "http://127.0.0.1:$($Port + 1)/healthz" -TimeoutSec 3 } catch { $ping = $null }
    Log "      console start DID come up: $([bool]$ping.ok) -- so the failure is specific to pythonw.exe"
    try { $p2.Kill() } catch { }
  } else {
    Log "      console start also exited (code $($p2.ExitCode))"
  }
  foreach ($f in @($tmpOut, $tmpErr)) {
    if ((Test-Path $f) -and (Get-Item $f).Length -gt 0) {
      Log "      $(Split-Path -Leaf $f):"
      foreach ($l in (Get-Content $f -Tail 30)) { Log "        $l" }
    }
  }
}

if ($ready) {
  # ------------------------------------------------------------ 7. API
  Step "7. API"
  $state = Invoke-RestMethod "$url/api/state"
  Log "      data_dir: $($state.data_dir)"
  Check "data dir is the self-test folder" ($state.data_dir -like "*selftest-data*") $state.data_dir
  foreach ($m in $state.modules) {
    $tag = if ($m.status.linked) { "LINKED $($m.status.linked)" } elseif ($m.status.env_ready) { 'ready' } else { 'not installed' }
    Log "      $($m.name.PadRight(18)) tabs=$($m.tabs.Count)  $tag"
  }
  Check "the six catalog modules + the smoke module are present" ($state.modules.Count -ge 7) "$($state.modules.Count) modules"
  $dv = $state.modules | Where-Object { $_.id -eq 'steltic_variations' }
  Check "Design variations is listed as bundled" ([bool]$dv.bundled) "$($dv.name)"
  $pa = $state.modules | Where-Object { $_.id -eq 'steltic_probabilistic' }
  Check "Probabilistic analysis is listed as bundled" ([bool]$pa.bundled) "$($pa.name) (needs HR Steel + Nonlinear to run)"
  $exm = $state.modules | Where-Object { $_.id -eq 'smoke_module' }
  Check "smoke_module linked" ([bool]$exm.status.linked) $exm.status.linked
  Check "smoke_module env ready" ([bool]$exm.status.env_ready)

  # ------------------------------------------------------------ 8. run it
  Step "8. Run the smoke module through the hub (subprocess + SSE on Windows)"
  $body = @{ job = 'WinSelfTest'; fields = @{ note = 'windows self-test'; steps = 3; mode = 'normal'; verbose = $true } } | ConvertTo-Json -Depth 5
  $sse = try { Invoke-RestMethod "$url/api/run/smoke_module/run" -Method Post -Body $body -ContentType 'application/json' | Out-String } catch { "REQUEST FAILED: $_" }
  foreach ($l in ($sse -split "`r?`n" | Where-Object { $_ -match '^data:' } | Select-Object -First 20)) { Log "      $($l.Substring(0,[Math]::Min(150,$l.Length)))" }
  Check "run streamed events" ($sse -match '"type": "log"')
  Check "run reported done ok" ($sse -match '"ok": true')
  Check "run reached the last step" ($sse -match 'step 3 of 3')

  Step "9. Artifacts landed in the project folder"
  $outs = Invoke-RestMethod "$url/api/out/smoke_module/WinSelfTest"
  foreach ($e in $outs.entries) { Log "      $($e.path)  $($e.bytes) b" }
  Check "smoke_viewer.html written" ([bool]($outs.entries | Where-Object { $_.path -eq 'smoke_viewer.html' }))
  Check "smoke_result.json written" ([bool]($outs.entries | Where-Object { $_.path -eq 'smoke_result.json' }))
  try {
    $r = Invoke-WebRequest "$url/out/smoke_module/WinSelfTest/smoke_viewer.html" -UseBasicParsing
    Check "viewer serves over http" ($r.StatusCode -eq 200) "$($r.StatusCode), $($r.RawContentLength) b"
  } catch { Check "viewer serves over http" $false "$_" }

  Step "10. Failure path and path-traversal guard"
  $body2 = @{ job = 'WinSelfTest'; fields = @{ note = 'x'; steps = 4; mode = 'fail' } } | ConvertTo-Json -Depth 5
  $sse2 = try { Invoke-RestMethod "$url/api/run/smoke_module/run" -Method Post -Body $body2 -ContentType 'application/json' | Out-String } catch { '' }
  Check "failure path reports non-zero rc" ($sse2 -match '"rc": 3')
  try {
    Invoke-WebRequest "$url/out/smoke_module/WinSelfTest/%2e%2e%2f%2e%2e%2f%2e%2e%2fstate.json" -UseBasicParsing | Out-Null
    Check "traversal refused" $false "request succeeded"
  } catch { Check "traversal refused" ($_.Exception.Response.StatusCode.value__ -eq 403) "HTTP $($_.Exception.Response.StatusCode.value__)" }

  Step "11. Bundled module: install Design variations through the hub and start its server"
  # Ships inside the hub (no clone); its environment is fastapi/uvicorn/httpx from PyPI. It runs
  # without HR Steel installed (the hub leaves STELTIC_URL unset), which is what this checks.
  $inst = try { Invoke-RestMethod "$url/api/modules/steltic_variations/install" -Method Post -TimeoutSec 600 | Out-String } catch { "REQUEST FAILED: $_" }
  foreach ($l in ($inst -split "`r?`n" | Where-Object { $_ -match '^data:' } | Select-Object -Last 4)) { Log "      $($l.Substring(0,[Math]::Min(150,$l.Length)))" }
  Check "Design variations env built" ($inst -match '"ok": true')
  $srv = try { Invoke-RestMethod "$url/api/modules/steltic_variations/server/start" -Method Post -TimeoutSec 120 } catch { $null }
  Check "Design variations server started" ([bool]$srv.url) "$($srv.url)"
  if ($srv.url) {
    $me = try { Invoke-RestMethod "$($srv.url)/api/me" -TimeoutSec 10 } catch { $null }
    Check "Design variations answers /api/me" ([bool]$me) "steltic_url='$($me.steltic_url)' jobs=$($me.jobs)"
    $lib = try { Invoke-RestMethod "$($srv.url)/api/library" -TimeoutSec 10 } catch { $null }
    Check "ten variation categories" ($lib.categories.Count -eq 10) "$($lib.categories.Count) categories, $($lib.metrics.Count) metrics"
    try {
      $page = Invoke-WebRequest "$($srv.url)/?project=WinSelfTest" -UseBasicParsing
      Check "module page serves" ($page.StatusCode -eq 200 -and $page.Content -match 'Design variations')
    } catch { Check "module page serves" $false "$_" }
    Invoke-RestMethod "$url/api/modules/steltic_variations/server/stop" -Method Post -ErrorAction SilentlyContinue | Out-Null
  }

  Step "12. Edge --app window"
  $edge = @("$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
            "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe") |
          Where-Object { Test-Path $_ } | Select-Object -First 1
  Check "Edge found" ([bool]$edge) $edge
  if ($edge) {
    Start-Process $edge -ArgumentList "--app=$url", "--window-size=1400,900", "--user-data-dir=$(Join-Path $Data 'window')"
    Log "      opened a chromeless window at $url -- leave it open, it is the app"
  }
}

# ---------------------------------------------------------------- summary
Step "SUMMARY"
Log "  passed: $script:Pass"
Log "  failed: $script:Fail"
Log ""
if ($ready) {
  Log "  The hub is still running on $url (pid $hubPid)."
  Log "  Close the Steltic window and run:  Stop-Process -Id $hubPid"
  Log "  (or: Invoke-RestMethod $url/api/hub/shutdown -Method Post -- graceful, module servers included)"
} else {
  Log "  The hub did not start -- see the diagnosis in step 6 above."
}
if (-not $KeepData) { Log "  Delete $Data to reset completely." }

$script:Lines -join "`r`n" | Set-Content -Path $Report -Encoding UTF8
Write-Host ""
Write-Host "Report written to $Report" -ForegroundColor Green
