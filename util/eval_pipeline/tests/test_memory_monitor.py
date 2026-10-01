# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the _memory_monitor module.

Tests resource limits (RLIMIT_AS) and hard-kill behavior in isolated
subprocesses to avoid poisoning the test runner's own process limits.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, mock_open

import pytest

from eval_pipeline import _memory_monitor as memory_module
from eval_pipeline._memory_monitor import (
    MemoryMonitor,
    clear_hard_limit,
    enable_tracking,
    get_rss_mb,
    set_hard_limit,
)


class TestEnableTracking:
    def test_enables_tracemalloc(self):
        """tracemalloc.start() is called and tracing is active."""
        was_tracing = tracemalloc.is_tracing()
        try:
            if was_tracing:
                tracemalloc.stop()
            enable_tracking()
            assert tracemalloc.is_tracing()
        finally:
            if not was_tracing:
                tracemalloc.stop()

    def test_idempotent(self):
        """Calling enable_tracking twice doesn't raise."""
        was_tracing = tracemalloc.is_tracing()
        try:
            enable_tracking()
            enable_tracking()  # should not raise
            assert tracemalloc.is_tracing()
        finally:
            if not was_tracing:
                tracemalloc.stop()


class TestGetRssMb:
    def test_windows_uses_working_set_reader(self, monkeypatch):
        monkeypatch.setattr(memory_module.platform, "system", lambda: "Windows")
        monkeypatch.setattr(memory_module, "_get_windows_rss_mb", lambda: 42.5, raising=False)
        assert get_rss_mb() == 42.5

    def test_linux_current_rss(self, monkeypatch):
        monkeypatch.setattr(memory_module.platform, "system", lambda: "Linux")
        monkeypatch.setattr(
            memory_module, "open", mock_open(read_data="VmRSS:\t2048 kB\n"), raising=False
        )
        assert get_rss_mb() == 2.0

    @pytest.mark.parametrize("system,peak", [("Linux", 2048), ("Darwin", 2 * 1024 * 1024)])
    def test_unix_fallback_units(self, monkeypatch, system, peak):
        monkeypatch.setattr(memory_module.platform, "system", lambda: system)
        monkeypatch.setattr(
            memory_module, "open", Mock(side_effect=FileNotFoundError), raising=False
        )
        monkeypatch.setattr(
            memory_module,
            "resource",
            SimpleNamespace(RUSAGE_SELF=0, getrusage=lambda _: SimpleNamespace(ru_maxrss=peak)),
        )
        assert get_rss_mb() == 2.0

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows memory API")
    @pytest.mark.parametrize("succeeds", [True, False])
    def test_windows_api_uses_current_not_peak_and_reports_errors(self, monkeypatch, succeeds):
        import ctypes

        def read_memory(handle, pointer, size):
            assert handle == -1
            assert size == ctypes.sizeof(pointer._obj) == pointer._obj.cb
            pointer._obj.WorkingSetSize = 42 * 1024 * 1024
            pointer._obj.PeakWorkingSetSize = 128 * 1024 * 1024
            ctypes.set_last_error(5)
            return succeeds

        kernel = SimpleNamespace(
            GetCurrentProcess=Mock(return_value=-1),
            K32GetProcessMemoryInfo=Mock(side_effect=read_memory),
        )
        monkeypatch.setattr(ctypes, "WinDLL", Mock(return_value=kernel))
        if succeeds:
            assert memory_module._get_windows_rss_mb() == 42.0
        else:
            with pytest.raises(OSError) as error:
                memory_module._get_windows_rss_mb()
            assert error.value.winerror == 5

    def test_returns_positive_float(self):
        rss = get_rss_mb()
        assert isinstance(rss, float)
        assert rss > 0

    def test_reasonable_range(self):
        """Current process RSS should be between 1 MB and 100 GB."""
        rss = get_rss_mb()
        assert 1.0 < rss < 100_000.0

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows process working set")
    def test_windows_rss_tracks_resident_allocation(self):
        script = textwrap.dedent("""\
            import runpy
            import sys

            get_rss_mb = runpy.run_path(sys.argv[1])["get_rss_mb"]

            before = get_rss_mb()
            payload = bytearray(32 * 1024 * 1024)
            after = get_rss_mb()
            assert after > before + 16, (before, after)
        """)
        result = subprocess.run(
            [sys.executable, "-c", script, memory_module.__file__],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0, result.stderr


class TestHardLimit:
    def test_unavailable_resource_does_not_claim_os_limit(self, monkeypatch):
        monkeypatch.setattr(memory_module, "resource", None)

        def unexpected_vas():
            raise AssertionError("Unavailable OS limits must not estimate virtual memory")

        monkeypatch.setattr(memory_module, "_get_vas_mb", unexpected_vas)
        assert set_hard_limit(512) is False
        clear_hard_limit()

    @pytest.mark.skipif(sys.platform == "win32", reason="RLIMIT_AS is unavailable on Windows")
    def test_set_and_clear(self):
        """set_hard_limit / clear_hard_limit round-trips without error.

        Run in a subprocess to avoid permanently lowering the test process's
        hard limit (which can't be raised back without privileges).
        """
        script = textwrap.dedent("""\
            import resource
            from eval_pipeline._memory_monitor import set_hard_limit, clear_hard_limit, _get_vas_mb

            _, orig_hard = resource.getrlimit(resource.RLIMIT_AS)
            vas_before = _get_vas_mb()

            applied = set_hard_limit(512)
            soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            assert hard == orig_hard, f"hard limit should not change"

            if applied:
                expected = int((vas_before + 512) * 1024 * 1024)
                # Clamp to hard ceiling
                if orig_hard != resource.RLIM_INFINITY and expected > orig_hard:
                    expected = orig_hard
                # Allow small tolerance for allocations between _get_vas_mb and setrlimit
                assert abs(soft - expected) < 10 * 1024 * 1024, f"soft={soft}, expected≈{expected}"

                clear_hard_limit()
                soft, hard = resource.getrlimit(resource.RLIMIT_AS)
                assert soft == orig_hard, f"soft should be restored to hard ceiling"

            print("OK")
        """)
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, (
            f"Subprocess failed: stdout={result.stdout!r}, stderr={result.stderr!r}"
        )
        assert "OK" in result.stdout

    @pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS enforcement requires Linux")
    def test_memory_error_on_exceed(self):
        """Allocating beyond the hard limit raises MemoryError.

        We run this in a subprocess to avoid poisoning the test process's
        RLIMIT_AS (which would cause pytest's own reporting to fail).
        """
        script = textwrap.dedent("""\
            from eval_pipeline._memory_monitor import set_hard_limit
            # Allow 50MB growth — then try to allocate 500MB
            set_hard_limit(50)
            try:
                _big = bytearray(500 * 1024 * 1024)  # 500 MB — should exceed
                print("NO_ERROR")
            except MemoryError:
                print("MEMORY_ERROR")
        """)
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert "MEMORY_ERROR" in result.stdout, (
            f"Expected MemoryError but got: stdout={result.stdout!r}, stderr={result.stderr!r}"
        )


class TestMemoryMonitor:
    def test_tracks_peak_rss(self, tmp_path: Path):
        """Monitor tracks peak RSS over its lifetime."""
        monitor = MemoryMonitor(
            limit_mb=99999,  # high limit so soft limit won't fire
            trace_dir=str(tmp_path),
            sample_id="test_peak",
            poll_interval=0.1,
        )
        monitor.start()
        time.sleep(0.3)  # Let a few polls happen
        monitor.stop()

        assert monitor.peak_rss_mb > 0
        assert not monitor.soft_limit_hit
        assert monitor.diag_file is None

    def test_soft_limit_fires_and_writes_diagnostics(self, tmp_path: Path, monkeypatch):
        """When RSS exceeds the soft limit, diagnostics file is written."""
        monkeypatch.setattr(memory_module, "get_rss_mb", lambda: 75.0)
        monitor = MemoryMonitor(
            limit_mb=100,
            trace_dir=str(tmp_path),
            sample_id="test_diag",
            soft_pct=0.5,
            poll_interval=0.1,
        )
        captured = threading.Event()
        capture = monitor._capture_diagnostics

        def capture_and_signal(rss):
            capture(rss)
            captured.set()

        monkeypatch.setattr(monitor, "_capture_diagnostics", capture_and_signal)

        # Enable tracemalloc so the diagnostics capture works
        was_tracing = tracemalloc.is_tracing()
        if not was_tracing:
            tracemalloc.start(5)
        try:
            monitor.start()
            assert captured.wait(10), "Memory diagnostics were not written"
        finally:
            monitor.stop()
            if not was_tracing:
                tracemalloc.stop()

        assert monitor.soft_limit_hit
        assert monitor.diag_file is not None

        diag_path = Path(monitor.diag_file)
        assert diag_path.exists()

        content = diag_path.read_text(encoding="utf-8")
        assert "MEMORY DIAGNOSTICS" in content
        assert "test_diag" in content
        assert "TOP 25 MEMORY ALLOCATORS" in content
        assert "TOP 25 OBJECT TYPES BY COUNT" in content
        assert "THREAD STACK TRACES" in content
        assert "RSS HISTORY" in content

    def test_rss_history_recorded(self, tmp_path: Path):
        """RSS history is accumulated over the monitor lifetime."""
        monitor = MemoryMonitor(
            limit_mb=99999,
            trace_dir=str(tmp_path),
            sample_id="test_history",
            poll_interval=0.05,
        )
        monitor.start()
        time.sleep(0.3)
        monitor.stop()

        assert len(monitor._rss_history) >= 2
        # Timestamps should be monotonically increasing
        times = [t for t, _ in monitor._rss_history]
        assert times == sorted(times)

    def test_hard_limit_exits_child_with_error_result(self, tmp_path: Path):
        script = textwrap.dedent("""\
            import sys
            from eval_pipeline._memory_monitor import MemoryMonitor

            monitor = MemoryMonitor(
                limit_mb=1,
                trace_dir=sys.argv[1],
                sample_id="hard_kill",
                proto_out=sys.stdout.buffer,
                task_meta={"test_id": "memory_child", "test_case": "resident_memory"},
                poll_interval=0.01,
            )
            monitor.start()
            monitor._thread.join(timeout=20)
            raise AssertionError("The over-limit worker did not terminate")
        """)
        result = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 137, (result.stdout, result.stderr)
        payload = json.loads(result.stdout)
        assert payload["test_id"] == "memory_child"
        assert payload["test_case"] == "resident_memory"
        assert payload["passed"] is False
        assert payload["error_type"] == "MemoryError"
        assert payload["peak_rss_mb"] >= 1
        assert "KILLED" in result.stderr
        diag_path = tmp_path / "hard_kill_memory_diag.txt"
        assert Path(payload["memory_diag_file"]) == diag_path
        assert "HARD KILL" in diag_path.read_text(encoding="utf-8")


class TestErrorClassification:
    """Verify that MemoryError patterns are classified correctly."""

    def test_memory_error(self):
        from eval_pipeline._utils import classify_error_type

        assert classify_error_type("MemoryError: process exceeded 4096 MB") == "MemoryError"

    def test_memory_soft_limit(self):
        from eval_pipeline._utils import classify_error_type

        assert (
            classify_error_type("Memory soft limit hit: 3500 MB (limit: 4096 MB)")
            == "MemoryWarning"
        )

    def test_existing_timeout_still_works(self):
        from eval_pipeline._utils import classify_error_type

        assert classify_error_type("TimeoutError: exceeded 30s") == "TimeoutError"
