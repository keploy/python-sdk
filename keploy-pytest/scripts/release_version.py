"""release_version.py TAG DIST: the publish workflow's version check.

TAG is the pushed git tag; DIST is the folder `python -m build` wrote. DIST
must hold exactly one wheel and one sdist of keploy-pytest, both of one
version, and TAG must name that version exactly (keploy-pytest-v<version>), so
the version on PyPI is always the one a tagged commit built. The version must
be a release (x.y.z): this workflow publishes releases only.

The version is read from the wheel's own METADATA, the file PyPI reads, not
from pyproject.toml.

Exit 0 with no output when the tag may publish; exit 1 and say why when not.
"""
import email.parser
import os
import re
import sys
import zipfile

PROJECT = "keploy-pytest"
TAG_PREFIX = "keploy-pytest-v"
# The file names `python -m build` writes for this project: the wheel's name
# part is the project name with "-" as "_" (PEP 427), and so is the sdist's
# (PEP 625).
WHEEL = re.compile(r"^keploy_pytest-([^-]+)-py3-none-any\.whl$")
SDIST = re.compile(r"^keploy_pytest-(.+)\.tar\.gz$")


def refusal(tag, version):
    """Why TAG may not publish VERSION, or "" when it may."""
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        return f"the version {version} is not a release (x.y.z); this workflow publishes releases only"
    want = TAG_PREFIX + version
    if tag != want:
        return f"the tag {tag or '(none)'} does not name the built version {version}: the tag for it is {want}"
    return ""


def wheel_metadata(path):
    """The (Name, Version) the wheel at PATH declares in its METADATA: each
    header once, or ValueError. METADATA is an email-style header block, read
    the way pip and PyPI read it (the description after it is not headers)."""
    with zipfile.ZipFile(path) as z:
        names = [n for n in z.namelist() if re.fullmatch(r"[^/]+\.dist-info/METADATA", n)]
        if len(names) != 1:
            raise ValueError(f"{os.path.basename(path)} holds {len(names)} METADATA files, not one")
        headers = email.parser.HeaderParser().parsestr(z.read(names[0]).decode("utf-8"))
    fields = []
    for key in ("Name", "Version"):
        values = headers.get_all(key) or []
        if len(values) > 1:
            raise ValueError(f"it has {len(values)} {key} headers, not one")
        fields.append(values[0] if values else "")
    return tuple(fields)


def dist_refusal(tag, dist):
    """Why the files in DIST may not publish under TAG, or "" when they may."""
    try:
        files = sorted(os.listdir(dist))
    except OSError as e:
        return f"cannot list {dist}: {e.strerror}"
    wheels = [f for f in files if f.endswith(".whl")]
    sdists = [f for f in files if f.endswith(".tar.gz")]
    other = [f for f in files if f not in wheels and f not in sdists]
    if len(wheels) != 1 or len(sdists) != 1 or other:
        return f"{dist} must hold one wheel and one sdist of {PROJECT}, and holds {', '.join(files) or 'nothing'}"
    w, s = WHEEL.match(wheels[0]), SDIST.match(sdists[0])
    if not w or not s:
        return f"{', '.join(wheels + sdists)} are not the file names {PROJECT}'s build writes"
    if w.group(1) != s.group(1):
        return f"the wheel {wheels[0]} and the sdist {sdists[0]} name different versions"
    try:
        name, version = wheel_metadata(os.path.join(dist, wheels[0]))
    except (OSError, ValueError, zipfile.BadZipFile, UnicodeDecodeError) as e:
        return f"cannot read {wheels[0]}'s METADATA: {e}"
    if name != PROJECT:
        return f"{wheels[0]}'s METADATA names the project {name or '(none)'}, not {PROJECT}"
    if version != w.group(1):
        return f"{wheels[0]}'s METADATA names the version {version or '(none)'}, not the file name's {w.group(1)}"
    return refusal(tag, version)


def main(argv):
    if len(argv) != 3:
        print("usage: release_version.py TAG DIST", file=sys.stderr)
        return 2
    why = dist_refusal(argv[1], argv[2])
    if why:
        print(f"refusing to publish: {why}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
