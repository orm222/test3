#!/usr/bin/env python3
"""Run the project harness with its local Python 3.9 environment on Windows or Linux."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def venv_python(root, platform):
    return root / ".venv" / ("Scripts/python.exe" if platform == "win32" else "bin/python")


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    if not arguments or arguments == ["--help"]:
        print("Usage: python run_harness.py {clarify|verify|status|fingerprint|review|loop} [arguments]")
        print("Create .venv with Python 3.9 and install requirements-harness.txt first.")
        print("Setup and platform-specific commands: docs/CROSS_PLATFORM.md")
        print("First run clarify and record answers, then configure acceptance tests and start work.")
        print("Work start: Linux uses tmux automatically; Windows uses the current terminal.")
        return 0

    python = venv_python(ROOT, sys.platform)
    if not python.is_file():
        print("Python 3.9 environment not found: " + str(python), file=sys.stderr)
        print("Create .venv on this computer; see docs/CROSS_PLATFORM.md.", file=sys.stderr)
        return 1

    environment = dict(os.environ)
    environment["PATH"] = str(python.parent) + os.pathsep + environment.get("PATH", "")
    environment["VIRTUAL_ENV"] = str(ROOT / ".venv")
    environment["PYTHONIOENCODING"] = "utf-8"
    # PYTHONHOME overrides pyvenv.cfg and can select another installation's libraries.
    environment.pop("PYTHONHOME", None)
    probe = (
        "import importlib.util,json,sys; "
        "print(json.dumps({'version':list(sys.version_info[:3]),'prefix':sys.prefix,"
        "'tomli':importlib.util.find_spec('tomli') is not None}))"
    )
    try:
        result = subprocess.run([str(python), "-c", probe], cwd=str(ROOT), env=environment,
                                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                encoding="utf-8", timeout=15, check=True)
        info = json.loads(result.stdout)
        if info["version"][:2] != [3, 9]:
            raise ValueError(".venv must use Python 3.9; found " + ".".join(map(str, info["version"])))
        if Path(info["prefix"]).resolve() != (ROOT / ".venv").resolve():
            raise ValueError("The selected interpreter is not using this project's .venv.")
        if not info["tomli"]:
            raise ValueError("Install requirements-harness.txt with the .venv interpreter first.")
        print("Harness: " + sys.platform + ", Python " + ".".join(map(str, info["version"])), flush=True)
        os.chdir(ROOT)
        if arguments[0] == "loop":
            entry = ROOT / "automation/codex-loop/run.py"
            forwarded = arguments[1:]
            if not any(arg == "--config" or arg.startswith("--config=") for arg in forwarded):
                forwarded = ["--config", str(entry.parent / "config.json")] + forwarded
        elif arguments[0] == "clarify":
            entry = ROOT / ".harness/clarify.py"
            forwarded = ["--root", str(ROOT)] + arguments[1:]
        else:
            entry = ROOT / ".harness/harness.py"
            forwarded = arguments
        command = [str(python), "-B", str(entry)] + forwarded
        diagnostics = ("--help", "-h", "--preflight", "--probe-models")
        if arguments[0] == "loop" and sys.platform.startswith("linux") and not any(
                flag in forwarded for flag in diagnostics):
            config = None
            for index, argument in enumerate(forwarded):
                if argument == "--config" and index + 1 < len(forwarded):
                    config = forwarded[index + 1]
                elif argument.startswith("--config="):
                    config = argument.split("=", 1)[1]
            readiness = [str(python), "-B", str(ROOT / ".harness/clarify.py"),
                         "--root", str(ROOT), "--check-ready"]
            if config is not None:
                readiness += ["--config", config]
            checked = subprocess.run(readiness, cwd=str(ROOT), env=environment,
                                     stdin=subprocess.DEVNULL, timeout=30)
            if checked.returncode:
                return checked.returncode
            bash = shutil.which("bash", path=environment["PATH"])
            if not bash:
                raise ValueError("Linux work startup requires Bash; see docs/CROSS_PLATFORM.md.")
            environment["PYTHON_BIN"] = str(python)
            os.execve(bash, [bash, str(entry.with_name("start.sh"))] + forwarded, environment)
            return 0
        if sys.platform == "win32":
            # Windows execve starts another process; explicitly wait for its exit status.
            return subprocess.call(command, env=environment)
        os.execve(str(python), command, environment)
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        # Probe stderr may contain environment details; keep the diagnostic focused on setup.
        if isinstance(exc, subprocess.SubprocessError):
            message = "Could not start the project's Python 3.9 environment; recreate it on this OS."
        else:
            message = str(exc)
        print("Harness setup failed: " + message, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
