#!/bin/sh
# Install or update Live Clipper on macOS or Linux. Safe to run again.
# Checks Python 3.11+, ffmpeg and pipx, asks before installing anything, then installs the app.
#   sh install.sh          interactive
#   sh install.sh --yes    answer yes to every question
#   sh install.sh --no-start
set -eu

REPO_URL="${LIVE_CLIPPER_REPO:-https://github.com/RattleSnakeSight/live-clipper}"
YES=0
START=1
for arg in "$@"; do
  case "$arg" in
    --yes|-y) YES=1 ;;
    --no-start) START=0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }

ask() {
  [ "$YES" = 1 ] && return 0
  printf '%s [y/N] ' "$1"
  read -r reply </dev/tty || return 1
  case "$reply" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

find_python() {
  for cmd in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$cmd" >/dev/null 2>&1 && "$cmd" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
      echo "$cmd"
      return 0
    fi
  done
  return 1
}

# Print the package manager command that installs the given packages, or nothing.
installer() {
  if command -v brew >/dev/null 2>&1; then echo "brew install"
  elif command -v apt-get >/dev/null 2>&1; then echo "sudo apt-get install -y"
  elif command -v dnf >/dev/null 2>&1; then echo "sudo dnf install -y"
  elif command -v pacman >/dev/null 2>&1; then echo "sudo pacman -S --noconfirm"
  fi
}

pkg_install() { # $1 = what to install (for the question), rest = package names
  what="$1"; shift
  cmd="$(installer)"
  if [ -z "$cmd" ]; then
    say "No supported package manager found. Please install $what yourself, then run this script again."
    exit 1
  fi
  if ask "Install $what with: $cmd $*?"; then
    # shellcheck disable=SC2086
    $cmd "$@"
  else
    say "Skipped. Install $what yourself, then run this script again."
    exit 1
  fi
}

if ! PY="$(find_python)"; then
  if command -v brew >/dev/null 2>&1; then pkg_install "Python 3.12" python@3.12
  else pkg_install "Python 3" python3 python3-venv python3-pip
  fi
  PY="$(find_python)" || { say "Python 3.11 or newer is still not available."; exit 1; }
fi
say "Python: $($PY --version)"

if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
  pkg_install "ffmpeg" ffmpeg
fi
say "ffmpeg: found"

if ! command -v pipx >/dev/null 2>&1; then
  if ask "Install pipx with: $PY -m pip install --user pipx?"; then
    "$PY" -m pip install --user pipx || "$PY" -m pip install --user --break-system-packages pipx
    "$PY" -m pipx ensurepath || true
  else
    say "pipx is needed for the tidy install. Falling back to: $PY -m pip install --user"
  fi
fi

if command -v pipx >/dev/null 2>&1; then PIPX="pipx"
elif "$PY" -m pipx --version >/dev/null 2>&1; then PIPX="$PY -m pipx"
else PIPX=""
fi

if [ -n "$PIPX" ]; then
  # --force makes this an update when Live Clipper is already installed
  $PIPX install --force --python "$PY" "git+$REPO_URL"
else
  "$PY" -m pip install --user --upgrade "git+$REPO_URL"
fi

export PATH="$HOME/.local/bin:$PATH"
if ! command -v live-clipper >/dev/null 2>&1; then
  say "Installed, but 'live-clipper' is not on your PATH yet. Open a new terminal, or run: $PY -m live_clipper start"
  exit 0
fi

live-clipper doctor || say "Some checks failed. Fix them, then run: live-clipper start"
if [ "$START" = 1 ]; then
  live-clipper start
else
  say "Done. Start it with: live-clipper start"
fi
