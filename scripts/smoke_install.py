# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Build wheels, install outside the checkout, and run offline acceptance tests."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("nooa", "nooa-cli", "nooa-acp", "nooa-memory", "nooa-bench")
NATIVE_TESTS = (
    "test_windows_job.py",
    "test_spawn_executor.py",
    "test_appcontainer.py",
    "test_lpac_executor.py",
    "test_lpac_codeact.py",
    "test_lpac_runtime.py",
    "test_lpac_brokers.py",
    "test_lpac_recovery.py",
    "test_lpac_policy.py",
    "test_lpac_workspace.py",
    "test_lpac_directories.py",
    "test_windows_session.py",
    "test_windows_api.py",
    "test_platform_support.py",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable, help="Python executable or uv version")
    parser.add_argument(
        "--test-file",
        action="append",
        choices=NATIVE_TESTS,
        help="Run selected native test files plus wheel provenance; default: the entire suite",
    )
    args = parser.parse_args()
    uv = shutil.which("uv")
    if uv is None:
        parser.error("uv must be on PATH")

    with tempfile.TemporaryDirectory(prefix="nooa-install-") as temporary:
        root = Path(temporary).resolve()
        wheels = root / "wheels"
        for package in PACKAGES:
            subprocess.run(
                [
                    uv,
                    "build",
                    "--no-sources",
                    "--wheel",
                    "--package",
                    package,
                    "--out-dir",
                    str(wheels),
                ],
                cwd=ROOT,
                check=True,
                timeout=300,
            )
        venv = root / "\u5e72\u51c0\u5b89\u88c5 environment"
        subprocess.run([uv, "venv", "--python", args.python, str(venv)], check=True, timeout=180)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        requirements = [
            str(wheel) + ("[viewer,tracing,mcp]" if wheel.name.startswith("nooa-") else "")
            for wheel in sorted(wheels.glob("*.whl"))
        ]
        assert len(requirements) == len(PACKAGES), requirements
        constraints = root / "constraints.txt"
        subprocess.run(
            [
                uv,
                "export",
                "--frozen",
                "--all-extras",
                "--no-extra",
                "sandbox",
                "--no-emit-workspace",
                "--no-hashes",
                "--output-file",
                str(constraints),
            ],
            cwd=ROOT,
            check=True,
            timeout=60,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "--constraint",
                str(constraints),
                *requirements,
                "pytest>=7.4",
                "pytest-asyncio>=0.21",
                "pytest-timeout>=2.3",
                "pyyaml>=6",
                "cryptography>=41.0",
            ],
            cwd=root,
            check=True,
            timeout=600,
        )

        # Copy only the self-contained tests: no source tree, root conftest, or
        # pytest pythonpath setting can make a broken wheel appear to work.
        suite = root / "acceptance"
        shutil.copytree(
            ROOT / "tests" / "acceptance",
            suite,
            ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
        )
        # This suite is self-contained and checks the installed native module;
        # unlike forked sandbox tests it runs on both Windows Python versions.
        for name in (*NATIVE_TESTS, "lpac_test_app.py"):
            shutil.copyfile(ROOT / "tests" / "runtime" / "sandbox" / name, suite / name)
        env = dict(os.environ)
        for name in ("PYTHONPATH", "PYTHONHOME"):
            env.pop(name, None)
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        env["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
        env["NOOA_SMOKE_INSTALL_ROOT"] = str(venv)
        targets = (
            [
                str(suite / "test_installed_workflows.py")
                + "::test_imports_are_from_installed_wheels",
                *(str(suite / name) for name in dict.fromkeys(args.test_file)),
            ]
            if args.test_file
            else [str(suite)]
        )
        subprocess.run(
            [
                str(python),
                "-I",
                "-m",
                "pytest",
                "-p",
                "pytest_asyncio.plugin",
                "-p",
                "pytest_timeout",
                "-c",
                str(suite / "pytest.ini"),
                "--confcutdir",
                str(suite),
                *targets,
            ],
            cwd=root,
            env=env,
            check=True,
            # The full native matrix includes repeated staging and teardown;
            # individual pytest deadlines remain unchanged in either mode.
            timeout=1800 if args.test_file else 3600,
        )
    print(
        "Selected clean-install acceptance passed."
        if args.test_file
        else "Clean-install acceptance passed."
    )


if __name__ == "__main__":
    main()
