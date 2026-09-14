#!/usr/bin/env python3
"""Build the Termux .deb and the apt metadata from the current source.

Run from the repo root:

    python scripts/build_deb.py

Regenerates termux-mcp_<version>_all.deb, Packages, Packages.gz and Release
from termux_mcp/ and pyproject.toml, so the package cannot drift from the
source the way it did before (a hand-built 1.0 deb was still being served
months after the project had moved on, missing most of its modules).

The package is installed to a fixed location and a .pth file is dropped into
whatever site-packages the device's python actually uses, in postinst. That
keeps it working when the Termux python minor version changes, which a
hardcoded python3.13 path does not.
"""

import gzip
import io
import pathlib
import sys
import tarfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
PREFIX = "/data/data/com.termux/files/usr"
LIBDIR = f"{PREFIX}/lib/termux-mcp"

LAUNCHER = f"""#!{PREFIX}/bin/sh
exec python -m termux_mcp "$@"
"""

POSTINST = f"""#!{PREFIX}/bin/sh
[ "$1" = "configure" ] || exit 0
SITE="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null)"
if [ -n "$SITE" ] && [ -d "$SITE" ]; then
    echo "{LIBDIR}" > "$SITE/termux-mcp.pth"
fi
exit 0
"""

PRERM = f"""#!{PREFIX}/bin/sh
[ "$1" = "remove" ] || exit 0
SITE="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null)"
[ -n "$SITE" ] && rm -f "$SITE/termux-mcp.pth"
exit 0
"""


def read_version() -> str:
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("version"):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("no version in pyproject.toml")


def read_description() -> str:
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        if line.startswith("description"):
            return line.split("=", 1)[1].strip().strip('"')
    return "Termux MCP server"


def source_files():
    for path in sorted((ROOT / "termux_mcp").rglob("*")):
        if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        if path.is_file():
            yield path


def add_bytes(tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = int(time.time())
    tar.addfile(info, io.BytesIO(data))


def add_file(tar: tarfile.TarFile, name: str, path: pathlib.Path, mode: int = 0o644):
    add_bytes(tar, name, path.read_bytes(), mode)


def make_control_tar(version: str, installed_size: int) -> bytes:
    desc = read_description()
    control = (
        "Package: termux-mcp\n"
        "Architecture: all\n"
        f"Installed-Size: {installed_size}\n"
        "Maintainer: Parixit Sutariya\n"
        f"Version: {version}\n"
        "Homepage: https://github.com/termuxgpt/termux-mcp\n"
        "Depends: python\n"
        f"Description: {desc}\n"
    )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        add_bytes(tar, "./control", control.encode())
        add_bytes(tar, "./postinst", POSTINST.encode(), 0o755)
        add_bytes(tar, "./prerm", PRERM.encode(), 0o755)
    return buf.getvalue()


def make_data_tar():
    buf = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=buf, mode="w") as tar:
        add_bytes(tar, "./usr/bin/termux-mcp", LAUNCHER.encode(), 0o755)
        for path in source_files():
            # as_posix(): pathlib str() uses backslashes on Windows, which
            # would put literal "\" characters into every archive path.
            rel = path.relative_to(ROOT / "termux_mcp").as_posix()
            data = path.read_bytes()
            total += len(data)
            add_bytes(tar, f"./usr/lib/termux-mcp/termux_mcp/{rel}", data)
        add_file(tar, "./usr/share/doc/termux-mcp/copyright", ROOT / "LICENSE")
    return buf.getvalue(), total


def xz(data: bytes) -> bytes:
    import lzma
    return lzma.compress(data, format=lzma.FORMAT_XZ, preset=9)


def ar(members) -> bytes:
    out = bytearray(b"!<arch>\n")
    for name, body in members:
        header = (
            name.ljust(16)
            + str(int(time.time())).ljust(12)
            + "0".ljust(6) + "0".ljust(6)
            + "100644".ljust(8)
            + str(len(body)).ljust(10)
            + "`\n"
        )
        out += header.encode("ascii")
        out += body
        if len(body) % 2:
            out += b"\n"
    return bytes(out)


def main() -> None:
    version = read_version()
    data_tar, total = make_data_tar()
    ctrl_tar = make_control_tar(version, max(1, total // 1024))

    deb = ar([
        ("debian-binary", b"2.0\n"),
        ("control.tar.xz", xz(ctrl_tar)),
        ("data.tar.xz", xz(data_tar)),
    ])

    tmp = ROOT / f"termux-mcp_{version}_all.deb"
    tmp.write_bytes(deb)

    for stale in ROOT.glob("termux-mcp_*_all.deb"):
        if stale != tmp:
            stale.unlink()

    desc = read_description()
    size = tmp.stat().st_size
    packages = (
        f"Package: termux-mcp\n"
        f"Architecture: all\n"
        f"Version: {version}\n"
        f"Maintainer: Parixit Sutariya\n"
        f"Installed-Size: {max(1, total // 1024)}\n"
        f"Filename: ./{tmp.name}\n"
        f"Size: {size}\n"
        f"Description: {desc}\n"
        "Depends: python\n"
    )
    (ROOT / "Packages").write_text(packages, encoding="utf-8")
    with gzip.open(ROOT / "Packages.gz", "wb") as fh:
        fh.write(packages.encode())

    release = (
        "Origin: Termux MCP\n"
        "Label: Termux MCP\n"
        "Suite: stable\n"
        "Codename: stable\n"
        "Architectures: all\n"
        "Components: main\n"
        f"Description: Termux MCP - MCP server for TermuxGPT\n"
        f"Date: {time.strftime('%a, %d %b %Y %H:%M:%S +0000', time.gmtime())}\n"
    )
    (ROOT / "Release").write_text(release, encoding="utf-8")

    n = sum(1 for _ in source_files())
    print(f"built {tmp.name}  ({size} bytes)")
    print(f"  version        : {version}")
    print(f"  modules shipped: {n} files, {total} bytes")
    print(f"  installs to    : {LIBDIR}")
    print(f"  launcher       : {PREFIX}/bin/termux-mcp")
    print("  regenerated    : Packages, Packages.gz, Release")


if __name__ == "__main__":
    sys.exit(main())
