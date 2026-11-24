"""python -m keploy_agent --pythonpath

Prints the PYTHONPATH that starts the Keploy agent in every Python process
of a command, with no change to the app: the agent's sitecustomize directory
first, then the PYTHONPATH already set, kept as it is. Use it as

    PYTHONPATH="$(python -m keploy_agent --pythonpath)" uvicorn app.main:app

(this prints a path only: it does not start the agent).
"""

import os
import sys


def pythonpath(current: str) -> str:
    boot = os.path.join(os.path.dirname(os.path.realpath(__file__)), "_boot")
    # Empty entries are kept: in PYTHONPATH one names the working directory.
    parts = current.split(os.pathsep) if current else []
    if boot in parts:
        return current
    return os.pathsep.join([boot] + parts)


def main(argv):
    if argv == ["--pythonpath"]:
        print(pythonpath(os.environ.get("PYTHONPATH", "")))
        return 0
    sys.stderr.write(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
