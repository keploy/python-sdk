"""Rows for the publish workflow's version check (scripts/release_version.py)."""
import importlib.util
import os
import subprocess
import sys
import zipfile

import pytest

SCRIPT = os.path.join(os.path.dirname(__file__), os.pardir, "scripts", "release_version.py")
_spec = importlib.util.spec_from_file_location("release_version", SCRIPT)
rv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rv)


def wheel(dist, version, name="keploy-pytest", meta_version=None, file_version=None, metadata=None):
    """A wheel holding only a METADATA file, as the check reads it."""
    file_version = file_version or version
    path = os.path.join(dist, f"keploy_pytest-{file_version}-py3-none-any.whl")
    if metadata is None:
        metadata = (
            "Metadata-Version: 2.4\n"
            f"Name: {name}\n"
            f"Version: {meta_version or version}\n"
            "Summary: s\n"
            "\n"
            "Version: 9.9.9\n"  # the description, after the headers, is not read
        )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"keploy_pytest-{file_version}.dist-info/METADATA", metadata)
    return path


def sdist(dist, version):
    path = os.path.join(dist, f"keploy_pytest-{version}.tar.gz")
    open(path, "wb").close()
    return path


def built(tmp_path, version="1.2.3"):
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel(str(dist), version)
    sdist(str(dist), version)
    return str(dist)


def run(*args):
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True)


def test_a_tag_naming_the_version_publishes():
    assert rv.refusal("keploy-pytest-v1.2.3", "1.2.3") == ""


def test_a_tag_naming_another_version_is_refused_and_names_the_right_tag():
    assert rv.refusal("keploy-pytest-v1.2.4", "1.2.3").endswith(
        "does not name the built version 1.2.3: the tag for it is keploy-pytest-v1.2.3"
    )
    assert "does not name" in rv.refusal("keploy-pytest-v1.2.3.1", "1.2.3")
    assert "does not name" in rv.refusal("v1.2.3", "1.2.3")
    assert "does not name" in rv.refusal("jest-v1.2.3", "1.2.3")
    assert "the tag (none) does not name" in rv.refusal("", "1.2.3")


@pytest.mark.parametrize("version", ["1.2.3rc1", "1.2.3.post1", "1.2.3.dev0", "1.2", "1.2.3+local", "1.2.3\n"])
def test_a_version_that_is_not_x_y_z_is_refused_even_with_its_own_tag(version):
    assert "is not a release (x.y.z)" in rv.refusal("keploy-pytest-v" + version, version)


def test_a_build_of_the_tagged_version_publishes(tmp_path):
    assert rv.dist_refusal("keploy-pytest-v1.2.3", built(tmp_path)) == ""


def test_the_version_is_read_from_the_wheels_metadata(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel(str(dist), "1.2.3", meta_version="1.2.4")
    sdist(str(dist), "1.2.3")
    assert rv.dist_refusal("keploy-pytest-v1.2.3", str(dist)) == (
        "keploy_pytest-1.2.3-py3-none-any.whl's METADATA names the version 1.2.4, not the file name's 1.2.3"
    )


def test_a_wheel_of_another_project_is_refused(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel(str(dist), "1.2.3", name="keploy-agent")
    sdist(str(dist), "1.2.3")
    assert rv.dist_refusal("keploy-pytest-v1.2.3", str(dist)) == (
        "keploy_pytest-1.2.3-py3-none-any.whl's METADATA names the project keploy-agent, not keploy-pytest"
    )


def test_metadata_with_no_name_or_version_is_refused(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel(str(dist), "1.2.3", metadata="Metadata-Version: 2.4\nName: keploy-pytest\n")
    sdist(str(dist), "1.2.3")
    assert "names the version (none)" in rv.dist_refusal("keploy-pytest-v1.2.3", str(dist))


def test_a_version_in_the_description_is_not_read_as_a_header(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel(str(dist), "1.2.3", metadata="Metadata-Version: 2.4\nName: keploy-pytest\n\nVersion: 1.2.3\n")
    sdist(str(dist), "1.2.3")
    assert "names the version (none)" in rv.dist_refusal("keploy-pytest-v1.2.3", str(dist))


@pytest.mark.parametrize("key", ["Name", "Version"])
def test_metadata_with_a_header_twice_is_refused(tmp_path, key):
    dist = tmp_path / "dist"
    dist.mkdir()
    headers = {"Name": "keploy-pytest", "Version": "1.2.3"}
    metadata = "Metadata-Version: 2.4\n" + "".join(f"{k}: {v}\n" for k, v in headers.items()) + f"{key}: {headers[key]}\n"
    wheel(str(dist), "1.2.3", metadata=metadata)
    sdist(str(dist), "1.2.3")
    assert rv.dist_refusal("keploy-pytest-v1.2.3", str(dist)) == (
        f"cannot read keploy_pytest-1.2.3-py3-none-any.whl's METADATA: it has 2 {key} headers, not one"
    )


def test_a_wheel_without_one_metadata_file_is_refused(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    with zipfile.ZipFile(dist / "keploy_pytest-1.2.3-py3-none-any.whl", "w") as z:
        z.writestr("keploy_pytest/__init__.py", "")
    sdist(str(dist), "1.2.3")
    assert "holds 0 METADATA files, not one" in rv.dist_refusal("keploy-pytest-v1.2.3", str(dist))


def test_a_wheel_with_two_metadata_files_is_refused(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    path = wheel(str(dist), "1.2.3")
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("keploy_agent-1.2.3.dist-info/METADATA", "Name: keploy-agent\nVersion: 1.2.3\n")
    sdist(str(dist), "1.2.3")
    assert "holds 2 METADATA files, not one" in rv.dist_refusal("keploy-pytest-v1.2.3", str(dist))


def test_a_metadata_file_inside_a_package_is_not_the_wheels(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    path = wheel(str(dist), "1.2.3")
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("keploy_pytest/_vendor/other-1.0.dist-info/METADATA", "Name: other\nVersion: 1.0\n")
    sdist(str(dist), "1.2.3")
    assert rv.dist_refusal("keploy-pytest-v1.2.3", str(dist)) == ""


def test_a_wheel_that_is_not_a_zip_is_refused(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "keploy_pytest-1.2.3-py3-none-any.whl").write_bytes(b"not a zip")
    sdist(str(dist), "1.2.3")
    assert "cannot read keploy_pytest-1.2.3-py3-none-any.whl's METADATA" in rv.dist_refusal(
        "keploy-pytest-v1.2.3", str(dist)
    )


@pytest.mark.parametrize(
    "files",
    [
        [],
        ["w"],
        ["s"],
        ["w", "s", "other.txt"],
        ["w", "s", "w2"],
        ["w", "s", "s2"],
    ],
)
def test_dist_must_hold_one_wheel_and_one_sdist_and_nothing_else(tmp_path, files):
    dist = tmp_path / "dist"
    dist.mkdir()
    for f in files:
        if f == "w":
            wheel(str(dist), "1.2.3")
        elif f == "w2":
            wheel(str(dist), "1.2.4")
        elif f == "s":
            sdist(str(dist), "1.2.3")
        elif f == "s2":
            sdist(str(dist), "1.2.4")
        else:
            (dist / f).write_text("")
    assert "must hold one wheel and one sdist of keploy-pytest" in rv.dist_refusal("keploy-pytest-v1.2.3", str(dist))


def test_file_names_another_build_writes_are_refused(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "keploy_agent-1.2.3-py3-none-any.whl").write_bytes(b"")
    sdist(str(dist), "1.2.3")
    assert "are not the file names keploy-pytest's build writes" in rv.dist_refusal("keploy-pytest-v1.2.3", str(dist))
    (dist / "keploy_agent-1.2.3-py3-none-any.whl").unlink()
    # The project's name as written, not as PEP 427 spells it in a wheel's name.
    wheel(str(dist), "1.2.3")
    (dist / "keploy_pytest-1.2.3-py3-none-any.whl").rename(dist / "keploy-pytest-1.2.3-py3-none-any.whl")
    assert "are not the file names keploy-pytest's build writes" in rv.dist_refusal("keploy-pytest-v1.2.3", str(dist))
    (dist / "keploy-pytest-1.2.3-py3-none-any.whl").unlink()
    wheel(str(dist), "1.2.3")
    (dist / "keploy_pytest-1.2.3.tar.gz").rename(dist / "keploy-pytest-1.2.3.tar.gz")
    assert "are not the file names keploy-pytest's build writes" in rv.dist_refusal("keploy-pytest-v1.2.3", str(dist))


def test_a_wheel_and_sdist_of_different_versions_are_refused(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    wheel(str(dist), "1.2.3")
    sdist(str(dist), "1.2.4")
    assert rv.dist_refusal("keploy-pytest-v1.2.3", str(dist)) == (
        "the wheel keploy_pytest-1.2.3-py3-none-any.whl and the sdist keploy_pytest-1.2.4.tar.gz name different versions"
    )


def test_a_missing_dist_is_refused(tmp_path):
    assert rv.dist_refusal("keploy-pytest-v1.2.3", str(tmp_path / "none")).startswith(f"cannot list {tmp_path / 'none'}")


def test_the_script_exits_1_with_the_reason_on_a_wrong_tag(tmp_path):
    dist = built(tmp_path)
    ok = run("keploy-pytest-v1.2.3", dist)
    assert (ok.returncode, ok.stdout, ok.stderr) == (0, "", "")
    bad = run("keploy-pytest-v0.0.0", dist)
    assert bad.returncode == 1
    assert bad.stderr == (
        "refusing to publish: the tag keploy-pytest-v0.0.0 does not name the built version 1.2.3: "
        "the tag for it is keploy-pytest-v1.2.3\n"
    )
    usage = run("keploy-pytest-v1.2.3")
    assert usage.returncode == 2
    assert usage.stderr.startswith("usage:")
