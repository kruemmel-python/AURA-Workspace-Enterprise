#!/usr/bin/env bash
# Run Linux GUI, database and integration scenarios on a graphical desktop.
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
python_bin=${AURA_PYTHON:-/usr/bin/python3}
if [[ ! -x "$python_bin" ]]; then
  printf 'Python ist nicht ausführbar: %s\n' "$python_bin" >&2
  exit 1
fi
log=$(mktemp "${TMPDIR:-/tmp}/aura-regression-XXXXXXXX")
trap 'rm -f -- "$log"' EXIT
go_bin=${AURA_GO:-go}
printf 'Linux-Client und Testserver werden gebaut …\n'
if ! clients/linux/build.sh >"$log" 2>&1; then cat "$log" >&2; exit 1; fi
if ! (cd services && CGO_ENABLED=1 GOTOOLCHAIN=local "$go_bin" build -trimpath -o ../KR_BUILD/linux-x64/aura-sync-server .) >"$log" 2>&1; then cat "$log" >&2; exit 1; fi
if ! (cd services && GOTOOLCHAIN=local "$go_bin" test ./...) >"$log" 2>&1; then printf 'FEHLER Go-Tests\n' >&2; cat "$log" >&2; exit 1; fi
printf 'OK Go-Tests\n'
if ! (cd rules && cargo test --locked --offline) >"$log" 2>&1; then printf 'FEHLER Rust-Tests\n' >&2; cat "$log" >&2; exit 1; fi
printf 'OK Rust-Tests\n'
if ! tests/run-linux-database-tests.sh >"$log" 2>&1; then printf 'FEHLER Datenbanktests\n' >&2; cat "$log" >&2; exit 1; fi
printf 'OK Datenbanktests\n'
test_timeout=${AURA_TEST_TIMEOUT:-300}
count=0
for testfile in tests/linux_*.py; do
  case "$testfile" in *test.py|*integration.py) ;; *) continue ;; esac
  if timeout "$test_timeout" "$python_bin" "$testfile" >"$log" 2>&1; then
    printf 'OK %s\n' "$testfile"
    ((count+=1))
  else
    printf 'FEHLER %s\n' "$testfile" >&2
    cat "$log" >&2
    exit 1
  fi
done
if ((count==0)); then printf 'Keine Linux-Tests gefunden.\n' >&2; exit 1; fi
printf '%d Linux-Tests erfolgreich.\n' "$count"
