"""Tests for the compare_with_base orchestration in run_action.py.

These cover the base-vs-PR comparison feature. The heavy detection engine
(duplicate_code_detection.run) and every GitHub interaction (the requests module)
are mocked, so the tests are hermetic and require no network, git, or NLP deps.

Regression target: a PR that introduces no new duplication must not fail just
because the codebase already contains pre-existing similarity above fail_above.
"""
import io
import os
import sys
import json
import types
import shutil
import unittest
import contextlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Stub the heavy / network deps before importing the modules under test. The
# engine's run() is mocked in every test, so these are never really exercised.
for _name in ("gensim", "astor", "requests"):
    sys.modules.setdefault(_name, types.ModuleType(_name))
_nltk = types.ModuleType("nltk")
_nltk_tok = types.ModuleType("nltk.tokenize")
_nltk_tok.word_tokenize = lambda s: s.split()
_nltk.tokenize = _nltk_tok
sys.modules.setdefault("nltk", _nltk)
sys.modules.setdefault("nltk.tokenize", _nltk_tok)

import duplicate_code_detection
import run_action

RC = duplicate_code_detection.ReturnCode

# A codebase whose a.java/b.java are 90% similar - pre-existing, above fail_above=80.
PRE_EXISTING = {"a.java": {"b.java": 90.0}, "b.java": {"a.java": 90.0}}


def _fake_requests():
    """Fake `requests` that records the posted/patched comment bodies so tests can
    assert on the actual report content, not just the return code."""
    posted = []

    def _post(url, json=None, headers=None):
        posted.append(json.get("body") if json else None)
        return types.SimpleNamespace(status_code=201, text="")

    def _patch(url, json=None, headers=None):
        posted.append(json.get("body") if json else None)
        return types.SimpleNamespace(status_code=200, text="")

    m = types.SimpleNamespace()
    m.get = lambda *a, **k: types.SimpleNamespace(json=lambda: [], status_code=200)
    m.post = _post
    m.patch = _patch
    m.posted = posted
    return m


class CompareWithBaseTest(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = os.path.join(
            os.environ.get("TMPDIR", "/tmp"), "dcdt_test_%d" % os.getpid()
        )
        os.makedirs(self._tmp, exist_ok=True)
        os.chdir(self._tmp)  # run_action writes message.md into cwd
        self._env = dict(os.environ)
        os.environ.update({
            "INPUT_FAIL_ABOVE": "80",
            "INPUT_DIRECTORIES": "src",
            "INPUT_IGNORE_DIRECTORIES": "",
            "INPUT_PROJECT_ROOT_DIR": ".",
            "INPUT_FILE_EXTENSIONS": "java",
            "INPUT_IGNORE_BELOW": "30",
            "INPUT_ONLY_CODE": "",
            "INPUT_MAX_INCREASE": "10",
            "INPUT_WARN_ABOVE": "50",
            "INPUT_HEADER_MESSAGE_START": "## dup report",
            "GITHUB_REPOSITORY": "astubbs/parallel-consumer",
            "GITHUB_API_URL": "https://api.github.com",
            "INPUT_GITHUB_TOKEN": "x",
            "INPUT_ONE_COMMENT": "true",
        })
        self._orig_run = duplicate_code_detection.run
        self._orig_requests = run_action.requests
        self._requests = _fake_requests()
        run_action.requests = self._requests

    def tearDown(self):
        duplicate_code_detection.run = self._orig_run
        run_action.requests = self._orig_requests
        os.environ.clear()
        os.environ.update(self._env)
        os.chdir(self._cwd)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _last_body(self):
        return self._requests.posted[-1] if self._requests.posted else ""

    def _run(self, argv, engine_result):
        """Drive run_action.main() with a mocked engine, return (rc, stdout)."""
        duplicate_code_detection.run = lambda *a, **k: engine_result
        old_argv = sys.argv
        sys.argv = argv
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                rc = run_action.main()
        finally:
            sys.argv = old_argv
        return rc, out.getvalue()

    def _write_base(self, data):
        path = os.path.join(self._tmp, "base_results.json")
        with open(path, "w") as f:
            json.dump(data, f)
        return path

    def test_base_scan_json_only_exits_zero_even_above_threshold(self):
        """--json-only is a data dump for the base scan: it must exit 0 on a
        successful scan even if similarity exceeds fail_above, otherwise
        entrypoint.sh discards the base data and comparison silently degrades."""
        rc, out = self._run(
            ["run_action.py", "--pull-request-id", "1", "--json-only"],
            (RC.THRESHOLD_EXCEEDED, dict(PRE_EXISTING)),
        )
        self.assertEqual(rc, RC.SUCCESS.value)
        self.assertEqual(json.loads(out), PRE_EXISTING)  # clean JSON on stdout

    def test_base_scan_json_only_propagates_bad_input(self):
        """Genuine failures (bad input) must still surface as non-zero, and must
        restore stdout so the message is visible (not swallowed by the capture)."""
        rc, out = self._run(
            ["run_action.py", "--pull-request-id", "1", "--json-only"],
            (RC.BAD_INPUT, {}),
        )
        self.assertEqual(rc, RC.BAD_INPUT.value)
        # The message is only visible if stdout was restored before printing.
        self.assertIn("bad user input", out)

    def test_pr_scan_passes_when_no_new_duplication(self):
        """Base == PR: pre-existing 90% similarity, nothing new introduced.
        Must PASS despite being above the absolute fail_above, and the CI log must
        NOT print the failure banner (it reflects the post-comparison verdict)."""
        base = self._write_base(PRE_EXISTING)
        rc, out = self._run(
            ["run_action.py", "--pull-request-id", "1", "--base-results", base],
            (RC.THRESHOLD_EXCEEDED, dict(PRE_EXISTING)),
        )
        self.assertEqual(rc, RC.SUCCESS.value)
        self.assertNotIn("Action failed", out)  # log must match the SUCCESS verdict
        self.assertIn("No new or increased", self._last_body())

    def test_pr_scan_fails_when_similarity_increases_beyond_max(self):
        """A real regression: a pair jumps from 70% (base) to 90% (PR), a +20
        increase over max_increase=10, must FAIL."""
        base = self._write_base({"a.java": {"b.java": 70.0}, "b.java": {"a.java": 70.0}})
        rc, _ = self._run(
            ["run_action.py", "--pull-request-id", "1", "--base-results", base],
            (RC.THRESHOLD_EXCEEDED, dict(PRE_EXISTING)),
        )
        self.assertEqual(rc, RC.THRESHOLD_EXCEEDED.value)

    def test_pr_scan_fails_when_new_pair_introduced_above_fail_above(self):
        """A PR that introduces a brand-new highly-similar pair (absent from base,
        90% > fail_above=80) is newly-introduced duplication and must FAIL -
        even though it is 'new', not an 'increase' of an existing pair."""
        base = self._write_base({})  # neither file exists on base
        rc, out = self._run(
            ["run_action.py", "--pull-request-id", "1", "--base-results", base],
            (RC.THRESHOLD_EXCEEDED, dict(PRE_EXISTING)),  # a.java/b.java = 90
        )
        self.assertEqual(rc, RC.THRESHOLD_EXCEEDED.value)
        self.assertIn("Action failed", out)  # log matches the FAIL verdict
        body = self._last_body()
        self.assertIn("New file similarities introduced", body)
        self.assertIn(":x:", body)  # the new pair is flagged as failing

    def test_pr_scan_passes_when_new_pair_below_fail_above(self):
        """A brand-new pair below fail_above (50% < 80%) is not duplication worth
        failing - it must PASS."""
        base = self._write_base({})
        rc, _ = self._run(
            ["run_action.py", "--pull-request-id", "1", "--base-results", base],
            (RC.SUCCESS, {"a.java": {"b.java": 50.0}, "b.java": {"a.java": 50.0}}),
        )
        self.assertEqual(rc, RC.SUCCESS.value)

    def test_pr_scan_without_base_still_gates_on_absolute(self):
        """When there is no base to compare against (comparison off/unavailable),
        the absolute fail_above remains the gate - unchanged behaviour."""
        rc, _ = self._run(
            ["run_action.py", "--pull-request-id", "1"],
            (RC.THRESHOLD_EXCEEDED, dict(PRE_EXISTING)),
        )
        self.assertEqual(rc, RC.THRESHOLD_EXCEEDED.value)

    def test_new_pair_exactly_at_fail_above_passes(self):
        """Boundary: a new pair exactly AT fail_above (80) must PASS - the gate is
        strict `> fail_above`, so equality is not a failure."""
        base = self._write_base({})
        rc, _ = self._run(
            ["run_action.py", "--pull-request-id", "1", "--base-results", base],
            (RC.SUCCESS, {"a.java": {"b.java": 80.0}, "b.java": {"a.java": 80.0}}),
        )
        self.assertEqual(rc, RC.SUCCESS.value)

    def test_increase_exactly_at_max_increase_passes_despite_float_noise(self):
        """Boundary + float noise: an increase of exactly max_increase (10) must
        PASS. 80.01 - 70.01 is 10.000000000000014 in binary float; rounding keeps
        it at the boundary so it does not spuriously fail."""
        base = self._write_base({"a.java": {"b.java": 70.01}, "b.java": {"a.java": 70.01}})
        rc, _ = self._run(
            ["run_action.py", "--pull-request-id", "1", "--base-results", base],
            (RC.SUCCESS, {"a.java": {"b.java": 80.01}, "b.java": {"a.java": 80.01}}),
        )
        self.assertEqual(rc, RC.SUCCESS.value)

    def test_corrupt_base_results_degrades_to_absolute_without_crashing(self):
        """A truncated/corrupt base-results file must NOT crash the action; it
        degrades to absolute-threshold gating (the engine's raw verdict) with a
        warning, matching the base-scan's own fault tolerance."""
        path = os.path.join(self._tmp, "base_results.json")
        with open(path, "w") as f:
            f.write('{"a.java": {"b.java": 90.0}')  # truncated JSON
        rc, out = self._run(
            ["run_action.py", "--pull-request-id", "1", "--base-results", path],
            (RC.THRESHOLD_EXCEEDED, dict(PRE_EXISTING)),
        )
        self.assertEqual(rc, RC.THRESHOLD_EXCEEDED.value)  # fell back to absolute
        self.assertIn("falling back to absolute-threshold gating", out)

    def test_compute_delta_dedupes_symmetric_pairs(self):
        """The engine emits both (A,B) and (B,A); compute_delta must count the
        unordered pair once, not twice, in the report."""
        new_pairs, increased = run_action.compute_delta(
            {"a.java": {"b.java": 99.0}, "b.java": {"a.java": 99.0}}, {}
        )
        self.assertEqual(len(new_pairs), 1)
        self.assertEqual(increased, [])


if __name__ == "__main__":
    unittest.main()
