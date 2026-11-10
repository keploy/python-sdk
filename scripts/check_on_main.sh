#!/usr/bin/env bash
# check_on_main.sh: fail unless the checked-out commit is on origin/main.
#
# main.yml runs this first on a GitHub release, in a checkout with the full
# history. A release can be created on any commit: a pull request's branch,
# or an old commit whose pyproject.toml names the same version. PyPI keeps
# what is uploaded under a version for good, so only a commit that main
# holds is built. keploy-agent.yml's package job runs it on throwaway
# repositories, so a pull request checks it too.
#
# Works in the repository of the current directory. Any failure to tell (no
# origin/main fetched, not a repository) is refused the same way, in one
# line.
set -euo pipefail

if ! git merge-base --is-ancestor HEAD origin/main 2>/dev/null; then
  # ::error:: makes the line an annotation on the run's summary page.
  echo "::error::the released commit $(git rev-parse --short HEAD 2>/dev/null || echo '(unknown)') is not on origin/main: keploy-agent is published only from a commit main holds, so nothing was built or uploaded"
  exit 1
fi
echo "the released commit $(git rev-parse --short HEAD) is on origin/main"
