import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT=Path(__file__).resolve().parents[2]
RUNNER=ROOT/"scripts/run_logged.py"

class ExternalLoggerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.folder=Path(self.temp.name)
        self.env=dict(os.environ,HARNESS_LOG_ROOT=str(self.folder/"evidence"))
    def tearDown(self):
        self.temp.cleanup()
    def invoke(self,args):
        return subprocess.run([sys.executable,str(RUNNER)]+args,env=self.env,capture_output=True,text=True,timeout=10)
    def test_real_success_failure_and_missing_executable(self):
        self.assertEqual(self.invoke([sys.executable,"-c","print('actual-success')"]).returncode,0)
        self.assertEqual(self.invoke([sys.executable,"-c","import sys; print('actual-failure');sys.exit(7)"]).returncode,7)
        self.assertEqual(self.invoke(["__nonexistent_diagnostic_executable__"]).returncode,127)
        self.assertIn("actual-success",(self.folder/"evidence/COMMANDS.md").read_text())
        self.assertIn("actual-failure",(self.folder/"evidence/ERRORS_AND_FIXES.md").read_text())
    def test_project_md_not_modified(self):
        targets=[ROOT/"docs/COMMANDS.md",ROOT/"docs/ERRORS_AND_FIXES.md"]
        before={p:p.read_bytes() for p in targets}
        self.assertEqual(self.invoke([sys.executable,"-c","print('external-only')"]).returncode,0)
        self.assertEqual(before,{p:p.read_bytes() for p in targets})
    def test_internal_log_path_rejected_before_command(self):
        self.env["HARNESS_LOG_ROOT"]=str(ROOT/"docs/unsafe-log-test")
        result=self.invoke([sys.executable,"-c","print('MUST_NOT_RUN')"])
        self.assertEqual(result.returncode,64)
        self.assertNotIn("MUST_NOT_RUN",result.stdout)
        self.assertFalse((ROOT/"docs/unsafe-log-test").exists())
    def test_concurrent_harness_stages_are_rejected(self):
        sentinel=self.folder/"started"
        fake=self.folder/"run_harness.py"
        fake.write_text("from pathlib import Path\nimport time\nPath("+repr(str(sentinel))+").write_text('yes')\ntime.sleep(1.5)\n")
        first=subprocess.Popen([sys.executable,str(RUNNER),sys.executable,str(fake)],env=self.env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
        try:
            deadline=time.monotonic()+5
            while not sentinel.exists() and time.monotonic()<deadline:
                time.sleep(.02)
            self.assertTrue(sentinel.exists())
            second=self.invoke([sys.executable,str(fake)])
            self.assertEqual(second.returncode,75)
            output,_=first.communicate(timeout=5)
            self.assertEqual(first.returncode,0,output)
        finally:
            if first.poll() is None:
                first.terminate(); first.communicate(timeout=5)
