"""The lines the app ran since the last window began.

On Python 3.12 and later the collector is sys.monitoring's LINE event, on a
tool id of its own: the callback notes the line when its file is an app file
and returns DISABLE, so each code location costs one call until the next
window's start re-enables them all (restart_events). Every thread, the
event loop and thread pools included, is measured.

Before 3.12 it is sys.settrace (this thread) and threading.settrace (threads
started from now on): a call into an app file gets a line tracer, any other
call none. Threads already running when it starts are not traced.
"""

from __future__ import annotations

import sys
import threading

from ._files import AppFiles


class Unavailable(Exception):
    """This process cannot be measured: why is the message."""


class _Collector:
    name = ""

    def __init__(self, files: AppFiles):
        self.files = files
        self.hits: set[tuple[str, int]] = set()
        # None: no error yet; (file, error): one to say; False: said.
        self._failed = None

    def reset(self) -> None:
        self.hits = set()

    def count(self) -> int:
        return len(self.hits)

    def stop(self) -> None:
        """Stop measuring."""

    def replaced(self) -> str:
        """Why keploy's measuring was replaced by another tool, or ""."""
        return ""

    def failed(self, code, e: Exception) -> None:
        """A callback's error, kept to be said later (take_failure), outside
        the callback: logging here could fail again in the app's frame (at
        the recursion limit) and raise there."""
        if self._failed is None:
            try:
                self._failed = (code.co_filename, type(e).__name__)
            except Exception:
                self._failed = ("?", "?")

    def take_failure(self):
        """The first callback error not said yet, as (file, error), or None."""
        f = self._failed
        if not f:
            return None
        self._failed = False
        return f

    def take(self) -> dict[str, list[int]]:
        """The lines noted since the last reset, by file, sorted."""
        by_file: dict[str, list[int]] = {}
        for f, ln in set(self.hits):
            by_file.setdefault(f, []).append(ln)
        for v in by_file.values():
            v.sort()
        return by_file


class Monitoring(_Collector):
    """sys.monitoring (3.12+)."""

    name = "sys.monitoring"

    def __init__(self, files: AppFiles):
        super().__init__(files)
        mon = sys.monitoring
        self.tool = None
        # 3 and 4 first: coverage.py's sysmon core takes COVERAGE_ID (1)
        # and fails when it is taken, and it may start after this agent.
        for tool in (3, 4, mon.COVERAGE_ID):
            try:
                mon.use_tool_id(tool, "keploy")
            except ValueError:
                continue
            self.tool = tool
            break
        if self.tool is None:
            raise Unavailable("sys.monitoring tool ids 1, 3 and 4 are all in use "
                              "(another coverage tool or profiler runs in the app)")
        rel = files.rel
        disable = mon.DISABLE

        # Line 0 is no line (an empty module's entry): it is in no table.
        # An error here would be raised in the app's own frame: it is
        # swallowed, said once, and the location measured no more.
        def on_line(code, line, _rel=rel, _disable=disable):
            try:
                if line > 0:
                    f = _rel(code.co_filename)
                    if f is not None:
                        self.hits.add((f, line))
            except Exception as e:
                self.failed(code, e)
            return _disable

        mon.register_callback(self.tool, mon.events.LINE, on_line)
        mon.set_events(self.tool, mon.events.LINE)

    def reset(self) -> None:
        self.hits = set()
        # Every location a past window disabled runs its callback once more.
        sys.monitoring.restart_events()

    def stop(self) -> None:
        sys.monitoring.set_events(self.tool, 0)
        sys.monitoring.register_callback(self.tool, sys.monitoring.events.LINE, None)
        sys.monitoring.free_tool_id(self.tool)


class Trace(_Collector):
    """sys.settrace / threading.settrace (before 3.12)."""

    name = "settrace"

    def __init__(self, files: AppFiles):
        super().__init__(files)
        if sys.gettrace() is not None:
            raise Unavailable("another tracer (a debugger or a coverage tool) is "
                              "set in the app, and before Python 3.12 keploy would "
                              "replace it")
        rel = files.rel

        # The local tracer is set only on app frames, so its file is an app
        # file and rel() a cached dict hit. An error in either would be
        # raised in the app's own frame: it is swallowed, said once, and the
        # frame traced no more.
        def local(frame, event, arg):
            try:
                if event == "line" and frame.f_lineno > 0:
                    self.hits.add((rel(frame.f_code.co_filename), frame.f_lineno))
                return local
            except Exception as e:
                self.failed(frame.f_code, e)
                return None

        def call(frame, event, arg, _rel=rel):
            try:
                if event == "call" and _rel(frame.f_code.co_filename) is not None:
                    return local
            except Exception as e:
                self.failed(frame.f_code, e)
            return None

        self._call = call
        sys.settrace(call)
        threading.settrace(call)
        others = [t for t in threading.enumerate()
                  if t is not threading.current_thread() and t.is_alive()]
        self.untraced_threads = len(others)

    def replaced(self) -> str:
        """Why keploy's tracer is no longer the one new threads, or this
        thread (a command's, started after the last threading.settrace),
        get; "" while it is. Before 3.12 a debugger, profiler or coverage
        tool started after the agent replaces it without a word."""
        if sys.gettrace() is not self._call:
            return "another tracer replaced keploy's in the app"
        gt = getattr(threading, "gettrace", None)
        if gt is not None and gt() is not self._call:
            return "another tracer replaced keploy's for the app's threads"
        return ""

    def stop(self) -> None:
        if sys.gettrace() is self._call:
            sys.settrace(None)
        threading.settrace(None)


def start(files: AppFiles) -> _Collector:
    """Start measuring: sys.monitoring on 3.12+, else settrace. Raises
    Unavailable when neither can run here."""
    if sys.version_info >= (3, 12):
        return Monitoring(files)
    return Trace(files)
