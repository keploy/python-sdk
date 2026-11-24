"""Starts the Keploy agent in every Python process whose PYTHONPATH holds
this directory (`PYTHONPATH=$(python -m keploy_agent --pythonpath)`), with no
change to the app: Python imports sitecustomize at startup.

The agent never breaks the app: an error starting it is said on stderr and
the process goes on unmeasured. Then the next sitecustomize on sys.path (the
app's or its environment's own) runs, as it would have without this one.
"""

import os
import sys


def _start_agent():
    sys._keploy_agent_boot = True
    try:
        import keploy_agent  # noqa: F401
    except Exception as e:  # never stop the app
        sys.stderr.write(f"[Keploy Agent] could not start: {e!r}\n")
    finally:
        try:
            del sys._keploy_agent_boot
        except AttributeError:
            pass


def _chain():
    """Run the next sitecustomize on sys.path, as `import sitecustomize`
    would have found it without this directory."""
    import importlib.machinery
    import importlib.util

    here = os.path.dirname(os.path.realpath(__file__))
    rest = [p for p in sys.path if os.path.realpath(p or os.curdir) != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
    if spec is None or spec.loader is None:
        return
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sitecustomize"] = mod
    spec.loader.exec_module(mod)


_start_agent()
_chain()
