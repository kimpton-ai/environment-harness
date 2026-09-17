"""Validate release archives against explicit positive manifests."""

from __future__ import annotations

import json
import stat
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
NATIVE_SUFFIXES = {".dll", ".dylib", ".exe", ".node", ".o", ".so"}
ROOT_FILES = {
    ".gitignore",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "README.md",
    "SECURITY.md",
    "SUPPORT.md",
    "pyproject.toml",
    "uv.toml",
}
DIRECTORY_SUFFIXES = {
    "contracts": {".json"},
    "docs": {".md"},
    "examples": {".py"},
    "src": {".css", ".html", ".js", ".py", ".sql"},
}


def project_version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def metadata_version(content: bytes, artifact: str) -> str:
    for line in content.decode().splitlines():
        if line.startswith("Version: "):
            return line.removeprefix("Version: ")
    raise SystemExit(f"embedded version is missing from {artifact}")


def source_manifest() -> set[str]:
    expected = {name for name in ROOT_FILES if (ROOT / name).is_file()}
    for directory, suffixes in DIRECTORY_SUFFIXES.items():
        expected.update(
            path.relative_to(ROOT).as_posix()
            for path in (ROOT / directory).rglob("*")
            if path.is_file() and path.suffix in suffixes and "__pycache__" not in path.parts
        )
    return expected


def reject_payload(name: str, mode: int = 0, *, symlink: bool = False) -> None:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise SystemExit(f"unsafe archive path: {name}")
    if symlink:
        raise SystemExit(f"symlink is prohibited in distribution: {name}")
    if path.suffix.lower() in NATIVE_SUFFIXES:
        raise SystemExit(f"native or executable payload is prohibited: {name}")
    if mode and stat.S_ISREG(mode) and mode & 0o111:
        raise SystemExit(f"executable file is prohibited in distribution: {name}")


def check_wheel(path: Path) -> None:
    version = project_version()
    if path.name != f"environment_harness-{version}-py3-none-any.whl":
        raise SystemExit(f"wheel filename does not match project version {version}: {path.name}")
    expected_sources = {
        path.relative_to(ROOT / "src").as_posix()
        for path in (ROOT / "src/environment_harness").rglob("*")
        if path.is_file() and path.suffix in DIRECTORY_SUFFIXES["src"] and "__pycache__" not in path.parts
    }
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        names = {info.filename for info in infos if not info.is_dir()}
        for info in infos:
            reject_payload(
                info.filename,
                info.external_attr >> 16,
                symlink=stat.S_ISLNK(info.external_attr >> 16),
            )
        dist_info = next(
            (name.split("/", 1)[0] for name in names if name.endswith(".dist-info/METADATA")), None
        )
        if not dist_info:
            raise SystemExit("wheel metadata is missing")
        if dist_info != f"environment_harness-{version}.dist-info":
            raise SystemExit(f"wheel metadata directory does not match project version {version}")
        if metadata_version(archive.read(f"{dist_info}/METADATA"), path.name) != version:
            raise SystemExit(f"wheel embedded version does not match project version {version}")
        metadata = {
            f"{dist_info}/METADATA",
            f"{dist_info}/RECORD",
            f"{dist_info}/WHEEL",
            f"{dist_info}/entry_points.txt",
            f"{dist_info}/licenses/LICENSE",
        }
        expected = expected_sources | metadata
        if names != expected:
            raise SystemExit(
                "wheel manifest mismatch; unexpected="
                + repr(sorted(names - expected))
                + ", missing="
                + repr(sorted(expected - names))
            )
        if archive.read(f"{dist_info}/licenses/LICENSE") != (ROOT / "LICENSE").read_bytes():
            raise SystemExit("wheel MIT license differs from the repository license")


def check_sdist(path: Path) -> None:
    version = project_version()
    if path.name != f"environment_harness-{version}.tar.gz":
        raise SystemExit(f"sdist filename does not match project version {version}: {path.name}")
    expected_sources = source_manifest()
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        files = [member for member in members if member.isfile()]
        for member in members:
            reject_payload(
                member.name,
                member.mode if member.isfile() else 0,
                symlink=member.issym() or member.islnk(),
            )
        roots = {PurePosixPath(member.name).parts[0] for member in files}
        if len(roots) != 1:
            raise SystemExit("sdist must have exactly one root directory")
        prefix = roots.pop()
        if prefix != f"environment_harness-{version}":
            raise SystemExit(f"sdist root does not match project version {version}: {prefix}")
        names = {str(PurePosixPath(member.name).relative_to(prefix)) for member in files}
        expected = expected_sources | {"PKG-INFO"}
        if names != expected:
            raise SystemExit(
                "sdist manifest mismatch; unexpected="
                + repr(sorted(names - expected))
                + ", missing="
                + repr(sorted(expected - names))
            )
        license_member = archive.extractfile(f"{prefix}/LICENSE")
        if not license_member or license_member.read() != (ROOT / "LICENSE").read_bytes():
            raise SystemExit("sdist MIT license differs from the repository license")
        package_metadata = archive.extractfile(f"{prefix}/PKG-INFO")
        if not package_metadata or metadata_version(package_metadata.read(), path.name) != version:
            raise SystemExit(f"sdist embedded version does not match project version {version}")


def check_npm(path: Path) -> None:
    package = json.loads((ROOT / "packages/typescript/package.json").read_text())
    version = project_version()
    if package.get("version") != version:
        raise SystemExit("npm and Python project versions differ")
    if path.name != f"environment-harness-client-{version}.tgz":
        raise SystemExit(f"npm filename does not match project version {version}: {path.name}")
    expected = {"package/package.json"} | {f"package/{name}" for name in package["files"]}
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        names = {member.name for member in members if member.isfile()}
        for member in members:
            reject_payload(
                member.name,
                member.mode if member.isfile() else 0,
                symlink=member.issym() or member.islnk(),
            )
        if names != expected:
            raise SystemExit(
                "npm manifest mismatch; unexpected="
                + repr(sorted(names - expected))
                + ", missing="
                + repr(sorted(expected - names))
            )
        license_member = archive.extractfile("package/LICENSE")
        if not license_member or license_member.read() != (ROOT / "LICENSE").read_bytes():
            raise SystemExit("npm MIT license differs from the repository license")
        package_member = archive.extractfile("package/package.json")
        if not package_member or json.load(package_member).get("version") != version:
            raise SystemExit(f"npm embedded version does not match project version {version}")


def main() -> None:
    artifacts = sorted((ROOT / "dist").iterdir())
    checked = 0
    for artifact in artifacts:
        if artifact.suffix == ".whl":
            check_wheel(artifact)
        elif artifact.name.endswith(".tar.gz"):
            check_sdist(artifact)
        elif artifact.suffix == ".tgz":
            check_npm(artifact)
        else:
            continue
        checked += 1
        print(artifact.name + ": positive distribution manifest passed")
    if not checked:
        raise SystemExit("no wheel, sdist, or npm tarball found in dist/")


if __name__ == "__main__":
    main()
