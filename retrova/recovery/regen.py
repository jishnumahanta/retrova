"""Regenerable-folder rules: dependency / build / cache folders a project can recreate.

Shared by the recovery "skip regenerable folders" scan (job kind "regen") and the
tools cleanup feature. API (unchanged from T5 Rescue):

    REGEN_RULES                       list of (name_glob, parent_globs, inside_globs, confidence, what, regenerate)
    classify_regen(name, parent_names, list_inside, lock_above=False) -> (confidence, what, regenerate) | None
    venv_verdict(name, parent_names) -> (confidence, what, regenerate)

confidence is "safe" (recreated exactly by the shown command) or "review".
"""
from __future__ import annotations

import fnmatch

LOCKFILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock", "npm-shrinkwrap.json")
_LOCKFILES = LOCKFILES
_PY_REQS = ("requirements*.txt", "pyproject.toml", "Pipfile", "Pipfile.lock",
            "poetry.lock", "uv.lock", "setup.py", "setup.cfg", "environment.yml")
_GRADLE = ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")
_DOTNET = ("*.csproj", "*.fsproj", "*.vbproj", "*.sln")

# name, parent must contain one of (globs; empty = anywhere), inside must contain one of, confidence, what, regenerate
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
VENV_NAMES = ("venv", ".venv", "env", ".env", "virtualenv", ".virtualenv", "venv3", ".venv3")
_VENV_NAMES = VENV_NAMES

# Quick "skip by name only" list (legacy DEV_JUNK) for the UI's simple pattern option.
DEV_JUNK = [
    "node_modules", ".next", ".nuxt", ".gradle", "DerivedData", "Pods", "__pycache__",
    ".venv", "venv", ".pio", ".cache", ".parcel-cache", ".turbo", ".expo", ".dart_tool",
]


def _any_match(names, globs) -> bool:
    return any(fnmatch.filter(names, g) for g in globs)


def any_lockfile(names) -> bool:
    """True when a folder listing contains a JavaScript lockfile (monorepo roots)."""
    return _any_match(names, LOCKFILES)


def venv_verdict(name: str, parent_names):
    if _any_match(parent_names, _PY_REQS):
        return ("safe", "Python virtual environment", "python -m venv " + name +
                " && pip install -r requirements.txt (or the project's installer)")
    return ("review", "Python virtual environment, but no requirements file next to it - "
                      "the exact package list can't be rebuilt", "python -m venv " + name)


def classify_regen(name: str, parent_names, list_inside, lock_above: bool = False):
    """Return (confidence, what, regenerate) if folder `name` is regenerable, else None.
    parent_names: names in the folder that contains it. list_inside(): names inside it."""
    if name == ".git":
        return None
    if name in _VENV_NAMES:
        inside = list_inside()
        if "pyvenv.cfg" in inside:
            return venv_verdict(name, parent_names)
        return None
    for rname, parent_globs, inside_globs, conf, what, regen in REGEN_RULES:
        if not fnmatch.fnmatchcase(name, rname):
            continue
        if parent_globs and not _any_match(parent_names, parent_globs):
            continue
        if inside_globs and not _any_match(list_inside(), inside_globs):
            continue
        if rname == "node_modules" and not lock_above and not _any_match(parent_names, _LOCKFILES):
            return ("review", "JavaScript packages, but no lockfile - npm install may fetch newer versions",
                    regen)
        return (conf, what, regen)
    return None
