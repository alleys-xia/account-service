"""Build owned protocol/SDK wheels and install the native account-service environment."""

import argparse
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def call(*args, cwd=ROOT):
    subprocess.run(
        ["uv", *map(str, args)],
        cwd=cwd,
        check=True,
        env=os.environ | {"UV_CACHE_DIR": str(ROOT / ".cache/uv")},
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--locked", action="store_true")
    args = parser.parse_args()
    wheels = ROOT / ".cache/wheels"
    wheels.mkdir(parents=True, exist_ok=True)
    for name in ("oms-protocol", "oms-client", "portfolio-client"):
        package = ROOT / "packages" / name
        call("build", "--wheel", "--out-dir", package / "dist", cwd=package)
        project = tomllib.loads((package / "pyproject.toml").read_text())["project"]
        filename = f"{project['name'].replace('-', '_')}-{project['version']}-py3-none-any.whl"
        shutil.copy2(package / "dist" / filename, wheels / filename)
    if not args.locked:
        call(
            "lock",
            "--upgrade-package",
            "oms-protocol",
            "--upgrade-package",
            "oms-client",
            "--upgrade-package",
            "portfolio-client",
        )
    call("sync", "--locked", "--no-editable", "--reinstall-package", "account-service")


if __name__ == "__main__":
    main()
