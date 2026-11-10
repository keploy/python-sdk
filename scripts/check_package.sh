#!/usr/bin/env bash
# check_package.sh TAG: build keploy-agent and check what would be uploaded.
#
# Both workflows run this file with the same Python: main.yml on a GitHub
# release (TAG is the release's tag), which uploads dist/ only after it
# passes, and keploy-agent.yml on a pull request (TAG is the tag the version
# would get; nothing is uploaded). So the build a release uploads is checked
# the way the pull request's was.
#
# It first refuses any TAG but keploy-agent-v<version>, the version being
# pyproject.toml's, in one line, before anything is built.
#
# Then, with a pinned build and twine:
#   - dist/ holds exactly one wheel and one sdist of that version;
#   - twine check --strict passes (README.md is the PyPI project page);
#   - the sdist carries every tracked file under tests/, so the tests it
#     ships can run;
#   - the wheel installs into a clean venv with no package index, so it
#     needs nothing but Python, and keploy_agent imports from that venv, not
#     from the checkout.
#
# Run from anywhere; it works in the repository root and writes dist/ there.
# Needs Python 3.11 or newer (tomllib) and git.
set -euo pipefail

cd "$(dirname "$0")/.."

fail() {
  # ::error:: makes the line an annotation on the run's summary page.
  echo "::error::$1"
  exit 1
}

version=$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')

tag=${1-}
if [ "$tag" != "keploy-agent-v$version" ]; then
  fail "the release tag ${tag:-(none)} does not name keploy-agent $version: it is published only from the tag keploy-agent-v$version, so nothing was built or uploaded"
fi

python -m pip install --quiet build==1.6.1 twine==7.0.0

# A dist/ left by an earlier build would be uploaded with this one's files.
rm -rf dist
python -m build --outdir dist .

ls dist
wheel="keploy_agent-$version-py3-none-any.whl"
sdist="keploy_agent-$version.tar.gz"
if [ "$(ls dist | sort | tr '\n' ' ')" != "$wheel $sdist " ]; then
  fail "dist/ is not exactly $wheel and $sdist"
fi

python -m twine check --strict dist/*

tar -tzf "dist/$sdist" > dist.files
for f in $(git ls-files tests); do
  if ! grep -qxF "keploy_agent-$version/$f" dist.files; then
    rm -f dist.files
    fail "the sdist ships tests/ without $f"
  fi
done
rm -f dist.files

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
python -m venv "$tmp/venv"
# --no-index: the agent needs nothing but Python, so nothing may be fetched.
"$tmp/venv/bin/python" -m pip install --quiet --no-index "dist/$wheel" \
  || fail "the wheel does not install with no package index: it needs more than Python"
# The agent's control thread is not a daemon, so an import that raises after
# the thread started would never exit: the timeout ends it.
(cd "$tmp" && KEPLOY_AGENT_CHECK_ROOT="$tmp" timeout 60 "$tmp/venv/bin/python" - <<'PY'
import os
import keploy_agent
where = os.path.realpath(keploy_agent.__file__)
print(where, flush=True)
ok = where.startswith(os.path.realpath(os.environ["KEPLOY_AGENT_CHECK_ROOT"]) + os.sep)
# Leave without waiting for the control thread.
os._exit(0 if ok else 1)
PY
) || fail "keploy_agent did not import from the clean venv within 60 s"

echo "keploy-agent $version: the wheel and the sdist pass"
