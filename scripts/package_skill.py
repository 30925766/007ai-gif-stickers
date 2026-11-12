"""Source allowlist packaging; never distribute local credentials or runtime."""
import argparse
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent
FILES = (
    "SKILL.md", "VERSION", ".gitignore", "agents/openai.yaml",
    "scripts/run.ps1", "scripts/run.sh", "scripts/bootstrap.py",
    "scripts/stickers.py", "scripts/package_skill.py",
    "references/api.md", "references/distribution.md",
)


def package(destination):
    destination = Path(destination).expanduser().resolve()
    if destination == ROOT or ROOT in destination.parents:
        raise ValueError("Write the distribution archive outside the installed skill.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in FILES:
            path = ROOT / relative
            content = path.read_bytes()
            # Preserve LF for the macOS shell entrypoint even on Windows.
            if relative.endswith(".sh"):
                content = content.replace(b"\r\n", b"\n")
            info = zipfile.ZipInfo(ROOT.name + "/" + relative)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100755 if relative.endswith(".sh") else 0o100644) << 16
            archive.writestr(info, content)
    return destination


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(package(args.output))
