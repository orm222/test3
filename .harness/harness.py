#!/usr/bin/env python3
"""One fresh Codex review, or explicit checks bound to current project files."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

sys.dont_write_bytecode = True
from verification import (acceptance, fingerprint, finish_process, read_json, ready_boundary, reports, start_process,
                          status, stop_process, verify, write_json)


def codex_command():
    executable = shutil.which("codex")
    if not executable:
        raise ValueError("Codex CLI가 없습니다. 허용된 환경에서 CLI와 로그인을 먼저 확인하세요.")
    if os.name == "nt" and Path(executable).suffix.lower() in (".cmd", ".bat"):
        # CMD reinterprets '&' and '%' even with shell=False; bypass npm's shim.
        entry = Path(executable).parent / "node_modules/@openai/codex/bin/codex.js"
        node = shutil.which("node.exe")
        if not node or not entry.is_file():
            raise ValueError("Windows 배치 래퍼 대신 네이티브 codex.exe 또는 npm Codex와 node.exe가 필요합니다.")
        return [node, str(entry)]
    return [executable]


def prepare_codex(root, timeout):
    command = codex_command()
    process = start_process(command + ["login", "status"], cwd=root,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
    try:
        exit_code = process.wait(timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        stop_process(process)
        raise
    finally:
        finish_process(process)
    if exit_code != 0:
        raise ValueError("Codex 로그인 정보가 없습니다. codex login 후 review를 다시 실행하세요.")
    return command


def project_file(root, name):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("리뷰 파일 경로가 비어 있습니다.")
    path = Path(name)
    if path.is_absolute() or ".." in path.parts or not (root / path).resolve().is_relative_to(root):
        raise ValueError("리뷰 파일은 프로젝트 내부 상대 경로여야 합니다: " + name)
    if not (root / path).is_file():
        raise ValueError("리뷰가 존재하지 않는 파일을 가리킵니다: " + name)
    return root / path


def validate_review(root, result):
    if not isinstance(result, dict) or set(result) != {"summary", "reviewed_files", "findings", "limitations", "criterion_scores"}:
        raise ValueError("리뷰 응답 형식이 올바르지 않습니다.")
    if not isinstance(result["summary"], str) or not result["summary"].strip():
        raise ValueError("리뷰 요약이 없습니다.")
    for key in ("reviewed_files", "findings", "limitations", "criterion_scores"):
        if not isinstance(result[key], list):
            raise ValueError("리뷰 배열 형식 오류: " + key)
    if not result["reviewed_files"]:
        raise ValueError("실제로 확인한 파일이 없는 리뷰입니다.")
    for name in result["reviewed_files"]:
        project_file(root, name)
    if any(not isinstance(item, str) or not item.strip() for item in result["limitations"]):
        raise ValueError("리뷰 한계는 비어 있지 않은 문자열이어야 합니다.")
    for finding in result["findings"]:
        if not isinstance(finding, dict) or set(finding) != {"severity", "file", "line", "input", "evidence"}:
            raise ValueError("리뷰 지적 형식이 올바르지 않습니다.")
        if finding["severity"] not in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            raise ValueError("리뷰 심각도가 올바르지 않습니다.")
        path = project_file(root, finding["file"])
        if type(finding["line"]) is not int or not 1 <= finding["line"] <= len(path.read_bytes().splitlines()):
            raise ValueError("리뷰 줄 번호가 파일 범위를 벗어났습니다.")
        if any(not isinstance(finding[key], str) or not finding[key].strip() for key in ("input", "evidence")):
            raise ValueError("리뷰 지적에는 발생 입력과 근거가 필요합니다.")
    return result


def canonical_command(command):
    if not isinstance(command, str):
        return ""
    # Unwrap only a known shell; keep the entire script so appended commands differ.
    ps = re.fullmatch(r'''\s*(?:"[^"\n]*[\\/](?:powershell|pwsh)\.exe"|'[^'\n]*[\\/](?:powershell|pwsh)\.exe'|(?:[^\s"']*[\\/])?(?:powershell|pwsh)(?:\.exe)?)\s+(?:(?:-NoProfile|-NonInteractive|-NoLogo)\s+)*-Command\s+(.+)''',
                      command, flags=re.IGNORECASE | re.DOTALL)
    if ps:
        script = ps.group(1).strip()
        if script.startswith('"') and script.endswith('"'):
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


def review_evidence(root, result, logfile, report_inputs, api_evidence=None):
    for entry in report_inputs.values():
        if reports.file_evidence(root, entry["path"])["sha256"] != entry["sha256"]:
            raise ValueError("Captured review input changed during the review")
    evidence = {"file:" + name: report_inputs.get("file:" + name) or
                reports.file_evidence(root, project_file(root, name)) for name in result["reviewed_files"]}
    for ref, entry in (api_evidence or {}).items():
        evidence[ref] = reports.validate_evidence(root, entry)
    seen, completed = set(), False
    for line in logfile.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = acceptance.decode(line)
        if not isinstance(event, dict) or event.get("type") in ("turn.failed", "error"):
            raise ValueError("Codex review reported an invalid or failed JSON event")
        if event.get("type") == "turn.completed":
            completed = True
        item = event.get("item", {})
        if event.get("type") != "item.completed" or not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        identity = item.get("id")
        if not isinstance(identity, str) or not identity or identity in seen:
            raise ValueError("Duplicate or missing review command event ID")
        seen.add(identity)
        if item.get("status") != "completed" or type(item.get("exit_code")) is not int:
            continue
        command = canonical_command(item.get("command"))
        if not command:
            raise ValueError("Completed review command has no command text")
        key = "command:" + command
        value = reports.file_evidence(root, logfile, kind="check",
                                      status="PASS" if item["exit_code"] == 0 else "FAIL",
                                      exit_code=item["exit_code"])
        # If a command was attempted more than once, a failed attempt remains visible.
        if key not in evidence or item["exit_code"] != 0:
            evidence[key] = value
    if not completed:
        raise ValueError("Codex review did not emit turn.completed")
    return evidence


def report_review(root, status, run_id=None, source_hash=None, **data):
    data.setdefault("review", None)
    reports.append_event(root, {"kind": "review", "scope": "legacy-review", "status": status,
                               "run_id": run_id, "source_hash": source_hash, "data": data})


def review(root, task, timeout=600, model=None, *, remaining_seconds, reserve_seconds):
    entered_at = time.monotonic()
    ready = ready_boundary(root)
    if not task.strip():
        raise ValueError("리뷰 대상·요구·판정 기준을 입력하세요.")
    for name, value in (("timeout", timeout), ("remaining_seconds", remaining_seconds),
                        ("reserve_seconds", reserve_seconds)):
        if type(value) is not int or value < 1:
            raise ValueError(name + "은 양의 초 단위 정수여야 합니다.")
    reports.initialize(root)
    reports.refresh(root)
    if remaining_seconds < timeout + reserve_seconds:
        report_review(root, "SKIP", limitations=["Insufficient review and repair time budget."])
        print("리뷰 생략: 리뷰 제한시간과 수정·관련 테스트·최종 검증·제출 예약 시간이 부족합니다.")
        return 3
    work = root / ".harness/work"
    claim = work / "independent-review-claim"
    if claim.exists() or any(work.glob("review-*")):
        report_review(root, "SKIP", limitations=["An independent review was already attempted; no automatic retry."])
        print("리뷰 생략: 이 프로젝트에서 독립 리뷰를 이미 시도했습니다. 자동 반복하지 않습니다.")
        return 3
    role = tomllib.loads((root / ".codex/agents/kty-reviewer.toml").read_text(encoding="utf-8"))
    instructions = role.get("developer_instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("리뷰 역할 지침이 없습니다.")
    roles_path = root / ".harness/review_instructions.md"
    procedure = roles_path.read_text(encoding="utf-8")
    schema = root / ".harness/review.schema.json"
    read_json(schema)
    criteria = reports.load_criteria(root)
    config_path = root / "automation/codex-loop/config.json"
    config = acceptance.read(config_path) if config_path.exists() else {}
    not_applicable = config.get("review_not_applicable", {})
    reports.validate_not_applicable(not_applicable, criteria)
    before = fingerprint(root)
    available = remaining_seconds - reserve_seconds - (time.monotonic() - entered_at)
    if available <= 0:
        report_review(root, "SKIP", source_hash=before, limitations=["Preparation reached the reserved repair time."])
        print("리뷰 생략: 준비 중 수정·검증·제출 예약 시간에 도달했습니다.")
        return 3
    try:
        command = prepare_codex(root, min(15, available))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        report_review(root, "UNVERIFIED", source_hash=before, limitations=[str(exc)])
        print("리뷰 준비 실패: " + str(exc), file=sys.stderr)
        return 1
    if time.monotonic() - entered_at >= remaining_seconds - reserve_seconds:
        report_review(root, "SKIP", source_hash=before, limitations=["Preparation reached the reserved repair time."])
        print("리뷰 생략: 준비 중 수정·검증·제출 예약 시간에 도달했습니다.")
        return 3
    run_dir = work / ("review-" + uuid.uuid4().hex)
    work.mkdir(parents=True, exist_ok=True)
    try:
        claim.mkdir()
    except FileExistsError:
        report_review(root, "SKIP", source_hash=before, limitations=["Another process already claimed the independent review."])
        print("리뷰 생략: 다른 실행에서 독립 리뷰를 이미 시작했습니다.")
        return 3
    write_json(claim / "claim.json", {"run_id": run_dir.name, "claimed_at": time.time()})
    run_dir.mkdir(parents=True)
    answer = run_dir / "review.json"
    receipt_path = run_dir / "receipt.json"
    receipt = {"completed": False, "fingerprint_before": before,
               "run_id": run_dir.name, "started_at": time.time(), "mode": "fresh-codex-exec", "exit_code": None,
               "remaining_seconds": remaining_seconds, "reserve_seconds": reserve_seconds,
               "timeout_seconds": timeout}
    write_json(receipt_path, receipt)
    report_review(root, "RUNNING", run_dir.name, before, goal=task, boundary_context=ready["context"])
    report_token = reports.guard(root)
    report_inputs = {}
    input_directory = run_dir / "report-inputs"
    input_directory.mkdir()
    for name in reports.MANAGED:
        captured = input_directory / Path(name).name
        captured.write_bytes((root / name).read_bytes())
        report_inputs["file:" + name] = reports.file_evidence(root, captured)
    prior_api = reports.current_api_evidence(root)
    prompt = f"""독립 코드 리뷰를 수행하라. 이것은 구현 요청이 아니다.
작성자의 이전 대화나 이전 리뷰 결과를 읽거나 재사용하지 말라.
파일 수정, 다른 에이전트 생성, 외부 전송, 권한 확대를 하지 말라.
프로젝트 지침의 구현·계획 승인 흐름은 이 읽기 전용 검토에 적용하지 않는다.

리뷰 역할 지침:
{instructions}

검수 절차:
{procedure}

요청한 검토 범위:
{task}

확정된 경계 조건과 실제 사용자 답변:
{ready['context']}
위 답변을 명세와 함께 검토 기준으로 사용하라. 답변·상태·질문 템플릿은 수정하지 말라.

공통 리뷰 기준:
{json.dumps(criteria, ensure_ascii=False)}
사전에 확정된 NA 기준과 사유: {json.dumps(not_applicable, ensure_ascii=False)}
criterion_scores에는 모든 criterion_id를 정확히 한 번씩 넣고 PASS/PARTIAL/FAIL/UNVERIFIED/NA,
reason, evidence_refs를 반환하라. 점수와 합계는 실행기가 계산한다.
실제로 읽은 파일 근거는 file:프로젝트상대경로, 이번 세션에서 실행한 명령 근거는
command:직접 실행한 정확한 셸 명령 문자열을 사용하라. 파일은 reviewed_files에도 포함하라.
실행하지 못한 항목은 UNVERIFIED로 기록하라. NA는 실행기가 사전에 허용한 경우만 가능하다.
근거 파일이나 성공한 명령이 있다는 사실만으로 관련 없는 기준을 PASS 처리하지 말라.
예를 들어 일반 단위 테스트 성공은 HAND02의 새 세션 문서만으로 인수인계한 검증이 아니다.
새 세션·다른 환경에서의 실제 검증을 하지 못했다면 해당 기준은 UNVERIFIED다.
사람이 수용하거나 이해했다는 사실을 임의로 확정하지 말라.
비밀정보를 발견해도 실제 값을 응답·기록에 복사하지 말고 위치·항목·노출 상태만 보고하라.
docs/progress.md와 docs/review-score.md는 실행기 전용 기록이므로 수정하지 말라.
두 자동 문서의 평가는 아래 캡처 파일을 읽고 원래 file:docs/... 이름을 evidence_refs에 사용하라.
이번 실행의 캡처이며 이전 리뷰 결론이 아니다. 캡처 파일도 수정하지 말라.
{json.dumps(report_inputs, ensure_ascii=False)}

선택 API 검수의 이전 실제 실행 근거:
{json.dumps(prior_api, ensure_ascii=False)}
context가 null이면 현재 코드에 유효한 API 검수 근거가 없다. 존재할 때만 원본 로그를 읽고
api-check:schemathesis 또는 api-check:hurl을 참조할 수 있다. 이는 이전 컨트롤러 실행이며
이번 리뷰에서 직접 실행한 명령으로 보고하지 말라. 이 근거 때문에 API 서버를 다시 실행하지 말라.
현재 코드와 원본 로그가 유효해도 검수 범위 밖의 기준이나 사람의 수용을 입증하지는 않는다.

현재 프로젝트 루트는 {root} 이다.
INTERVIEW.md, FUNCTIONAL_SPEC.md, PLAN.md, HANDOFF.md를 읽고 PLAN.md에 문서 매핑이 있으면 따른다.
누락·모호한 요구를 임의로 사실로 만들지 말고 limitations에 기록하라.
현재 코드와 테스트, 변경된 코드의 호출자를 직접 읽어 요구 누락, 잘못된 가정,
경계 입력과 예외 처리, 회귀, 독립 검산의 실제 검증력을 확인하라.
Git diff가 비어 있어도 현재 파일을 검토하라. .harness/work/의 이전 리뷰는 읽지 말라.
실행하지 않은 테스트를 통과로 보고하지 말라. 검증 실행이 막히면 한계를 밝혀라.
지적 0건도 유효하다. 스타일 취향만으로 지적을 만들지 말라.
최종 응답은 제공된 JSON 스키마를 따르고 한국어로 작성하라.
reviewed_files에는 실제 읽은 현재 파일의 프로젝트 상대 경로를 넣어라.
지적은 severity / file / line / input(발생 입력·상태와 실패 결과) / evidence로 반환하라.
file은 현재 존재하는 프로젝트 상대 경로, line은 1부터 시작하는 정확한 줄이다.
삭제된 파일의 문제는 현재 호출자 또는 남은 관련 코드에 연결하라.
"""
    try:
        command = command + ["exec", "--cd", str(root), "--sandbox", "read-only",
            "--ephemeral", "--config", 'approval_policy="never"', "--output-schema", str(schema),
            "--output-last-message", str(answer), "--json", "--color", "never"]
        if model is not None:
            command.extend(["--model", model])
        command.append("-")
        write_json(run_dir / "invocation.json", {"argv": command, "task": task})
        print("별도 Codex 리뷰 실행: " + str(run_dir), flush=True)
        with (run_dir / "process.log").open("w", encoding="utf-8") as log, \
                (run_dir / "process.stderr.log").open("w", encoding="utf-8") as errors:
            process = start_process(command, cwd=root, stdin=subprocess.PIPE, stdout=log,
                stderr=errors, text=True, encoding="utf-8",
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
            try:
                available = remaining_seconds - reserve_seconds - (time.monotonic() - entered_at)
                process.communicate(prompt, timeout=max(0, min(timeout, available)))
            except (subprocess.TimeoutExpired, KeyboardInterrupt):
                stop_process(process)
                raise
            finally:
                finish_process(process)
        receipt["exit_code"] = process.returncode
        if process.returncode != 0:
            raise ValueError("Codex 호출 실패. process.log를 확인하세요.")
        result = validate_review(root, read_json(answer))
        reports.guard(root, report_token)
        scored = reports.score_review(root, result["criterion_scores"],
                                      review_evidence(root, result, run_dir / "process.log", report_inputs, prior_api["evidence"]), not_applicable)
        after = fingerprint(root)
        receipt["fingerprint_after"] = after
        if before != after:
            raise ValueError("리뷰 중 파일이 바뀌었습니다. 현재 코드의 완료된 리뷰로 처리하지 않습니다.")
        receipt["completed"] = True
        receipt["blocking_findings"] = sum(item["severity"] in ("CRITICAL", "HIGH") for item in result["findings"])
        receipt["review_score"] = scored
        print(result["summary"])
        print("리뷰 결과: " + str(answer))
        print("메인이 지적을 재현·판단하고 수정 후 관련 검증을 다시 실행해야 합니다.")
        return 2 if receipt["blocking_findings"] else 0
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        receipt["error"] = str(exc)
        print("리뷰 미완료: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        receipt["error"] = "interrupted"
        raise
    finally:
        receipt["finished_at"] = time.time()
        write_json(receipt_path, receipt)
        try:
            reports.guard(root, report_token)
            report_review(root, "PASS" if receipt["completed"] and not receipt.get("blocking_findings") else
                          "FAIL" if receipt["completed"] else "UNVERIFIED", run_dir.name,
                          receipt.get("fingerprint_after") or before,
                          review=receipt.get("review_score"), limitations=[receipt["error"]] if receipt.get("error") else [])
        except (OSError, ValueError) as exc:
            receipt.update(completed=False, error="Review reporting failed: " + str(exc))
            write_json(receipt_path, receipt)
            return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("verify", "status", "fingerprint"):
        commands.add_parser(name)
    reviewer = commands.add_parser("review")
    reviewer.add_argument("task", help="검토 대상·요구·판정 기준")
    reviewer.add_argument("--model", help="허용된 모델을 명시할 때만 지정; 기본은 Codex 설정 유지")
    reviewer.add_argument("--timeout", type=int, default=600)
    reviewer.add_argument("--remaining-seconds", type=int, required=True,
                          help="실제 시계와 제출 마감으로 계산한 현재 남은 시간")
    reviewer.add_argument("--reserve-seconds", type=int, required=True,
                          help="수정·관련 테스트·최종 검증·제출에 반드시 남길 시간")
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.command == "verify":
            return verify(root)
        if args.command == "status":
            return status(root)
        if args.command == "fingerprint":
            print(fingerprint(root))
            return 0
        if args.timeout < 1:
            raise ValueError("timeout은 양의 초 단위 정수여야 합니다.")
        return review(root, args.task, args.timeout, args.model,
                      remaining_seconds=args.remaining_seconds, reserve_seconds=args.reserve_seconds)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print("오류: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("중단됨: 완료로 처리하지 않습니다.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
