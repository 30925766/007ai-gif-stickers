#!/usr/bin/env bash
set -euo pipefail
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
skill_root="$(cd -- "$script_dir/.." && pwd)"
if [[ "$(uname -s)" != "Darwin" ]]; then
  printf '%s\n' "This launcher supports macOS. Use run.ps1 on Windows." >&2
  exit 1
fi
find_python() {
  local candidate
  for candidate in python3 python /opt/homebrew/bin/python3 /usr/local/bin/python3 \
    /opt/homebrew/opt/python@3.12/bin/python3.12 /usr/local/opt/python@3.12/bin/python3.12 \
    /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
      "$candidate" -c 'import sys, ssl, venv; sys.exit(0 if sys.version_info >= (3,10) else 1)' >/dev/null 2>&1; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}
python_exe="$(find_python || true)"
if [[ -z "$python_exe" ]]; then
  printf '%s\n' "Installing Python for the GIF sticker skill..." >&2
  brew_exe=""
  for candidate in brew /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if command -v "$candidate" >/dev/null 2>&1; then
      brew_exe="$(command -v "$candidate")"
      break
    fi
  done
  if [[ -n "$brew_exe" ]]; then
    "$brew_exe" install python@3.12 >&2 || true
    python_exe="$(find_python || true)"
  fi
  if [[ -z "$python_exe" ]]; then
    mkdir -p "$skill_root/.runtime"
    installer="$skill_root/.runtime/python-3.12.10-macos11.pkg"
    curl --fail --location --proto '=https' --tlsv1.2 \
      'https://www.python.org/ftp/python/3.12.10/python-3.12.10-macos11.pkg' -o "$installer"
    signature="$(/usr/sbin/pkgutil --check-signature "$installer")"
    if [[ "$signature" != *"Python Software Foundation"* || "$signature" != *"DJ3H93M7VJ"* ]]; then
      printf '%s\n' "Python installer signature verification failed." >&2
      exit 1
    fi
    if [[ "$(id -u)" == "0" ]]; then
      /usr/sbin/installer -pkg "$installer" -target / >&2
    elif sudo -n true 2>/dev/null; then
      sudo -n /usr/sbin/installer -pkg "$installer" -target / >&2
    else
      printf 'Administrator authorization is needed. Open this signed installer, then rerun:\n%s\n' "$installer" >&2
      exit 1
    fi
    python_exe="$(find_python || true)"
  fi
fi
if [[ -z "$python_exe" ]]; then
  printf '%s\n' "Python 3.10+ is unavailable. Install Python, then rerun." >&2
  exit 1
fi
exec "$python_exe" "$script_dir/bootstrap.py" "$@"
