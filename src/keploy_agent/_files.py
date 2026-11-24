"""Which files are the app's own: the files keploy measures and lists.

One rule serves both the build table (a walk of the app root) and the
per-test records (a code object's file), so every file a test ran is a unit
of the table, under the same app-root-relative path.

An app file is a ``.py`` file under the app root (KEPLOY_APP_SOURCE_DIR,
else the working directory when the agent starts), outside:
  - the interpreter's own files and its libraries, by path: ``sys.prefix``,
    ``sys.base_prefix``, ``sys.exec_prefix``, the ``sysconfig`` stdlib and
    library dirs, ``site.getsitepackages()`` and the user site dir;
  - virtual environments (any directory holding ``pyvenv.cfg``, or
    ``conda-meta`` for a conda env), library
    directories (any directory holding a ``*.dist-info``: ``pip install
    --target``, ``__pypackages__``; an ``*.egg-info`` is an editable
    install's, beside the app's own source), ``site-packages``,
    ``dist-packages``, ``__pycache__``, ``node_modules`` and hidden
    directories (a name starting with ``.``);
  - test directories (``tests``, ``test``) and test files (``test_*.py``,
    ``*_test.py``, ``conftest.py``);
  - the agent's own directory;
  - any glob of KEPLOY_COVERAGE_EXCLUDE (comma separated, matched with
    fnmatch against the app-root-relative path, ``*`` crossing ``/``).

A root that is the filesystem's root or a system directory itself is
refused (refused_root): it is no app's source dir, and its walk would read
the whole machine.
"""

from __future__ import annotations

import fnmatch
import os
import site
import sys
import sysconfig

# Directory names never searched or measured.
SKIP_DIRS = frozenset({
    "__pycache__", "site-packages", "dist-packages", "node_modules",
    "tests", "test", "__pypackages__",
})

# A walk that finds more app files than this, or visits more entries, is not
# an app root: the table is refused rather than read for minutes.
MAX_FILES = 20000
MAX_ENTRIES = 300000

# A file name cache past this many names starts over (generated code makes
# names without end).
MAX_CACHE = 100000

# Roots no app's source lives in directly: the filesystem's root and the
# system's own dirs, each by itself (an app may live below one: /opt/app).
SYSTEM_DIRS = frozenset({
    "/", "/bin", "/boot", "/dev", "/etc", "/home", "/lib", "/lib32", "/lib64",
    "/libx32", "/opt", "/proc", "/run", "/sbin", "/sys", "/usr", "/usr/lib",
    "/usr/local", "/var", "/Users", "/System", "/Library", "/private",
})


def refused_root(root: str, explicit: bool = False) -> str:
    """Why root is not an app root to measure, or "" when it is. A root the
    app names itself (KEPLOY_APP_SOURCE_DIR) is refused only when it is /. A
    home dir is a root like any other (s2i, Heroku and many images run the
    app from it): the walk's bounds and the library rules keep it safe."""
    r = os.path.realpath(root)
    if r == os.path.dirname(r) or (not explicit and r in SYSTEM_DIRS):
        return (f"the app root is {r}, a system directory: run the app from its "
                "source dir, or set KEPLOY_APP_SOURCE_DIR to it")
    return ""


def library_dirs() -> list[str]:
    """The interpreter's own dirs and its library dirs, as real paths."""
    dirs = {sys.prefix, sys.base_prefix, sys.exec_prefix,
            getattr(sys, "base_exec_prefix", sys.exec_prefix)}
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        try:
            dirs.add(sysconfig.get_paths()[key])
        except (KeyError, OSError):
            pass
    try:
        dirs.update(site.getsitepackages())
    except AttributeError:   # an old virtualenv's site module
        pass
    user = getattr(site, "USER_SITE", None)
    if user:
        dirs.add(user)
    return sorted({os.path.realpath(d) for d in dirs if d})


def excludes_from_env(value: str | None) -> tuple[str, ...]:
    """The globs of KEPLOY_COVERAGE_EXCLUDE: comma separated, blanks dropped."""
    if not value:
        return ()
    return tuple(g.strip() for g in value.split(",") if g.strip())


def _test_file(name: str) -> bool:
    return (name == "conftest.py" or name.startswith("test_")
            or name.endswith("_test.py"))


def _skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".")


def _under(p: str, d: str) -> bool:
    return p == d or p.startswith(d if d.endswith(os.sep) else d + os.sep)


class AppFiles:
    """The app's file rule for one app root, with a cache per file name."""

    def __init__(self, root: str, agent_dir: str,
                 excludes: tuple[str, ...] = (), libs: list[str] | None = None,
                 explicit: bool = False):
        self.root = os.path.realpath(root)
        self.explicit = explicit
        self.prefix = self.root if self.root.endswith(os.sep) else self.root + os.sep
        self.agent_dir = os.path.realpath(agent_dir)
        self.excludes = excludes
        # A library dir that holds the root (a venv's prefix above an app
        # inside it, which no real layout has) excludes nothing here.
        self.libs = [d for d in (library_dirs() if libs is None else libs)
                     if not _under(self.root, d)]
        # A relative co_filename is relative to the working directory the
        # process started in (the agent starts with it), not to the root.
        self.cwd = os.getcwd()
        self._lib: dict[str, bool] = {}
        self._rel: dict[str, str | None] = {}

    def _library_dir(self, d: str) -> bool:
        """Whether directory d (absolute, real, under root) is a venv or a
        library dir, or inside one below the root."""
        hit = self._lib.get(d)
        if hit is not None:
            return hit
        if d == self.root or not d.startswith(self.prefix):
            hit = False
        else:
            hit = self._holds_library(d) or self._library_dir(os.path.dirname(d))
        self._lib[d] = hit
        return hit

    @staticmethod
    def _holds_library(d: str) -> bool:
        if (os.path.isfile(os.path.join(d, "pyvenv.cfg"))
                or os.path.isdir(os.path.join(d, "conda-meta"))):
            return True
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.endswith(".dist-info") and e.is_dir():
                        return True
        except OSError:
            pass
        return False

    def rel(self, filename: str) -> str | None:
        """The app-root-relative slash path of filename (a code object's
        co_filename, or a path the walk found), or None when it is not an
        app file. Cached by the name as given."""
        try:
            return self._rel[filename]
        except KeyError:
            pass
        r = self._compute(filename)
        if len(self._rel) >= MAX_CACHE:
            self._rel = {}
        self._rel[filename] = r
        return r

    def _compute(self, filename: str) -> str | None:
        if not filename or filename.startswith("<") or not filename.endswith(".py"):
            return None
        try:
            p = os.path.realpath(os.path.join(self.cwd, filename))
        except (OSError, ValueError):
            return None
        if not p.startswith(self.prefix):
            return None
        if _under(p, self.agent_dir) or any(_under(p, d) for d in self.libs):
            return None
        rel = p[len(self.prefix):].replace(os.sep, "/")
        try:
            rel.encode("utf-8")
        except UnicodeEncodeError:
            return None  # a name that is not UTF-8 cannot key a record
        parts = rel.split("/")
        if any(_skip_dir(d) for d in parts[:-1]) or _test_file(parts[-1]):
            return None
        if self._library_dir(os.path.dirname(p)):
            return None
        if self.excluded(rel):
            return None
        return rel

    def excluded(self, rel: str) -> bool:
        """Whether an exclude glob takes rel (a file or a dir), or a dir it
        is in: one rule for the walk's pruning and for rel()."""
        if not self.excludes:
            return False
        parts = rel.split("/")
        for i in range(1, len(parts) + 1):
            p = "/".join(parts[:i])
            for g in self.excludes:
                if fnmatch.fnmatchcase(p, g) or (i < len(parts) and fnmatch.fnmatchcase(p + "/", g)):
                    return True
        return False

    def _dir_excluded(self, reldir: str) -> bool:
        # The checks excluded() makes of a file's ancestors, for this dir:
        # true exactly when every file below it is excluded.
        return self.excluded(reldir + "/")

    def walk(self) -> list[str]:
        """Every app file under the root, as sorted relative paths. Raises
        ValueError for a refused root, and past MAX_FILES app files or
        MAX_ENTRIES entries visited."""
        why = refused_root(self.root, self.explicit)
        if why:
            raise ValueError(why)
        out = []
        seen = 0
        for d, dirs, files in os.walk(self.root):
            seen += len(dirs) + len(files)
            if seen > MAX_ENTRIES:
                raise ValueError(
                    f"more than {MAX_ENTRIES} entries under {self.root}: "
                    "set KEPLOY_APP_SOURCE_DIR to the app's own source dir")
            real = os.path.realpath(d)
            reldir = "" if real == self.root else real[len(self.prefix):].replace(os.sep, "/") + "/"
            dirs[:] = sorted(
                x for x in dirs if not _skip_dir(x)
                and not self._holds_library(os.path.join(d, x))
                and not any(_under(os.path.realpath(os.path.join(real, x)), lib) for lib in self.libs)
                # A dir an exclude glob takes (media, media/*, */migrations)
                # is not walked, as rel() takes none of its files.
                and not self._dir_excluded(reldir + x))
            for f in files:
                if not f.endswith(".py"):
                    continue
                r = self.rel(os.path.join(d, f))
                if r is not None:
                    out.append(r)
                    if len(out) > MAX_FILES:
                        raise ValueError(
                            f"more than {MAX_FILES} Python files under {self.root}: "
                            "set KEPLOY_APP_SOURCE_DIR to the app's own source dir")
        # A file reached twice (a symlinked dir) is listed once.
        return sorted(set(out))
