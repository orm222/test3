"""Write-free unittest adapter: one JSON receipt on stdout, diagnostics on stderr."""
import argparse
import contextlib
import importlib.util
import json
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path.cwd()))
_spec = importlib.util.spec_from_file_location("unittest_acceptance", Path(__file__).with_name("acceptance.py"))
acceptance = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(acceptance)


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self.active = {}

    def startTest(self, test):
        super().startTest(test)
        row = {"id": test.id(), "status": "unverified"}
        self.records.append(row)
        self.active[id(test)] = row

    def record(self, test, status):
        if id(test) in self.active:
            self.active[id(test)]["status"] = status
        else:
            self.records.append({"id": test.id(), "status": status})

    def addSuccess(self, test):
        super().addSuccess(test)
        self.record(test, "passed")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.record(test, "failed")

    def addError(self, test, err):
        super().addError(test, err)
        if id(test) in self.active:
            self.record(test, "error")
        else:
            self.records.append({"id": test.id(), "status": "error"})

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.record(test, "skipped")

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self.record(test, "expected_failure")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self.record(test, "unexpected_success")

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.record(test, "failed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-directory", default="tests")
    parser.add_argument("--pattern", default="test*.py")
    args = parser.parse_args()
    report = dict(schema="project-acceptance/v1", status="failed", test_results=[])
    try:
        contract = acceptance.load_contract(Path.cwd())
        # Test prints belong in diagnostics, never in the machine-readable receipt.
        with contextlib.redirect_stdout(sys.stderr):
            loader = unittest.TestLoader()
            suite = loader.discover(args.start_directory, pattern=args.pattern)
            collected = suite.countTestCases()
            result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2, resultclass=RecordedResult).run(suite)
        rows = result.records
        valid = (collected > 0 and result.testsRun == collected == len(rows)
                 and len({row["id"] for row in rows}) == len(rows)
                 and not loader.errors
                 and all(row["status"] == "passed" for row in rows))
        report = dict(schema="project-acceptance/v1", status="passed" if valid else "failed",
                      collected_tests=collected, tests_run=result.testsRun, test_results=rows)
        if valid:
            acceptance.validate_report(report, contract)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        report.update(status="failed", error=str(exc))
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
