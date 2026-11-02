#!/usr/bin/env bash
# The e2e lane of keploy-pytest: this commit's package, built as a wheel and
# installed with a pinned pytest into a virtualenv for a small pytest suite
# (e2e/shop), run under a real keploy that judges each test. The suite's code
# calls a provider over HTTP; keploy records those calls with the provider
# up, and replays them with the provider stopped.
#
#   KEPLOY_BIN=/path/to/keploy bash keploy-pytest/e2e/lane.sh
#
# KEPLOY_BIN is a keploy build that judges each test under KEPLOY_PROVEN=1
# (keploy's enterprise build; the CI job downloads a pinned release). keploy
# needs root for its eBPF hooks: the lane runs it through sudo unless it is
# root already. python3 (3.10 or later, for the pinned pytest) with venv and
# pip must be on PATH.
#
# Checks, each judged by what a user reads (exit codes, pytest's summary and
# each test's report, the files keploy wrote):
#   1. without keploy the suite passes as plain pytest, and nothing says
#      keploy;
#   2. keploy mock record keeps each test's calls under the test's name;
#   3. keploy mock replay, with the provider stopped, passes every test;
#   4. a test whose call has no recorded answer errors, by keploy, at its
#      teardown with keploy's text; the others pass;
#   5. a test that no longer makes its recorded call errors, by keploy, at
#      its teardown with keploy's text; the others pass;
#   6. a test quarantined in the set is skipped by pytest with keploy's
#      reason, and the replay passes.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG="$(cd "$HERE/.." && pwd)"
KEPLOY_BIN="${KEPLOY_BIN:?set KEPLOY_BIN to a keploy binary}"
PORT="${SHOP_PORT:-18741}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/keploy-pytest-e2e.XXXXXX")"
FAILED=0
PROVIDER_PID=""
export SHOP_PROVIDER="http://127.0.0.1:$PORT"

cleanup() {
  [ -z "$PROVIDER_PID" ] || kill "$PROVIDER_PID" 2>/dev/null
  wait 2>/dev/null
}
trap cleanup EXIT

ok() { echo "  ok: $*"; }
fail() { echo "  FAIL: $*"; FAILED=1; }
# show ID: the run's output, for a failing check.
show() {
  local stream
  for stream in out err; do
    echo "  --- $1: std$stream (last 120 lines, keploy's banner left out) ---"
    grep -av '38;5;' "$WORK/logs/$1.$stream" | tail -n 120 | sed 's/^/  | /'
  done
}

if [ "$(id -u)" -eq 0 ]; then AS_ROOT=(env); else AS_ROOT=(sudo -E env "PATH=$PATH" "HOME=$HOME"); fi

# The package as PyPI would take it (a wheel), installed beside the pinned
# pytest into a virtualenv; the suite runs from a copy of e2e/shop.
mkdir -p "$WORK/logs" "$WORK/pkg"
tar -C "$PKG" --exclude=./e2e --exclude=./build --exclude='*.egg-info' -cf - . | tar -C "$WORK/pkg" -xf -
{ python3 -m venv "$WORK/venv" &&
  "$WORK/venv/bin/python" -m pip wheel --no-deps --wheel-dir "$WORK/dist" "$WORK/pkg" &&
  "$WORK/venv/bin/python" -m pip install -r "$HERE/shop/requirements.txt" "$WORK"/dist/keploy_pytest-*.whl; } >"$WORK/logs/install.log" 2>&1 ||
  { cat "$WORK/logs/install.log"; echo "FAIL: could not build and install the package"; exit 1; }
WHL=$(ls "$WORK"/dist/keploy_pytest-*.whl)
export PATH="$WORK/venv/bin:$PATH"
cp -R "$HERE/shop" "$WORK/shop"
cd "$WORK/shop" || exit 1
echo "installed $(basename "$WHL") and $(python -m pytest --version 2>&1) into $WORK/venv; the suite is at $WORK/shop"

start_provider() {
  : >"$WORK/logs/provider.log"
  python provider.py "$PORT" >"$WORK/logs/provider.log" 2>&1 &
  PROVIDER_PID=$!
  until grep -qx ready "$WORK/logs/provider.log"; do
    kill -0 "$PROVIDER_PID" 2>/dev/null || { cat "$WORK/logs/provider.log"; echo "FAIL: the provider exited before it listened"; exit 1; }
    sleep 0.2
  done
}
stop_provider() {
  kill "$PROVIDER_PID" 2>/dev/null
  wait "$PROVIDER_PID" 2>/dev/null
  PROVIDER_PID=""
}

# run ID CMD...: one run, stdin closed, stdout and stderr kept apart. Sets EC.
run() {
  local id=$1
  shift
  echo
  echo "== $id: $*"
  "$@" </dev/null >"$WORK/logs/$id.out" 2>"$WORK/logs/$id.err"
  EC=$?
  echo "  exit $EC"
  # LANE_VERBOSE=1 shows every run's output, not only a failing check's.
  [ -z "${LANE_VERBOSE:-}" ] || show "$id"
}
# keploy ID ARGS...: one keploy run under the proven switch, as root.
keploy() {
  local id=$1
  shift
  run "$id" "${AS_ROOT[@]}" KEPLOY_PROVEN=1 KEPLOY_INVOKER=ci "SHOP_PROVIDER=$SHOP_PROVIDER" \
    "SHOP_CHANGE=${SHOP_CHANGE:-}" "$KEPLOY_BIN" "$@" --disable-tele
}
# error_of ID HEAD: pytest's report of the error HEAD in a run (pytest's
# "ERROR at teardown of Class.test" header): the lines after it, up to the
# next header.
error_of() {
  awk -v h="$2" '{ t = $0; gsub(/^_+ | _+$/, "", t) }
    on && ($0 ~ /^_{3,} / || $0 ~ /^={3,} /) { on = 0 }
    t == h { on = 1; next }
    on' "$WORK/logs/$1.out"
}
# pytest_summary ID: pytest's closing line of a run, its counts only.
pytest_summary() {
  grep -aE '^=+ .* in [0-9.]+s' "$WORK/logs/$1.out" | tail -n 1 | sed -E 's/^=+ //; s/ in [0-9.]+s.*$//'
}
# has ID TEXT: whether the run's output (pytest's and keploy's) has a line
# that is TEXT.
has() { grep -aqxF -- "$2" "$WORK/logs/$1.out" "$WORK/logs/$1.err"; }

PYTEST='python -m pytest -rfEs -p no:cacheprovider'
# keploy's IDs: file::name, the name pytest's node ID within its file, whose
# "::" keploy's ID escapes.
PRICES='tests/test_orders.py::TestOrders%3A%3Atest_prices_a_book'
RESERVES='tests/test_orders.py::TestOrders%3A%3Atest_reserves_a_book'
CHARGES='tests/test_payments.py::test_charges_a_card'
# judge ID WHAT: reports a check from the reasons WHY holds against it.
judge() {
  if [ "${#WHY[@]}" -eq 0 ]; then ok "$2"; return; fi
  fail "$2"
  printf '    - %s\n' "${WHY[@]}"
  show "$1"
}

# 1. Without keploy: plain pytest.
start_provider
run inert $PYTEST
WHY=()
[ "$EC" -eq 0 ] || WHY+=("exit $EC, want 0")
[ "$(pytest_summary inert)" = "3 passed" ] || WHY+=("pytest says '$(pytest_summary inert)'")
# pytest's header names the installed plugins (keploy-pytest among them),
# and its rootdir: no other line may say keploy.
! cat "$WORK/logs/inert.out" "$WORK/logs/inert.err" | grep -av -e '^plugins: ' -e '^rootdir: ' | grep -aqi keploy || WHY+=("a line says keploy")
judge inert "without keploy the suite passes as plain pytest, and nothing says keploy"

# 2. Record, with the provider up: keploy's own rounds replay it twice more.
keploy record mock record --local -c "$PYTEST"
stop_provider
WHY=()
[ "$EC" -eq 0 ] || WHY+=("exit $EC, want 0")
has record "keploy: committed keploy/shop and its verdicts.yaml: 3 replayable, 0 quarantined" ||
  WHY+=("keploy did not commit 3 replayable tests")
for t in "$PRICES" "$RESERVES" "$CHARGES"; do
  grep -aqxF -- "    - id: $t" keploy/shop/mappings.yaml 2>/dev/null || WHY+=("keploy/shop/mappings.yaml does not name $t")
done
judge record "keploy mock record keeps each test's calls under its file and pytest's node ID"

# 3. Replay with the provider stopped.
keploy replay mock replay --local -c "$PYTEST"
WHY=()
[ "$EC" -eq 0 ] || WHY+=("exit $EC, want 0")
[ "$(pytest_summary replay)" = "3 passed" ] || WHY+=("pytest says '$(pytest_summary replay)'")
has replay "keploy: shop: 3 test(s) ran: 3 passed, 0 failed (0 held by keploy), 0 skipped by the test; 0 quarantined (skipped by keploy)" ||
  WHY+=("keploy did not judge 3 tests passed")
has replay "VERDICT  passed" || WHY+=("keploy's verdict is not passed")
judge replay "keploy mock replay with the provider stopped passes every test"

# 4. A call the recording has no answer for: keploy errors that test at its
# teardown, with keploy's text, and only that test.
SHOP_CHANGE=miss keploy replay-miss mock replay --local -c "$PYTEST"
WHY=()
[ "$EC" -eq 1 ] || WHY+=("exit $EC, want 1")
[ "$(pytest_summary replay-miss)" = "1 failed, 2 passed, 1 error" ] || WHY+=("pytest says '$(pytest_summary replay-miss)'")
error_of replay-miss "ERROR at teardown of TestOrders.test_prices_a_book" |
  grep -aqF "keploy: GET /price/pen to 127.0.0.1:$PORT had no recorded answer (path differs)" ||
  WHY+=("pytest's error of test_prices_a_book does not carry keploy's miss")
has replay-miss "FAILED_BY_KEPLOY  $PRICES (failed): keploy/unmocked_call" || WHY+=("keploy does not hold the miss against test_prices_a_book")
judge replay-miss "a call with no recorded answer errors its own test with keploy's text; the others pass"

# 5. A recorded call the test no longer makes: keploy errors it at its end.
SHOP_CHANGE=unconsumed keploy replay-unconsumed mock replay --local -c "$PYTEST"
WHY=()
[ "$EC" -eq 1 ] || WHY+=("exit $EC, want 1")
[ "$(pytest_summary replay-unconsumed)" = "3 passed, 1 error" ] || WHY+=("pytest says '$(pytest_summary replay-unconsumed)'")
error_of replay-unconsumed "ERROR at teardown of TestOrders.test_reserves_a_book" |
  grep -aqF "keploy: recorded call POST /reserve (mock-1) was never made." ||
  WHY+=("pytest's error of test_reserves_a_book does not carry keploy's unmade call")
has replay-unconsumed "FAILED_BY_KEPLOY  $RESERVES (failed): keploy/unconsumed_mock" || WHY+=("keploy does not hold the unmade call against test_reserves_a_book")
judge replay-unconsumed "a recorded call the test no longer makes errors that test with keploy's text; the others pass"

# 6. A test quarantined in the set: pytest skips it, before its own
# fixtures, with keploy's reason; the replay passes.
keploy quarantine mock quarantine "$CHARGES" --reason 'e2e: set aside by the lane' --local --name shop
WHY=()
[ "$EC" -eq 0 ] || WHY+=("keploy mock quarantine exited $EC")
keploy replay-quarantined mock replay --local -c "$PYTEST"
[ "$EC" -eq 0 ] || WHY+=("replay exit $EC, want 0")
[ "$(pytest_summary replay-quarantined)" = "2 passed, 1 skipped" ] || WHY+=("pytest says '$(pytest_summary replay-quarantined)'")
grep -aqE "^SKIPPED \[1\] tests/test_payments\.py:[0-9]+: keploy: .*e2e: set aside by the lane" "$WORK/logs/replay-quarantined.out" ||
  WHY+=("pytest does not show keploy's reason for skipping test_charges_a_card")
grep -aqE "^$CHARGES: keploy: " "$WORK/logs/replay-quarantined.out" ||
  WHY+=("keploy-pytest's summary does not list test_charges_a_card as set aside")
grep -aqF "1 quarantined (skipped by keploy)" "$WORK/logs/replay-quarantined.err" || WHY+=("keploy does not count 1 quarantined test")
judge replay-quarantined "a quarantined test is skipped by pytest with keploy's reason; the replay passes"

echo
if [ "$FAILED" -ne 0 ]; then echo "e2e: FAILED (logs in $WORK/logs)"; exit 1; fi
echo "e2e: every check passed"
