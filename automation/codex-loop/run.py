#!/usr/bin/env python3
"""Sequential Codex implementation/review runner. Python 3.9, Windows/Linux."""
import argparse
import contextlib
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

BASE = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
# Share the harness's Windows Job Object and Linux process-group cleanup.
_process_spec = importlib.util.spec_from_file_location(
    "codex_loop_processes", BASE.parents[1] / ".harness/verification.py")
_processes = importlib.util.module_from_spec(_process_spec)
_process_spec.loader.exec_module(_processes)
acceptance = _processes.acceptance
boundary = _processes.boundary
_report_spec = importlib.util.spec_from_file_location(
    "codex_loop_reports", BASE.parents[1] / ".harness/reports.py")
reports = importlib.util.module_from_spec(_report_spec)
_report_spec.loader.exec_module(reports)
if os.name == "nt":
    import msvcrt
else:
    import fcntl
DEFAULT_EXCLUDES = [".git", ".venv", "venv", "node_modules", "__pycache__",
                    ".pytest_cache", ".ruff_cache", ".mypy_cache", ".harness/work"]
STATUSES = {"PASS", "FAIL", "UNVERIFIED"}


class StopRun(Exception):
    """An incomplete run: never interpreted as successful verification."""


def require(condition, message):
    if not condition:
        raise StopRun(message)


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON key: " + key)
        result[key] = value
    return result


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"),
                          object_pairs_hook=unique_pairs,
                          parse_constant=lambda x: (_ for _ in ()).throw(StopRun("Invalid number: " + x)))
    except (OSError, ValueError) as exc:
        raise StopRun("Cannot read JSON {}: {}".format(path, exc)) from exc


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(path))


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value)


def shell_command(argv, windows=None):
    """Literal check command for the reviewer's native shell, never shell=True."""
    windows = os.name == "nt" if windows is None else windows
    if windows:
        return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
    return shlex.join(argv)


def resolve_codex_command(argv):
    """Bypass npm's Windows batch shim; CMD would reinterpret literal arguments."""
    if os.name != "nt":
        return argv[:]
    executable = shutil.which(argv[0])
    if not executable and (Path(argv[0]).is_absolute() or Path(argv[0]).parent != Path(".")):
        path = Path(argv[0])
        executable = shutil.which(path.name, path=str(path.parent))
    require(executable is not None, "Codex executable not found: " + argv[0])
    if Path(executable).suffix.lower() in (".cmd", ".bat"):
        entry = Path(executable).parent / "node_modules/@openai/codex/bin/codex.js"
        node = shutil.which("node.exe")
        require(Path(executable).stem.lower() == "codex" and node and entry.is_file(),
                "Use native codex.exe or npm Codex with node.exe; arbitrary batch shims are unsupported")
        return [node, str(entry)] + argv[1:]
    return [executable] + argv[1:]


def inside(path, directory):
    try:
        path.resolve().relative_to(directory.resolve())
        return True
    except ValueError:
        return False


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def report_file_hashes(project):
    """Seal the exact validated outputs/state/history between controller updates."""
    names = set(reports.MANAGED) | {reports.STATE}
    names.update(path.relative_to(project).as_posix()
                 for path in (project / reports.DIRECTORY / "events").glob("*.json"))
    result = {}
    for name in sorted(names):
        path = project / name
        if not path.exists() and not path.is_symlink():
            continue
        require(path.is_file() and not path.is_symlink() and path.resolve() == path.absolute(),
                "Managed reporting path changed or redirected: " + name)
        result[name] = file_hash(path)
    return result


def snapshot(project, excludes=DEFAULT_EXCLUDES, _validated_managed=None):
    """Hash all files, including untracked/ignored files except explicit exclusions.

    Symlinks and special files fail closed; do not silently ignore external inputs.
    A bare exclusion names a directory anywhere; a path names a root-relative directory.
    """
    digest = hashlib.sha256()
    count = 0
    # A phase guard already sealed these bytes; avoid re-rendering the whole history
    # on every process poll. Other callers still perform the full ownership check.
    managed = reports.validate_managed(project) if _validated_managed is None else _validated_managed
    require(set(managed) <= set(reports.MANAGED), "Only exact controller report paths may be excluded")
    for root, dirs, files in os.walk(str(project), followlinks=False):
        relative = Path(root).relative_to(project)
        kept = []
        for name in sorted(dirs):
            rel = (relative / name).as_posix()
            if name in excludes or rel in excludes:
                continue
            require(not (Path(root) / name).is_symlink(), "Symlink directory is not supported: " + rel)
            kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            path = Path(root) / name
            rel = (relative / name).as_posix()
            mode = path.lstat().st_mode
            require(stat.S_ISREG(mode), "Symlink/special file is not supported: " + rel)
            if rel in managed:
                continue
            item = [rel, stat.S_IMODE(mode), file_hash(path)]
            digest.update(json.dumps(item, ensure_ascii=False).encode("utf-8") + b"\n")
            count += 1
    require(count > 0, "Project has no fingerprinted files")
    return digest.hexdigest()


def load_settings(config_path):
    config_path = Path(config_path).expanduser().resolve()
    c = read_json(config_path)
    require(isinstance(c, dict), "Config must be an object")
    for key in ("project", "spec", "tasks_file", "builder_model", "reviewer_model"):
        require(nonempty(c.get(key)), "Set config field: " + key)
    project = (config_path.parent / Path(c["project"]).expanduser()).resolve()
    require(project.is_dir(), "Project directory not found: " + str(project))
    reports.precheck(project)
    c["review_criteria"] = reports.load_criteria()
    c.setdefault("review_not_applicable", {})
    reports.validate_not_applicable(c["review_not_applicable"], c["review_criteria"])
    spec = (project / c["spec"]).resolve()
    require(inside(spec, project) and spec.is_file(), "spec must be an existing file inside project")
    try:
        c["boundary"] = boundary.require_ready(project, c["spec"])
    except ValueError as exc:
        raise StopRun(str(exc)) from exc
    for key in ("builder_model", "reviewer_model"):
        require(not c[key].startswith("SET_"), "Set the actual CLI model ID: " + key)
    tasks_path = (config_path.parent / c["tasks_file"]).resolve()
    tasks = read_json(tasks_path)
    require(isinstance(tasks, list) and tasks, "tasks_file must be a nonempty array")
    seen = set()
    for task in tasks:
        require(isinstance(task, dict) and identifier(task.get("id")), "Invalid task ID")
        require(task["id"].lower() != "final", "Task ID FINAL/final is reserved for combined verification")
        require(task["id"] not in seen, "Duplicate task ID")
        seen.add(task["id"])
        require(nonempty(task.get("goal")), "Every task needs a goal")
        checks = task.get("checks")
        requirements = task.get("requirements")
        require(isinstance(checks, list) and checks, "Every task needs registered checks")
        require(isinstance(requirements, list) and requirements, "Every task needs requirements")
        check_ids = set()
        for check in checks:
            require(isinstance(check, dict) and identifier(check.get("id")), "Invalid check ID")
            require(check["id"] not in check_ids, "Duplicate check ID")
            check_ids.add(check["id"])
            argv = check.get("argv")
            require(isinstance(argv, list) and argv and all(nonempty(x) for x in argv), "Check argv must be a string array")
            require(all("\0" not in arg for arg in argv), "Check argv cannot contain NUL characters")
            require(os.name != "nt" or all('"' not in arg for arg in argv),
                    "Windows check arguments cannot contain literal double quotes; put that logic in a check script file (PowerShell 5/7 quote handling differs)")
            shell = re.split(r"[/\\]", argv[0])[-1].lower()
            require(not (shell in ("sh", "bash", "zsh") and any(x in ("-c", "-lc") for x in argv[1:]))
                    and not (shell in ("powershell", "powershell.exe", "pwsh", "pwsh.exe", "cmd", "cmd.exe")
                             and any(x.lower() in ("-command", "-c", "-encodedcommand", "-ec", "/c", "/k") for x in argv[1:])),
                    "Register a check script file instead of an inline shell command")
            require(nonempty(check.get("success_regex")), "Each check needs success_regex to reject empty test runs")
            check["argv"] = [sys.executable if arg == "{python}" else arg for arg in argv]
            check["shell_command"] = shell_command(check["argv"])
            check["shell"] = "PowerShell" if os.name == "nt" else "POSIX shell"
            try:
                re.compile(check["success_regex"])
            except re.error as exc:
                raise StopRun("Invalid success_regex: " + str(exc)) from exc
        req_ids = set()
        used_checks = set()
        for req in requirements:
            require(isinstance(req, dict) and identifier(req.get("id")), "Invalid requirement ID")
            require(req["id"] not in req_ids and nonempty(req.get("text")), "Duplicate/empty requirement")
            req_ids.add(req["id"])
            ids = req.get("check_ids")
            require(isinstance(ids, list) and ids and all(isinstance(x, str) for x in ids), "Missing requirement check_ids")
            require(len(ids) == len(set(ids)) and set(ids) <= check_ids, "Unknown/duplicate check_ids")
            used_checks.update(ids)
        require(used_checks == check_ids, "Every registered check must map to a requirement")
    try:
        c["acceptance"] = acceptance.load_contract(project)
        acceptance.validate_boundary(c["acceptance"], c["boundary"])
        for task in tasks:
            acceptance.validate_registration(task["checks"], c["acceptance"], "argv", "id")
            for check in task["checks"]:
                check["_acceptance"] = acceptance.is_checker(check["argv"], c["acceptance"])
    except ValueError as exc:
        raise StopRun(str(exc)) from exc
    for key, default in (("max_repairs", 3), ("stage_timeout_seconds", 900), ("total_timeout_seconds", 7200)):
        c.setdefault(key, default)
        require(type(c[key]) is int and c[key] >= (0 if key == "max_repairs" else 1), "Invalid " + key)
    c.setdefault("codex_command", ["codex"])
    require(isinstance(c["codex_command"], list) and c["codex_command"] and
            all(nonempty(x) for x in c["codex_command"]), "codex_command must be an argv array")
    c.setdefault("exclude_dirs", DEFAULT_EXCLUDES[:])
    require(isinstance(c["exclude_dirs"], list) and all(nonempty(x) for x in c["exclude_dirs"]), "Invalid exclude_dirs")
    for exclusion in c["exclude_dirs"]:
        require(not Path(exclusion).anchor and ".." not in Path(exclusion).parts and
                exclusion not in (".", "", "/") and not any(x in exclusion for x in "*?[]"), "Unsafe exclusion: " + exclusion)
    if ".harness/work" not in c["exclude_dirs"]:
        c["exclude_dirs"].append(".harness/work")
    c.setdefault("protected_paths", [])
    require(isinstance(c["protected_paths"], list) and all(nonempty(x) for x in c["protected_paths"]), "Invalid protected_paths")
    protected = ([config_path, tasks_path, spec]
                 + [Path(p) for p in c["acceptance"]["protected_files"]]
                 + list(c["boundary"]["protected_files"]))
    for name in c["protected_paths"]:
        raw_path = project / name
        require(not raw_path.is_symlink(), "Protected symlink unsupported")
        path = raw_path.resolve()
        require(inside(path, project) and path.exists(), "Missing/outside protected path: " + name)
        require(not path.is_symlink(), "Protected symlink unsupported")
        protected.extend(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else protected.append(path)
    # Protect the running harness and its prompts from accidental self-modification.
    protected += [p for p in BASE.rglob("*") if p.suffix in (".py", ".md", ".json", ".sh") and "__pycache__" not in p.parts]
    protected.append(BASE.parents[1] / ".harness/verification.py")
    protected.append(BASE.parents[1] / ".harness/acceptance.py")
    protected.append(BASE.parents[1] / ".harness/check_unittest.py")
    protected.append(BASE.parents[1] / ".harness/boundary.py")
    protected.append(BASE.parents[1] / ".harness/clarify.py")
    protected.append(BASE.parents[1] / ".harness/clarify_prompt.md")
    protected.append(BASE.parents[1] / ".harness/clarify.schema.json")
    protected.append(BASE.parents[1] / ".harness/reports.py")
    protected.append(BASE.parents[1] / ".harness/review_criteria.json")
    c["project_path"], c["spec_path"] = project, spec
    c["config_path"], c["tasks_path"], c["tasks"] = config_path, tasks_path, tasks
    c["protected"] = {str(p): file_hash(p) for p in protected}
    run_root = Path(c.get("run_root", str(Path.home() / ".local/state/codex-loop"))).expanduser()
    c["run_root"] = (config_path.parent / run_root).resolve()
    require(not inside(c["run_root"], project), "run_root must be outside the target project")
    require(not inside(c["run_root"], BASE), "run_root must be outside the harness directory")
    return c


def verify_protected(c):
    for path, expected in c["protected"].items():
        require(Path(path).is_file() and file_hash(path) == expected, "Protected file changed: " + path)


@contextlib.contextmanager
def project_lock(project):
    key = hashlib.sha256(os.path.normcase(str(project.resolve())).encode()).hexdigest()
    owner = str(os.getuid()) if hasattr(os, "getuid") else "user"
    path = Path(tempfile.gettempdir()) / ("codex-loop-{}-{}.lock".format(owner, key))
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise StopRun("Another runner is using this project") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
    # Never unlink an advisory lock file: that permits a second inode and two owners.


def stop_group(process):
    _processes.stop_process(process)


def execute(argv, cwd, prefix, timeout, prompt=None, guard=None):
    """Never shell=True. Stop the process group on timeout/cancellation AND normal exit."""
    prefix = Path(prefix)
    pycache = prefix.with_suffix(".pycache")
    pycache.mkdir(exist_ok=False)
    python_env = {"PYTHONDONTWRITEBYTECODE": "1", "PYTHONPYCACHEPREFIX": str(pycache.resolve())}
    environment = dict(os.environ, **python_env)
    environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
    environment["PYTHONIOENCODING"] = "utf-8"
    environment.pop("PYTHONHOME", None)
    environment.pop("VIRTUAL_ENV", None)
    if sys.prefix != sys.base_prefix:
        environment["VIRTUAL_ENV"] = sys.prefix
    stdin_path = prefix.with_suffix(".input.txt")
    stdin_path.write_text(prompt or "", encoding="utf-8")
    write_json(prefix.with_suffix(".command.json"), {"argv": argv, "cwd": str(cwd), "timeout": timeout, "python_environment": python_env})
    started = time.monotonic()
    with stdin_path.open("rb") as stdin, prefix.with_suffix(".stdout.log").open("wb") as stdout, prefix.with_suffix(".stderr.log").open("wb") as stderr:
        process = _processes.start_process(argv, cwd=str(cwd), stdin=stdin, stdout=stdout, stderr=stderr,
                                   start_new_session=os.name != "nt", env=environment,
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
        try:
            while process.poll() is None:
                if time.monotonic() - started >= timeout:
                    raise StopRun("Timeout: " + str(prefix))
                if guard:
                    guard()
                try:
                    process.wait(timeout=min(0.25, max(0.01, timeout - (time.monotonic() - started))))
                except subprocess.TimeoutExpired:
                    pass
            code = process.returncode
        finally:
            stop_group(process)
    if guard:
        guard()
    write_json(prefix.with_suffix(".exit.json"), {"exit_code": code, "seconds": time.monotonic() - started})
    return code


def command_events(path):
    """Only completed Codex command_execution events count as reviewer execution evidence."""
    result = {}
    completed = False
    with Path(path).open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            try:
                event = json.loads(line, object_pairs_hook=unique_pairs)
            except ValueError as exc:
                raise StopRun("Invalid Codex JSONL event") from exc
            require(isinstance(event, dict), "Invalid event object")
            require(event.get("type") not in ("turn.failed", "error"), "Codex reported a failed turn")
            if event.get("type") == "turn.completed":
                completed = True
            item = event.get("item", {})
            if event.get("type") == "item.completed" and isinstance(item, dict) and item.get("type") == "command_execution":
                require(nonempty(item.get("id")) and item["id"] not in result, "Duplicate/missing command event ID")
                result[item["id"]] = item
    require(completed, "Codex did not emit turn.completed")
    return result


def canonical_command(command):
    if not isinstance(command, str):
        return ""
    # Only unwrap a known shell invocation; preserve the complete script verbatim.
    # Matching an arbitrary command prefix would accept forged appended commands.
    ps = re.fullmatch(r'''\s*(?:"[^"\n]*[\\/](?:powershell|pwsh)\.exe"|'[^'\n]*[\\/](?:powershell|pwsh)\.exe'|(?:[^\s"']*[\\/])?(?:powershell|pwsh)(?:\.exe)?)\s+(?:(?:-NoProfile|-NonInteractive|-NoLogo)\s+)*-Command\s+(.+)''',
                      command, flags=re.IGNORECASE | re.DOTALL)
    if ps:
        script = ps.group(1).strip()
        if script.startswith('"') and script.endswith('"'):
            # Windows command-line quoting generated by subprocess.list2cmdline.
            inner = script[1:-1]
            if '\\"' not in inner and '"' not in inner:
                return inner
        if re.fullmatch(r"'(?:[^']|'')*'", script, flags=re.DOTALL):
            return script[1:-1].replace("''", "'")
        return script
    try:
        parts = shlex.split(command)
    except ValueError:
        return command.strip()
    if len(parts) == 3 and Path(parts[0]).name in ("bash", "sh", "zsh") and parts[1] in ("-c", "-lc"):
        return canonical_command(parts[2])
    return command.strip()


def validate_report(report, expected, task, events, contract=None):
    require(isinstance(report, dict), "Review must be an object")
    for key, value in expected.items():
        require(type(report.get(key)) is type(value) and report[key] == value, "Review identity mismatch: " + key)
    require(report.get("verdict") in STATUSES, "Unknown review verdict")
    require(nonempty(report.get("summary")), "Empty review summary")
    require(isinstance(report.get("repair_plan"), list) and all(nonempty(x) for x in report["repair_plan"]), "Invalid repair_plan")
    rows = report.get("checks")
    require(isinstance(rows, list) and rows, "Review has no checks")
    required = {req["id"] for req in task["requirements"]}
    req_map = {req["id"]: req for req in task["requirements"]}
    check_map = {check["id"]: check for check in task["checks"]}
    seen, statuses = set(), set()
    for row in rows:
        require(isinstance(row, dict), "Invalid review row")
        rid = row.get("requirement_id")
        require(isinstance(rid, str) and rid in required and rid not in seen, "Missing/duplicate/unknown requirement ID")
        seen.add(rid)
        status = row.get("status")
        require(status in STATUSES, "Invalid requirement status")
        statuses.add(status)
        require(all(nonempty(row.get(k)) for k in ("expected", "actual")), "Missing expected/actual result")
        commands = row.get("verification_commands")
        require(isinstance(commands, list) and all(nonempty(x) for x in commands), "Invalid verification_commands")
        matched = [[event for event in events.values()
                    if canonical_command(event.get("command", "")) == canonical_command(cmd)] for cmd in commands]
        require(all(matched), "Evidence does not match actual Codex commands")
        if status == "PASS":
            require(commands and all(any(type(e.get("exit_code")) is int and e["exit_code"] == 0
                                         and e.get("status") == "completed" for e in group) for group in matched),
                    "PASS has no successful executed evidence")
            for cid in req_map[rid]["check_ids"]:
                check = check_map[cid]
                registered = shell_command(check["argv"])
                require(any(canonical_command(cmd) == registered for cmd in commands),
                        "PASS omits registered verification command: " + cid)
                proof = [e for e in events.values() if canonical_command(e.get("command", "")) == registered]
                require(any(type(e.get("exit_code")) is int and e["exit_code"] == 0 and
                            e.get("status") == "completed" and
                            isinstance(e.get("aggregated_output"), str) and
                            re.search(check["success_regex"], e["aggregated_output"]) for e in proof),
                        "Reviewer did not independently pass registered check: " + cid)
                if check.get("_acceptance"):
                    accepted = False
                    for event in proof:
                        if (type(event.get("exit_code")) is not int or event["exit_code"] != 0
                                or event.get("status") != "completed"
                                or not isinstance(event.get("aggregated_output"), str)
                                or not re.search(check["success_regex"], event["aggregated_output"])):
                            continue
                        try:
                            acceptance.validate_output(event.get("aggregated_output", ""), contract, mixed=True)
                            accepted = True
                        except (ValueError, TypeError):
                            pass
                    require(accepted, "Reviewer acceptance evidence is missing required executed IDs")
    require(seen == required, "Required checks omitted")
    verdict = "UNVERIFIED" if "UNVERIFIED" in statuses else "FAIL" if "FAIL" in statuses else "PASS"
    require(report["verdict"] == verdict, "Overall verdict contradicts requirement results")
    if verdict == "FAIL":
        require(report["repair_plan"], "FAIL requires a repair plan")
    return verdict


class Runner:
    def __init__(self, c):
        self.c = c
        self.run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
        self.directory = c["run_root"] / self.run_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.deadline = time.monotonic() + c["total_timeout_seconds"]
        self.state = {"run_id": self.run_id, "project": str(c["project_path"]), "status": "STARTING", "completed_tasks": []}
        self.last_acceptance_hash = None
        self.report_token = None
        self.report_hashes = None
        self.current_hash = None

    def status(self, status, report_data=None, **fields):
        self.state.update(status=status, **fields)
        write_json(self.directory / "state.json", self.state)
        text = "{} {}".format(status, json.dumps(fields, ensure_ascii=False))
        print(text, flush=True)
        with (self.directory / "progress.log").open("a", encoding="utf-8") as stream:
            stream.write(text + "\n")
        if self.report_token is not None:
            self.record(status, dict(fields, **(report_data or {})))

    def report_guard(self):
        if self.report_token is not None:
            require(report_file_hashes(self.c["project_path"]) == self.report_hashes,
                    "Managed reporting history changed during worker execution")

    def record(self, status, data):
        self.report_guard()
        reports.append_event(self.c["project_path"], {
            "kind": "loop", "status": status, "run_id": self.run_id,
            "task_id": self.state.get("task_id"), "round": self.state.get("round"),
            "source_hash": self.current_hash, "scope": "loop", "data": data})
        self.report_token = reports.guard(self.c["project_path"])
        self.report_hashes = report_file_hashes(self.c["project_path"])

    def stop(self, reason):
        try:
            self.status("STOPPED", report_data={"review": None, "score_error": reason,
                        "limitations": reason, "next_steps": "Resolve the recorded failure, then start a new run."}, reason=reason)
        except (StopRun, OSError, ValueError, TypeError) as exc:
            # Keep the damaged evidence intact; only the external run record may change.
            self.state.update(status="STOPPED", reason=reason, reporting_error=str(exc))
            write_json(self.directory / "state.json", self.state)

    def budget(self):
        remaining = self.deadline - time.monotonic()
        require(remaining > 0, "Total time budget exceeded")
        return min(self.c["stage_timeout_seconds"], remaining)

    def source_guard(self, expected):
        self.report_guard()
        verify_protected(self.c)
        require(snapshot(self.c["project_path"], self.c["exclude_dirs"], reports.MANAGED) == expected,
                "Source changed during verification; results invalidated")

    def codex(self, role, folder, prompt):
        c = self.c
        prefix = folder / role
        answer = folder / (role + ".answer.json" if role == "reviewer" else role + ".answer.txt")
        argv = resolve_codex_command(c["codex_command"]) + ["--ask-for-approval", "never", "exec", "--cd", str(c["project_path"]),
                "--model", c[role + "_model"], "--sandbox", "read-only" if role == "reviewer" else "workspace-write",
                "--skip-git-repo-check", "--json", "--output-last-message", str(answer)]
        argv += ["--config", 'shell_environment_policy.set.PYTHONDONTWRITEBYTECODE="1"',
                 "--config", "shell_environment_policy.set.PYTHONPYCACHEPREFIX=" + json.dumps(str(prefix.with_suffix(".pycache")))]
        if role == "reviewer":
            argv += ["--output-schema", str(BASE / "review.schema.json")]
        argv.append("-")
        def builder_guard():
            self.report_guard()
            verify_protected(c)
        guard = (lambda: self.source_guard(self.current_hash)) if role == "reviewer" else builder_guard
        code = execute(argv, c["project_path"], prefix, self.budget(), prompt=prompt, guard=guard)
        require(code == 0, "{} CLI failed (exit {}); see {}".format(role, code, prefix))
        events = command_events(prefix.with_suffix(".stdout.log"))
        require(answer.is_file() and answer.stat().st_size > 0, "Codex final answer is missing")
        return answer, events

    def run_checks(self, task, folder):
        results = []
        for check in task["checks"]:
            prefix = folder / ("check-" + check["id"])
            self.status("CHECKING", report_data={"checks": [{"id": check["id"], "status": "RUNNING",
                "command": check["argv"], "exit_code": None, "evidence_refs": []}]}, check_id=check["id"])
            code = execute(check["argv"], self.c["project_path"], prefix, self.budget(),
                           guard=lambda: self.source_guard(self.current_hash))
            stdout = prefix.with_suffix(".stdout.log").read_text(encoding="utf-8", errors="replace")
            stderr = prefix.with_suffix(".stderr.log").read_text(encoding="utf-8", errors="replace")
            matched = bool(re.search(check["success_regex"], stdout + "\n" + stderr))
            acceptance_error = None
            if check.get("_acceptance") and code == 0:
                try:
                    acceptance.validate_output(stdout, self.c["acceptance"])
                except ValueError as exc:
                    matched = False
                    acceptance_error = str(exc)
            results.append({"id": check["id"], "argv": check["argv"], "exit_code": code,
                            "success_pattern_matched": matched, "passed": code == 0 and matched,
                            "acceptance_error": acceptance_error,
                            "stdout": str(prefix.with_suffix(".stdout.log")), "stderr": str(prefix.with_suffix(".stderr.log")),
                            "stdout_sha256": file_hash(prefix.with_suffix(".stdout.log")),
                            "stderr_sha256": file_hash(prefix.with_suffix(".stderr.log"))})
            self.record("CHECKED", {"checks": [{"id": check["id"],
                "status": "PASS" if code == 0 and matched else "FAIL", "command": check["argv"],
                "exit_code": code, "evidence_refs": [str(prefix.with_suffix(".stdout.log")),
                                                        str(prefix.with_suffix(".stderr.log"))]}]})
        write_json(folder / "registered-checks.json", results)
        self.record("CHECKS_FINISHED", {"checks": [{"id": row["id"], "status": "PASS" if row["passed"] else "FAIL",
            "command": row["argv"], "exit_code": row["exit_code"], "evidence_refs": [row["stdout"], row["stderr"]]}
            for row in results]})
        return results

    def run(self):
        c = self.c
        try:
            with project_lock(c["project_path"]):
                reports.initialize(c["project_path"])
                self.report_token = reports.guard(c["project_path"])
                self.report_hashes = report_file_hashes(c["project_path"])
                self.current_hash = snapshot(c["project_path"], c["exclude_dirs"])
                self.status("RUNNING", report_data={"review": None, "constraints": {
                    "required_test_ids": c["acceptance"]["required_test_ids"],
                    "review_not_applicable": c["review_not_applicable"],
                    "max_repairs": c["max_repairs"], "total_timeout_seconds": c["total_timeout_seconds"]}},
                    run_directory=str(self.directory))
                for task in c["tasks"]:
                    self.perform_task(task, task["id"], initial_build=True)
                # Later tasks can regress earlier ones: rerun ALL checks and obtain a fresh global review.
                self.final_review()
                self.source_guard(self.current_hash)
                require(self.last_acceptance_hash == self.current_hash,
                        "Final source has no common acceptance check and independent review")
                self.status("COMPLETED", message="All registered checks and required review items passed on the final source.")
                return 0
        except KeyboardInterrupt:
            self.stop("Interrupted; no previous PASS is reused on restart")
            return 130
        except (StopRun, OSError, ValueError, TypeError) as exc:
            self.stop(str(exc))
            return 2

    def perform_task(self, task, label, initial_build):
        c = self.c
        feedback, previous_failure, repeat_count = None, None, 0
        for round_number in range(c["max_repairs"] + 1):
            self.budget()
            self.report_guard()
            verify_protected(c)
            folder = self.directory / "{}-{:02d}".format(label, round_number)
            folder.mkdir()
            payload = {"run_id": self.run_id, "task_id": task["id"], "round": round_number,
                       "project": str(c["project_path"]), "spec": str(c["spec_path"]), "task": task,
                       "protected_paths": list(c["protected"]) + [str(c["project_path"] / name)
                            for name in (*reports.MANAGED, reports.DIRECTORY)], "previous_review": feedback,
                       "boundary_context": c["boundary"]["context"],
                       "boundary_tests": c["acceptance"]["boundary_tests"],
                       "review_criteria": c["review_criteria"],
                       "review_not_applicable": c["review_not_applicable"],
                       "prior_progress": (c["project_path"] / "docs/progress.md").read_text(encoding="utf-8")}
            payload["prior_api_review"] = reports.current_api_evidence(c["project_path"])["context"]
            self.status("IMPLEMENTING" if round_number == 0 and initial_build else "REPAIRING" if round_number else "FINAL_REVIEW",
                        report_data={"review": None, "goal": task["goal"], "boundary_context": c["boundary"]["context"],
                                     "traceability": c["acceptance"]["boundary_tests"],
                                     "next_steps": "Implement and run the registered checks, then obtain independent review."},
                        task_id=task["id"], round=round_number)
            if initial_build or round_number:
                prompt = (BASE / "prompts/sol.md").read_text(encoding="utf-8") + "\n\nCONTEXT_JSON\n" + json.dumps(payload, ensure_ascii=False)
                builder_answer, _ = self.codex("builder", folder, prompt)
                self.current_hash = snapshot(c["project_path"], c["exclude_dirs"])
                self.record("IMPLEMENTED", {"builder_report": "Builder self-report (not execution evidence):\n"
                            + builder_answer.read_text(encoding="utf-8", errors="replace")})
            verify_protected(c)
            self.current_hash = snapshot(c["project_path"], c["exclude_dirs"])
            checks = self.run_checks(task, folder)
            payload.update(source_hash=self.current_hash, registered_checks=checks,
                           evidence_catalog=self.score_evidence(checks, {}, folder))
            prior_api = reports.current_api_evidence(c["project_path"])
            payload["prior_api_review"] = prior_api["context"]
            payload["evidence_catalog"].update(prior_api["evidence"])
            expected = {k: payload[k] for k in ("run_id", "task_id", "round", "source_hash")}
            write_json(folder / "target.json", expected)
            self.status("REVIEWING", report_data={"review": None}, source_hash=self.current_hash)
            # These controller-owned documents change when the review is recorded.
            # Preserve exactly what the read-only reviewer sees as stable evidence.
            for relative in reports.MANAGED:
                saved = folder / ("reviewed-" + Path(relative).name)
                shutil.copyfile(c["project_path"] / relative, saved)
                payload["evidence_catalog"]["file:" + relative] = reports.file_evidence(c["project_path"], saved)
            prompt = (BASE / "prompts/terra.md").read_text(encoding="utf-8") + "\n\nCONTEXT_JSON\n" + json.dumps(payload, ensure_ascii=False)
            answer, events = self.codex("reviewer", folder, prompt)
            self.source_guard(self.current_hash)
            report = read_json(answer)
            verdict = validate_report(report, expected, task, events, c["acceptance"])
            catalog = self.score_evidence(checks, events, folder)
            catalog.update(payload["evidence_catalog"])
            for row in report.get("criterion_scores", []) or []:
                if not isinstance(row, dict) or not isinstance(row.get("evidence_refs"), list):
                    continue
                for reference in row["evidence_refs"]:
                    if isinstance(reference, str) and reference.startswith("file:") and reference not in catalog:
                        path = boundary.project_path(c["project_path"], reference[5:])
                        catalog[reference] = reports.file_evidence(c["project_path"], path)
            score = reports.score_review(c["project_path"], report.get("criterion_scores"),
                                         catalog, c["review_not_applicable"])
            self.source_guard(self.current_hash)
            self.record("REVIEWED", {"review": score})
            if verdict == "UNVERIFIED":
                raise StopRun("Required items remain UNVERIFIED; see " + str(answer))
            if verdict == "PASS" and all(x["passed"] for x in checks):
                self.budget()
                if any(check.get("_acceptance") for check in task["checks"]):
                    self.last_acceptance_hash = self.current_hash
                self.state["completed_tasks"].append({"id": task["id"], "source_hash": self.current_hash, "round": round_number})
                self.status("TASK_PASSED")
                return
            failure = (self.current_hash, tuple((x["id"], x["exit_code"], x["success_pattern_matched"]) for x in checks), verdict)
            repeat_count = repeat_count + 1 if failure == previous_failure else 1
            require(repeat_count < 3, "No progress: same source and failure repeated three times")
            previous_failure = failure
            feedback = {"review": report, "registered_checks": checks,
                        "instruction": "Reproduce failures and fix implementation; preserve requirements and acceptance tests."}
            write_json(folder / "repair-feedback.json", feedback)
            self.status("REVIEW_FAILED", report_data={"review": score, "next_steps": report["repair_plan"]})
            require(round_number < c["max_repairs"], "Repair limit reached; task remains incomplete")

    def score_evidence(self, checks, events, folder):
        catalog = {}
        for check in checks:
            catalog["check:" + check["id"]] = reports.file_evidence(
                self.c["project_path"], check["stdout"], kind="check",
                status="PASS" if check["passed"] else "FAIL", exit_code=check["exit_code"], command=check["argv"])
        for event_id, event in events.items():
            completed = type(event.get("exit_code")) is int and event.get("status") == "completed"
            evidence = reports.file_evidence(
                self.c["project_path"], folder / "reviewer.stdout.log", kind="check",
                status=("PASS" if event["exit_code"] == 0 else "FAIL") if completed else "UNVERIFIED",
                exit_code=event.get("exit_code"), command=canonical_command(event.get("command", "")))
            catalog["reviewer:" + event_id] = evidence
            catalog["command:" + canonical_command(event.get("command", ""))] = evidence
        return catalog

    def final_review(self):
        # ponytail: one final combined review instead of maintaining a dependency graph.
        if len(self.c["tasks"]) == 1 and self.last_acceptance_hash == self.current_hash:
            self.source_guard(self.current_hash)
            return
        combined = {"id": "FINAL", "goal": "Recheck every task against the final source", "requirements": [], "checks": []}
        for t, task in enumerate(self.c["tasks"], 1):
            ids = {check["id"]: "F{:03d}_C{:03d}".format(t, n) for n, check in enumerate(task["checks"], 1)}
            for check in task["checks"]:
                combined["checks"].append(dict(check, id=ids[check["id"]]))
            for n, req in enumerate(task["requirements"], 1):
                combined["requirements"].append(dict(req, id="F{:03d}_R{:03d}".format(t, n),
                    text="[" + task["id"] + "/" + req["id"] + "] " + req["text"], check_ids=[ids[x] for x in req["check_ids"]]))
        common = next((check for check in combined["checks"] if check.get("_acceptance")), None)
        if common is None:
            argv = acceptance.resolved_argv(self.c["acceptance"]["checker_argv"])
            common = dict(id="GLOBAL_ACCEPTANCE", argv=argv, _acceptance=True,
                          success_regex=acceptance.SUCCESS_REGEX, shell_command=shell_command(argv),
                          shell="PowerShell" if os.name == "nt" else "POSIX shell")
            combined["checks"].append(common)
        combined["requirements"].append(dict(id="GLOBAL_ACCEPTANCE", text="All required project acceptance IDs execute and pass on the final source",
                                            check_ids=[common["id"]]))
        self.perform_task(combined, "final", initial_build=False)


def preflight(c, probe_models=False):
    verify_protected(c)
    initial_hash = snapshot(c["project_path"], c["exclude_dirs"])
    reporting = report_file_hashes(c["project_path"])
    managed = reports.MANAGED if reports.STATE in reporting else ()
    directory = c["run_root"] / ("preflight-" + uuid.uuid4().hex[:12])
    directory.mkdir(parents=True, exist_ok=False)
    print("Preflight logs: " + str(directory), flush=True)

    def guard():
        verify_protected(c)
        require(report_file_hashes(c["project_path"]) == reporting, "Managed reporting history changed during preflight")
        require(snapshot(c["project_path"], c["exclude_dirs"], managed) == initial_hash,
                "Source changed during preflight")

    codex = resolve_codex_command(c["codex_command"])
    code = execute(codex + ["exec", "--help"], c["project_path"], directory / "help", 30, guard=guard)
    require(code == 0, "codex exec --help failed; see " + str(directory))
    help_text = (directory / "help.stdout.log").read_text(encoding="utf-8", errors="replace")
    for flag in ("--output-schema", "--output-last-message", "--json", "--model", "--sandbox", "--skip-git-repo-check"):
        require(flag in help_text, "Installed Codex CLI lacks: " + flag)
    code = execute(codex + ["login", "status"], c["project_path"], directory / "login", 30, guard=guard)
    require(code == 0, "Codex login check failed; run codex login; logs: " + str(directory))
    if probe_models:
        for role in ("builder", "reviewer"):
            argv = codex + ["--ask-for-approval", "never", "exec", "--model", c[role + "_model"],
                    "--sandbox", "read-only", "--skip-git-repo-check", "--json", "-"]
            code = execute(argv, c["project_path"], directory / role, min(120, c["stage_timeout_seconds"]),
                           prompt="Do not call tools. Reply with OK only.", guard=guard)
            require(code == 0, role + " model probe failed; verify model ID/account access; logs: " + str(directory))
            command_events(directory / (role + ".stdout.log"))
    print("Preflight passed. Model access " + ("probed." if probe_models else "not probed; use --probe-models."))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--probe-models", action="store_true", help="Make two short real CLI calls during preflight")
    args = parser.parse_args(argv)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        require(sys.version_info[:2] == (3, 9), "Run this loop with Python 3.9 (use run_harness.py loop)")
        c = load_settings(args.config)
        if args.preflight or args.probe_models:
            preflight(c, args.probe_models)
            return 0
        preflight(c)
        return Runner(c).run()
    except (StopRun, OSError, ValueError, TypeError) as exc:
        print("STOPPED: " + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
