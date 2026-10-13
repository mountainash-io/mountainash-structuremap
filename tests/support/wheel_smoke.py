"""Qualify installed-wheel behavior in fresh, isolated Python processes."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {command}\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def main() -> None:
    hatch = shutil.which("hatch")
    if hatch is None:
        raise RuntimeError("hatch must be installed to build the wheel")
    clean_env = os.environ.copy()
    clean_env.pop("PYTHONPATH", None)
    clean_env.pop("HATCH_ENV_ACTIVE", None)
    clean_env.pop("VIRTUAL_ENV", None)

    with tempfile.TemporaryDirectory(prefix="structuremap-wheel-") as temporary:
        temp = Path(temporary)
        wheel_dir = temp / "wheel-output"
        wheel_dir.mkdir()
        run([hatch, "build", "-t", "wheel", str(wheel_dir)], cwd=ROOT, env=clean_env)
        built = list(wheel_dir.glob("mountainash_structuremap-*.whl"))
        if len(built) != 1:
            raise RuntimeError(f"expected one structuremap wheel in {wheel_dir}, found {len(built)}")
        venv = temp / "venv"
        run([sys.executable, "-m", "venv", str(venv)], cwd=temp, env=clean_env)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run([str(python), "-m", "pip", "install", str(built[0])], cwd=temp, env=clean_env)
        run([str(python), "-m", "pip", "check"], cwd=temp, env=clean_env)

        fixture = temp / "fixture"
        fixture.mkdir()
        support = ROOT / "tests" / "support" / "wheel_smoke"
        shutil.copy2(support / "models.py", fixture / "wheel_models.py")
        shutil.copy2(support / "runner.py", fixture / "runner.py")
        for mode in ("independent", "write", "read"):
            print(run([str(python), "-I", str(fixture / "runner.py"), mode], cwd=temp, env=clean_env))
        print("Installed-wheel smoke passed using the wheel's declared pinned GitHub dependency")


if __name__ == "__main__":
    main()
