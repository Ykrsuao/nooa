# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Copy explicit installed dependency closures, never the host site directory."""

from __future__ import annotations

import importlib.metadata as metadata
import importlib.util
import json
import shutil
import sys
from collections import deque
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from nooa.runtime.sandbox._appcontainer import (
    _AppContainerPython,
    _check_source,
    _copy_stdlib,
    _input_name,
    _long_path,
)


def stage_framework(
    runtime: _AppContainerPython,
    *,
    application_modules: Mapping[str, Path] | None = None,
    application_requirements: Iterable[str] = (),
) -> Path:
    """Stage installed PEP 508 requirements into a fresh owned runtime.

    No resolution, download or installation occurs. Imports execute inside LPAC;
    packages requiring install hooks, external data or unavailable OS capabilities
    are not supported. A failed stage leaves the runtime unready; close it.
    """
    with runtime._lock:
        if (
            runtime._closed
            or runtime._profile is None
            or runtime.root.resolve() != runtime.root
            or runtime.runtime.parent != runtime.root
            or runtime.runtime.resolve() != runtime.runtime
        ):
            raise ValueError("framework staging requires a live, owned LPAC runtime")
        destination = _long_path(runtime.runtime / "packages")
        _stage_packages(destination, application_requirements)
        _stage_applications(destination, application_modules or {})
        shutil.copyfile(
            destination / "nooa/runtime/sandbox/_lpac_asyncio.py",
            _long_path(runtime.runtime / "Lib/asyncio/windows_events.py"),
        )
        runtime._framework_staged = True
        return destination


def _stage_applications(destination: Path, modules: Mapping[str, Path]) -> None:
    """Copy explicit import-safe Python files, never a source tree or import hook."""
    destination = _long_path(destination)
    sources = {}
    targets = set()
    packages = set()
    names = set()
    stdlib = {name.casefold() for name in sys.stdlib_module_names}
    installed = {path.name.split(".")[0].casefold() for path in destination.iterdir()}
    for name, path in modules.items():
        if not isinstance(name, str):
            raise ValueError("application module names must be strings")
        parts = name.split(".")
        if any(not part.isidentifier() or part.startswith("_") for part in parts):
            raise ValueError(f"invalid application module name: {name!r}")
        for part in parts:
            _input_name(part)
        if name.casefold() in names:
            raise ValueError(f"application module name collision: {name}")
        names.add(name.casefold())
        if parts[0].casefold() in stdlib | installed:
            raise ValueError(f"application module shadows a runtime package: {name}")
        source = Path(path).absolute()
        if source.resolve() != source or source.suffix != ".py" or not source.is_file():
            raise ValueError(f"application module must be a regular Python source file: {name}")
        _check_source(source)
        is_package = source.name == "__init__.py"
        target = destination.joinpath(*parts)
        target = target / "__init__.py" if is_package else target.with_suffix(".py")
        identity = str(target).casefold()
        if identity in targets or target.exists():
            raise ValueError(f"application module destination collision: {name}")
        targets.add(identity)
        sources[name] = (source, target)
        if is_package:
            packages.add(name)
    for name in sources:
        parts = name.split(".")
        if any(".".join(parts[:i]) not in packages for i in range(1, len(parts))):
            raise ValueError(f"application package parents must be explicitly staged: {name}")
    for source, target in sources.values():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def _dependency_closure(requirements: Iterable[str]) -> list[metadata.Distribution]:
    if isinstance(requirements, str):
        raise TypeError("application_requirements must be an iterable of requirement strings")
    pending = deque((Requirement("nooa"), *(Requirement(text) for text in requirements)))
    visited = set()
    distributions = {}
    while pending:
        requirement = pending.popleft()
        if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
            continue
        if requirement.url:
            raise ValueError("LPAC dependencies must be installed named requirements, not URLs")
        name = canonicalize_name(requirement.name)
        dist = distributions.get(name)
        if dist is None:
            dist = metadata.distribution(name)
        # Validate every edge, including a stricter constraint on an already seen package.
        if requirement.specifier and dist.version not in requirement.specifier:
            raise ValueError(f"installed dependency does not satisfy {requirement}")
        extras = frozenset(canonicalize_name(extra) for extra in requirement.extras)
        provided = {
            canonicalize_name(extra) for extra in dist.metadata.get_all("Provides-Extra", [])
        }
        if extras - provided:
            raise ValueError(
                f"distribution {name!r} does not provide extras: {sorted(extras - provided)}"
            )
        key = (name, extras)
        if key in visited:
            continue
        visited.add(key)
        distributions[name] = dist
        for text in dist.requires or ():
            dependency = Requirement(text)
            if dependency.marker and not any(
                dependency.marker.evaluate({"extra": extra}) for extra in ("", *extras)
            ):
                continue
            # The marker was evaluated in the requiring distribution's extra context.
            dependency.marker = None
            pending.append(dependency)
    return list(distributions.values())


def _stage_packages(destination: Path, requirements: Iterable[str] = ()) -> None:
    """Populate a new private directory from trusted installed distributions.

    Editable NOOA and the first-party CLI use their exact package directories,
    not their repositories. CLI source fallback copies Python only. Other editable
    distributions fail explicitly. .pth execution, launchers and bytecode are not
    transferred.
    """
    import nooa

    destination = _long_path(destination)
    distributions = _dependency_closure(requirements)
    # Keep validation on the caller and bound both I/O concurrency and queued work.
    # Executor exit joins every copy, including when validation or a copy fails.
    with ThreadPoolExecutor(max_workers=4, thread_name_prefix="nooa-stage") as pool:
        pending = deque()
        for source, target in _package_files(destination, distributions):
            if len(pending) >= 32:
                pending.popleft().result()
            pending.append(pool.submit(shutil.copyfile, source, target))
        for future in pending:
            future.result()
    package = destination / "nooa"
    if not package.exists():
        _copy_stdlib(Path(nooa.__file__).resolve().parent, package)
    if (
        any(
            canonicalize_name(dist.metadata["Name"]) == "nooa-cli" and _is_editable(dist)
            for dist in distributions
        )
        and not (destination / "nooa_cli").exists()
    ):
        _stage_editable_cli(destination / "nooa_cli")


def _is_editable(dist: metadata.Distribution) -> bool:
    direct_url = dist.read_text("direct_url.json")
    return bool(direct_url and json.loads(direct_url).get("dir_info", {}).get("editable"))


def _stage_editable_cli(destination: Path) -> None:
    """Copy only Python source from the installed first-party CLI package.

    Resolving a top-level spec does not import the package or execute its editable
    .pth hook. The host has already selected its installed package location.
    """
    spec = importlib.util.find_spec("nooa_cli")
    if spec is None or spec.origin is None or spec.submodule_search_locations is None:
        raise ValueError("editable nooa-cli requires an installed Python package source")
    origin = Path(spec.origin).absolute()
    source = origin.parent
    locations = tuple(Path(path).absolute() for path in spec.submodule_search_locations)
    if (
        origin.name != "__init__.py"
        or locations != (source,)
        or source.resolve() != source
        or not origin.is_file()
    ):
        raise ValueError("editable nooa-cli requires a non-reparse Python package source")
    _check_source(source)
    _check_source(origin)

    def copy(directory: Path) -> None:
        for item in directory.iterdir():
            if item.name.startswith(".") or item.name.casefold() == "__pycache__":
                continue
            if item.is_dir():
                _check_source(item)
                copy(item)
            elif item.suffix.casefold() in (".py", ".pyi"):
                _check_source(item)
                target = destination / item.relative_to(source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(_long_path(item), target)

    copy(source)


def _package_files(
    destination: Path, distributions: Iterable[metadata.Distribution]
) -> Iterable[tuple[Path, Path]]:
    """Validate manifests serially before yielding each private copy destination."""
    core_names = {canonicalize_name(dist.metadata["Name"]) for dist in _dependency_closure(())}
    destination.mkdir()
    copied = {}
    import_roots = {}
    stdlib = {name.casefold() for name in sys.stdlib_module_names}
    for dist in distributions:
        name = canonicalize_name(dist.metadata["Name"])
        editable = _is_editable(dist)
        first_party_editable = name in ("nooa", "nooa-cli") and editable
        if name not in ("nooa", "nooa-cli") and editable:
            raise ValueError(f"editable distribution is unsupported: {name}")
        files = dist.files
        if files is None:
            raise ValueError(f"distribution {name!r} has no file manifest")
        base = Path(str(dist.locate_file(""))).resolve()
        package_files = 0
        for item in files:
            relative = Path(str(item))
            suffix = relative.suffix.casefold()
            # Core dependencies already run without their install hooks (notably
            # setuptools' distutils-precedence.pth). New dependency hooks are unsupported.
            if (
                name not in core_names
                and not first_party_editable
                and suffix in (".pth", ".egg-link")
            ):
                raise ValueError(f"distribution requires an unsupported install hook: {name}")
            if (
                relative.is_absolute()
                or relative.drive
                or ".." in relative.parts
                or any(part.startswith(".") for part in relative.parts)
                or "__pycache__" in (part.casefold() for part in relative.parts)
                or relative.name.casefold() in (".env", "direct_url.json", "record")
                or suffix in (".pyc", ".pth", ".egg-link")
            ):
                continue
            for part in relative.parts:
                _input_name(part)
            import_root = relative.parts[0].split(".")[0].casefold()
            if (
                import_root in stdlib
                or (name != "nooa" and import_root == "nooa")
                or (name != "nooa-cli" and import_root == "nooa_cli")
            ):
                raise ValueError(f"distribution shadows a runtime package: {name}/{relative}")
            is_package = len(relative.parts) > 1
            previous_root = import_roots.get(import_root)
            if previous_root is not None and (
                previous_root[0] != is_package or (not is_package and previous_root[1] != name)
            ):
                raise ValueError(f"distributions overlap at import root {import_root}")
            import_roots[import_root] = (is_package, name)
            source = base / relative
            if source.resolve() != source or not source.is_file():
                raise ValueError(f"distribution contains an invalid source: {name}/{relative}")
            _check_source(source)
            target = destination / relative
            if not any(part.casefold().endswith(".dist-info") for part in relative.parts):
                package_files += 1
            identity = str(target).casefold()
            previous = copied.get(identity)
            if previous is not None:
                if previous != source:
                    raise ValueError(f"distributions overlap at {relative}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            copied[identity] = source
            yield source, target
        if name != "nooa" and not first_party_editable and not package_files:
            raise ValueError(f"editable/empty distribution is unsupported: {name}")
