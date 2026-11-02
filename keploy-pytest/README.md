# keploy-pytest

Joins a pytest suite to keploy's Universal Mocker. `keploy mock record` records the calls each test's code makes; `keploy mock replay` answers them from the recording, so the suite runs without its dependencies. keploy judges every recorded test, and lists each test it cannot replay with a reason.

This plugin tells keploy where each test begins and ends. Through it, keploy skips a test it set aside and fails a test whose calls changed. It speaks keploy's scope protocol v1, the same one go-sdk's `keploytest`, `@keploy/playwright` and `@keploy/jest` speak, and decides nothing itself.

## Use

The package is not published yet: install it from this folder.

```sh
pip install /path/to/python-sdk/keploy-pytest
```

It needs Python 3.9 or later and pytest 8 or later. Its rows run on Python 3.9 with pytest 8.0, Python 3.12 with pytest 8.4 and Python 3.14 with pytest 9. Its e2e lane (`e2e/lane.sh`) installs the built wheel into a small suite and runs it under keploy 3.8.106: recorded with its provider up, replayed with the provider stopped.

Installed, pytest loads it by itself (its `pytest11` entry point, named `keploy`). With `PYTEST_DISABLE_PLUGIN_AUTOLOAD` set, load it with `-p keploy_pytest.plugin`.

keploy needs the project's `keploy.yml` to name the recording and CI's test command; without `mock.command`, keploy refuses the run with `no keploy project here: set mock.command ...`:

```yaml
# keploy.yml
mock:
  name: shop                  # the recording: keploy/shop/
  command: python -m pytest   # CI's test command
```

Run the suite under keploy with `KEPLOY_PROVEN=1`, in one pytest process. `--local` keeps the recording on disk, in `keploy/<name>/`:

```sh
KEPLOY_PROVEN=1 keploy mock record --local -c "python -m pytest"
KEPLOY_PROVEN=1 keploy mock replay --local -c "python -m pytest"
```

`keploy mock record` runs the command against the real dependencies, then replays it twice to judge each test before it keeps the recording. `keploy mock replay` answers every call from the recording.

`KEPLOY_PROVEN=1` is keploy's switch for its proven model. keploy speaks this plugin's wire, scope protocol v1 (the claim, begin and end), only under it. Without it, keploy answers the claim with a plain 404, and every test errors with `keploy: this keploy agent answered 404 to POST /agent/instance, so this pytest process cannot claim keploy's run: ...`, which names the switch.

keploy's first release runs one test process at a time. Under pytest-xdist, the controller claims nothing and each worker claims for itself, so `-n 1` works; with more workers, keploy refuses the second worker's claim while the first is alive, and that worker's tests error with keploy's reason.

## Without keploy

With `KEPLOY_MOCK_AGENT` unset or `off`, the plugin registers nothing, and pytest runs as it would without it. Only `keploy mock record` and `keploy mock replay` set that variable.

## Under keploy

- **Claim.** The pytest process claims keploy's run when its session starts (`POST /agent/instance`), before pytest collects, and so imports, any test module. The claim's key is `.`, and it names the project directory keploy names (`KEPLOY_MOCK_PROJECT`) as the folder the test files are named from (the current directory only when keploy names none). After the claim, `KEPLOY_TEST_PID` names the pytest process, so a process a test starts (the service under test, a helper) is a member of the run and claims nothing.
- **Test ID.** keploy names a test `file::name`. `file` is the test's file relative to the project directory, with `/` separators. `name` is the test's pytest node ID within that file: `test_charges_a_card`, `TestOrders::test_prices_a_book`, `test_price[book]`. keploy's ID escapes a `::` inside the name, so keploy shows a class's test as `tests/test_orders.py::TestOrders%3A%3Atest_prices_a_book`.
- **Begin.** Each test begins (`POST /agent/scope/begin`) once the fixtures of a wider scope it uses (session, package, module, class) are set up, and before its own function-scoped fixtures, whoever declares them: the test module, a `conftest.py`, or another plugin (an autouse fixture included). pytest's scopes decide where a call belongs: a call a session or module fixture makes while pytest sets it up before a test belongs to the test process, outside every test, and a call a function-scoped fixture or the test makes belongs to the test. A test pytest skips by a mark is never begun.
- **A wider fixture set up inside a test.** A fixture of a wider scope (session, package, module, class) that pytest sets up only once a test has begun would have its calls counted as that test's, and they would move to whichever test sets it up first in another run (a `-k` subset, `--lf`, a random order). That happens when it is first requested with `request.getfixturevalue` while the test runs, or through a function fixture that overrides it by the same name (`def db(db)`). keploy-pytest fails that test at its teardown, naming each such fixture, its scope and the fix: name it in the test's arguments or a fixture's, or in an autouse fixture of its scope in `conftest.py`. The run's end lists each such test in a `keploy: N test(s) failed: a fixture of a wider scope was set up inside them` section.
- **pytest-django.** pytest-django creates and migrates its test database in the session fixture `django_db_setup`, which it sets up from inside a test for `@pytest.mark.django_db` and for `live_server`. keploy-pytest sets `django_db_setup` up before such a test begins, as pytest does for a test that requests `db` or `transactional_db`, so the test database's calls belong to the test process.
- **Skip.** A test keploy set aside (quarantined, or held by a judge) is skipped by pytest itself, before its own fixtures run, with keploy's reason as pytest's skip reason (`-rs` shows it). The plugin also lists each test keploy set aside, with its reason, in a `keploy` section of pytest's summary. These lists travel in pytest's reports, so under pytest-xdist the controller prints them too.
- **End.** The test ends (`POST /agent/scope/end`) once its own function-scoped fixtures are torn down, before any fixture of a wider scope is, with its outcome: pytest's outcome of its setup and call (`passed`, `failed`, or `skipped`, the worst of the two; an expected failure, `xfail`, is pytest's skipped). When keploy saw a call with no recorded answer, or a recorded call the test no longer made, the test errors at its teardown with keploy's text.
- **Fail closed.** Any answer of the agent the plugin cannot trust (a non-2xx answer, an unreadable body, a missing or other protocol version) errors the test it belongs to; a claim that fails errors every test that uses pytest's fixtures. A test ID begun a second time in one process (a rerun, such as pytest-rerunfailures' `--reruns`) errors with the reason, so no such test runs unjudged. Nothing is retried, and there is no timeout: keploy answers once the work it owes is done.

A fixture's teardown error is pytest's error of the test, and pytest fails the run with it; keploy is told the outcome of the test's setup and call, as the end comes after the test's own teardown and pytest reports teardown errors only once the whole teardown is done.

## Limits of this version

- A fixture of a wider scope set up inside a test fails that test (see above), rather than being moved out of it. On pytest 8, a fixture that overrides pytest-django's `db` or `transactional_db` by the same name hides `django_db_setup` from pytest's static order, so the test fails and names it; request it in the override's arguments (`def db(db, django_db_setup)`).

- A test is judged through pytest's fixtures. An item that uses none (a custom `pytest.Item` from a `pytest_collect_file` hook, such as a linter plugin's) runs as pytest runs it, not judged by keploy, even after a failed claim; its calls are the test process's. The run's end names each such item in a `keploy: N item(s) not judged by keploy` section.

## Tests

```sh
pip install -e . pytest-xdist pytest-django django
python -m pytest tests
KEPLOY_BIN=/path/to/keploy bash e2e/lane.sh   # needs root (sudo) for keploy
```
