
import pathlib
import re
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent

def pyproject_version() -> str:
    with open(ROOT / "pyproject.toml", "rb") as handle:
        return tomllib.load(handle)["project"]["version"]

def init_version() -> str:
    text = (ROOT / "termux_mcp" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)', text, re.MULTILINE)
    return match.group(1) if match else ""

def packages_version() -> str:
    path = ROOT / "Packages"
    if not path.exists():
        return ""
    match = re.search(r"^Version:\s*(\S+)", path.read_text(encoding="utf-8"),
                      re.MULTILINE)
    return match.group(1) if match else ""

def packages_filename() -> str:
    path = ROOT / "Packages"
    if not path.exists():
        return ""
    match = re.search(r"^Filename:\s*\./(\S+)", path.read_text(encoding="utf-8"),
                      re.MULTILINE)
    return match.group(1) if match else ""

def main() -> int:
    versions = {
        "pyproject.toml": pyproject_version(),
        "termux_mcp/__init__.py": init_version(),
        "Packages": packages_version(),
    }
    problems = []

    missing = [name for name, value in versions.items() if not value]
    for name in missing:
        problems.append(f"{name}: no version found")

    agreed = {v for v in versions.values() if v}
    if len(agreed) > 1:
        listed = ", ".join(f"{name}={value}" for name, value in versions.items())
        problems.append(f"versions disagree: {listed}")

    deb = packages_filename()
    if deb:
        if not (ROOT / deb).exists():
            problems.append(f"Packages names a file that is not here: {deb}")
        elif agreed and f"_{next(iter(agreed))}_" not in deb:
            problems.append(f"{deb} is not built for {next(iter(agreed))}")

    if problems:
        print("version check failed:")
        for problem in problems:
            print(f"  - {problem}")
        print("\nRebuild with: python scripts/build_deb.py")
        return 1

    print(f"versions agree: {next(iter(agreed))} "
          f"(pyproject, __init__, Packages, {deb or 'no deb'})")
    return 0

if __name__ == "__main__":
    sys.exit(main())
