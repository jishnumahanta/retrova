"""Regenerable developer folders (node_modules, venv, build caches ...).

Uses the shared rules in retrova.recovery.regen when that module is importable and
falls back to a private copy of the same rules otherwise, so the tools keep working
even while the recovery package is being changed.

API: classify(name, parent_names, list_inside, lock_above=False) -> (confidence, what, regenerate) | None
     has_lockfile(names) -> bool
     venv_verdict(name, parent_names)
"""
from __future__ import annotations

import fnmatch

from ..util import Once

LOCKFILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock", "npm-shrinkwrap.json")
_PY_REQS = ("requirements*.txt", "pyproject.toml", "Pipfile", "Pipfile.lock",
            "poetry.lock", "uv.lock", "setup.py", "setup.cfg", "environment.yml")
_GRADLE = ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")
_DOTNET = ("*.csproj", "*.fsproj", "*.vbproj", "*.sln")

# Fallback copy of retrova.recovery.regen.REGEN_RULES (same format):
# name glob, parent must contain one of, inside must contain one of, confidence, what, regenerate
REGEN_RULES = [
    ("node_modules", ("package.json",), (), "safe", "JavaScript packages", "npm install  (or yarn / pnpm install)"),
    (".next", ("package.json",), (), "safe", "Next.js build cache", "npm run build"),
    (".nuxt", ("package.json",), (), "safe", "Nuxt build cache", "npm run build"),
    (".output", ("nuxt.config.*",), (), "safe", "Nuxt build output", "npm run build"),
    (".svelte-kit", ("package.json",), (), "safe", "SvelteKit build cache", "npm run build"),
    (".turbo", ("package.json", "turbo.json"), (), "safe", "Turborepo cache", "npm run build"),
    (".parcel-cache", ("package.json",), (), "safe", "Parcel cache", "npm run build"),
    (".angular", ("angular.json",), (), "safe", "Angular cache", "ng build"),
    (".expo", ("package.json", "app.json"), (), "safe", "Expo cache", "npx expo start"),
    ("bower_components", ("bower.json",), (), "safe", "Bower packages", "bower install"),
    (".pio", ("platformio.ini",), (), "safe", "PlatformIO libraries + build", "pio run  (re-downloads lib_deps)"),
    (".gradle", _GRADLE + ("gradlew",), (), "safe", "Gradle cache", "./gradlew build"),
    ("build", _GRADLE, (), "safe", "Gradle/Android build output", "./gradlew build"),
    (".cxx", _GRADLE, (), "safe", "Android NDK build cache", "./gradlew build"),
    (".externalNativeBuild", _GRADLE, (), "safe", "Android NDK build cache", "./gradlew build"),
    ("Pods", ("Podfile",), (), "safe", "CocoaPods packages", "pod install"),
    ("DerivedData", (), (), "safe", "Xcode build data", "build in Xcode"),
    ("target", ("Cargo.toml",), (), "safe", "Rust build output", "cargo build"),
    ("target", ("pom.xml",), (), "safe", "Maven build output", "mvn package"),
    ("bin", _DOTNET, (), "safe", ".NET build output", "dotnet build"),
    ("obj", _DOTNET, (), "safe", ".NET build output", "dotnet build"),
    (".dart_tool", ("pubspec.yaml",), (), "safe", "Dart/Flutter tool cache", "flutter pub get"),
    ("build", ("pubspec.yaml",), (), "safe", "Flutter build output", "flutter build"),
    ("vendor", ("composer.json",), (), "safe", "PHP Composer packages", "composer install"),
    (".terraform", ("*.tf",), (), "safe", "Terraform providers", "terraform init"),
    ("build", (), ("CMakeCache.txt",), "safe", "CMake build folder", "cmake -B build && cmake --build build"),
    ("cmake-build-*", (), ("CMakeCache.txt",), "safe", "CLion/CMake build folder", "rebuild in CLion"),
    ("__pycache__", (), (), "safe", "Python bytecode cache", "created automatically"),
    (".pytest_cache", (), (), "safe", "pytest cache", "created automatically"),
    (".mypy_cache", (), (), "safe", "mypy cache", "created automatically"),
    (".ruff_cache", (), (), "safe", "ruff cache", "created automatically"),
    (".tox", ("tox.ini", "pyproject.toml", "setup.py"), (), "safe", "tox environments", "tox"),
    ("dist", ("package.json",), (), "review", "JavaScript build output - check it is not hand-made",
     "npm run build"),
    ("build", ("package.json",), (), "review", "JavaScript build output - check it is not hand-made",
     "npm run build"),
    ("out", ("package.json",), (), "review", "JavaScript build output - check it is not hand-made",
     "npm run build"),
    ("dist", ("setup.py", "pyproject.toml"), (), "review", "Python package builds", "python -m build"),
    (".cache", (), (), "review", "Generic cache folder", "usually recreated automatically"),
]
_VENV_NAMES = ("venv", ".venv", "env", ".env", "virtualenv", ".virtualenv", "venv3", ".venv3")

# Rules the tools add on top of the shared ones (checked first).
EXTRA_RULES = [
    (".pnpm-store", (), (), "safe", "pnpm package store", "pnpm install  (downloads the packages again)"),
    (".yarn-cache", (), (), "safe", "Yarn package cache", "yarn install"),
]


def _any_match(names, globs) -> bool:
    return any(fnmatch.filter(names, g) for g in globs)


def _local_venv_verdict(name, parent_names):
    if _any_match(parent_names, _PY_REQS):
        return ("safe", "Python virtual environment", "python -m venv " + name +
                " && pip install -r requirements.txt (or the project's installer)")
    return ("review", "Python virtual environment, but no requirements file next to it - "
                      "the exact package list can't be rebuilt", "python -m venv " + name)


def _local_classify(name, parent_names, list_inside, lock_above=False):
    if name == ".git":
        return None
    if name in _VENV_NAMES:
        if "pyvenv.cfg" in list_inside():
            return _local_venv_verdict(name, parent_names)
        return None
    for rname, parent_globs, inside_globs, conf, what, regen in REGEN_RULES:
        if not fnmatch.fnmatchcase(name, rname):
            continue
        if parent_globs and not _any_match(parent_names, parent_globs):
            continue
        if inside_globs and not _any_match(list_inside(), inside_globs):
            continue
        if rname == "node_modules" and not lock_above and not _any_match(parent_names, LOCKFILES):
            return ("review", "JavaScript packages, but no lockfile - npm install may fetch newer versions", regen)
        return (conf, what, regen)
    return None


def _load():
    try:
        from ..recovery import regen as shared  # type: ignore
        return shared.classify_regen, shared.venv_verdict, "shared"
    except Exception:
        return _local_classify, _local_venv_verdict, "local"


_IMPL = Once(_load)


def classify(name: str, parent_names, list_inside, lock_above: bool = False):
    """(confidence, what, regenerate) if folder `name` can be recreated by a tool, else None."""
    for rname, parent_globs, inside_globs, conf, what, regen in EXTRA_RULES:
        if fnmatch.fnmatchcase(name, rname) and (not parent_globs or _any_match(parent_names, parent_globs)):
            return (conf, what, regen)
    fn = _IMPL.get()[0]
    try:
        return fn(name, parent_names, list_inside, lock_above)
    except Exception:
        return _local_classify(name, parent_names, list_inside, lock_above)


def venv_verdict(name: str, parent_names):
    try:
        return _IMPL.get()[1](name, parent_names)
    except Exception:
        return _local_venv_verdict(name, parent_names)


def has_lockfile(names) -> bool:
    return _any_match(names, LOCKFILES)


def source() -> str:
    """'shared' when retrova.recovery.regen is in use, 'local' for the fallback copy."""
    return _IMPL.get()[2]
