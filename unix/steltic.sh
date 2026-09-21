#!/usr/bin/env bash
# Steltic -- first-run bootstrap and launcher (Linux and macOS).
#
# The Windows counterpart is windows/Steltic.ps1 and this mirrors it step for step, so a change to
# one belongs in the other. Everything heavy is fetched on first run, so what you ship stays small:
#   1. uv          (~15 MB, private to Steltic -- your PATH is not touched)
#   2. CPython     (uv downloads it; the hub itself is version-agnostic)
#   3. steltic-hub (a few hundred KB)
#   4. modules     (installed from the app's Modules page, each into its own environment)
#
# Why an explicit --python on every uv call: uv ignores a package's upper Python bound, which is how
# installs land on 3.13 where openseespy cannot load. The hub pins the interpreter per module.
set -euo pipefail

PORT=8300
REINSTALL=0; RESTART=0; NO_WINDOW=0; BOOTSTRAP_ONLY=0; CONSOLE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --reinstall)      REINSTALL=1 ;;
    --restart)        RESTART=1 ;;
    --no-window)      NO_WINDOW=1 ;;
    --bootstrap-only) BOOTSTRAP_ONLY=1 ;;
    --console)        CONSOLE=1 ;;
    --port)           PORT="${2:?--port needs a number}"; shift ;;
    -h|--help)
      sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
      echo; echo "usage: steltic.sh [--reinstall] [--restart] [--no-window] [--bootstrap-only] [--console] [--port N]"
      exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

# ONE data root for the launcher and the hub, matching steltic_hub/config.py exactly. Without this
# the launcher's private uv lands somewhere the hub does not look, and the hub downloads its own.
case "$(uname -s)" in
  Darwin) ROOT="$HOME/Library/Application Support/Steltic" ;;
  *)      ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/Steltic" ;;
esac
export STELTIC_HUB_DATA="$ROOT"
HUBENV="$ROOT/hubenv"
BINDIR="$ROOT/bin"
HUBPY="$HUBENV/bin/python"
UV="$BINDIR/uv"
LOG="$ROOT/launcher.log"
URLFILE="$ROOT/hub.url"          # written by the hub with the URL it ACTUALLY bound
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL="$(cd "$HERE/.." && pwd)"  # the checkout, when this script is run from one

mkdir -p "$ROOT" "$BINDIR"

say()  { printf '%s\n' "$*"; printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*" >> "$LOG"; }
fail() { say "ERROR: $*"; say "Full log: $LOG"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------- 1. uv
if [ ! -x "$UV" ]; then
  say 'First run: fetching uv (one time, ~15 MB)...'
  if have curl;   then fetch() { curl -fsSL "$1"; }
  elif have wget; then fetch() { wget -qO- "$1"; }
  else fail 'neither curl nor wget is installed -- install one and run this again'; fi
  UV_INSTALL_DIR="$BINDIR" UV_NO_MODIFY_PATH=1 sh -c "$(fetch https://astral.sh/uv/install.sh)" \
    || fail 'could not download uv'
  [ -x "$UV" ] || UV="$(find "$BINDIR" -name uv -type f -perm -u+x 2>/dev/null | head -1)"
  [ -n "${UV:-}" ] && [ -x "$UV" ] || fail 'uv did not install'
  say 'uv ready.'
fi

# ---------------------------------------------------------------- 2 + 3. hub environment
if [ "$REINSTALL" = 1 ] && [ -d "$HUBENV" ]; then
  say 'Removing the existing hub environment...'
  rm -rf "$HUBENV"
fi
if [ ! -x "$HUBPY" ]; then
  say 'Creating the Steltic environment (downloads Python if this machine has none)...'
  "$UV" venv --python 3.12 "$HUBENV" || fail 'could not create the Steltic environment'
fi

HUBBIN="$HUBENV/bin/steltic-hub"
STAMP="$HUBENV/steltic-hub.installed"     # when pip last ran for the hub itself
PYPROJ="$LOCAL/pyproject.toml"
need_install=0
[ "$REINSTALL" = 1 ] && need_install=1
[ -x "$HUBBIN" ] || need_install=1
if [ "$need_install" = 0 ] && [ -f "$PYPROJ" ]; then
  # Running from a checkout (an editable install): code changes are live, but a dependency added to
  # pyproject.toml is not until pip runs again. Cheap when nothing changed.
  if [ ! -f "$STAMP" ] || [ "$PYPROJ" -nt "$STAMP" ]; then
    say 'pyproject.toml is newer than the installed hub -- refreshing the hub environment...'
    need_install=1
  fi
fi
if [ "$need_install" = 1 ]; then
  say 'Installing the Steltic hub...'
  if [ -f "$PYPROJ" ]; then "$UV" pip install --python "$HUBPY" -e "$LOCAL"
  else                      "$UV" pip install --python "$HUBPY" steltic-hub; fi || fail 'could not install the Steltic hub'
  date -u +%FT%TZ > "$STAMP"
  say 'Hub installed.'
fi
[ "$BOOTSTRAP_ONLY" = 1 ] && { say 'Bootstrap complete.'; exit 0; }

# ---------------------------------------------------------------- window
open_window() {
  local u="$1"
  local profile="$ROOT/window"
  if [ "$(uname -s)" = "Darwin" ]; then
    for app in "Google Chrome" "Microsoft Edge" "Brave Browser" "Chromium"; do
      if [ -d "/Applications/$app.app" ]; then
        open -na "$app" --args "--app=$u" "--window-size=1400,900" "--user-data-dir=$profile"
        return 0
      fi
    done
    open "$u"; return 0
  fi
  for b in google-chrome chromium chromium-browser microsoft-edge brave-browser; do
    if have "$b"; then
      nohup "$b" "--app=$u" "--window-size=1400,900" "--user-data-dir=$profile" >/dev/null 2>&1 &
      return 0
    fi
  done
  have xdg-open && { nohup xdg-open "$u" >/dev/null 2>&1 & return 0; }
  say "Open this in a browser: $u"
}

# ---------------------------------------------------------------- 4. run
export STELTIC_HUB_PORT="$PORT"
export STELTIC_HUB_UV="$UV"          # the hub uses the same uv the launcher fetched
URL="http://127.0.0.1:$PORT"

# Only a Steltic hub answers /healthz with a module count -- another app on the port does not.
hub_info() {
  if have curl; then curl -fsS --max-time 2 "$1/healthz" 2>/dev/null
  else wget -qO- --timeout=2 "$1/healthz" 2>/dev/null; fi
}
hub_alive() { [ -n "$(hub_info "$1" | tr -d '[:space:]')" ]; }
hub_field() { hub_info "$1" | "$HUBPY" -c "import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(1)
v=d.get('$2')
print('' if v is None else v)" 2>/dev/null; }

stop_hub() {
  local u="$1" stopped=0
  if have curl; then curl -fsS -X POST --max-time 5 "$u/api/hub/shutdown" >/dev/null 2>&1 && stopped=1; fi
  if [ "$stopped" = 0 ]; then
    # a hub too old to have the route is stopped by pid -- the one listening on the port
    local p; p="$(printf '%s' "$u" | sed 's#.*:##')"
    local pids=""
    have lsof && pids="$(lsof -nP -iTCP:"$p" -sTCP:LISTEN -t 2>/dev/null || true)"
    [ -z "$pids" ] && have ss && pids="$(ss -lptnH "sport = :$p" 2>/dev/null | grep -oE 'pid=[0-9]+' | cut -d= -f2 || true)"
    for pid in $pids; do
      [ "$pid" = "$$" ] && continue
      say "  stopping the old hub by pid $pid (it predates /api/hub/shutdown)"
      kill "$pid" 2>/dev/null || true
    done
  fi
  local n=0
  while [ $n -lt 150 ] && hub_alive "$u"; do sleep 0.3; n=$((n+1)); done
  hub_alive "$u" && fail "The hub on $u did not stop. Close it and run steltic.sh again."
  sleep 1     # let the port close
}

# Already running? Raise a window at it (on the port it chose, which may differ) -- unless the code
# on disk moved on since it started (a git pull, an edit in this checkout): that hub is the OLD
# version and is replaced instead. --restart forces it.
for candidate in "$URL" "$( [ -f "$URLFILE" ] && tr -d '[:space:]' < "$URLFILE" )"; do
  [ -z "$candidate" ] && continue
  hub_alive "$candidate" || continue
  stale="$(hub_field "$candidate" stale)"
  # a hub without the field predates the freshness check: treat as stale
  if [ "$RESTART" = 1 ] || [ -z "$stale" ] || [ "$stale" = "True" ] || [ "$stale" = "true" ]; then
    say "Steltic is running on $candidate -- restarting it (new code on disk, or --restart)."
    stop_hub "$candidate"
    break
  fi
  say "Steltic is already running on $candidate."
  [ "$NO_WINDOW" = 1 ] || open_window "$candidate"
  exit 0
done
rm -f "$URLFILE"

say "Starting Steltic on $URL"
if [ "$CONSOLE" = 1 ]; then
  "$HUBPY" -m steltic_hub.cli --port "$PORT" --no-browser &
else
  nohup "$HUBPY" -m steltic_hub.cli --port "$PORT" --no-browser >> "$ROOT/logs/launcher-stdout.log" 2>&1 &
  disown || true
fi

# wait for health -- the hub falls back to a free port when $PORT is taken and records it in hub.url
n=0; ready=0
while [ $n -lt 225 ]; do
  [ -f "$URLFILE" ] && { u="$(tr -d '[:space:]' < "$URLFILE")"; [ -n "$u" ] && URL="$u"; }
  hub_alive "$URL" && { ready=1; break; }
  sleep 0.4; n=$((n+1))
done
[ "$ready" = 1 ] || fail "Steltic did not start within 90 s. See $LOG and $ROOT/logs/hub.log."
say "Steltic is running on $URL."

[ "$NO_WINDOW" = 1 ] && exit 0
open_window "$URL"
