"""Create/repair the private runtime and launch the GIF CLI."""
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time
import uuid
import venv

ROOT = Path(__file__).resolve().parent.parent
PROBE = (
    "import sys,ssl,json,PIL,certifi; from PIL import features; "
    "assert sys.version_info >= (3,10); assert features.check('webp'); "
    "print(json.dumps({'prefix':sys.prefix,'python':sys.executable,"
    "'version':sys.version.split()[0],'pillow':PIL.__version__}))"
)


def probe(executable, expected_prefix):
    try:
        result = subprocess.run(
            [str(executable), "-c", PROBE], capture_output=True, text=True, timeout=20
        )
        data = json.loads(result.stdout) if result.returncode == 0 else {}
        if Path(data.get("prefix", "")).resolve() == expected_prefix.resolve():
            return data
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None


def env_python(directory):
    return directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def main():
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10+ is required.")
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    manifest = runtime / "environment.json"
    directory = ROOT / ".venv"
    try:
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        candidate = Path(saved["venv"]).resolve()
        if candidate.parent == ROOT and candidate.name.startswith(".venv"):
            directory = candidate
    except (OSError, ValueError, KeyError, TypeError):
        pass
    executable = env_python(directory)
    state = probe(executable, directory)
    if not state:
        # Preserve an old environment rather than recursively deleting anything.
        if directory.exists():
            directory = ROOT / (".venv-" + uuid.uuid4().hex[:8])
        executable = env_python(directory)
        print("Preparing private Python environment...", file=sys.stderr, flush=True)
        venv.EnvBuilder(with_pip=True).create(directory)
        command = [
            str(executable), "-m", "pip", "install", "--disable-pip-version-check",
            "--only-binary=:all:", "Pillow>=10,<13", "certifi>=2024.2.2",
        ]
        result = subprocess.run(command, stdout=sys.stderr, stderr=sys.stderr)
        if result.returncode:
            raise RuntimeError("Dependency installation failed. Check network/proxy and rerun.")
        state = probe(executable, directory)
        if not state:
            raise RuntimeError("The private Python environment did not pass validation.")
    state.update({
        "venv": str(directory), "os": platform.system(),
        "architecture": platform.machine(), "checked_at": time.time(),
    })
    temp = manifest.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temp, manifest)
    result = subprocess.run([str(executable), str(ROOT / "scripts/stickers.py"), *sys.argv[1:]])
    return result.returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print("Environment error: " + str(error), file=sys.stderr)
        raise SystemExit(1)
