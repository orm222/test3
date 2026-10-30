"""Controller-owned progress and rubric reports, with append-only evidence history."""
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import stat
import time
import uuid

MANAGED = ("docs/progress.md", "docs/review-score.md")
DIRECTORY = ".harness/work/reporting"
STATE = DIRECTORY + "/state.json"
OWNER = "codex-harness-reports/v1"
MARKER = "<!-- managed by " + OWNER + "; edit source inputs, not this document -->"
ZERO = "0" * 64
ROW_STATUSES = ("PASS", "PARTIAL", "FAIL", "UNVERIFIED", "NA")
CHECK_STATUSES = ("PASS", "FAIL", "SKIP", "NOT_RUN", "UNVERIFIED", "RUNNING")
SOURCE_EXCLUDED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}


class ReportError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ReportError(message)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip()) and "\0" not in value


def pairs(items):
    result = {}
    for key, value in items:
        require(key not in result, "Duplicate reporting JSON key: " + key)
        result[key] = value
    return result


def decode(value):
    def invalid(number):
        raise ReportError("Invalid reporting JSON number: " + number)
    try:
        return json.loads(value, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, TypeError) as exc:
        raise ReportError("Invalid reporting JSON: " + str(exc)) from exc


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def sha(value):
    return hashlib.sha256(value).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def safe_path(root, name):
    root = Path(root).resolve()
    require(nonempty(name), "Reporting path must be a project-relative file")
    relative = Path(name)
    require(not relative.anchor and not PureWindowsPath(name).anchor and ".." not in relative.parts and "\\" not in name,
            "Reporting path must stay inside the project: " + name)
    path = root / relative
    require(path.absolute() == path.resolve() and path.resolve().is_relative_to(root),
            "Reporting path is outside or redirected: " + name)
    for member in (path, *path.parents):
        if member == root:
            break
        require(not member.is_symlink(), "Reporting symlink is not allowed: " + name)
        if member != path:
            require(not member.exists() or member.is_dir(), "A file blocks reporting directory: " + name)
    require(not path.exists() or path.is_file(), "Reporting destination must be a file: " + name)
    return path


@contextlib.contextmanager
def locked(root):
    path = safe_path(root, DIRECTORY + "/lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        deadline = time.monotonic() + 5
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ReportError("Reporting lock is busy; retry after the current controller update")
                time.sleep(0.01)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def load_criteria(root=None):
    path = Path(__file__).with_name("review_criteria.json") if root is None else safe_path(root, ".harness/review_criteria.json")
    value = decode(path.read_text(encoding="utf-8-sig"))
    require(isinstance(value, dict) and type(value.get("version")) is int and value["version"] == 1 and isinstance(value.get("axes"), list)
            and len(value["axes"]) == 5, "Review criteria need five axes and version 1")
    axes, ids = set(), set()
    for axis in value["axes"]:
        require(isinstance(axis, dict) and set(axis) == {"id", "title", "criteria"}
                and nonempty(axis["id"]) and axis["id"] not in axes and nonempty(axis["title"])
                and isinstance(axis["criteria"], list) and axis["criteria"], "Invalid review axis")
        axes.add(axis["id"])
        for item in axis["criteria"]:
            require(isinstance(item, dict) and set(item) == {"id", "text", "execution"}
                    and nonempty(item["id"]) and item["id"] not in ids and nonempty(item["text"])
                    and type(item["execution"]) is bool, "Invalid or duplicated review criterion")
            ids.add(item["id"])
    return value


def score_rows_schema():
    ids = [item["id"] for axis in load_criteria()["axes"] for item in axis["criteria"]]
    return {"type": "array", "items": {"type": "object", "additionalProperties": False,
            "required": ["criterion_id", "status", "reason", "evidence_refs"], "properties": {
                "criterion_id": {"type": "string", "enum": ids},
                "status": {"type": "string", "enum": list(ROW_STATUSES)},
                "reason": {"type": "string"}, "evidence_refs": {"type": "array", "items": {"type": "string"}}}}}


def validate_not_applicable(value, criteria=None):
    rubric = load_criteria() if criteria is None else criteria
    ids = {item["id"] for axis in rubric["axes"] for item in axis["criteria"]}
    require(isinstance(value, dict) and set(value) <= ids and all(nonempty(reason) for reason in value.values()),
            "Invalid predetermined review_not_applicable map")
    return value


def file_evidence(root, path, kind="file", **details):
    path = Path(path)
    if not path.is_absolute():
        path = Path(root).resolve() / path
    require(path.is_file() and not path.is_symlink() and path.absolute() == path.resolve(), "Evidence file missing or redirected: " + str(path))
    require(kind in ("file", "check"), "Evidence kind must be file or check")
    allowed = {"status", "exit_code", "command"}
    require(set(details) <= allowed, "Unknown evidence details")
    resolved = path.resolve()
    project = Path(root).resolve()
    location = resolved.relative_to(project).as_posix() if resolved.is_relative_to(project) else str(resolved)
    return dict(kind=kind, path=location, sha256=sha(path.read_bytes()), **details)


def validate_evidence(root, entry):
    require(isinstance(entry, dict) and set(entry) <= {"kind", "path", "sha256", "status", "exit_code", "command", "artifacts"}
            and {"kind", "path", "sha256"} <= set(entry), "Invalid evidence catalog entry")
    actual = file_evidence(root, entry["path"], kind=entry["kind"],
                           **{key: entry[key] for key in ("status", "exit_code", "command") if key in entry})
    require(actual["sha256"] == entry["sha256"], "Evidence file changed: " + str(entry["path"]))
    if "artifacts" in entry:
        require(isinstance(entry["artifacts"], list) and entry["artifacts"], "Invalid evidence artifact dependencies")
        for artifact in entry["artifacts"]:
            require(isinstance(artifact, dict) and set(artifact) == {"path", "sha256"}, "Invalid evidence artifact")
            require(file_evidence(root, artifact["path"])["sha256"] == artifact["sha256"], "Evidence artifact changed: " + str(artifact["path"]))
        actual["artifacts"] = entry["artifacts"]
    return actual


API_PROTOCOL = "harness-api-review/v1"
API_CHECKS = ("schemathesis", "hurl")


def validate_api_result(root, result, exit_code, source_hash=None):
    """Validate a controller-observed receipt, including every referenced raw artifact."""
    require(isinstance(result, dict) and result.get("schema") == API_PROTOCOL, "Invalid API review protocol")
    require(result.get("status") in ("passed", "failed", "error") and type(exit_code) is int
            and exit_code == {"passed": 0, "failed": 1, "error": 2}[result["status"]], "API status and process exit disagree")
    current = source_digest(root) if source_hash is None else source_hash
    require(result.get("source_hash") == current, "API evidence is stale for the current source")
    config_path = safe_path(root, ".harness/api-review.json")
    config = decode(config_path.read_bytes())
    require(isinstance(config, dict), "Invalid API configuration")
    scenarios = config.get("scenarios")
    require(isinstance(scenarios, list) and scenarios and all(nonempty(item) for item in scenarios)
            and len(scenarios) == len(set(scenarios)), "API scenarios must be a nonempty unique list")
    for name in scenarios:
        require(not any(part.casefold() in SOURCE_EXCLUDED_DIRS for part in Path(name).parts)
                and not name.casefold().startswith(("work/", ".harness/work/")), "API scenario cannot be in a runtime/ignored directory: " + name)
        require(safe_path(root, name).is_file(), "Missing API scenario: " + name)
    require(result.get("config_digest") == sha(config_path.read_bytes()) and result.get("scenario_files") == scenarios,
            "API result does not match the configured scenarios")
    require(result.get("required_check_ids") == list(API_CHECKS), "API required check IDs are missing or changed")
    checks = result.get("checks")
    require(isinstance(checks, list) and len(checks) == 2 and all(isinstance(check, dict) for check in checks)
            and [check.get("id") for check in checks] == list(API_CHECKS), "API result must contain both fixed checks exactly once")
    tools = result.get("tools")
    require(isinstance(tools, dict) and set(tools) <= set(API_CHECKS), "Invalid API tool versions")
    for name, info in tools.items():
        require(isinstance(info, dict) and info.get("version") == {"schemathesis": "4.4.4", "hurl": "8.1.0"}[name],
                "API tool version does not match the installed contract: " + name)
    artifacts, seen = [], set()
    def collect(items):
        require(isinstance(items, list), "API artifacts must be a list")
        for item in items:
            require(isinstance(item, dict) and set(item) == {"path", "sha256"} and nonempty(item["path"])
                    and item["path"].startswith(".harness/work/api-review/"), "API artifact is outside the controlled run directory")
            path = safe_path(root, item["path"])
            require(item["path"] not in seen, "Duplicate API artifact")
            seen.add(item["path"])
            require(file_evidence(root, path)["sha256"] == item["sha256"], "API artifact changed: " + item["path"])
            artifacts.append(item)
    collect(result.get("artifacts", []))
    for check in checks:
        require(check.get("tool") == check["id"] and check.get("status") in ("PASS", "FAIL", "SKIP", "NOT_RUN"), "Invalid API check identity/status")
        code, counts = check.get("exit_code"), check.get("counts")
        require(code is None or type(code) is int, "Invalid API check exit code")
        require(isinstance(counts, dict) and set(counts) == {"collected", "passed", "failed", "skipped"}
                and all(type(value) is int and value >= 0 for value in counts.values())
                and counts["collected"] == counts["passed"] + counts["failed"] + counts["skipped"], "Invalid API execution counts")
        collect(check.get("artifacts"))
        if check["status"] == "PASS":
            require(code == 0 and counts["collected"] > 0 and counts["passed"] == counts["collected"]
                    and check["artifacts"] and check["id"] in tools, "API PASS needs actual non-skipped execution evidence")
            if check["id"] == "hurl":
                require(counts["collected"] == len(scenarios), "Not all fixed Hurl scenarios ran")
        elif check["status"] == "FAIL":
            require(type(code) is int and check["artifacts"], "API FAIL needs an actual attempted command")
        else:
            require(code is None and counts["passed"] == counts["failed"] == 0, "Unexecuted API check claims execution")
    if result["status"] == "passed":
        require(all(check["status"] == "PASS" for check in checks) and set(tools) == set(API_CHECKS)
                and result.get("source_hash_after", current) == current, "API success requires both tools on unchanged source")
    return result


def current_api_evidence(root):
    """Prior controller runs are optional evidence, never this reviewer's own execution."""
    root = Path(root).resolve()
    if not safe_path(root, STATE).exists():
        return {"context": None, "evidence": {}}
    with locked(root):
        _, events = _read(root)
        latest = next((event for event in reversed(events) if "api_review" in event["data"]), None)
        if latest is None or latest["data"]["api_review"] is None:
            return {"context": None, "evidence": {}}
        current = _source_digest(root, set(MANAGED))
        if latest["source_digest"] != current:
            return {"context": None, "evidence": {}}
        try:
            result = validate_api_result(root, latest["data"]["api_review"], latest["data"]["api_exit_code"], current)
        except (OSError, ValueError):
            return {"context": None, "evidence": {}}
        dependencies = result.get("artifacts", []) + [item for check in result["checks"] for item in check["artifacts"]]
        evidence = {}
        for check in result["checks"]:
            if check["status"] in ("PASS", "FAIL"):
                entry = file_evidence(root, check["artifacts"][0]["path"], kind="check",
                                      status=check["status"], exit_code=check["exit_code"])
                entry["artifacts"] = dependencies
                evidence["api-check:" + check["id"]] = entry
        return {"context": {"provenance": "Prior controller API execution; not commands executed by this reviewer",
                            "recorded_at": latest["timestamp_utc"], "run_id": latest["run_id"], "result": result},
                "evidence": evidence}


def score_review(root, rows, evidence, not_applicable=None):
    rubric = load_criteria(root)
    criteria = {item["id"]: item for axis in rubric["axes"] for item in axis["criteria"]}
    not_applicable = {} if not_applicable is None else not_applicable
    validate_not_applicable(not_applicable, rubric)
    require(isinstance(rows, list) and isinstance(evidence, dict), "Review criterion rows and evidence catalog are required")
    selected, used = {}, {}
    for row in rows:
        require(isinstance(row, dict) and set(row) == {"criterion_id", "status", "reason", "evidence_refs"}, "Invalid review criterion row")
        cid = row["criterion_id"]
        require(isinstance(cid, str) and cid in criteria and cid not in selected, "Missing/duplicate/unknown review criterion ID")
        status = row["status"]
        require(isinstance(status, str) and status in ROW_STATUSES and nonempty(row["reason"]), "Invalid criterion status/reason")
        require((status == "NA") == (cid in not_applicable), "NA must match the predetermined configuration: " + cid)
        refs = row["evidence_refs"]
        require(isinstance(refs, list) and all(nonempty(ref) for ref in refs) and len(refs) == len(set(refs)), "Invalid evidence references")
        proofs = []
        for ref in refs:
            require(ref in evidence and isinstance(evidence[ref], dict), "Evidence reference was not observed: " + ref)
            actual = validate_evidence(root, evidence[ref])
            if actual["kind"] == "check":
                require(actual.get("status") in CHECK_STATUSES and (actual.get("exit_code") is None or type(actual["exit_code"]) is int), "Invalid actual check evidence")
            used[ref] = actual
            proofs.append(actual)
        if status in ("PASS", "PARTIAL"):
            require(proofs, "Positive criterion score requires actual evidence: " + cid)
            if criteria[cid]["execution"]:
                require(any(item["kind"] == "check" and item.get("status") in ("PASS", "FAIL")
                            and type(item.get("exit_code")) is int for item in proofs),
                        "Execution criterion requires an actually completed check: " + cid)
        selected[cid] = dict(row, criterion=criteria[cid]["text"],
                             points=None if status == "NA" else {"PASS": 2, "PARTIAL": 1}.get(status, 0))
    require(set(selected) == set(criteria), "Review must include every rubric criterion exactly once")
    axes = []
    for axis in rubric["axes"]:
        points = [selected[item["id"]]["points"] for item in axis["criteria"] if selected[item["id"]]["points"] is not None]
        earned, possible = sum(points), 2 * len(points)
        axes.append(dict(id=axis["id"], title=axis["title"], earned=earned, possible=possible,
                         score=round(100 * earned / possible, 2) if possible else None))
    return {"version": 1, "axes": axes, "rows": [selected[cid] for cid in criteria],
            "evidence": used, "not_applicable": not_applicable}


def _source_digest(root, managed):
    """A common report freshness hash, independent of controller fingerprint algorithms."""
    root = Path(root).resolve()
    digest = hashlib.sha256()
    for directory, dirs, files in os.walk(str(root), followlinks=False):
        relative = Path(directory).relative_to(root)
        kept = []
        for name in sorted(dirs):
            rel = (relative / name).as_posix()
            if name in SOURCE_EXCLUDED_DIRS or rel == ".harness/work":
                continue
            path = Path(directory) / name
            require(not path.is_symlink() and path.resolve() == path.absolute(), "Reporting source directory is redirected: " + rel)
            kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            rel = (relative / name).as_posix()
            if rel in managed:
                continue
            path = Path(directory) / name
            mode = path.lstat().st_mode
            require(stat.S_ISREG(mode) and not path.is_symlink(), "Reporting source is not an ordinary file: " + rel)
            digest.update(encoded([rel, stat.S_IMODE(mode), sha(path.read_bytes())]))
    return digest.hexdigest()


def source_digest(root):
    return _source_digest(root, validate_managed(root))


def cell(value):
    return str(value).replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def detail(value):
    if isinstance(value, str):
        return "\n".join("> " + line for line in value.splitlines())
    return "```json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n```"


def api_section(events, current):
    latest = next((event for event in reversed(events) if "api_review" in event["data"]), None)
    if latest is None:
        return []
    result = latest["data"]["api_review"]
    lines = ["## 선택 API 검수 — api-check", "",
             "이 기록은 별도 실행한 API 검수 범위입니다. 기존 독립 리뷰 점수에 자동 반영하거나 재채점하지 않습니다.", "",
             "run: {} · UTC: {}".format(latest["run_id"] or "—", latest["timestamp_utc"]), ""]
    if result is None:
        return lines + ["API 근거 상태: 미검증 / 무효", "", detail(latest["data"].get("api_error", "실행되지 않았거나 검증 가능한 결과가 없습니다.")), ""]
    lines += ["API 실행 결과: " + result["status"], "",
              "API 근거 상태: " + ("현재 코드와 일치" if latest["source_digest"] == current else "오래됨 — 현재 코드에서 재실행 필요"), ""]
    if result.get("error"):
        lines += [detail(result["error"]), ""]
    lines += [
              "| 도구 | 결과 | 수집 / 성공 / 실패 / 건너뜀 | 원본 근거 |", "|---|---|---|---|"]
    for check in result["checks"]:
        counts = check["counts"]
        lines.append("| {} | {} | {} | {} |".format(cell(check["id"]), cell(check["status"]),
            " / ".join(str(counts[key]) for key in ("collected", "passed", "failed", "skipped")),
            cell([item["path"] for item in check["artifacts"]])))
    return lines + ["", "현재 코드와 원본 로그가 유효한 경우에만 다음 허용된 독립 리뷰의 이전 실행 근거로 제공됩니다.", ""]


def render(events, current):
    last = events[-1]
    identity = "UTC: {} · run: {} · task: {} · round: {} · scope: {}".format(
        last["timestamp_utc"], last["run_id"] or "—", last["task_id"] or "—", last["round"] if last["round"] is not None else "—", last["scope"] or "—")
    progress = [MARKER, "# 진행 기록", "", "현재 상태: " + ("미착수 / 미검증" if last["kind"] == "initialize" else last["status"]), "", identity,
                "", "현재 코드 해시: `" + current + "`", "", "검증 완료는 실제 하네스 판정으로 결정합니다. 작성자 자기보고는 독립 검증 결과가 아닙니다.", ""]
    for key, title in (("goal", "원래 목표"), ("constraints", "제약"), ("boundary_context", "경계 질문과 실제 답변"),
                       ("traceability", "요구사항 → 코드 → 검증"), ("next_steps", "남은 일과 다음 단계"), ("limitations", "한계·미해결 사항")):
        found = next((event["data"][key] for event in reversed(events) if key in event["data"]), None)
        if found is not None:
            progress += ["## " + title, "", detail(found), ""]
    check_event = next((event for event in reversed(events) if "checks" in event["data"]), None)
    progress += ["## 실제 검증", ""]
    if check_event is None:
        progress += ["미검증. 실행한 검사 결과가 없습니다.", ""]
    else:
        stale = check_event["source_digest"] != current
        progress += ["아래 표는 최근 검사 기록의 해당 범위입니다.", "",
                     "run: {} · task: {} · round: {} · scope: {}".format(check_event["run_id"] or "—", check_event["task_id"] or "—", check_event["round"] if check_event["round"] is not None else "—", check_event["scope"] or "—"), "",
                     "결과 상태: " + ("오래됨 — 현재 코드에서 다시 검증해야 합니다." if stale else "현재 코드와 일치"), "",
                     "| 검사 | 결과 | 종료 코드 | 명령 | 근거 |", "|---|---|---|---|---|"]
        for check in check_event["data"]["checks"]:
            progress.append("| " + " | ".join(cell(check.get(key, "—")) for key in ("id", "status", "exit_code", "command", "evidence_refs")) + " |")
        progress.append("")
    progress += api_section(events, current)
    builder = next((event for event in reversed(events) if "builder_report" in event["data"]), None)
    if builder is not None:
        progress += ["## 작성자 자기보고 — 미검증 주장", "", detail(builder["data"]["builder_report"]), ""]
    progress += ["## 실행 이력", "", "| UTC | run / task / round | 단계 | 상태 | 원본 코드 해시 | 기록 |", "|---|---|---|---|---|---|"]
    for event in events:
        progress.append("| " + " | ".join(cell(value) for value in (event["timestamp_utc"],
            "{} / {} / {}".format(event["run_id"] or "—", event["task_id"] or "—", event["round"] if event["round"] is not None else "—"),
            event["kind"], event["status"], event["source_hash"] or "—", "{}/events/{:08d}.json".format(DIRECTORY, event["sequence"]))) + " |")
    progress += ["", "이 문서는 하네스가 생성합니다. 직접 편집하면 다음 실행에서 변경을 감지하고 중단합니다.", ""]
    score = [MARKER, "# 리뷰 점수", "", "점수는 참고 정보이며 필수 검증과 독립 리뷰의 통과 조건을 대체하지 않습니다.", "",
             "기준별 PASS=2, PARTIAL=1, FAIL/UNVERIFIED=0. NA는 사전에 지정된 사유가 있을 때만 분모에서 제외합니다.", "",
             "각 축 = 획득점 ÷ (2 × 적용 항목 수) × 100. 적용 항목이 없으면 미산정입니다.", ""]
    reviewed = [event for event in events if "review" in event["data"]]
    if not reviewed or reviewed[-1]["data"]["review"] is None:
        score += ["리뷰 상태: 미검증 / 점수 미산정", ""]
        if reviewed:
            score += ["최근 시도: " + reviewed[-1]["status"], "",
                      detail(reviewed[-1]["data"].get("score_error", reviewed[-1]["data"].get("limitations", "리뷰가 아직 완료되지 않았습니다."))), ""]
    else:
        event = reviewed[-1]
        result = event["data"]["review"]
        score += ["리뷰 상태: " + ("오래됨 — 현재 코드와 다릅니다." if event["source_digest"] != current else "현재 코드와 일치"), "",
                  "UTC: {} · run: {} · task: {} · round: {} · scope: {}".format(event["timestamp_utc"], event["run_id"] or "—", event["task_id"] or "—", event["round"] if event["round"] is not None else "—", event["scope"] or "—"), "",
                  "검토 코드 해시: `" + (event["source_hash"] or event["source_digest"]) + "`", "",
                  "| 축 | 점수 / 100 | 획득 / 가능 |", "|---|---|---|"]
        for axis in result["axes"]:
            score.append("| {} | {} | {} / {} |".format(cell(axis["title"]), "미산정" if axis["score"] is None else axis["score"], axis["earned"], axis["possible"]))
        score += ["", "| 기준 ID | 기준 내용 | 상태 | 점수 | 이유 | 근거 |", "|---|---|---|---|---|---|"]
        for row in result["rows"]:
            score.append("| " + " | ".join(cell(result["not_applicable"][row["criterion_id"]] if key == "reason" and row["status"] == "NA" else row[key]) for key in ("criterion_id", "criterion", "status", "points", "reason", "evidence_refs")) + " |")
        score += ["", "## 근거 파일", "", detail(result["evidence"]), ""]
    score += api_section(events, current)
    score += ["## 리뷰 이력", "", "| UTC | run / task | 범위 | 코드 상태 | 축별 점수 |", "|---|---|---|---|---|"]
    for event in reviewed:
        marks = (", ".join(axis["title"] + "=" + ("미산정" if axis["score"] is None else str(axis["score"])) for axis in event["data"]["review"]["axes"])
                 if event["data"]["review"] is not None else "미산정 (" + event["status"] + ")")
        score.append("| " + " | ".join(cell(value) for value in (event["timestamp_utc"], "{} / {}".format(event["run_id"] or "—", event["task_id"] or "—"), event["scope"] or "—", "현재" if event["source_digest"] == current else "오래됨", marks)) + " |")
    score.append("")
    return {MANAGED[0]: "\n".join(progress).encode("utf-8"), MANAGED[1]: "\n".join(score).encode("utf-8")}


def _read(root):
    path = safe_path(root, STATE)
    if not path.exists():
        return None, []
    state = decode(path.read_bytes())
    require(isinstance(state, dict) and set(state) == {"owner", "version", "count", "head", "current_source_digest", "output_hashes"}
            and state["owner"] == OWNER and type(state["version"]) is int and state["version"] == 1
            and type(state["count"]) is int and state["count"] >= 1, "Invalid reporting ownership/state")
    directory = safe_path(root, DIRECTORY + "/events/probe").parent
    paths = sorted(directory.glob("*.json"))
    require([path.name for path in paths] == ["{:08d}.json".format(n) for n in range(1, state["count"] + 1)], "Reporting history is missing or has unexpected records")
    previous, events = ZERO, []
    for number, path in enumerate(paths, 1):
        raw = safe_path(root, path.relative_to(Path(root).resolve()).as_posix()).read_bytes()
        event = decode(raw)
        require(isinstance(event, dict) and type(event.get("sequence")) is int and event["sequence"] == number
                and event.get("previous_hash") == previous and type(event.get("version")) is int
                and event["version"] == 1, "Reporting history chain was changed")
        events.append(event)
        previous = sha(raw)
    require(previous == state["head"], "Reporting history head was changed")
    expected = render(events, state["current_source_digest"])
    require(state["output_hashes"] == {name: sha(value) for name, value in expected.items()}, "Reporting state/render mismatch")
    for name, content in expected.items():
        require(safe_path(root, name).is_file() and safe_path(root, name).read_bytes() == content,
                "Managed report changed outside the controller: " + name)
    return state, events


def _precheck_unlocked(root):
    for name in (*MANAGED, STATE, DIRECTORY + "/events/probe", DIRECTORY + "/lock"):
        safe_path(root, name)
    if not safe_path(root, STATE).exists():
        require(not any(safe_path(root, name).exists() for name in MANAGED), "Existing report documents are not owned by this harness; preserve or relocate them before attach")
        require(not list((root / DIRECTORY / "events").glob("*.json")), "Orphaned reporting history; refusing to overwrite it")
        return None, []
    return _read(root)


def precheck(root):
    root = Path(root).resolve()
    lock = safe_path(root, DIRECTORY + "/lock")
    if lock.exists():
        with locked(root):
            _precheck_unlocked(root)
        return
    try:
        _precheck_unlocked(root)
    except ReportError:
        # A first initializer creates its lock before publishing either document.
        if not lock.exists():
            raise
        with locked(root):
            _precheck_unlocked(root)


def _atomic(root, name, content):
    path = safe_path(root, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = safe_path(root, DIRECTORY + "/.atomic-" + uuid.uuid4().hex)
    try:
        temporary.write_bytes(content)
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def _publish(root, events, current):
    output = render(events, current)
    last = safe_path(root, DIRECTORY + "/events/{:08d}.json".format(len(events)))
    state = {"owner": OWNER, "version": 1, "count": len(events), "head": sha(last.read_bytes()),
             "current_source_digest": current, "output_hashes": {name: sha(value) for name, value in output.items()}}
    for name, content in output.items():
        _atomic(root, name, content)
    _atomic(root, STATE, encoded(state))
    return state


def _append(root, state, events, value):
    require(isinstance(value, dict) and set(value) <= {"kind", "status", "run_id", "task_id", "round", "source_hash", "scope", "data"}, "Unknown report event fields")
    event = {"version": 1, "sequence": len(events) + 1, "previous_hash": state["head"] if state else ZERO,
             "timestamp_utc": utc_now(), "source_digest": _source_digest(root, set(MANAGED)), "run_id": None, "task_id": None,
             "round": None, "source_hash": None, "scope": "", "data": {}}
    event.update(value)
    require(nonempty(event.get("kind")) and nonempty(event.get("status")) and isinstance(event["data"], dict), "Report event needs kind/status/data")
    for name in ("run_id", "task_id", "source_hash"):
        require(event[name] is None or nonempty(event[name]), "Invalid report identity: " + name)
    require(event["round"] is None or (type(event["round"]) is int and event["round"] >= 0), "Invalid report round")
    require(isinstance(event["scope"], str), "Invalid report scope")
    if event["data"].get("api_review") is not None:
        validate_api_result(root, event["data"]["api_review"], event["data"].get("api_exit_code"), event["source_digest"])
    if "checks" in event["data"]:
        checks = event["data"]["checks"]
        require(isinstance(checks, list), "Report checks must be a list")
        for check in checks:
            require(isinstance(check, dict) and nonempty(check.get("id")) and check.get("status") in CHECK_STATUSES
                    and (check.get("exit_code") is None or type(check["exit_code"]) is int), "Invalid actual check result")
    review = event["data"].get("review")
    if review is not None:
        require(isinstance(review, dict) and set(review) == {"version", "axes", "rows", "evidence", "not_applicable"}, "Invalid computed review score")
        rows = [{key: row[key] for key in ("criterion_id", "status", "reason", "evidence_refs")} for row in review["rows"]]
        require(score_review(root, rows, review["evidence"], review["not_applicable"]) == review,
                "Review scores must be computed from the shared criteria, not supplied as totals")
    path = safe_path(root, DIRECTORY + "/events/{:08d}.json".format(event["sequence"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(encoded(event))
    events.append(event)
    _publish(root, events, event["source_digest"])
    return event


def _ensure_unlocked(root):
    state, events = _precheck_unlocked(root)
    if state is None:
        _append(root, None, events, {"kind": "initialize", "status": "NOT_STARTED", "data": {}})
        return _read(root)
    return state, events


def initialize(root):
    root = Path(root).resolve()
    with locked(root):
        state, _ = _ensure_unlocked(root)
        return {"count": state["count"], "head": state["head"]}


def validate_managed(root):
    root = Path(root).resolve()
    if not safe_path(root, STATE).exists() and not safe_path(root, DIRECTORY + "/lock").exists():
        return set()
    with locked(root):
        state, _ = _read(root)
        return set(MANAGED) if state is not None else set()


def guard(root, token=None):
    root = Path(root).resolve()
    if not safe_path(root, STATE).exists() and not safe_path(root, DIRECTORY + "/lock").exists():
        require(token is None or token == {"count": 0, "head": ZERO}, "Reporting state disappeared")
        return {"count": 0, "head": ZERO}
    with locked(root):
        state, events = _read(root)
        if state is None:
            require(token is None or token == {"count": 0, "head": ZERO}, "Reporting state disappeared")
            return {"count": 0, "head": ZERO}
        if token is not None:
            require(isinstance(token, dict) and set(token) == {"count", "head"} and type(token["count"]) is int
                    and 0 <= token["count"] <= state["count"], "Reporting history was removed")
            previous = ZERO if token["count"] == 0 else sha(safe_path(root, DIRECTORY + "/events/{:08d}.json".format(token["count"])).read_bytes())
            require(token["head"] == previous, "Existing reporting history was modified")
        return {"count": state["count"], "head": state["head"]}


def append_event(root, event):
    root = Path(root).resolve()
    with locked(root):
        state, events = _ensure_unlocked(root)
        return _append(root, state, events, event)


def refresh(root):
    root = Path(root).resolve()
    with locked(root):
        state, events = _ensure_unlocked(root)
        current = _source_digest(root, set(MANAGED))
        latest = next((event for event in reversed(events) if "review" in event["data"]), None)
        if latest is not None and latest["data"]["review"] is not None and latest["source_digest"] == current:
            invalid = []
            for ref, entry in latest["data"]["review"]["evidence"].items():
                try:
                    validate_evidence(root, entry)
                except (OSError, ValueError):
                    invalid.append(ref)
            if invalid:
                _append(root, state, events, {"kind": "review_evidence_invalid", "status": "UNVERIFIED",
                    "run_id": latest["run_id"], "task_id": latest["task_id"], "round": latest["round"],
                    "source_hash": latest["source_hash"], "scope": latest["scope"],
                    "data": {"review": None, "score_error": "Review evidence changed or is missing: " + ", ".join(invalid)}})
                state, events = _read(root)
        api = next((event for event in reversed(events) if "api_review" in event["data"]), None)
        if api is not None and api["data"]["api_review"] is not None and api["source_digest"] == current:
            try:
                validate_api_result(root, api["data"]["api_review"], api["data"].get("api_exit_code"), current)
            except (OSError, ValueError) as exc:
                _append(root, state, events, {"kind": "api_evidence_invalid", "status": "UNVERIFIED",
                    "run_id": api["run_id"], "scope": "api-check", "source_hash": api["source_hash"],
                    "data": {"api_review": None, "api_error": str(exc)}})
                state, events = _read(root)
        if state["current_source_digest"] != current:
            state = _publish(root, events, current)
        return {"count": state["count"], "head": state["head"]}
