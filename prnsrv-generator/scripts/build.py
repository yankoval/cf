#!/usr/bin/env python3
"""Build a Cloud Functions ZIP with prnsrv installed as a separate package."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


FUNCTION_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRNSRV_SOURCE = FUNCTION_ROOT.parents[1] / "prnsrv"
DEFAULT_PRNSRV_REQUIREMENT = (
    "git+https://github.com/yankoval/prnsrv.git"
    "@64a594d3e9063cca70452ddd566ec3b5a1bb966b"
)
APPLICATION_FILES = ("index.py", "app.py", "config.py", "storage.py")


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def git_metadata(source: Path) -> dict[str, object]:
    try:
        commit = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "-C", str(source), "status", "--porcelain"], text=True
            ).strip()
        )
        return {"source_type": "local", "git_commit": commit, "git_dirty": dirty}
    except (subprocess.CalledProcessError, FileNotFoundError):
        return {"source_type": "local", "git_commit": None, "git_dirty": None}


def build(args: argparse.Namespace) -> Path:
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="prnsrv-generator-build-") as temporary:
        stage = Path(temporary) / "stage"
        stage.mkdir()

        for filename in APPLICATION_FILES:
            shutil.copy2(FUNCTION_ROOT / filename, stage / filename)

        if not args.skip_third_party:
            run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--disable-pip-version-check",
                    "--target",
                    str(stage),
                    "--platform",
                    args.target_platform,
                    "--implementation",
                    "cp",
                    "--python-version",
                    args.python_version,
                    "--only-binary=:all:",
                    "-r",
                    str(FUNCTION_ROOT / "requirements.txt"),
                ]
            )

        if args.prnsrv_source is not None:
            source = args.prnsrv_source.resolve()
            if not (source / "pyproject.toml").is_file():
                raise SystemExit(f"prnsrv package is not found at {source}")
            requirement = str(source)
            metadata = git_metadata(source)
        else:
            requirement = args.prnsrv_requirement or DEFAULT_PRNSRV_REQUIREMENT
            metadata = {"source_type": "requirement", "requirement": requirement}

        install_command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            "--target",
            str(stage),
            requirement,
        ]
        run(install_command)

        if not (stage / "prnsrv" / "__init__.py").is_file():
            raise SystemExit("Built archive does not contain the prnsrv package")

        (stage / "build-manifest.json").write_text(
            json.dumps(
                {
                    "function": "prnsrv-generator",
                    "runtime": {
                        "implementation": "cp",
                        "python_version": args.python_version,
                        "platform": args.target_platform,
                    },
                    "prnsrv": metadata,
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    archive.write(path, path.relative_to(stage))

    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=FUNCTION_ROOT / "dist" / "prnsrv-generator.zip",
    )
    source_group = parser.add_mutually_exclusive_group()
    source_group.add_argument(
        "--prnsrv-source",
        type=Path,
        help=f"Explicit local development checkout (usually {DEFAULT_PRNSRV_SOURCE})",
    )
    source_group.add_argument(
        "--prnsrv-requirement",
        help=(
            "Override the pinned pip requirement; the default is "
            f"{DEFAULT_PRNSRV_REQUIREMENT}"
        ),
    )
    parser.add_argument(
        "--skip-third-party",
        action="store_true",
        help="Build a structural test ZIP without boto3/requests",
    )
    parser.add_argument("--python-version", default="314")
    parser.add_argument("--target-platform", default="manylinux2014_x86_64")
    return parser.parse_args()


if __name__ == "__main__":
    print(build(parse_args()))
