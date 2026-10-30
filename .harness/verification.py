"""Local check receipts, adapted from KTY; Python standard library only."""

import hashlib
import contextlib
import importlib.util
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid

_acceptance_spec = importlib.util.spec_from_file_location("harness_acceptance", Path(__file__).with_name("acceptance.py"))
acceptance = importlib.util.module_from_spec(_acceptance_spec)
_acceptance_spec.loader.exec_module(acceptance)
_boundary_spec = importlib.util.spec_from_file_location("harness_boundary", Path(__file__).with_name("boundary.py"))
boundary = importlib.util.module_from_spec(_boundary_spec)
_boundary_spec.loader.exec_module(boundary)
_reports_spec = importlib.util.spec_from_file_location("harness_reports", Path(__file__).with_name("reports.py"))
reports = importlib.util.module_from_spec(_reports_spec)
_reports_spec.loader.exec_module(reports)

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL

    class _JobBasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong), ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t), ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD), ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class _JobExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _JobBasicLimits), ("IoInfo", ctypes.c_ulonglong * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


STATE = ".harness/work"
EXCLUDED = ("work/", STATE + "/")
RESULTS_START = b"<!-- harness:results:start -->"
RESULTS_END = b"<!-- harness:results:end -->"


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def configuration(root):
    data = read_json(root / ".harness/checks.json")
    if not isinstance(data, dict):
        raise ValueError("checks.json은 JSON 객체여야 합니다.")
    inputs = data.get("inputs", [])
    if not isinstance(inputs, list) or any(not isinstance(p, str) or not p for p in inputs):
        raise ValueError("inputs는 프로젝트 상대 파일·폴더 경로 배열이어야 합니다.")
    return data


def ready_boundary(root):
    config = Path(root) / "automation/codex-loop/config.json"
    selected = None
    if config.exists():
        value = acceptance.read(config)
        if not isinstance(value, dict) or not isinstance(value.get("spec"), str) or not value["spec"].strip():
            raise ValueError("Loop configuration needs a specification path before boundary verification")
        selected = value["spec"]
    return boundary.require_ready(root, selected)


def load_checks(root):
    ready = ready_boundary(root)
    checks = configuration(root).get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError(".harness/checks.json에 실제 검증 명령을 하나 이상 등록하세요.")
    names = set()
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get("name"), str):
            raise ValueError("각 check에는 name 문자열이 필요합니다.")
        if not check["name"].strip() or check["name"] in names:
            raise ValueError("check 이름은 비어 있지 않은 고유 문자열이어야 합니다.")
        names.add(check["name"])
        command = check.get("command")
        if not isinstance(command, list) or not command or any(
            not isinstance(arg, str) or "\0" in arg for arg in command
        ) or not command[0]:
            raise ValueError("command는 실행 파일부터 시작하는 문자열 배열이어야 합니다.")
        timeout = check.get("timeout_seconds", 120)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds는 유한한 양수여야 합니다.")
        if check.get("protocol") is not None:
            if (check["protocol"] != reports.API_PROTOCOL
                    or command != ["{python}", "-B", ".harness/api-review/check_api.py"]):
                raise ValueError("API protocol checks must use the registered Python API review runner")
    contract = acceptance.load_contract(root)
    acceptance.validate_boundary(contract, ready)
    acceptance.validate_registration(checks, contract, "command", "name")
    checks = [dict(check, _acceptance=acceptance.is_checker(check["command"], contract)) for check in checks]
    matches = [check for check in checks if check["_acceptance"]]
    if len(matches) > 1:
        raise ValueError("Register the common acceptance checker only once")
    if not matches:
        checks.append({"name": "acceptance", "command": contract["checker_argv"],
                       "timeout_seconds": 120, "_acceptance": True})
    return checks


def fingerprint(root):
    """Hash Git files and explicit ignored inputs, including tracked deletions."""
    root = Path(root).resolve()
    ready = ready_boundary(root)
    managed = reports.validate_managed(root)
    top = subprocess.check_output(["git", "-C", str(root), "rev-parse", "--show-toplevel"], stderr=subprocess.PIPE)
    if Path(os.fsdecode(top).strip()).resolve() != root:
        raise ValueError("--root는 Git 저장소의 최상위 폴더여야 합니다.")
    raw_names = subprocess.check_output(
        ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        stderr=subprocess.PIPE,
    )
    names = {os.fsdecode(n) for n in raw_names.split(b"\0") if n}
    # The receipt definition and core evidence cannot disappear behind .gitignore.
    names.update((".harness/checks.json", ".harness/harness.py", ".harness/verification.py",
                  ".harness/acceptance.py", ".harness/check_unittest.py", ".harness/acceptance.json",
                  ".harness/boundary.py", ".harness/clarify.py", ".harness/clarify.schema.json",
                  ".harness/clarify_prompt.md",
                  ".harness/reports.py", ".harness/review_criteria.json",
                  "automation/codex-loop/config.json", "automation/codex-loop/review.schema.json",
                  ".harness/review_instructions.md", ".harness/review.schema.json",
                  ".codex/agents/kty-reviewer.toml",
                  "REQUIREMENTS.md", "PLAN.md", "DECISIONS.md", "HANDOFF.md",
                  "INTERVIEW.md", "FUNCTIONAL_SPEC.md", "README.md", ".gitignore"))
    names.update(Path(p).relative_to(root).as_posix() for p in ready["protected_files"])
    names.update(p.relative_to(root).as_posix() for p in (root / ".harness/tests").rglob("*.py"))
    if (root / acceptance.CONTRACT).is_file():
        names.update(Path(p).relative_to(root).as_posix()
                     for p in acceptance.load_contract(root)["protected_files"])
    for value in configuration(root).get("inputs", []):
        path = (root / value).resolve()
        if Path(value).is_absolute() or not path.is_relative_to(root):
            raise ValueError("inputs 경로는 프로젝트 안에 있어야 합니다: " + value)
        relative = path.relative_to(root).as_posix()
        if relative == ".git" or relative.startswith((".git/", *EXCLUDED)) or relative in ("work", STATE):
            raise ValueError("inputs에 Git 내부·검증 기록 폴더를 등록할 수 없습니다: " + value)
        if not path.exists():
            raise ValueError("등록한 inputs 경로가 없습니다: " + value)
        if path.is_dir():
            names.update(p.relative_to(root).as_posix() for p in path.rglob("*") if p.is_file() or p.is_symlink())
        else:
            names.add(relative)
    digest = hashlib.sha256()
    for name in sorted(names):
        if name in managed or name.startswith(EXCLUDED) or name == ".git" or name.startswith(".git/"):
            continue
        path = root / name
        digest.update(os.fsencode(name) + b"\0")
        try:
            mode = path.lstat().st_mode
            digest.update(str(stat.S_IMODE(mode)).encode() + b"\0")
            if stat.S_ISLNK(mode):
                digest.update(b"link:" + os.fsencode(os.readlink(path)))
            elif stat.S_ISREG(mode):
                digest.update(b"file:")
                content = hashlib.sha256()
                if name in ("HANDOFF.md", "PLAN.md"):
                    data = path.read_bytes()
                    starts, ends = data.count(RESULTS_START), data.count(RESULTS_END)
                    if starts or ends:
                        if (starts != 1 or ends != 1 or data.index(RESULTS_START) > data.index(RESULTS_END)
                                or data.splitlines().count(RESULTS_START) != 1
                                or data.splitlines().count(RESULTS_END) != 1):
                            raise ValueError(name + ": results 마커는 정확한 단독 줄에 올바른 순서의 한 쌍이어야 합니다.")
                        start = data.index(RESULTS_START) + len(RESULTS_START)
                        data = data[:start] + data[data.index(RESULTS_END):]
                    content.update(data)
                else:
                    with path.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            content.update(chunk)
                digest.update(content.digest())
            else:
                raise ValueError("파일 지문이 지원하지 않는 경로입니다 (서브모듈 포함): " + name)
        except FileNotFoundError:
            digest.update(b"missing")
        digest.update(b"\0")
    return digest.hexdigest()


def start_process(*args, **kwargs):
    if os.name == "nt":
        command = args[0] if args else kwargs["args"]
        if isinstance(command, (str, bytes)):
            raise ValueError("Windows command must be an argument array with a native executable or an explicitly chosen shell.")
        executable = os.fsdecode(kwargs.get("executable") or command[0])
        search_path = (kwargs.get("env") or os.environ).get("PATH")
        resolved = shutil.which(executable, path=search_path)
        if not resolved and (Path(executable).is_absolute() or Path(executable).parent != Path(".")):
            candidate = Path(kwargs.get("cwd") or Path.cwd()) / executable
            # Python 3.9 applies PATHEXT only when which() receives a basename.
            resolved = shutil.which(candidate.name, path=str(candidate.parent))
        if Path(executable).suffix.lower() in (".cmd", ".bat") or (resolved and Path(resolved).suffix.lower() in (".cmd", ".bat")):
            raise ValueError("Windows batch shim rejected; use a native executable or an explicitly chosen shell: " + executable)
    process = subprocess.Popen(*args, **kwargs)
    if os.name == "nt":
        job = _kernel32.CreateJobObjectW(None, None)
        limits = _JobExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not job or not _kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                or not _kernel32.AssignProcessToJobObject(job, int(process._handle))):
            error = ctypes.WinError(ctypes.get_last_error())
            if job:
                _kernel32.CloseHandle(job)
            process.kill()
            process.wait()
            raise error
        process._harness_job = job
    return process


def finish_process(process):
    if os.name == "nt" and getattr(process, "_harness_job", None):
        _kernel32.TerminateJobObject(process._harness_job, 1)
        _kernel32.CloseHandle(process._harness_job)
        process._harness_job = None
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def stop_process(process):
    """Stop the check/review process group we started, including child runners."""
    finish_process(process)
    if process.poll() is None:
        process.kill()
    process.wait()


def run_check(root, check, logfile, on_start=None):
    command = [sys.executable if arg == "{python}" else arg for arg in check["command"]]
    with contextlib.ExitStack() as files:
        output = files.enter_context(logfile.open("wb"))
        errors = (files.enter_context(logfile.with_suffix(".stderr.log").open("wb"))
                  if check.get("_acceptance") or check.get("protocol") else subprocess.STDOUT)
        process = start_process(command, cwd=root, stdin=subprocess.DEVNULL,
                                stdout=output, stderr=errors, shell=False,
                                start_new_session=os.name != "nt",
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
        try:
            if on_start:
                on_start(process)
            return process.wait(timeout=check.get("timeout_seconds", 120))
        except BaseException:
            stop_process(process)
            raise
        finally:
            finish_process(process)


def report_verification(root, receipt, result=None):
    outcomes = {"passed": "PASS", "failed": "FAIL", "timed_out": "FAIL", "launch_failed": "FAIL",
                "interrupted": "UNVERIFIED", "running": "RUNNING", "not_run": "NOT_RUN"}
    rows = [result] if result is not None else receipt["results"]
    checks = [{"id": row["name"], "status": outcomes[row["status"]],
               "command": row["resolved_command"], "exit_code": row["exit_code"],
               "evidence_refs": [row["log"]] if row.get("log") else []} for row in rows]
    reports.append_event(root, {"kind": "verification", "scope": "verify", "run_id": receipt["run_id"],
        "status": outcomes[receipt["status"]] if result is None else outcomes[result["status"]],
        "source_hash": receipt.get("fingerprint_after") or receipt.get("fingerprint_before"),
        "data": {"checks": checks, "limitations": [receipt["error"]] if receipt.get("error") else []}})
    for row in rows:
        if row.get("protocol") != reports.API_PROTOCOL or (result is None and row["status"] != "not_run"):
            continue
        api = row.get("api_review")
        reports.append_event(root, {"kind": "api_verification", "scope": "api-check", "run_id": receipt["run_id"],
            "status": outcomes[row["status"]], "source_hash": receipt.get("fingerprint_before"),
            "data": {"api_review": api, "api_exit_code": row["exit_code"], "api_error": row.get("error"),
                     "checks": [{"id": check["id"], "status": check["status"], "exit_code": check["exit_code"],
                                 "command": row["resolved_command"], "evidence_refs": [item["path"] for item in check["artifacts"]]}
                                for check in api["checks"]] if api else []}})


def verify(root):
    root = Path(root).resolve()
    started_ns = time.time_ns()
    run_id = f"{started_ns:020d}-{uuid.uuid4().hex}"
    run_dir = root / STATE / ("verify-" + run_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    receipt_path = run_dir / "receipt.json"
    receipt = {"version": 2, "run_id": run_id, "root": str(root), "started_ns": started_ns,
               "owner_pid": os.getpid(),
               "started_at": utc_now(), "finished_at": None, "status": "running", "passed": False,
               "fingerprint_before": None, "fingerprint_after": None,
               "configuration": {"status": "pending", "error": None}, "results": [], "error": None}
    # Publish the attempt before parsing configuration so a failed new run cannot hide behind an old pass.
    write_json(receipt_path, receipt)
    report_token = None
    try:
        reports.initialize(root)
        report_token = reports.guard(root)
        try:
            checks = load_checks(root)
            contract = acceptance.load_contract(root)
        except (OSError, ValueError) as exc:
            receipt["configuration"] = {"status": "invalid", "error": str(exc)}
            raise
        receipt["configuration"] = {"status": "valid", "error": None}
        receipt["results"] = [
            {"name": check["name"], "command": check["command"],
             "resolved_command": [sys.executable if arg == "{python}" else arg for arg in check["command"]],
             "timeout_seconds": check.get("timeout_seconds", 120), "status": "not_run", "exit_code": None,
             "protocol": check.get("protocol"),
             "process_pid": None,
             "error": "앞선 검사가 끝나지 않아 실행하지 않았습니다.", "seconds": None,
             "started_at": None, "finished_at": None, "log": None, "log_sha256": None}
            for check in checks]
        receipt["fingerprint_before"] = fingerprint(root)
        if load_checks(root) != checks:
            raise ValueError("검증 준비 중 설정이 변경됐습니다. 다시 검증하세요.")
        write_json(receipt_path, receipt)
        report_verification(root, receipt)
        for index, (check, result) in enumerate(zip(checks, receipt["results"]), 1):
            reports.guard(root, report_token)
            logfile = run_dir / f"check-{index}.log"
            result.update(status="running", error=None, started_at=utc_now(),
                          log=logfile.relative_to(root).as_posix())
            # Even launch errors and interruptions have an individual log and a persisted running state.
            logfile.touch()
            write_json(receipt_path, receipt)
            print("검증: " + check["name"], flush=True)
            started = time.monotonic()
            def record_process(process):
                result["process_pid"] = process.pid
                write_json(receipt_path, receipt)
            try:
                result["exit_code"] = run_check(root, check, logfile, on_start=record_process)
                reports.guard(root, report_token)
                result["status"] = "passed" if result["exit_code"] == 0 else "failed"
                if check["_acceptance"] and result["exit_code"] == 0:
                    try:
                        acceptance.validate_output(logfile.read_text(encoding="utf-8"), contract)
                        result["acceptance_passed"] = True
                    except ValueError as exc:
                        result.update(status="failed", error=str(exc))
                if check.get("protocol") == reports.API_PROTOCOL:
                    try:
                        result["api_review"] = reports.validate_api_result(
                            root, reports.decode(logfile.read_bytes()), result["exit_code"])
                    except (OSError, ValueError) as exc:
                        result.update(status="failed", error=str(exc))
            except subprocess.TimeoutExpired as exc:
                result.update(status="timed_out", exit_code=-1, error=str(exc))
            except (OSError, ValueError) as exc:
                result.update(status="launch_failed", exit_code=-1, error=str(exc))
            except KeyboardInterrupt:
                result.update(status="interrupted", exit_code=-1, error="검증이 중단됐습니다.")
                raise
            finally:
                result.update(seconds=round(time.monotonic() - started, 3), finished_at=utc_now())
                result["log_sha256"] = file_digest(logfile)
                if (check["_acceptance"] or check.get("protocol")) and logfile.with_suffix(".stderr.log").is_file():
                    errors = logfile.with_suffix(".stderr.log")
                    result["stderr_log"] = errors.relative_to(root).as_posix()
                    result["stderr_sha256"] = file_digest(errors)
                write_json(receipt_path, receipt)
                reports.guard(root, report_token)
                report_verification(root, receipt, result)
            if result["status"] != "passed":
                break
        receipt["fingerprint_after"] = fingerprint(root)
        if receipt["fingerprint_before"] != receipt["fingerprint_after"]:
            receipt["error"] = "검증 도중 파일이 변경됐습니다. 최신 상태에서 다시 검증하세요."
        receipt["passed"] = (receipt["fingerprint_before"] == receipt["fingerprint_after"]
                             and all(r["status"] == "passed" for r in receipt["results"]))
    except KeyboardInterrupt:
        receipt.update(status="interrupted", error="검증이 중단됐습니다.")
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        receipt["error"] = str(exc)
    finally:
        if receipt["status"] != "interrupted":
            receipt["status"] = "passed" if receipt["passed"] else "failed"
        receipt["finished_at"] = utc_now()
        write_json(receipt_path, receipt)
        if report_token is not None:
            try:
                reports.guard(root, report_token)
                report_verification(root, receipt)
                reports.refresh(root)
            except (OSError, ValueError) as exc:
                receipt.update(passed=False, status="failed", error="Progress reporting failed: " + str(exc))
                write_json(receipt_path, receipt)
    if receipt["error"]:
        print(receipt["error"], file=sys.stderr)
    print(("검증 통과: " if receipt["passed"] else "검증 실패: ") + receipt_path.relative_to(root).as_posix(), flush=True)
    return 0 if receipt["passed"] else 1


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def owner_running(pid):
    """An inaccessible or reused PID is conservatively treated as still running."""
    if os.name == "nt":
        handle = _kernel32.OpenProcess(0x100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: no such process
        try:
            return _kernel32.WaitForSingleObject(handle, 0) != 0
        finally:
            _kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def check_process_running(pid):
    if os.name == "nt":
        return owner_running(pid)
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def status(root):
    root = Path(root).resolve()
    try:
        reports.initialize(root)
        reports.refresh(root)
        checks = load_checks(root)
        current = fingerprint(root)
        runs = sorted((root / STATE).glob("verify-*"))
        if not runs:
            raise ValueError("실행별 검증 기록이 없습니다.")
        receipt = None
        unfinished = []
        for run_dir in runs:
            try:
                if run_dir.is_symlink():
                    raise ValueError("검증 실행 폴더가 심볼릭 링크입니다.")
                record = read_json(run_dir / "receipt.json")
                if (not isinstance(record, dict) or record.get("version") != 2
                        or record.get("run_id") != run_dir.name.removeprefix("verify-")
                        or record.get("root") != str(root) or type(record.get("started_ns")) is not int
                        or not record["run_id"].startswith(f'{record["started_ns"]:020d}-')
                        or type(record.get("owner_pid")) is not int or not 0 < record["owner_pid"] < 2**32
                        or not isinstance(record.get("started_at"), str)
                        or not isinstance(record.get("results"), list)
                        or any(not isinstance(result, dict) for result in record["results"])
                        or record.get("status") not in ("running", "passed", "failed", "interrupted")):
                    raise ValueError("검증 기록이 손상됐습니다.")
            except (OSError, ValueError) as exc:
                if run_dir == runs[-1]:
                    raise ValueError("최신 검증 기록 손상: " + run_dir.name + ": " + str(exc)) from exc
                print("손상된 이전 검증 기록: " + run_dir.name, file=sys.stderr)
                continue
            if record["status"] == "running" or record.get("finished_at") is None:
                if not owner_running(record["owner_pid"]):
                    print("중단된 검증 기록 (실행 프로세스 종료): " + run_dir.name, file=sys.stderr)
                    for result in record.get("results", []):
                        if not isinstance(result, dict) or result.get("status") != "running":
                            continue
                        pid = result.get("process_pid")
                        if type(pid) is not int or not 0 < pid < 2**32:
                            print("검사 프로세스 상태 불명: " + run_dir.name, file=sys.stderr)
                            unfinished.append(run_dir.name)
                            break
                        if check_process_running(pid):
                            print("중단된 검사의 프로세스가 남아 있습니다: " + run_dir.name, file=sys.stderr)
                            unfinished.append(run_dir.name)
                            break
                elif record.get("fingerprint_before") in (None, current):
                    unfinished.append(run_dir.name)
                else:
                    print("다른 입력의 미완료 검증 실행: " + run_dir.name, file=sys.stderr)
            receipt = record
        if unfinished:
            raise ValueError("미완료 검증 실행: " + ", ".join(unfinished))
        results = receipt.get("results")
        valid = (receipt.get("passed") is True and receipt.get("status") == "passed"
                 and receipt.get("error") is None
                 and receipt.get("configuration") == {"status": "valid", "error": None}
                 and receipt.get("fingerprint_before") == current == receipt.get("fingerprint_after")
                 and isinstance(receipt.get("finished_at"), str) and bool(receipt["finished_at"])
                 and isinstance(results, list) and len(results) == len(checks))
        if valid:
            for index, (result, check) in enumerate(zip(results, checks), 1):
                logfile = runs[-1] / f"check-{index}.log"
                if (not isinstance(result, dict) or result.get("name") != check["name"]
                        or result.get("command") != check["command"]
                        or result.get("protocol") != check.get("protocol")
                        or result.get("resolved_command") != [sys.executable if a == "{python}" else a for a in check["command"]]
                        or result.get("timeout_seconds") != check.get("timeout_seconds", 120)
                        or result.get("status") != "passed" or type(result.get("exit_code")) is not int
                        or result["exit_code"] != 0 or result.get("error") is not None
                        or type(result.get("process_pid")) is not int or not 0 < result["process_pid"] < 2**32
                        or not isinstance(result.get("started_at"), str) or not result["started_at"]
                        or not isinstance(result.get("finished_at"), str) or not result["finished_at"]
                        or type(result.get("seconds")) not in (int, float)
                        or not math.isfinite(result["seconds"]) or result["seconds"] < 0
                        or result.get("log") != logfile.relative_to(root).as_posix()
                        or logfile.is_symlink() or not logfile.is_file()
                        or result.get("log_sha256") != file_digest(logfile)):
                    valid = False
                    break
                if check["_acceptance"] or check.get("protocol"):
                    errors = logfile.with_suffix(".stderr.log")
                    if (result.get("stderr_log") != errors.relative_to(root).as_posix()
                            or errors.is_symlink() or not errors.is_file()
                            or result.get("stderr_sha256") != file_digest(errors)):
                        valid = False
                        break
                    if check["_acceptance"]:
                        if result.get("acceptance_passed") is not True:
                            valid = False
                            break
                        acceptance.validate_output(logfile.read_text(encoding="utf-8"), acceptance.load_contract(root))
                    if check.get("protocol") == reports.API_PROTOCOL:
                        actual = reports.validate_api_result(root, reports.decode(logfile.read_bytes()), result["exit_code"])
                        if result.get("api_review") != actual:
                            valid = False
                            break
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print("최신 검증 없음: " + str(exc), file=sys.stderr)
        return 1
    print(("최신 검증 통과: " if valid else "재검증 필요: ") + runs[-1].name)
    return 0 if valid else 1
