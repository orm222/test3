"""Run commands with external evidence; coordinate wrapper-managed harness stages."""
import datetime
import fcntl
import os
from pathlib import Path
import shlex
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]

def append(path, text):
    with path.open("a", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.write(text)
        stream.flush()
        fcntl.flock(stream, fcntl.LOCK_UN)

def main():
    args = sys.argv[1:]
    if not args:
        print("usage: run_logged.py COMMAND [ARGS...]", file=sys.stderr)
        return 64
    folder = Path(os.environ.get("HARNESS_LOG_ROOT", str(ROOT.parent / "harness-runtime" / ROOT.name))).expanduser().resolve()
    if folder == ROOT or ROOT in folder.parents:
        print("Evidence directory must be outside the project", file=sys.stderr)
        return 64
    try:
        folder.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex
        now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).isoformat()
        prefix = "\n## " + now + " / " + run_id + "\n\n~~~bash\n" + shlex.join(args) + "\n~~~\n"
        command_log = folder / "COMMANDS.md"
        error_log = folder / "ERRORS_AND_FIXES.md"
        append(command_log, prefix + "Started.\n")
        stage_lock = None
        if any(Path(arg).name == "run_harness.py" for arg in args):
            stage_lock = (folder / "harness-stage.lock").open("a+")
            try:
                fcntl.flock(stage_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                output, rc = "Another wrapper-managed harness stage is active. Retry after it finishes.", 75
            else:
                output, rc = execute(args)
        else:
            output, rc = execute(args)
        append(command_log, "\n~~~text\n" + output + "\nexit=" + str(rc) + "\n~~~\n")
        if rc:
            append(error_log, prefix + "\n~~~text\n" + output + "\nexit=" + str(rc) + "\n~~~\n原因・조치·재검증은 담당자가 확인한다. 예상 실패/답변 대기는 구분한다.\n")
        if stage_lock:
            stage_lock.close()
    except OSError as exc:
        print("Evidence recording failed: " + str(exc), file=sys.stderr)
        return 74
    print(output)
    print("exit=", rc, "| external evidence:", folder)
    return rc

def execute(args):
    try:
        result = subprocess.run(args, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, errors="replace")
        return result.stdout, result.returncode
    except OSError as exc:
        return str(exc), 127

if __name__ == "__main__":
    raise SystemExit(main())
