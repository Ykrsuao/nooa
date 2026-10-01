# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Recovery of newly enrolled LPAC resources, never a scan of arbitrary temp files."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

if sys.platform != "win32":
    pytest.skip("Windows LPAC recovery", allow_module_level=True)

from nooa.runtime.sandbox import _lpac_recovery as recovery  # noqa: E402
from nooa.runtime.sandbox import _win_appcontainer as native  # noqa: E402
from nooa.runtime.sandbox._appcontainer import _AppContainerPython  # noqa: E402


@pytest.fixture
def enrolled(tmp_path):
    store = tmp_path / "ledger"
    lease = recovery._RuntimeLease.create(store)
    profile = native.Profile(name=lease.profile_name)
    lease.record_profile(profile.name)
    try:
        yield store, lease, profile
    finally:
        if lease._fd is not None:
            lease.cleanup(profile.close)
        else:
            profile.close()
            recovery.recover_orphans(store)


def test_live_lease_is_skipped_even_in_the_same_process(enrolled):
    store, lease, _ = enrolled
    result = recovery.recover_orphans(store)
    assert result.active == [lease.root.parent.name]
    assert not result.recovered and not result.errors and not result.unclaimed
    assert lease.root.is_dir()


def test_released_committed_lease_is_recovered_idempotently(enrolled, monkeypatch):
    store, lease, profile = enrolled
    name = lease.root.parent.name
    (lease.root / "data").write_bytes(b"owned")
    lease.release()
    result = recovery.recover_orphans(store)
    assert result.recovered == [name] and not result.errors
    assert not lease.root.parent.exists()
    assert not recovery.recover_orphans(store).recovered
    # The real profile name is reusable only after recovery deleted that profile.
    with monkeypatch.context() as patch:
        patch.setattr(
            native.uuid, "uuid4", lambda: uuid.UUID(profile.name.removeprefix("nooa.lpac."))
        )
        replacement = native.Profile()
        replacement.close()
    profile._created = False


def test_uncommitted_entry_and_unrelated_files_are_preserved(tmp_path):
    store = tmp_path / "ledger"
    lease = recovery._RuntimeLease.create(store)
    unrelated = store / "keep.txt"
    unrelated.write_bytes(b"unrelated")
    entry = lease.root.parent
    lease.release()
    result = recovery.recover_orphans(store)
    assert result.unclaimed == [entry.name]
    assert not result.errors and not result.recovered
    assert entry.exists() and unrelated.read_bytes() == b"unrelated"


@pytest.mark.parametrize(
    "contents", [b"{", b"{}", b"x" * 16385], ids=["malformed", "missing-fields", "oversized"]
)
def test_invalid_records_do_not_authorize_cleanup(enrolled, contents):
    store, lease, profile = enrolled
    record_path = lease.root.parent / "owner.json"
    original = record_path.read_bytes()
    lease.release()
    record_path.write_bytes(contents)
    try:
        result = recovery.recover_orphans(store)
        assert lease.root.parent.name in result.errors
        assert lease.root.exists() and profile._created
    finally:
        record_path.write_bytes(original)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 2),
        ("version", True),
        ("entry", "../outside"),
        ("identity", [True, 1]),
        ("profile", "unrelated-profile"),
        ("profile", "nooa.lpac." + "a" * 32),
        ("extra", "unexpected"),
    ],
)
def test_invalid_record_fields_are_refused(enrolled, field, value):
    store, lease, _ = enrolled
    path = lease.root.parent / "owner.json"
    original = path.read_bytes()
    record = json.loads(original)
    record[field] = value
    lease.release()
    path.write_text(json.dumps(record), encoding="utf-8")
    try:
        result = recovery.recover_orphans(store)
        assert lease.root.parent.name in result.errors
        assert lease.root.exists()
    finally:
        path.write_bytes(original)


def test_failed_profile_delete_is_retained_for_retry(enrolled, monkeypatch):
    store, lease, _ = enrolled
    lease.release()
    with monkeypatch.context() as patch:
        patch.setattr(native, "_DeleteProfile", lambda name: -2147024891)
        result = recovery.recover_orphans(store)
        assert lease.root.parent.name in result.errors
        assert lease.root.exists()
    assert recovery.recover_orphans(store).recovered == [lease.root.parent.name]


def test_normal_close_failure_keeps_the_lease_active_for_retry(enrolled, monkeypatch):
    store, lease, profile = enrolled
    with monkeypatch.context() as patch:
        patch.setattr(native, "_DeleteProfile", lambda name: -2147024891)
        with pytest.raises(OSError):
            lease.cleanup(profile.close)
    assert recovery.recover_orphans(store).active == [lease.root.parent.name]
    lease.cleanup(profile.close)
    assert not lease.root.parent.exists()


def test_profile_name_collision_does_not_adopt_or_delete_existing_profile(
    enrolled, tmp_path, monkeypatch
):
    _, lease, profile = enrolled
    other_store = tmp_path / "other-ledger"
    with monkeypatch.context() as patch:
        patch.setattr(
            native.uuid,
            "uuid4",
            lambda: uuid.UUID(profile.name.removeprefix("nooa.lpac.")),
        )
        with pytest.raises(OSError):
            _AppContainerPython(recovery_directory=other_store)
    with pytest.raises(OSError):
        native.Profile(name=profile.name)
    assert lease.root.exists()
    assert [path.name for path in other_store.iterdir()] == ["recovery.lock"]


@pytest.mark.parametrize("name", ["", "other.profile", "nooa.lpac." + "A" * 32, "../outside"])
def test_invalid_explicit_profile_names_fail_before_native_creation(monkeypatch, name):
    def unexpected(*args):
        pytest.fail("invalid profile names must not reach the native API")

    monkeypatch.setattr(native, "_CreateProfile", unexpected)
    with pytest.raises(ValueError):
        native.Profile(name=name)


def test_concurrent_recovery_deletes_each_entry_once(enrolled):
    from concurrent.futures import ThreadPoolExecutor

    store, lease, _ = enrolled
    lease.release()
    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(recovery.recover_orphans, (store, store)))
    assert [name for report in reports for name in report.recovered] == [lease.root.parent.name]
    assert all(not report.errors for report in reports)


@pytest.mark.parametrize("name", ["owner.json", "lease"])
def test_hardlinked_metadata_and_lease_files_are_refused(enrolled, tmp_path, name):
    store, lease, _ = enrolled
    lease.release()
    alias = tmp_path / "alias"
    os.link(lease.root.parent / name, alias)
    try:
        result = recovery.recover_orphans(store)
        assert lease.root.parent.name in result.errors and not result.recovered
        assert lease.root.exists()
    finally:
        alias.unlink()


def test_missing_record_is_not_recreated_or_adopted(enrolled):
    store, lease, _ = enrolled
    record = lease.root.parent / "owner.json"
    original = record.read_bytes()
    lease.release()
    record.unlink()
    try:
        assert lease.root.parent.name in recovery.recover_orphans(store).errors
        assert not record.exists() and lease.root.exists()
    finally:
        record.write_bytes(original)


def test_profile_registration_cannot_change_a_committed_or_released_lease(enrolled):
    _, lease, profile = enrolled
    with pytest.raises(RuntimeError, match="uncommitted live lease"):
        lease.record_profile(profile.name)
    lease.release()
    with pytest.raises(RuntimeError, match="uncommitted live lease"):
        lease.record_profile(profile.name)


def test_failed_record_replacement_preserves_the_previous_record(tmp_path, monkeypatch):
    lease = recovery._RuntimeLease.create(tmp_path / "ledger")
    profile = native.Profile(name=lease.profile_name)
    original = (lease.root.parent / "owner.json").read_bytes()

    def refuse_replace(*args):
        raise OSError("synthetic rename failure")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(Path, "replace", refuse_replace)
            with pytest.raises(OSError, match="rename failure"):
                lease.record_profile(profile.name)
        assert (lease.root.parent / "owner.json").read_bytes() == original
        assert recovery.recover_orphans(lease.store).active == [lease.root.parent.name]
        lease.record_profile(profile.name)
    finally:
        lease.cleanup(profile.close)


def test_initial_record_failure_cleans_only_the_new_entry(tmp_path, monkeypatch):
    store = tmp_path / "ledger"

    def refuse_record(*args):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(recovery, "_write_record", refuse_record)
    with pytest.raises(OSError, match="write failure"):
        recovery._RuntimeLease.create(store)
    assert [path.name for path in store.iterdir()] == ["recovery.lock"]


def test_root_and_entry_cannot_be_replaced_during_recovery(enrolled, tmp_path, monkeypatch):
    store, lease, _ = enrolled
    lease.release()
    delete = native._DeleteProfile
    blocked = []

    def try_substitution(name):
        for path in (lease.root, lease.root.parent):
            with pytest.raises(OSError):
                path.rename(tmp_path / path.name)
            blocked.append(path)
        return delete(name)

    monkeypatch.setattr(native, "_DeleteProfile", try_substitution)
    assert recovery.recover_orphans(store).recovered == [lease.root.parent.name]
    assert blocked == [lease.root, lease.root.parent]


def test_failed_tree_delete_after_profile_delete_is_retryable(enrolled, monkeypatch):
    store, lease, _ = enrolled
    lease.release()

    def refuse(*args, **kwargs):
        raise PermissionError("synthetic disk failure")

    with monkeypatch.context() as patch:
        patch.setattr(recovery, "_remove_pinned_tree", refuse)
        assert lease.root.parent.name in recovery.recover_orphans(store).errors
    assert recovery.recover_orphans(store).recovered == [lease.root.parent.name]


def test_recovery_rejects_substituted_payload_before_deleting_profile(enrolled, monkeypatch):
    store, lease, profile = enrolled
    lease.release()
    saved = lease.root.with_name("original")
    lease.root.rename(saved)
    lease.root.mkdir()
    try:
        calls = []
        with monkeypatch.context() as patch:
            patch.setattr(native, "_DeleteProfile", lambda name: calls.append(name) or 0)
            assert lease.root.parent.name in recovery.recover_orphans(store).errors
        assert not calls and profile._created
    finally:
        lease.root.rmdir()
        saved.rename(lease.root)


def test_payload_junction_cannot_redirect_recovery(enrolled, tmp_path):
    import _winapi

    store, lease, _ = enrolled
    lease.release()
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "keep"
    canary.write_bytes(b"untouched")
    saved = lease.root.with_name("original")
    lease.root.rename(saved)
    _winapi.CreateJunction(str(outside), str(lease.root))
    try:
        assert lease.root.parent.name in recovery.recover_orphans(store).errors
        assert canary.read_bytes() == b"untouched"
    finally:
        lease.root.rmdir()
        saved.rename(lease.root)


def test_nested_workspace_junction_is_unlinked_not_traversed(enrolled, tmp_path):
    import _winapi

    store, lease, _ = enrolled
    outside = tmp_path / "outside"
    outside.mkdir()
    canary = outside / "keep"
    canary.write_bytes(b"untouched")
    _winapi.CreateJunction(str(outside), str(lease.root / "junction"))
    lease.release()
    assert recovery.recover_orphans(store).recovered == [lease.root.parent.name]
    assert canary.read_bytes() == b"untouched"


def test_recovery_removes_long_payload_paths(enrolled):
    store, lease, _ = enrolled
    parent = lease.root / ("nested" * 8)
    target = parent / ("r" * max(64, 270 - len(str(parent))) + ".txt")
    assert len(str(target)) > 260
    extended = Path("\\\\?\\" + str(target))
    extended.parent.mkdir()
    extended.write_bytes(b"owned long-path resource")
    lease.release()
    report = recovery.recover_orphans(store)
    assert report.recovered == [lease.root.parent.name] and not report.errors
    assert not lease.root.parent.exists()


def test_existing_unprotected_directory_is_not_adopted_or_modified(tmp_path):
    store = tmp_path / "unowned"
    store.mkdir()
    canary = store / "keep"
    canary.write_bytes(b"untouched")
    with pytest.raises(PermissionError):
        recovery.recover_orphans(store)
    assert canary.read_bytes() == b"untouched"
    assert not (store / "recovery.lock").exists()


def test_recovery_store_junction_is_rejected(tmp_path):
    import _winapi

    target = tmp_path / "outside"
    target.mkdir()
    link = tmp_path / "ledger"
    _winapi.CreateJunction(str(target), str(link))
    try:
        with pytest.raises(OSError):
            recovery.recover_orphans(link)
        assert not list(target.iterdir())
    finally:
        link.rmdir()


def test_managed_runtime_is_usable_and_close_removes_its_entry(tmp_path):
    store = tmp_path / "\u9694\u79bb ledger"
    with _AppContainerPython(recovery_directory=store) as runtime:
        root = runtime.root
        result = runtime.run("print(42)")
        assert result.returncode == 0 and result.stdout.strip() == b"42"
        assert runtime.recover_orphans(store).active == [root.parent.name]
        # A worker must not read or rewrite its host-side ownership record.
        result = runtime.run(
            f"from pathlib import Path\nPath({str(root.parent / 'owner.json')!r}).read_bytes()"
        )
        assert result.returncode != 0 and b"PermissionError" in result.stderr
        for path in (root.parent / "owner.json", root.parent / "lease", store / "recovery.lock"):
            result = runtime.run(
                f"from pathlib import Path\nPath({str(path)!r}).write_bytes(b'tampered')"
            )
            assert result.returncode != 0 and b"PermissionError" in result.stderr
    assert not root.parent.exists()
    runtime.close()


@pytest.mark.timeout(180)
async def test_managed_runtime_supports_persistent_framework_cells(tmp_path):
    from nooa.runtime.sandbox._lpac import _LpacExecutor
    from nooa.runtime.sandbox._lpac_runtime import stage_framework

    store = tmp_path / "ledger"
    source = tmp_path / "application.py"
    source.write_text("answer = 42\n", encoding="utf-8")
    with _AppContainerPython(recovery_directory=store) as runtime:
        root = runtime.root
        module = "managed_" + "x" * max(100, 270 - len(str(runtime.runtime / "packages")))
        assert len(str(runtime.runtime / "packages" / f"{module}.py")) > 260
        stage_framework(runtime, application_modules={module: source})
        executor = _LpacExecutor(runtime, startup_timeout_s=60)
        try:
            result = await executor.run_cell("seed = 40\nseed")
            assert result.success and result.returned_value == 40, result.error
            result = await executor.run_cell("seed + 2")
            assert result.success and result.returned_value == 42, result.error
            result = await executor.run_cell(f"import {module}\n{module}.answer")
            assert result.success and result.returned_value == 42, result.error
            result = await executor.run_cell(f"open({str(root.parent / 'owner.json')!r}).read()")
            assert not result.success and "PermissionError" in str(result.error)
            assert runtime.recover_orphans(store).active == [root.parent.name]
        finally:
            await executor.aclose()
    assert not root.parent.exists()


@pytest.mark.parametrize("phase", ["registered", "staging", "uncommitted"])
def test_crashed_owner_resources_are_recovered_without_pid_guessing(tmp_path, phase, monkeypatch):
    store = tmp_path / "ledger"
    receipt = tmp_path / "receipt.json"
    source = f"""
import json, os
from pathlib import Path
from nooa.runtime.sandbox import _appcontainer
from nooa.runtime.sandbox._lpac_recovery import _RuntimeLease
from nooa.runtime.sandbox._win_appcontainer import Profile
def crash(lease, profile):
    Path({str(receipt)!r}).write_text(json.dumps(
        {{"entry": lease.root.parent.name, "profile": profile.name}}), encoding="utf-8")
    os._exit(23)
if {phase!r} == "registered":
    lease = _RuntimeLease.create(Path({str(store)!r}))
    profile = Profile(name=lease.profile_name)
    lease.record_profile(profile.name)
    crash(lease, profile)
elif {phase!r} == "uncommitted":
    lease = _RuntimeLease.create(Path({str(store)!r}))
    profile = Profile(name=lease.profile_name)
    crash(lease, profile)
else:
    original = _appcontainer._copy_stdlib
    def fail_during_staging(*args):
        entry = next(Path({str(store)!r}).glob("runtime-*"))
        record = json.loads((entry / "owner.json").read_text(encoding="ascii"))
        Path({str(receipt)!r}).write_text(json.dumps(
            {{"entry": entry.name, "profile": record["profile"]}}), encoding="utf-8")
        os._exit(23)
    _appcontainer._copy_stdlib = fail_during_staging
    _appcontainer._AppContainerPython(recovery_directory=Path({str(store)!r}))
"""
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "PATH", "TEMP", "TMP"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    result = subprocess.run(
        [sys.executable, "-I", "-c", source],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        timeout=60,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 23, result.stderr
    record = json.loads(receipt.read_text(encoding="utf-8"))
    report = recovery.recover_orphans(store)
    if phase == "uncommitted":
        assert report.unclaimed == [record["entry"]] and not report.recovered
        # Test-only receipt proves ownership. Production does not infer or adopt
        # the profile that might have been created before its record committed.
        entry = store / record["entry"]
        metadata = recovery._read_record(entry)
        recovery._write_record(entry, {**metadata, "profile": record["profile"]})
        report = recovery.recover_orphans(store)
    assert report.recovered == [record["entry"]] and not report.errors
    assert not (store / record["entry"]).exists()
    with monkeypatch.context() as patch:
        patch.setattr(
            native.uuid, "uuid4", lambda: uuid.UUID(record["profile"].removeprefix("nooa.lpac."))
        )
        profile = native.Profile()
        profile.close()
