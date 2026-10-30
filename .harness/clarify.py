#!/usr/bin/env python3
"""Ask boundary questions before implementation; never invent a human answer."""
import argparse
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
import uuid

sys.dont_write_bytecode = True
import boundary

BASE = Path(__file__).resolve().parent


def configuration(root, name):
    if name is None:
        path = root / "automation/codex-loop/config.json"
        if not path.exists():
            return {}, None
        name = path.relative_to(root).as_posix()
    candidate = Path(name)
    path = candidate if candidate.is_absolute() else root / candidate
    boundary.require(path.is_file() and path.absolute() == path.resolve() and not path.is_symlink(),
                     "Clarify config must be an ordinary file without redirected paths")
    value = boundary.decode(path.read_text(encoding="utf-8-sig"))
    boundary.require(isinstance(value, dict), "Clarify config must be an object")
    return value, path


def selected_spec(root, explicit, config):
    value = explicit or config.get("spec")
    if value is None and (root / boundary.STATE).exists():
        value = boundary.load_state(root, current=False)["spec"]
    boundary.require(boundary.text(value), "Supply --spec or config.spec before clarification")
    return boundary.project_path(root, value).relative_to(root).as_posix()


def source_snapshot(root):
    files = {}
    excluded = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
    for directory, dirs, names in os.walk(str(root), followlinks=False):
        relative = Path(directory).relative_to(root)
        kept = []
        for name in sorted(dirs):
            path = Path(directory) / name
            rel = (relative / name).as_posix()
            if name in excluded or rel == ".harness/work":
                continue
            boundary.require(not path.is_symlink() and path.resolve() == path.absolute(), "Redirected project directory: " + rel)
            kept.append(name)
        dirs[:] = kept
        for name in sorted(names):
            path = Path(directory) / name
            rel = (relative / name).as_posix()
            mode = path.lstat().st_mode
            boundary.require(stat.S_ISREG(mode) and not path.is_symlink(), "Special/symlink project file: " + rel)
            files[rel] = [stat.S_IMODE(mode), boundary.digest(path.read_bytes())]
    return files


def codex_command(config):
    argv = config.get("codex_command", ["codex"])
    boundary.require(isinstance(argv, list) and argv and all(boundary.text(arg) for arg in argv), "Invalid codex_command")
    executable = shutil.which(argv[0])
    boundary.require(executable is not None, "Codex CLI is missing; install it and sign in before clarify")
    if os.name == "nt" and Path(executable).suffix.lower() in (".cmd", ".bat"):
        entry = Path(executable).parent / "node_modules/@openai/codex/bin/codex.js"
        node = shutil.which("node.exe")
        boundary.require(Path(executable).stem.lower() == "codex" and node and entry.is_file(),
                         "Use native codex.exe or npm Codex with node.exe; arbitrary batch shims are unsupported")
        return [node, str(entry)] + argv[1:]
    return [executable] + argv[1:]


@contextlib.contextmanager
def state_lock(root):
    path = boundary.project_path(root, ".harness/work/clarify.lock", exists=False, runtime=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise boundary.BoundaryError("Another clarification owns .harness/work/clarify.lock; confirm it has stopped before removing the lock") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        yield
    finally:
        path.unlink()


def show(state):
    print("Boundary status: " + state["status"])
    answers = boundary.actual_answers(state)
    for question in state["questions"]:
        if question["id"] not in answers:
            print("\n[{} | {}]\n질문: {}\n확정할 내용: {}".format(
                question["id"], question["template"], question["question"], question["decides"]))
    if state["status"] == "needs_analysis":
        print("Actual answers were recorded. Run clarify again to check whether anything remains ambiguous.")
    if state["error"]:
        print("Clarification needs attention: " + state["error"])


def analyze(root, spec, config, model, timeout):
    boundary.require(boundary.text(model) and not model.startswith("SET_"), "Supply an actual model ID with --model or config.builder_model")
    boundary.require(type(timeout) is int and timeout > 0, "Clarify timeout must be a positive integer")
    with state_lock(root):
        state = (boundary.load_state(root, current=False) if (root / boundary.STATE).exists()
                 else boundary.create_state(root, spec))
        unanswered = {q["id"] for q in state["questions"]} - set(boundary.actual_answers(state))
        if unanswered:
            show(state)
            raise boundary.BoundaryError("Submit actual answers for existing questions before reanalysis: " + ", ".join(sorted(unanswered)))
        state["spec"] = spec
        # Start each analysis unready. Failure cannot leave an older ready decision usable.
        state["status"], state["analysis"], state["error"] = "analyzing", None, None
        state["references"] = [name for name in state["references"] if (root / name).is_file()]
        state["input_hashes"] = boundary.input_hashes(root, spec, state["references"])
        boundary.save_state(root, state)
        try:
            command = codex_command(config)
            run_id = "clarify-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
            answer = boundary.project_path(root, ".harness/work/" + run_id + "/answer.json", exists=False, runtime=True)
            answer.parent.mkdir(parents=True, exist_ok=False)
            context = {"spec": spec, "template": boundary.TEMPLATE, "previous_questions": state["questions"],
                       "actual_answers": state["answers"], "previous_references": state["references"]}
            prompt = (BASE / "clarify_prompt.md").read_text(encoding="utf-8")
            prompt += "\n\nBOUNDARY_CLARIFICATION_CONTEXT\n" + json.dumps(context, ensure_ascii=False)
            (answer.parent / "prompt.txt").write_text(prompt, encoding="utf-8")
            argv = command + ["--ask-for-approval", "never", "exec", "--cd", str(root), "--model", model,
                              "--sandbox", "read-only", "--skip-git-repo-check", "--json",
                              "--output-schema", str(BASE / "clarify.schema.json"), "--output-last-message", str(answer),
                              "--config", 'shell_environment_policy.set.PYTHONDONTWRITEBYTECODE="1"', "-"]
            loader = importlib.util.spec_from_file_location("clarify_processes", BASE / "verification.py")
            processes = importlib.util.module_from_spec(loader)
            loader.loader.exec_module(processes)
            before = source_snapshot(root)
            environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
            environment.pop("PYTHONHOME", None)
            with (answer.parent / "prompt.txt").open("rb") as stdin, (answer.parent / "stdout.log").open("wb") as stdout, (answer.parent / "stderr.log").open("wb") as stderr:
                process = processes.start_process(argv, cwd=str(root), stdin=stdin, stdout=stdout, stderr=stderr,
                    env=environment, shell=False, start_new_session=os.name != "nt",
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
                deadline = time.monotonic() + timeout
                try:
                    while process.poll() is None:
                        boundary.require(source_snapshot(root) == before, "Project source changed during read-only clarification")
                        if time.monotonic() >= deadline:
                            raise boundary.BoundaryError("Clarification timed out")
                        time.sleep(0.1)
                    boundary.require(process.returncode == 0, "Clarification CLI failed; see " + str(answer.parent))
                except BaseException:
                    processes.stop_process(process)
                    raise
                finally:
                    processes.finish_process(process)
            boundary.require(source_snapshot(root) == before, "Project source changed during read-only clarification")
            boundary.project_path(root, answer.relative_to(root).as_posix(), runtime=True)
            report = boundary.decode(answer.read_text(encoding="utf-8-sig"))
            boundary.complete_analysis(root, state, report)
        except BaseException as exc:
            state["status"], state["analysis"], state["error"] = "failed", None, str(exc) or type(exc).__name__
            boundary.save_state(root, state)
            raise
        show(state)
        return 0 if state["status"] == "ready" else 2


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(BASE.parent))
    parser.add_argument("--spec")
    parser.add_argument("--config")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=int, default=600)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--answer", metavar="Q-ID")
    modes.add_argument("--status", action="store_true")
    modes.add_argument("--check-ready", action="store_true")
    parser.add_argument("--answer-file")
    args = parser.parse_args(arguments)
    try:
        boundary.require((sys.version_info.major, sys.version_info.minor) == (3, 9), "Clarify requires Python 3.9")
        root = Path(args.root).resolve()
        config, config_path = configuration(root, args.config)
        if "project" in config:
            boundary.require(boundary.text(config["project"]), "Config project must be a path")
            project = config_path.parent / config["project"]
            boundary.require(project.is_dir(), "Config project does not exist")
            root = project.resolve()
        spec = selected_spec(root, args.spec, config)
        boundary.require(bool(args.answer) == bool(args.answer_file), "Use --answer Q-ID together with --answer-file UTF8_FILE")
        if args.answer:
            answer_file = Path(args.answer_file).expanduser()
            boundary.require(answer_file.is_file() and not answer_file.is_symlink()
                             and answer_file.absolute() == answer_file.resolve(), "Answer file must be an ordinary UTF-8 file")
            with state_lock(root):
                state = boundary.load_state(root, current=False)
                boundary.require(state["spec"] == spec, "Answer belongs to a different specification; reanalyze first")
                state = boundary.submit_answer(root, args.answer, answer_file.read_text(encoding="utf-8-sig"))
            show(state)
            return 0
        if args.status or args.check_ready:
            state = boundary.load_state(root)
            if args.status:
                show(state)
            boundary.require_ready(root, spec)
            if args.check_ready:
                print("Boundary questions ready")
            return 0
        return analyze(root, spec, config, args.model or config.get("builder_model"), args.timeout)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        print("Boundary clarification stopped: " + str(exc), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Boundary clarification interrupted; analysis is not ready", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
