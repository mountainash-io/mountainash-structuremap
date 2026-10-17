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
        token = env.get("CI_READ_TOKEN")
        stderr = result.stderr.replace(token, "[REDACTED]") if token else result.stderr
        stdout = result.stdout.replace(token, "[REDACTED]") if token else result.stdout
        raise RuntimeError(f"command failed ({result.returncode}): {command}\n{stdout}\n{stderr}")
    return result.stdout.strip()


def install(
    wheel: Path, temp: Path, *, extra: bool, pydantic_constraint: str | None = None
) -> Path:
    environment = (
        f"pydantic-{pydantic_constraint.replace('=', '').replace('<', '').replace('>', '')}-venv"
        if pydantic_constraint
        else ("xml-files-venv" if extra else "base-venv")
    )
    venv = temp / environment
    run([sys.executable, "-m", "venv", str(venv)], cwd=temp, env=CLEAN_ENV)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    requirement = f"{wheel}[xml,files]" if extra else str(wheel)
    requirements = [requirement]
    if pydantic_constraint:
        requirements.append(f"pydantic{pydantic_constraint}")
    run([str(python), "-m", "pip", "install", *requirements], cwd=temp, env=CLEAN_ENV)
    run([str(python), "-m", "pip", "check"], cwd=temp, env=CLEAN_ENV)
    print(
        run(
            [
                str(python),
                "-c",
                "from importlib.metadata import PackageNotFoundError, version\n"
                "def installed(name):\n"
                "    try:\n"
                "        return version(name)\n"
                "    except PackageNotFoundError:\n"
                "        return 'absent'\n"
                "print('Installed versions: structuremap=' + installed('mountainash-structuremap') "
                "+ ', pydantic=' + installed('pydantic') "
                "+ ', xsdata=' + installed('xsdata'))",
            ],
            cwd=temp,
            env=CLEAN_ENV,
        )
    )
    return python


CLEAN_ENV = os.environ.copy()
CLEAN_ENV.pop("PYTHONPATH", None)
CLEAN_ENV.pop("HATCH_ENV_ACTIVE", None)
CLEAN_ENV.pop("VIRTUAL_ENV", None)


def execute_smoke(python: Path, fixture: Path, temp: Path, modes: tuple[str, ...]) -> None:
    for mode in modes:
        print(run([str(python), "-I", str(fixture / "runner.py"), mode], cwd=temp, env=CLEAN_ENV))


def main() -> None:
    hatch = shutil.which("hatch")
    if hatch is None:
        raise RuntimeError("hatch must be installed to build the wheel")
    with tempfile.TemporaryDirectory(prefix="structuremap-wheel-") as temporary:
        temp = Path(temporary)
        wheel_dir = temp / "wheel-output"
        wheel_dir.mkdir()
        run([hatch, "build", "-t", "wheel", str(wheel_dir)], cwd=ROOT, env=CLEAN_ENV)
        built = list(wheel_dir.glob("mountainash_structuremap-*.whl"))
        if len(built) != 1:
            raise RuntimeError(f"expected one structuremap wheel in {wheel_dir}, found {len(built)}")

        fixture = temp / "fixture"
        fixture.mkdir()
        support = ROOT / "tests" / "support" / "wheel_smoke"
        shutil.copy2(support / "external.py", fixture / "wheel_external.py")
        shutil.copy2(support / "models.py", fixture / "wheel_models.py")
        shutil.copy2(support / "runner.py", fixture / "runner.py")
        shutil.copy2(support / "execution_cases.py", fixture / "execution_cases.py")

        xml_python = install(built[0], temp, extra=True)
        execute_smoke(xml_python, fixture, temp, (
            "independent", "write", "read", "invalid", "pydantic",
            "authoring-persist", "authoring-independent",
            "execution-write-parquet", "execution-read-parquet",
            "execution-write-arrow", "execution-read-arrow",
        ))
        base_python = install(built[0], temp, extra=False)
        execute_smoke(base_python, fixture, temp, ("base", "missing-xml", "pydantic", "authoring-independent"))
        pydantic_python = install(
            built[0], temp, extra=False, pydantic_constraint="==2.11.0"
        )
        execute_smoke(pydantic_python, fixture, temp, ("base", "pydantic", "authoring-independent"))
        print("Installed-wheel smoke passed using the wheel's declared pinned GitHub dependency")


if __name__ == "__main__":
    main()




