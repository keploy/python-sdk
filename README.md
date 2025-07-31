# Keploy Python Coverage Agent

The Keploy Python Coverage Agent is a lightweight, sidecar module designed to integrate with the Keploy integration testing platform (enterprise version). When imported into a Python application, it enables Keploy to capture and report code coverage on a per-test-case basis.

## Installation and Usage

Follow these steps to integrate the coverage agent into your Python project.

### Prerequisites

You must have the `coverage` library installed in your project's Python environment.

```bash
pip install coverage
```

### Step 1: Install the Agent

Install the `keploy-agent` package into your virtual environment. If you are developing the agent, you can install it in an editable mode from its source directory:

```bash
pip install -e /path/to/keploy_agent
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
