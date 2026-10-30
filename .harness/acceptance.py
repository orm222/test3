"""One project-owned acceptance contract shared by verification and the loop."""
import json
from pathlib import Path
import sys

SCHEMA = "project-acceptance/v1"
CONTRACT = ".harness/acceptance.json"
SUCCESS_REGEX = r'"status"\s*:\s*"passed"'


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate JSON key: " + key)
        result[key] = value
    return result


def invalid_constant(value):
    raise ValueError("Invalid JSON constant: " + value)


def decode(text):
    return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid_constant)


def read(path):
    try:
        return decode(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ValueError("Cannot read acceptance JSON: " + str(path)) from exc


def project_path(root, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Acceptance paths must be nonempty project-relative paths")
    relative = Path(value)
    if (relative.parts and relative.parts[0] in (".git", "work", ".venv", "venv")
            or relative.parts[:2] == (".harness", "work")):
        raise ValueError("Acceptance files cannot be stored in runtime/state directories: " + value)
    path = root / relative
    if (relative.anchor or ".." in relative.parts or path.resolve() != path.absolute()
            or not path.resolve().is_relative_to(root)):
        raise ValueError("Acceptance path is outside or redirected: " + value)
    if not path.exists():
        raise ValueError("Missing acceptance path: " + value)
    return path


def resolved_argv(argv):
    return [sys.executable if arg == "{python}" else arg for arg in argv]


def load_contract(root):
    root = Path(root).resolve()
    path = project_path(root, CONTRACT)
    value = read(path)
    if not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1:
        raise ValueError("Acceptance contract version must be 1")
    argv = value.get("checker_argv")
    if (not isinstance(argv, list) or len(argv) < 3 or argv[:2] != ["{python}", "-B"]
            or any(not isinstance(arg, str) or not arg or "\0" in arg for arg in argv)):
        raise ValueError("Configure checker_argv as ['{python}', '-B', 'project/checker.py', ...]")
    checker = project_path(root, argv[2])
    if not checker.is_file() or checker.suffix != ".py":
        raise ValueError("Acceptance checker must be a project Python script")
    if checker.name in ("run_harness.py", "harness.py", "run.py"):
        raise ValueError("Acceptance checker must not recursively invoke a controller")
    manifest = project_path(root, value.get("required_ids_manifest"))
    required = read(manifest)
    ids = required.get("required_test_ids") if isinstance(required, dict) else None
    if (not isinstance(required, dict) or type(required.get("version")) is not int or required["version"] != 1
            or not isinstance(ids, list) or not ids
            or any(not isinstance(item, str) or not item.strip() for item in ids)
            or len(ids) != len(set(ids))):
        raise ValueError("Manifest needs version 1 and unique, nonempty required_test_ids")
    protected = value.get("protected_paths")
    if not isinstance(protected, list) or not protected:
        raise ValueError("Register the acceptance tests and reference inputs in protected_paths")
    files = {path, checker, manifest}
    for item in protected:
        target = project_path(root, item)
        members = list(target.rglob("*")) if target.is_dir() else [target]
        selected = []
        for member in members:
            if "__pycache__" in member.parts:
                continue
            project_path(root, member.relative_to(root).as_posix())
            if member.is_file():
                selected.append(member)
            elif not member.is_dir():
                raise ValueError("Unsupported acceptance path: " + str(member))
        if not selected:
            raise ValueError("Acceptance protection has no files: " + item)
        files.update(selected)
    mapping = value.get("boundary_tests", {})
    if (not isinstance(mapping, dict)
            or any(not isinstance(question, str) or not question.strip()
                   or not isinstance(tests, list) or not tests
                   or any(not isinstance(test, str) or test not in ids for test in tests)
                   or len(tests) != len(set(tests)) for question, tests in mapping.items())):
        raise ValueError("boundary_tests must map question IDs to nonempty unique required_test_ids")
    return dict(checker_argv=argv, required_test_ids=ids, boundary_tests=mapping,
                protected_files=sorted(map(str, files)))


def validate_boundary(contract, ready):
    """Every answered decision needs an explicit mandatory acceptance test."""
    expected = set(ready["answered_question_ids"])
    mapped = set(contract["boundary_tests"])
    if mapped != expected:
        missing, unknown = sorted(expected - mapped), sorted(mapped - expected)
        raise ValueError("boundary_tests must cover exactly the answered boundary question IDs; "
                         "missing=" + repr(missing) + ", unknown=" + repr(unknown))


def is_checker(argv, contract):
    return resolved_argv(argv) == resolved_argv(contract["checker_argv"])


def validate_registration(checks, contract, command_key, name_key):
    for check in checks:
        if str(check.get(name_key, "")).lower() == "acceptance" and not is_checker(check[command_key], contract):
            raise ValueError("Registered acceptance command differs from .harness/acceptance.json")


def validate_report(report, contract):
    if not isinstance(report, dict) or report.get("schema") != SCHEMA or report.get("status") != "passed":
        raise ValueError("Acceptance report must declare project-acceptance/v1 and passed")
    rows = report.get("test_results")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Acceptance report has no executed checks")
    seen, passed = set(), set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            raise ValueError("Acceptance result needs a nonempty test ID")
        if row["id"] in seen:
            raise ValueError("Duplicate acceptance test ID: " + row["id"])
        seen.add(row["id"])
        if row.get("status") == "passed":
            passed.add(row["id"])
        else:
            raise ValueError("Acceptance check did not pass: " + row["id"])
    missing = set(contract["required_test_ids"]) - passed
    if missing:
        raise ValueError("Required acceptance IDs did not execute and pass: " + ", ".join(sorted(missing)))
    return report


def validate_output(text, contract, mixed=False):
    if not mixed:
        return validate_report(decode(text), contract)
    # Codex merges stdout with stderr. Find the single JSON receipt among test logs.
    receipts, cursor = [], 0
    while cursor < len(text):
        # Only whole top-level documents at line boundaries are candidates. Consume
        # a balanced object even when invalid, so nested receipts cannot be salvaged.
        endline = text.find("\n", cursor)
        endline = len(text) if endline < 0 else endline + 1
        start = cursor
        while start < endline and text[start] in " \t\r":
            start += 1
        if start >= len(text) or text[start] not in "{[":
            cursor = endline
            continue
        brackets, quoted, escaped, end = [], False, False, None
        for index in range(start, len(text)):
            char = text[index]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char in "{[":
                brackets.append(char)
            elif char in "}]":
                if not brackets or brackets[-1] != ("{" if char == "}" else "["):
                    raise ValueError("Mismatched JSON brackets in executed acceptance output")
                brackets.pop()
                if not brackets:
                    end = index + 1
                    break
        if end is None:
            raise ValueError("Unterminated JSON document in executed acceptance output")
        candidate = text[start:end]
        cursor = end
        try:
            value = decode(candidate)
        except ValueError:
            if SCHEMA in candidate:
                raise ValueError("Malformed acceptance JSON document in executed output")
            continue
        if isinstance(value, dict) and value.get("schema") == SCHEMA:
            receipts.append(value)
    if len(receipts) != 1:
        raise ValueError("Expected exactly one acceptance JSON receipt in executed output")
    return validate_report(receipts[0], contract)
