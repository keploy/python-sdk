# Keploy Python Coverage Agent

The Keploy Python Coverage Agent is a lightweight, sidecar module designed to integrate with the Keploy integration testing platform (enterprise version). When imported into a Python application, it enables Keploy to capture and report code coverage on a per-test-case basis.

## Installation and Usage

Follow these steps to integrate the coverage agent into your Python project.

### Step 1: Install the Agent

Install the `keploy-agent` package from PyPI into your virtual environment:

```bash
pip install keploy-agent
```

If you are developing the agent, install it in editable mode from a checkout of this repository instead:

```bash
pip install -e /path/to/python-sdk
```

### Step 2: Integrate into Your Application

To enable coverage tracking, import the `keploy_agent` module at the **very top** of your application's main entry point file (e.g., `app.py`, `main.py`).

It is crucial that this is one of the first imports, as this ensures the agent is initialized before your application code begins to execute.

**Example `app.py`:**

```python
import keploy_agent  # <-- Add this line at the top
import os
from flask import Flask

app = Flask(__name__)

@app.get("/")
def hello():
    # This function will be tracked by the coverage agent
    # when its endpoint is hit during a Keploy test.
    return "Hello, World!"

if __name__ == "__main__":
    app.run()
```

#### Or: no change to the app

Instead of the import, put the agent's start-up hook first on the app's
`PYTHONPATH`. Python then starts the agent in the app's process at start-up:

```bash
PYTHONPATH="$(python -m keploy_agent --pythonpath)" uvicorn app.main:app --port 8000
```

`python -m keploy_agent --pythonpath` prints the hook's directory followed by
the `PYTHONPATH` already set, which is kept as it is; an app's own
`sitecustomize` still runs after the agent's.

One process is measured: the one that serves keploy's control socket.

- A reloader's server child takes the socket over from its parent before any
  test runs, also when the parent is a container's command (pid 1), so the
  process that serves the requests is the one measured. A server child is
  Django `runserver`'s (`RUN_MAIN=true`), Flask's debug reloader's
  (`WERKZEUG_RUN_MAIN=true`), `uvicorn --reload`'s or hypercorn's
  (multiprocessing's `spawn_main`), or one started with
  `KEPLOY_COVERAGE_TAKEOVER=1` (another reloader). The socket changes hands
  once per process tree: the server child never gives it on, also to its own
  children, which carry its marks. Any other process the app starts (a pool,
  a sidecar, a script) does not take the socket: the process that holds it
  keeps it, and a test whose window ran no line there is reported as not
  measured while that other process lives. On macOS, where `spawn_main` also starts
  multiprocessing's pool workers, a pool created before the first test can
  take the socket: create it after the app is ready, or run without it. While the parent lives, a test
  whose window ran no line of the child is reported to keploy as not
  measured, with the reason: keploy cannot tell that the parent did not
  serve it.
- multiprocessing's own helpers (its resource tracker, forkserver and
  semaphore tracker) never start the agent, and a helper process the app
  starts once tests run never takes the socket.
- A prefork server (gunicorn, `uvicorn --workers`) serves requests in
  processes keploy does not measure: a test whose window then ran no line of
  the measuring process is reported as not measured, with that reason, never
  as a test that ran no code. So is a window of a process that holds the
  socket and listens on no socket of its own (Linux).
- Measuring every process of such an app and merging their lines into one
  record per test is the next step; until then, run the app as one process to
  measure it.

### What is measured

- The app's own `.py` files under the app root (`KEPLOY_APP_SOURCE_DIR`, else
  the working directory): not virtual environments, `site-packages`, hidden
  directories, `tests/`, `test_*.py`, `*_test.py` or `conftest.py`. Leave more
  out with `KEPLOY_COVERAGE_EXCLUDE` (comma-separated globs of app-root-relative
  paths, such as `scripts/*,migrations/*`).
- Python 3.12 and later: `sys.monitoring`, which measures every thread at the
  cost of about one call per line a test runs. Python 3.9 to 3.11:
  `sys.settrace`, which measures threads started after the agent: import it
  first.
- Keploy asks the agent once per build for every app file's coverable lines,
  so its coverage report shows the lines no test ran too.
- A test's lines are the app lines run between keploy's START and END for it.
  Lines that run when a module is imported (`import`, `def` and `class` lines,
  module and class-body code) run before the first test unless the import
  itself happens during a test, so no test covers them and the report counts
  them as not run.
- Work that goes on after a test's response (a background task, a scheduler,
  a thread the request started) is counted with the test whose window it runs
  in. A test whose new lines keep coming for 0.1 s after its END is reported
  as cut short (`windowCut`) and its lines are not used for dedup.

### Step 3: Run with Keploy

Now, you can run your application tests using the Keploy CLI. The agent will automatically connect with Keploy.

```bash
sudo -E keploy-enterprise test -c "python3 app.py" --language python --dedup
```

Now you will see `dedupData.yaml` getting created. 

Run `sudo -E keploy-enterprise dedup` to get the tests which are duplicate in `duplicates.yaml` file

In order to remove the duplicate tests, run the following command:

```bash
sudo -E keploy-enterprise dedup --rm
```
