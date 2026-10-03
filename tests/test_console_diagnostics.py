# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Unicode paths must not make diagnostics abort the operation being reported."""

import io
import os
import sys

import pytest


@pytest.mark.parametrize("encoding", ["utf-8", "cp1252"])
@pytest.mark.parametrize("errors", ["strict", "surrogateescape"])
def test_viewer_starts_with_unicode_db_on_narrow_output(monkeypatch, tmp_path, encoding, errors):
    import uvicorn
    from nooa_cli.commands.start_dev import command

    database = tmp_path / "中文 workspace" / "轨迹 data.db"
    started = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: started.append(kwargs))
    monkeypatch.setenv("NOOA_VIEWER_AUTH_TOKEN", "")
    monkeypatch.setenv("NOOA_TRACE_DB", "")
    monkeypatch.setenv("NEMO_OO_TRACE_DB", "")
    with io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors=errors) as stream:
        with monkeypatch.context() as output_patch:
            output_patch.setattr(sys, "stdout", stream)
            assert command.callback is not None
            command.callback(port=5001, host="127.0.0.1", db_path_opt=str(database))
            assert sys.stdout is stream and stream.encoding == encoding
        stream.flush()
        output = stream.buffer.getvalue().decode(encoding)
    assert len(started) == 1
    assert database.parent.is_dir()
    assert os.environ["NOOA_TRACE_DB"] == str(database.resolve())
    expected = str(database.resolve()).encode(encoding, "backslashreplace").decode(encoding)
    assert f"DB:   {expected}" in output


@pytest.mark.parametrize("encoding", ["utf-8", "cp1252"])
def test_trace_target_with_unicode_path_on_narrow_output(monkeypatch, tmp_path, encoding):
    from nooa.tracing import _print_trace_target, exporters

    directory = tmp_path / "评测 output"
    exporter = exporters.jsonl(directory)
    try:
        with io.TextIOWrapper(io.BytesIO(), encoding=encoding) as stream:
            with monkeypatch.context() as output_patch:
                output_patch.setattr(sys, "stdout", stream)
                _print_trace_target([exporter], experiment="中文 experiment")
                assert sys.stdout is stream and stream.encoding == encoding
            stream.flush()
            output = stream.buffer.getvalue().decode(encoding)
        expected = str(directory).encode(encoding, "backslashreplace").decode(encoding)
        assert expected in output
        assert "OTel tracing enabled:" in output
    finally:
        exporter.shutdown()
