#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
python_bin=${AURA_PYTHON:-/usr/bin/python3}
if [[ ! -x "$root/KR_BUILD/linux-x64/aura-workspace-enterprise" ]]; then
  echo 'Linux-Client fehlt. Bitte zuerst clients/linux/build.sh ausführen.' >&2
  exit 1
fi
if [[ ! -x "$python_bin" ]]; then
  echo 'Python 3 fehlt oder ist nicht ausführbar.' >&2
  exit 1
fi
if ! "$python_bin" - "$root/KR_BUILD/linux-x64/libaura_linux_local.so" <<'PY'
import ctypes
import sys

try:
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk
except (ImportError, ValueError) as error:
    print('GTK für Python fehlt. Unter Ubuntu: sudo apt install python3-gi gir1.2-gtk-3.0', file=sys.stderr)
    print(error, file=sys.stderr)
    sys.exit(1)

try:
    ctypes.CDLL(sys.argv[1])
except OSError as error:
    print('Die lokale AURA-Bibliothek oder eine ihrer Systemabhängigkeiten fehlt:', file=sys.stderr)
    print(error, file=sys.stderr)
    sys.exit(1)
PY
then
  exit 1
fi
exec "$python_bin" "$root/clients/linux/aura_gui.py" "$@"
