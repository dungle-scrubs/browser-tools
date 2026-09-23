"""Run the network-dependent installation acceptance checks in fresh directories."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def run(command: list[str], *, env: dict[str, str], cwd: Path | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"{command} exited {result.returncode}: {result.stderr}")
    return result.stdout


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    evidence = Path(sys.argv[1]).resolve()
    evidence.mkdir(parents=True, exist_ok=False)
    adapter = root / "src/browser_tools/_insights"
    env = {
        **os.environ,
        "UV_CACHE_DIR": str(evidence / "uv-cache"),
        "npm_config_cache": str(evidence / "npm-cache"),
    }
    inventories = []
    for name in ["first", "second"]:
        directory = evidence / name
        directory.mkdir()
        for file in ["package.json", "package-lock.json"]:
            shutil.copy(adapter / file, directory / file)
        output = run(["npm", "ci", "--omit=dev", "--no-audit", "--no-fund"], cwd=directory, env=env)
        (evidence / f"{name}-npm.log").write_text(output)
        tree = json.loads(run(["npm", "ls", "--all", "--json"], cwd=directory, env=env))
        (evidence / f"{name}-tree.json").write_text(json.dumps(tree, indent=2) + "\n")
        versions = {
            str(path.parent.relative_to(directory)): json.loads(path.read_text())["version"]
            for path in (directory / "node_modules").rglob("package.json")
        }
        inventories.append(versions)
    assert inventories[0] == inventories[1], inventories
    run(["uv", "build", "--wheel", "--out-dir", str(evidence / "dist")], cwd=root, env=env)
    wheel = next((evidence / "dist").glob("*.whl"))
    run(["uv", "venv", "--seed", str(evidence / "base")], env=env)
    python = str(evidence / "base/bin/python")
    # No Node/npm executable is visible during pip installation or base CLI checks.
    base_env = {**env, "PATH": str(evidence / "base/bin"), "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
    install = run([python, "-m", "pip", "install", "--no-cache-dir", str(wheel)], env=base_env)
    (evidence / "base-pip.log").write_text(install)
    packages = json.loads(run([python, "-m", "pip", "list", "--format=json"], env=base_env))
    assert {package["name"] for package in packages} == {"pip", "browser-tools", "websockets"}, (
        packages
    )
    inspection = run(
        [
            python,
            "-c",
            """
import importlib.resources, json, shutil
p = importlib.resources.files('browser_tools').joinpath('_insights')
assert not p.joinpath('node_modules').exists()
assert not p.joinpath('.installed').exists()
assert p.joinpath('package-lock.json').is_file()
assert p.joinpath('vendor/PerformanceInsightFormatter.js').is_file()
assert shutil.which('node') is None and shutil.which('npm') is None
print(json.dumps({'node': None, 'engineInstalled': False}))
""",
        ],
        env=base_env,
    )
    run([python, "-m", "browser_tools.cli", "guide"], env=base_env)
    refusal = subprocess.run(
        [python, "-m", "browser_tools.cli", "insights", "--trace", "absent.json"],
        env=base_env,
        capture_output=True,
        text=True,
    )
    assert refusal.returncode == 2 and "bt insights --setup" in refusal.stderr, refusal
    # Also execute the installed adapter, not just the source checkout.
    installed_setup = run([python, "-m", "browser_tools.cli", "insights", "--setup"], env=env)
    result = json.loads(
        run(
            [
                python,
                "-m",
                "browser_tools.cli",
                "insights",
                "--trace",
                str(root / "tests/fixtures/insights/http-navigation.json"),
            ],
            env=env,
        )
    )
    assert len(result["navigations"][0]["insights"]) == 19
    report = {
        "identicalDependencyVersions": inventories[0],
        "basePackages": packages,
        "baseInspection": json.loads(inspection),
        "beforeSetupExit": refusal.returncode,
        "installedWheelSetup": json.loads(installed_setup),
        "installedWheelModels": 19,
    }
    (evidence / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
