# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Installed dependency closure and private staging, without launching workers."""

from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest
from packaging.utils import canonicalize_name

from nooa.runtime.sandbox import _lpac_runtime as staging

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows staging paths")


@pytest.fixture
def distributions(tmp_path, monkeypatch):
    installed = {}

    def add(name, *, version="1.0", requires=(), extras=(), files=None, editable=False):
        base = tmp_path / name
        base.mkdir()
        info = Message()
        info["Name"] = name
        for extra in extras:
            info["Provides-Extra"] = extra
        paths = files if files is not None else {f"{name}/__init__.py": ""}
        for relative, data in paths.items():
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(data, encoding="utf-8")
        dist = SimpleNamespace(
            metadata=info,
            version=version,
            requires=requires,
            files=list(paths),
            locate_file=lambda relative: base / relative,
            read_text=lambda name: (
                json.dumps({"dir_info": {"editable": True}})
                if name == "direct_url.json" and editable
                else None
            ),
        )
        installed[canonicalize_name(name)] = dist
        return dist

    def find(name):
        try:
            return installed[canonicalize_name(name)]
        except KeyError:
            raise staging.metadata.PackageNotFoundError(name) from None

    monkeypatch.setattr(staging.metadata, "distribution", find)
    add("nooa")
    return add


def test_requirements_include_transitive_extras_markers_and_cycles(distributions):
    distributions(
        "example",
        extras=("feature",),
        requires=("child>=1; extra == 'feature'", "absent; sys_platform != 'win32'"),
    )
    distributions("child", requires=("example",))
    result = staging._dependency_closure(("example[feature]>=1",))
    assert [dist.metadata["Name"] for dist in result] == ["nooa", "example", "child"]


@pytest.mark.parametrize(
    ("requirements", "match"),
    [
        (("example>=2",), "does not satisfy"),
        (("example", "example>=2"), "does not satisfy"),
        (("example[missing]",), "does not provide extras"),
        (("example @ https://invalid.example/pkg.whl",), "not URLs"),
        (("example @ file:///C:/private/pkg.whl",), "not URLs"),
    ],
)
def test_invalid_requirements_fail_before_copy(distributions, tmp_path, requirements, match):
    distributions("example")
    destination = tmp_path / "packages"
    with pytest.raises(ValueError, match=match):
        staging._stage_packages(destination, requirements)
    assert not destination.exists()


def test_missing_dependency_and_string_collection_are_rejected(distributions):
    with pytest.raises(staging.metadata.PackageNotFoundError):
        staging._dependency_closure(("missing",))
    with pytest.raises(TypeError, match="iterable"):
        staging._dependency_closure("nooa")


def test_transitive_constraints_checked_even_after_distribution_was_visited(distributions):
    distributions("example", requires=("child>=2",))
    distributions("child")
    with pytest.raises(ValueError, match="does not satisfy"):
        staging._dependency_closure(("child", "example"))


def test_later_extras_expand_an_already_visited_distribution(distributions):
    distributions("example", extras=("feature",), requires=("child; extra == 'feature'",))
    distributions("child")
    result = staging._dependency_closure(("example", "example[feature]"))
    assert [dist.metadata["Name"] for dist in result] == ["nooa", "example", "child"]


def test_staging_includes_resources_but_not_local_metadata(distributions, tmp_path):
    distributions(
        "example",
        files={
            "example/__init__.py": "",
            "example/data.txt": "public resource",
            "example/.env": "synthetic-secret",
            "example/__pycache__/cached.pyc": "not bytecode",
            "example-1.dist-info/direct_url.json": "private location",
            "example-1.dist-info/RECORD": "host manifest",
            "example-1.dist-info/METADATA": "Name: example",
        },
    )
    destination = tmp_path / "packages"
    staging._stage_packages(destination, ("example",))
    assert sorted(
        str(path.relative_to(destination)).replace("\\", "/")
        for path in destination.rglob("*")
        if path.is_file()
    ) == [
        "example-1.dist-info/METADATA",
        "example/__init__.py",
        "example/data.txt",
        "nooa/__init__.py",
    ]


def test_staging_supports_dependency_paths_beyond_max_path(distributions, tmp_path):
    destination = tmp_path / ("runtime-" + "a" * 32) / "payload" / "runtime" / "packages"
    parent = Path("example") / ("nested" * 8)
    filename = "r" * max(64, 270 - len(str(destination / parent))) + ".txt"
    relative = parent / filename
    distributions("example", files={str(relative): "long-path resource"})
    destination.parent.mkdir(parents=True)
    target = destination / relative
    assert len(str(target)) > 260
    staging._stage_packages(destination, ("example",))
    assert Path("\\\\?\\" + str(target)).read_text(encoding="utf-8") == "long-path resource"


@pytest.mark.parametrize("suffix", [".pth", ".PTH", ".egg-link"])
def test_third_party_install_hooks_are_refused(distributions, tmp_path, suffix):
    distributions("example", files={"example/__init__.py": "", f"hook{suffix}": "import os"})
    with pytest.raises(ValueError, match="install hook"):
        staging._stage_packages(tmp_path / "packages", ("example",))


def test_existing_core_install_hooks_remain_excluded(distributions, tmp_path):
    distributions(
        "setuptools", files={"setuptools/__init__.py": "", "distutils-precedence.pth": "import os"}
    )
    staging.metadata.distribution("nooa").requires = ("setuptools",)
    destination = tmp_path / "packages"
    staging._stage_packages(destination)
    assert (destination / "setuptools/__init__.py").exists()
    assert not list(destination.rglob("*.pth"))


def test_editable_dependency_is_refused_even_with_manifested_source(distributions, tmp_path):
    distributions("example", editable=True)
    with pytest.raises(ValueError, match="editable"):
        staging._stage_packages(tmp_path / "packages", ("example",))


@pytest.mark.parametrize("path", ["os.py", "JSON/__init__.py", "nooa/extra.py"])
def test_third_party_cannot_shadow_runtime(distributions, tmp_path, path):
    distributions("example", files={path: ""})
    with pytest.raises(ValueError, match="shadows"):
        staging._stage_packages(tmp_path / "packages", ("example",))


def test_distribution_file_overlap_is_refused(distributions, tmp_path):
    distributions("first", files={"shared/__init__.py": "one"})
    distributions("second", files={"SHARED/__init__.py": "two"})
    with pytest.raises(ValueError, match="overlap"):
        staging._stage_packages(tmp_path / "packages", ("first", "second"))


@pytest.mark.parametrize("second", ["shared/__init__.py", "shared.cp312-win_amd64.pyd"])
def test_distribution_import_shadowing_is_refused(distributions, tmp_path, second):
    distributions("first", files={"shared.py": "one"})
    distributions("second", files={second: "two"})
    with pytest.raises(ValueError, match="overlap"):
        staging._stage_packages(tmp_path / "packages", ("first", "second"))


def test_application_package_cannot_shadow_installed_single_file_module(tmp_path):
    destination = tmp_path / "packages"
    destination.mkdir()
    (destination / "example.py").write_text("", encoding="utf-8")
    init = tmp_path / "__init__.py"
    init.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="shadows"):
        staging._stage_applications(destination, {"example": init})


def test_failed_staging_does_not_mark_runtime_ready(distributions, tmp_path):
    import threading

    runtime = SimpleNamespace(
        _lock=threading.RLock(),
        _closed=False,
        _profile=object(),
        _framework_staged=False,
        root=tmp_path.resolve(),
        runtime=tmp_path.resolve() / "runtime",
    )
    runtime.runtime.mkdir()
    distributions("example", editable=True)
    with pytest.raises(ValueError, match="editable"):
        staging.stage_framework(runtime, application_requirements=("example",))
    assert runtime._framework_staged is False


def test_invalid_manifest_source_is_refused(distributions, tmp_path):
    dist = distributions("example")
    Path(dist.locate_file("example/__init__.py")).unlink()
    with pytest.raises(ValueError, match="invalid source"):
        staging._stage_packages(tmp_path / "packages", ("example",))


def test_package_copies_are_parallel_bounded_and_complete(distributions, tmp_path, monkeypatch):
    distributions("example", files={f"example/file{i}.txt": str(i) for i in range(64)})
    destination = tmp_path / "packages"
    release = threading.Event()
    workers_started = threading.Event()
    window_full = threading.Event()
    lock = threading.Lock()
    active = peak = checked = 0
    validators = set()
    writers = set()
    original_copy = staging.shutil.copyfile
    original_check = staging._check_source

    def check(source):
        nonlocal checked
        original_check(source)
        validators.add(threading.get_ident())
        checked += 1
        if checked == 33:
            window_full.set()

    def copy(source, target):
        nonlocal active, peak
        with lock:
            writers.add(threading.get_ident())
            active += 1
            peak = max(peak, active)
            if active == 4:
                workers_started.set()
        try:
            assert release.wait(10), "copy gate was not released"
            return original_copy(source, target)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(staging, "_check_source", check)
    monkeypatch.setattr(staging.shutil, "copyfile", copy)
    with ThreadPoolExecutor(max_workers=1) as caller:
        result = caller.submit(staging._stage_packages, destination, ("example",))
        try:
            assert workers_started.wait(5), "package copying did not overlap"
            assert window_full.wait(5), "copy window did not fill"
            assert checked == 33
            assert not result.done()
        finally:
            release.set()
        result.result(timeout=10)
    assert active == 0 and peak == 4
    assert len(validators) == 1 and validators.isdisjoint(writers)
    assert (destination / "nooa/__init__.py").is_file()
    for i in range(64):
        assert (destination / f"example/file{i}.txt").read_text(encoding="utf-8") == str(i)


@pytest.fixture
def framework_runtime(distributions, tmp_path):
    runtime = SimpleNamespace(
        _lock=threading.RLock(),
        _closed=False,
        _profile=object(),
        _framework_staged=False,
        root=tmp_path.resolve(),
        runtime=tmp_path.resolve() / "runtime",
    )
    runtime.runtime.joinpath("Lib/asyncio").mkdir(parents=True)
    dist = staging.metadata.distribution("nooa")
    shim = "nooa/runtime/sandbox/_lpac_asyncio.py"
    source = Path(dist.locate_file(shim))
    source.parent.mkdir(parents=True)
    source.write_text("# staged event loop\n", encoding="utf-8")
    dist.files.append(shim)
    return runtime


@pytest.mark.parametrize(
    "outcome", ["success", "copy-error", "queue-error", "invalid-source", "overlap"]
)
def test_framework_waits_for_copies_on_success_and_failure(
    distributions, framework_runtime, tmp_path, monkeypatch, outcome
):
    files = {"example/slow.txt": "finished"}
    if outcome == "queue-error":
        files.update({f"example/file{i}.txt": str(i) for i in range(64)})
    distributions("example", files=files)
    requirements = ["example"]
    expected = None
    if outcome == "invalid-source":
        dist = distributions("invalid")
        Path(dist.locate_file("invalid/__init__.py")).unlink()
        requirements.append("invalid")
        expected = "invalid source"
    elif outcome == "overlap":
        distributions("collision", files={"EXAMPLE/slow.txt": "not admitted"})
        requirements.append("collision")
        expected = "overlap"
    elif outcome in ("copy-error", "queue-error"):
        expected = "copy failed"
    app = tmp_path / "application.py"
    app.write_text("VALUE = 1\n", encoding="utf-8")
    release = threading.Event()
    started = threading.Event()
    finished = threading.Event()
    error_seen = threading.Event()
    joining = threading.Event()
    original_copy = staging.shutil.copyfile
    original_check = staging._check_source
    original_stage_apps = staging._stage_applications
    phases = []

    class JoiningExecutor(ThreadPoolExecutor):
        def __exit__(self, *args):
            joining.set()
            return super().__exit__(*args)

    def check(source):
        if source.name == "slow.txt" and outcome in ("copy-error", "queue-error"):
            assert error_seen.wait(5), "failed copy did not run"
        original_check(source)

    def copy(source, target):
        if Path(source).name == "__init__.py" and outcome in ("copy-error", "queue-error"):
            error_seen.set()
            raise OSError("copy failed")
        if Path(source).name == "slow.txt":
            started.set()
            assert release.wait(10), "copy gate was not released"
            original_copy(source, target)
            finished.set()
            return target
        if Path(target).name == "windows_events.py":
            assert finished.is_set()
            assert not framework_runtime._framework_staged
            phases.append("shim")
        return original_copy(source, target)

    def applications(destination, modules):
        assert finished.is_set()
        assert not framework_runtime._framework_staged
        phases.append("applications")
        original_stage_apps(destination, modules)

    monkeypatch.setattr(staging, "_check_source", check)
    monkeypatch.setattr(staging, "ThreadPoolExecutor", JoiningExecutor)
    monkeypatch.setattr(staging.shutil, "copyfile", copy)
    monkeypatch.setattr(staging, "_stage_applications", applications)
    with ThreadPoolExecutor(max_workers=1) as caller:
        result = caller.submit(
            staging.stage_framework,
            framework_runtime,
            application_modules={"application": app},
            application_requirements=requirements,
        )
        try:
            assert started.wait(5), "pending copy did not start"
            if expected is not None:
                assert joining.wait(5), "failed staging did not join its pending copies"
            assert not result.done()
            assert not framework_runtime._framework_staged
            assert phases == []
        finally:
            release.set()
        if expected is None:
            result.result(timeout=10)
        else:
            with pytest.raises((ValueError, OSError), match=expected):
                result.result(timeout=10)
    assert finished.is_set()
    assert framework_runtime._framework_staged is (expected is None)
    assert phases == (["applications", "shim"] if expected is None else [])
