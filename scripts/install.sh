#!/usr/bin/env bash
# myx one-liner installer & updater — friendly, non-technical, idempotent.
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/factnest365-ops/myx/main/scripts/install.sh | bash
#   ./scripts/install.sh [--update] [--brew|--uv|--pipx|--pip]
set -euo pipefail

BOLD=$'\033[1m'; DIM=$'\033[2m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; RESET=$'\033[0m'
GIT_PKG_URL="git+https://github.com/factnest365-ops/myx.git"

info()  { printf "${BOLD}  %s${RESET}\n" "$*"; }
ok()    { printf "${GREEN}  ✓ %s${RESET}\n" "$*"; }
warn()  { printf "${YELLOW}  ! %s${RESET}\n" "$*"; }
err()   { printf "${RED}  ✗ %s${RESET}\n" "$*"; }

has() { command -v "$1" >/dev/null 2>&1; }

MODE="install"
METHOD="auto"
for arg in "$@"; do
  case "$arg" in
    --update|--upgrade|-u) MODE="update" ;;
    --brew) METHOD="brew" ;;
    --uv)   METHOD="uv" ;;
    --pipx) METHOD="pipx" ;;
    --pip)  METHOD="pip" ;;
    -h|--help)
      echo "Usage: install.sh [--update] [--brew|--uv|--pipx|--pip]"
      exit 0
      ;;
  esac
done

prompt_tty() {
  local default_val="$1"
  local ans=""
  if [[ -t 0 ]]; then
    read -r ans || ans=""
  elif [[ -r /dev/tty ]]; then
    read -r ans </dev/tty || ans=""
  fi
  printf "%s" "${ans:-$default_val}"
}

# Detect repo root if running from checkout
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." 2>/dev/null && pwd || echo "")"

# Fast path: if `--update` was passed and `myx` already supports `myx update`, delegate to it
if [[ "$MODE" == "update" ]] && has myx; then
  info "Delegating to built-in self-updater: myx update"
  exec myx update
fi

printf "\n${BOLD}myx installer${RESET} ${DIM}— terminal music for YouTube/YT Music${RESET}\n\n"

# Optional pure-Homebrew formula install
if [[ "$METHOD" == "brew" ]]; then
  if ! has brew; then
    err "Homebrew (brew) not found."
    exit 1
  fi
  if [[ -n "$REPO_ROOT" ]] && [[ -f "$REPO_ROOT/Formula/myx.rb" ]]; then
    info "Installing/upgrading myx via local Formula/myx.rb..."
    brew install --HEAD "$REPO_ROOT/Formula/myx.rb" || brew upgrade --fetch-HEAD "$REPO_ROOT/Formula/myx.rb"
  else
    info "Installing/upgrading myx via Homebrew..."
    brew tap factnest365-ops/myx https://github.com/factnest365-ops/myx.git 2>/dev/null || true
    brew install --HEAD factnest365-ops/myx/myx || brew upgrade --fetch-HEAD factnest365-ops/myx/myx
  fi
  ok "myx installed via Homebrew"
  exit 0
fi

# 1. Python check
if ! has python3; then
  err "python3 not found. Install Python ≥ 3.11 first:"
  echo "    macOS: brew install python@3.12"
  echo "    Ubuntu: sudo apt-get install python3 python3-pip"
  exit 1
fi
PY_VER="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
PY_OK="$(python3 -c 'import sys; print(1 if sys.version_info >= (3,11) else 0)')"
if [[ "$PY_OK" != "1" ]]; then
  err "Python $PY_VER found — need ≥ 3.11. Upgrade Python and re-run."
  exit 1
fi
ok "Python $PY_VER"

# 2. System bins: mpv, yt-dlp, ffmpeg
need_bins=()
for bin in mpv yt-dlp ffmpeg; do
  if has "$bin"; then ok "$bin found ($(command -v "$bin"))"
  else need_bins+=("$bin"); warn "$bin missing"
  fi
done

if (( ${#need_bins[@]} > 0 )); then
  info "Missing bins: ${need_bins[*]}"
  if has brew; then
    printf "  Install with Homebrew? [Y/n] "
    ans="$(prompt_tty "Y")"
    echo ""
    if [[ "$ans" =~ ^[Yy] ]]; then
      brew install "${need_bins[@]}"
      ok "Installed via brew"
    else
      warn "Skipping — install manually: brew install ${need_bins[*]}"
    fi
  elif has apt-get; then
    echo "  Try: sudo apt-get update && sudo apt-get install -y mpv ffmpeg"
    echo "       pipx install yt-dlp  # yt-dlp is rarely in apt"
    printf "  Run apt install now? [y/N] "
    ans="$(prompt_tty "N")"
    echo ""
    if [[ "$ans" =~ ^[Yy] ]]; then
      sudo apt-get update
      apt_pkgs=(); pipx_pkgs=()
      for b in "${need_bins[@]}"; do
        if [[ "$b" == "yt-dlp" ]]; then pipx_pkgs+=("$b"); else apt_pkgs+=("$b"); fi
      done
      (( ${#apt_pkgs[@]} )) && sudo apt-get install -y "${apt_pkgs[@]}" || true
      if (( ${#pipx_pkgs[@]} )); then
        has pipx || python3 -m pip install --user pipx
        for p in "${pipx_pkgs[@]}"; do pipx install "$p" || pipx upgrade "$p" || true; done
      fi
    fi
  else
    warn "Install missing bins with your package manager, then re-run this script."
    echo "    mpv:    https://mpv.io/installation/"
    echo "    yt-dlp: https://github.com/yt-dlp/yt-dlp#installation"
    echo "    ffmpeg: https://ffmpeg.org/download.html"
  fi
fi

# 3. Install or upgrade myx (Python package via uv / pipx / pip)
info "Installing/updating myx (Python package)..."

install_with_uv() {
  local src="$1"
  if [[ "$src" == "local" ]]; then
    uv tool install --force -e "$REPO_ROOT"
  else
    uv tool install --upgrade myx-player 2>/dev/null || uv tool install --force "$GIT_PKG_URL"
  fi
}

install_with_pipx() {
  local src="$1"
  if [[ "$src" == "local" ]]; then
    pipx install -e "$REPO_ROOT" --force 2>/dev/null || pipx upgrade myx-player
  else
    pipx install myx-player 2>/dev/null || pipx upgrade myx-player 2>/dev/null || pipx install --force "$GIT_PKG_URL"
  fi
}

install_with_pip() {
  local src="$1"
  if [[ "$src" == "local" ]]; then
    python3 -m pip install --upgrade -e "$REPO_ROOT" --break-system-packages 2>/dev/null || python3 -m pip install --upgrade -e "$REPO_ROOT"
  else
    python3 -m pip install --user --upgrade myx-player 2>/dev/null \
      || python3 -m pip install --upgrade myx-player --break-system-packages 2>/dev/null \
      || python3 -m pip install --user --upgrade "$GIT_PKG_URL" 2>/dev/null \
      || python3 -m pip install --upgrade "$GIT_PKG_URL" --break-system-packages
  fi
}

install_myx() {
  local src="remote"
  if [[ -n "$REPO_ROOT" ]] && [[ -f "$REPO_ROOT/pyproject.toml" ]] && grep -Eq 'name = "myx(-player)?"' "$REPO_ROOT/pyproject.toml" 2>/dev/null; then
    src="local"
    info "Detected local checkout at $REPO_ROOT — installing from source"
  fi

  case "$METHOD" in
    uv)   install_with_uv "$src" ;;
    pipx) install_with_pipx "$src" ;;
    pip)  install_with_pip "$src" ;;
    auto)
      if has uv; then install_with_uv "$src"
      elif has pipx; then install_with_pipx "$src"
      else install_with_pip "$src"
      fi
      ;;
  esac
}

if ! install_myx; then
  err "Install failed. Try manually:"
  echo "    uv tool install myx-player   # or: pipx install myx-player  /  pip install myx-player"
  exit 1
fi
ok "myx installed"

# 4. Verify
if has myx; then
  ok "myx on PATH: $(command -v myx) ($(myx --version 2>/dev/null || echo 'installed'))"
  if myx doctor 2>&1 | head -20; then
    echo ""
  fi
else
  warn "myx not on PATH yet. You may need to add uv/pipx bin to PATH:"
  has uv && echo "    uv tool update-shell  &&  exec \$SHELL -l"
  has pipx && echo "    pipx ensurepath  &&  exec \$SHELL -l"
fi

printf "\n${GREEN}${BOLD}Done!${RESET} Try:\n"
printf "  ${BOLD}myx${RESET}              ${DIM}# launch app shell (type to search)${RESET}\n"
printf "  ${BOLD}myx update${RESET}       ${DIM}# update myx (brew/uv/pipx/pip) + yt-dlp${RESET}\n"
printf "  ${BOLD}myx doctor${RESET}       ${DIM}# check setup${RESET}\n"
printf "  ${BOLD}myx --help${RESET}       ${DIM}# all commands${RESET}\n\n"
