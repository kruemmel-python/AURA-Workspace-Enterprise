#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
test_dir=$(mktemp -d "${TMPDIR:-/tmp}/aura-db-test.XXXXXXXX")
trap 'rm -rf -- "$test_dir"' EXIT
mkdir -p "$test_dir/KR_BUILD"
cxx=${CXX:-c++}
"$cxx" -std=c++20 -O2 "$root/tests/enterprise_database_tests.cpp" "$root/core/enterprise_database.cpp" -lsqlite3 -lcrypto -o "$test_dir/database-tests"
"$cxx" -std=c++20 -O2 "$root/tests/linux_database_compat_tests.cpp" "$root/core/enterprise_database.cpp" -lsqlite3 -lcrypto -o "$test_dir/compat-tests"
(cd "$test_dir" && ./database-tests && ./compat-tests)
