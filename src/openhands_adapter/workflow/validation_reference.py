"""Pinned ContextSniper validation semantics; execution is supplied by Agent-Diet.

Source: ContextSniper/ContextSniper-Codex/evaluator/validator.py
Source SHA256: ca535b71a2547832228244aa7f049a1daaef2f362c672cee15752e94e9247af6
Do not alter evidence/classification rules without differential verification.
"""
from __future__ import annotations
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable

BUILTIN_TEST_SYSTEMS = {
    "autotools", "bazel", "cmake", "make", "meson", "ninja",
}


AUTHORITATIVE_TEST_ID_SOURCES = frozenset({
    "caller-supplied",
    "configured-pattern",
    "framework-marker",
    "structured-report",
})


TEST_EXECUTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"\b(?:collected|ran)\s+[1-9]\d*\s+(?:items?|tests?)\b",
        r"\b[1-9]\d*\s+(?:passed|failed|errors?|tests?)\b",
        r"\btests?\s+run:\s*[1-9]\d*\b",
        r"\btest result:\s*(?:ok|failed)\b",
        r"^\s*(?:ok|not ok)\s+[1-9]\d*\b",
        r"^\s*(?:ok|fail)\s+\S+",
        r"\b\d+%\s+tests passed\b",
        r"\b(?:ok|fail):\s*[1-9]\d*\b",
        r"\b[1-9]\d*\s+examples?,\s*\d+\s+failures?\b",
        r"\btest suites?:\s*.*\b(?:passed|failed)\b",
        r"\btests?:\s*.*\b(?:passed|failed)\b",
    )
)


ZERO_TEST_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"no tests? (?:were )?(?:found|ran|to run)",
        r"running 0 tests",
        r"collected 0 items",
        r"ran 0 tests",
        r"tests? run:\s*0\b",
        r"\[no test files\]",
        r"\[no tests? to run\]",
        r"no tests? (?:to run|to execute)",
    )
)


TEST_FAILURE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    for pattern in (
        r"\b[1-9]\d*\s+failed\b",
        r"\btest result:\s*failed\b",
        r"\bfail(?:ures?)?:\s*[1-9]\d*\b",
        r"\berrors?:\s*[1-9]\d*\b",
        r"^\s*not ok\s+[1-9]\d*\b",
        r"^\s*fail\s+\S+",
        r"\bthe following tests failed\b",
    )
)


class ProjectValidator:
    def _run_target_and_regression(
        self,
        root: Path,
        plan: BuildPlan,
        artifact_dir: Path,
        failing_tests: tuple[str, ...],
    ) -> dict:
        target_commands = self._target_commands(plan, failing_tests)
        if not target_commands:
            regression = self._run_plan(
                root,
                plan,
                artifact_dir,
                prefix="patched-regression",
                test_commands=plan.regression_test,
                test_scope="regression",
            )
            regression["target_tests"] = list(failing_tests)
            regression["target_commands"] = []
            regression["target_selection_mode"] = "full-suite-only"
            regression["target_executed"] = False
            regression["target_status"] = "not-run"
            regression["target_passed"] = None
            regression["regression_executed"] = bool(
                regression.get("tests_executed")
            )
            regression["regression_status"] = regression.get("status")
            regression["regression_passed"] = regression.get("status") == "plausible"
            if (
                regression.get("status") == "failing"
                and not self._has_authoritative_test_ids(regression)
            ):
                regression["status"] = "invalid"
                regression["validation_error"] = (
                    "regression_test_ids_unverified:"
                    + str(regression.get("test_id_source") or "none")
                )
            return regression

        target = self._run_plan(
            root,
            plan,
            artifact_dir,
            prefix="patched-target",
            test_commands=target_commands,
            test_scope="target",
        )
        target["target_tests"] = list(failing_tests)
        target["target_commands"] = [item.as_dict() for item in target_commands]
        target["target_selection_mode"] = "configured-target"
        target["target_executed"] = True
        target["target_status"] = target.get("status")
        if target.get("status") not in {"plausible", "failing"}:
            target["status"] = "invalid"
            target["validation_error"] = (
                "target_test_invalid:" + str(target.get("validation_error") or "unknown")
            )
            return target
        if not self._has_authoritative_test_ids(target):
            target["status"] = "invalid"
            target["validation_error"] = (
                "target_test_ids_unverified:"
                + str(target.get("test_id_source") or "none")
            )
            return target

        matched_failed = self._matching_requested_ids(
            target.get("failed_test_ids", []), failing_tests
        )
        matched_passed = self._matching_requested_ids(
            target.get("passed_test_ids", []), failing_tests
        )
        observed_requested = matched_failed | matched_passed
        if observed_requested != set(failing_tests):
            target["status"] = "invalid"
            target["validation_error"] = "target_test_unverified"
            return target

        target_failed_ids = sorted(matched_failed)
        target_passed_ids = sorted(set(failing_tests) - matched_failed)

        regression = self._run_plan(
            root,
            plan,
            artifact_dir,
            prefix="patched-regression",
            test_commands=plan.regression_test,
            test_scope="regression",
        )
        regression["target_tests"] = list(failing_tests)
        regression["target_commands"] = [item.as_dict() for item in target_commands]
        regression["target_status"] = target.get("status")
        regression["target_passed"] = not target_failed_ids
        regression["target_test_id_source"] = target.get("test_id_source", "none")
        regression["regression_test_id_source"] = regression.get(
            "test_id_source", "none"
        )
        regression["target_selection_mode"] = "configured-target"
        regression["target_executed"] = True
        regression["target_output"] = "\n\n".join(
            item.get("output", "") for item in target.get("test_commands", [])
        )
        regression["regression_status"] = regression.get("status")
        regression["regression_passed"] = regression.get("status") == "plausible"
        regression["regression_executed"] = bool(regression.get("tests_executed"))
        if regression.get("status") not in {"plausible", "failing"}:
            regression["status"] = "invalid"
            regression["validation_error"] = (
                "regression_invalid:"
                + str(regression.get("validation_error") or "unknown")
            )
            return regression
        if (
            regression.get("status") == "failing"
            and not self._has_authoritative_test_ids(regression)
        ):
            regression["status"] = "invalid"
            regression["validation_error"] = (
                "regression_test_ids_unverified:"
                + str(regression.get("test_id_source") or "none")
            )
            return regression

        regression_failed_ids = self._canonical_test_ids(
            regression.get("failed_test_ids", [])
        )
        regression_passed_ids = self._canonical_test_ids(
            regression.get("passed_test_ids", [])
        )
        failed_ids = sorted(set(target_failed_ids) | set(regression_failed_ids))
        passed_ids = sorted(
            (set(target_passed_ids) | set(regression_passed_ids)) - set(failed_ids)
        )
        regression["status"] = "failing" if failed_ids else "plausible"
        regression["validation_error"] = ""
        regression["failed_test_ids"] = failed_ids
        regression["passed_test_ids"] = passed_ids
        regression["post_failed_tests"] = failed_ids
        regression["post_passed_tests"] = passed_ids
        regression["test_id_granularity"] = "test-case"
        return regression


    @classmethod
    def _canonical_test_ids(cls, observed: object) -> list[str]:
        values: set[str] = set()
        for raw in observed if isinstance(observed, list) else []:
            value = cls._canonical_test_id(str(raw))
            if not value:
                continue
            parts = value.split("::")
            if len(parts) == 2 and parts[0] == parts[1]:
                value = parts[0]
            values.add(value)
        return sorted(values)


    @staticmethod
    def _has_authoritative_test_ids(snapshot: dict) -> bool:
        return str(snapshot.get("test_id_source") or "") in (
            AUTHORITATIVE_TEST_ID_SOURCES
        )


    @staticmethod
    def _target_ids_verified(observed: object, requested: tuple[str, ...]) -> bool:
        values = {
            str(value).strip() for value in (observed if isinstance(observed, list) else [])
            if str(value).strip() and not str(value).startswith("command:")
        }
        if not values:
            return False
        return all(
            any(
                ProjectValidator._test_ids_match(requested_id, value)
                for value in values
            )
            for requested_id in requested
        )


    @classmethod
    def _matching_requested_ids(
        cls, observed: object, requested: tuple[str, ...]
    ) -> set[str]:
        values = [
            cls._canonical_test_id(str(value))
            for value in (observed if isinstance(observed, list) else [])
            if str(value).strip() and not str(value).startswith("command:")
        ]
        return {
            requested_id
            for requested_id in requested
            if any(
                cls._test_ids_match(requested_id, value)
                for value in values
            )
        }


    @staticmethod
    def _canonical_test_id(value: str) -> str:
        return str(value).strip().replace("#", "::")


    @classmethod
    def _test_ids_match(cls, left: str, right: str) -> bool:
        """Match equal test IDs or a qualified ``::`` segment suffix.

        Test runners may report ``test_case`` where the configured selector is
        ``path::Class::test_case`` (or the reverse), but arbitrary string
        suffixes such as ``other_test_case`` must never satisfy that contract.
        """

        left_parts = tuple(
            part for part in cls._canonical_test_id(left).split("::") if part
        )
        right_parts = tuple(
            part for part in cls._canonical_test_id(right).split("::") if part
        )
        if not left_parts or not right_parts:
            return False
        shorter = min(len(left_parts), len(right_parts))
        return left_parts[-shorter:] == right_parts[-shorter:]


    def _run_plan(
        self,
        root: Path,
        plan: BuildPlan,
        artifact_dir: Path,
        prefix: str,
        test_commands: Iterable[CommandSpec] | None = None,
        test_scope: str = "regression",
    ) -> dict:
        setup = self._run_commands(root, plan.setup, artifact_dir, f"{prefix}-setup")
        if not all(item.ok for item in setup):
            return self._snapshot(plan, setup, [], [], "invalid", "setup_failed")
        build = self._run_commands(root, plan.build, artifact_dir, f"{prefix}-build")
        if not all(item.ok for item in build):
            return self._snapshot(plan, setup, build, [], "invalid", "build_failed")
        active_tests = tuple(
            plan.regression_test if test_commands is None else test_commands
        )
        reports_before = self._test_report_state(root)
        # Every selected test command must run so multi-target validation has
        # complete evidence even when an earlier target still fails.  Setup
        # and build commands intentionally keep their fail-fast behavior.
        tests = self._run_commands(
            root,
            active_tests,
            artifact_dir,
            f"{prefix}-test",
            fail_fast=False,
        )
        if not tests:
            return self._snapshot(plan, setup, build, tests, "invalid", "no_test_command")
        report_summary = (
            self._structured_test_summary(root, reports_before)
            if len(active_tests) == 1 else None
        )
        test_case_results, test_id_source = self._test_case_results(
            root, reports_before, active_tests, tests
        )

        def snapshot(status: str, error: str) -> dict:
            return self._snapshot(
                plan, setup, build, tests, status, error,
                test_case_results=test_case_results,
                test_id_source=test_id_source,
            )

        report_count = report_summary[0] if report_summary is not None else None
        report_failures = report_summary[1] if report_summary is not None else None
        no_test_outputs = [
            any(pattern.search(item.output) for pattern in ZERO_TEST_PATTERNS)
            for item in tests
        ]
        if no_test_outputs and (
            (len(tests) == 1 and no_test_outputs[0]) or all(no_test_outputs)
        ) or report_count == 0:
            return snapshot("invalid", "no_tests_discovered")
        if any(item.timed_out for item in tests):
            return snapshot("invalid", "test_timeout")
        if any(
            item.returncode == 127
            or "command not found" in item.output.lower()
            for item in tests
        ):
            return snapshot("invalid", "test_runner_unavailable")
        unverified = [
            result.label
            for spec, result in zip(active_tests, tests)
            if not self._has_test_execution_evidence(
                plan.system, spec, result, report_count if len(tests) == 1 else None
            )
        ]
        if unverified:
            return snapshot("invalid", "test_execution_unverified:" + ",".join(unverified))
        output_failure = [
            bool(
                (report_failures is not None and report_failures > 0)
                or self._output_reports_test_failure(spec, result)
            )
            for spec, result in zip(active_tests, tests)
        ]
        conflicts = [
            result.label
            for result, reports_failure in zip(tests, output_failure)
            if result.ok and reports_failure
        ]
        if conflicts:
            return snapshot("invalid", "test_status_output_conflict:" + ",".join(conflicts))
        unverified_failures = [
            result.label
            for result, reports_failure in zip(tests, output_failure)
            if not result.ok and not reports_failure
        ]
        if unverified_failures:
            return snapshot("invalid", "test_failure_unverified:" + ",".join(unverified_failures))
        tests_ok = all(item.ok for item in tests)
        status = "plausible" if tests_ok else "failing"
        value = snapshot(status, "")
        value["test_scope"] = test_scope
        # Keep the executed CommandResult records produced by _snapshot. Replacing
        # them with CommandSpec records discarded return codes and output exactly
        # when the failure-report producer needed that structural evidence.
        value["test_command_specs"] = [item.as_dict() for item in active_tests]
        return value


    @staticmethod
    def _has_test_execution_evidence(
        system: str,
        spec: CommandSpec,
        result: CommandResult,
        report_count: int | None,
    ) -> bool:
        if spec.evidence_pattern:
            return bool(re.search(spec.evidence_pattern, result.output, re.MULTILINE))
        if system.strip().lower() not in BUILTIN_TEST_SYSTEMS:
            return False
        if report_count is not None and report_count > 0:
            return True
        return any(pattern.search(result.output) for pattern in TEST_EXECUTION_PATTERNS)


    @staticmethod
    def _output_reports_test_failure(spec: CommandSpec, result: CommandResult) -> bool:
        if spec.failure_pattern:
            return bool(re.search(spec.failure_pattern, result.output, re.MULTILINE))
        return any(pattern.search(result.output) for pattern in TEST_FAILURE_PATTERNS)


    @staticmethod
    def _test_report_paths(root: Path) -> set[Path]:
        candidates: set[Path] = set()
        for pattern in (
            "**/surefire-reports/TEST-*.xml",
            "**/failsafe-reports/TEST-*.xml",
            "**/test-results/**/*.xml",
            "**/TestResults/**/*.trx",
            ".debugging-framework/*.xml",
        ):
            candidates.update(path for path in root.glob(pattern) if path.is_file())
        return candidates


    @classmethod
    def _test_report_state(cls, root: Path) -> dict[Path, tuple[int, int]]:
        state: dict[Path, tuple[int, int]] = {}
        for path in cls._test_report_paths(root):
            try:
                stat = path.stat()
            except OSError:
                continue
            state[path] = (stat.st_mtime_ns, stat.st_size)
        return state


    @classmethod
    def _changed_test_reports(
        cls, root: Path, before: dict[Path, tuple[int, int]]
    ) -> set[Path]:
        changed = set()
        for path in cls._test_report_paths(root):
            try:
                stat = path.stat()
            except OSError:
                continue
            if before.get(path) != (stat.st_mtime_ns, stat.st_size):
                changed.add(path)
        return changed


    @classmethod
    def _structured_test_summary(
        cls, root: Path, before: dict[Path, tuple[int, int]] | None = None
    ) -> tuple[int, int] | None:
        candidates = cls._changed_test_reports(root, before or {})
        if not candidates:
            return None
        total = 0
        failures = 0
        parsed = False
        for path in candidates:
            try:
                tree = ET.parse(path)
            except (ET.ParseError, OSError):
                continue
            parsed = True
            document = tree.getroot()
            count_found = False
            for node in document.iter():
                local_name = node.tag.rsplit("}", 1)[-1]
                if local_name in {"testsuite", "testsuites"} and "tests" in node.attrib:
                    try:
                        total += int(node.attrib["tests"])
                    except ValueError:
                        pass
                    for attribute in ("failures", "errors"):
                        try:
                            failures += int(node.attrib.get(attribute, "0"))
                        except ValueError:
                            pass
                    count_found = True
                    break
            if not count_found:
                test_nodes = [
                    node for node in document.iter()
                    if node.tag.rsplit("}", 1)[-1] in {"testcase", "UnitTestResult"}
                ]
                total += len(test_nodes)
                for node in test_nodes:
                    local_name = node.tag.rsplit("}", 1)[-1]
                    if local_name == "UnitTestResult":
                        if str(node.attrib.get("outcome") or "").lower() not in {
                            "passed", "completed"
                        }:
                            failures += 1
                    elif any(
                        child.tag.rsplit("}", 1)[-1] in {"failure", "error"}
                        for child in node
                    ):
                        failures += 1
        return (total, failures) if parsed else None


    @classmethod
    def _test_case_results(
        cls,
        root: Path,
        before: dict[Path, tuple[int, int]],
        specs: tuple[CommandSpec, ...],
        commands: list[CommandResult],
    ) -> tuple[dict[str, bool], str]:
        """Return test IDs from one authoritative source, never mixed levels."""
        configured: dict[str, bool] = {}
        for spec, command in zip(specs, commands):
            for test_id, passed in cls._configured_test_results(
                spec, command.output
            ).items():
                configured[test_id] = configured.get(test_id, True) and passed
        if configured:
            return configured, "configured-pattern"

        protocol: dict[str, bool] = {}
        for command in commands:
            for test_id, passed in cls._protocol_test_results(
                command.output
            ).items():
                protocol[test_id] = protocol.get(test_id, True) and passed
        if protocol:
            return protocol, "framework-marker"

        cases: dict[str, bool] = {}
        for path in cls._changed_test_reports(root, before):
            try:
                document = ET.parse(path).getroot()
            except (ET.ParseError, OSError):
                continue
            for node in document.iter():
                local_name = node.tag.rsplit("}", 1)[-1]
                if local_name == "testcase":
                    name = str(node.attrib.get("name") or "").strip()
                    scope = str(
                        node.attrib.get("classname") or node.attrib.get("file") or ""
                    ).strip()
                    test_id = f"{scope}::{name}" if scope and name else name
                    if not test_id or any(
                        child.tag.rsplit("}", 1)[-1] == "skipped" for child in node
                    ):
                        continue
                    passed = not any(
                        child.tag.rsplit("}", 1)[-1] in {"failure", "error"}
                        for child in node
                    )
                    cases[test_id] = cases.get(test_id, True) and passed
                elif local_name == "UnitTestResult":
                    test_id = str(
                        node.attrib.get("testName") or node.attrib.get("testId") or ""
                    ).strip()
                    outcome = str(node.attrib.get("outcome") or "").strip().lower()
                    if test_id and outcome:
                        cases[test_id] = cases.get(test_id, True) and outcome in {
                            "passed", "completed"
                        }
        if cases:
            return cases, "structured-report"

        output_ids_found = False
        for command in commands:
            extracted = cls._failed_test_ids_from_output(command.output)
            if extracted:
                output_ids_found = True
                for test_id in extracted:
                    cases[test_id] = False
            passed = cls._passed_test_ids_from_output(command.output)
            if passed:
                output_ids_found = True
                for test_id in passed:
                    cases[test_id] = cases.get(test_id, True) and True
        if not any(not passed for passed in cases.values()) and not output_ids_found:
            for command in commands:
                if not command.ok:
                    cases[f"command:{command.label}"] = False
        return cases, "output-heuristic" if output_ids_found else "command"


    @staticmethod
    def _configured_test_results(
        spec: CommandSpec, output: str
    ) -> dict[str, bool]:
        """Extract per-test results explicitly declared by a command contract."""
        if not spec.evidence_pattern:
            return {}
        evidence = re.compile(spec.evidence_pattern, re.MULTILINE)
        failure = (
            re.compile(spec.failure_pattern, re.MULTILINE)
            if spec.failure_pattern else None
        )
        results: dict[str, bool] = {}
        for match in evidence.finditer(output):
            groups = match.groupdict()
            test_id = str(groups.get("test_id") or "").strip()
            status = str(groups.get("status") or "").strip().lower()
            marker = re.fullmatch(
                r"\s*(PASSED|FAILED)\s+(\S+)\s*",
                match.group(0),
                re.IGNORECASE,
            )
            if not test_id and marker:
                test_id = marker.group(2)
            if not test_id:
                continue
            if status in {"pass", "passed", "ok", "success", "succeeded"}:
                passed = True
            elif status in {"fail", "failed", "failure", "error", "errored"}:
                passed = False
            elif marker:
                passed = marker.group(1).upper() == "PASSED"
            elif failure is not None:
                passed = failure.search(match.group(0)) is None
            else:
                continue
            results[test_id] = results.get(test_id, True) and passed
        return results


    @staticmethod
    def _protocol_test_results(output: str) -> dict[str, bool]:
        """Parse the framework's unambiguous one-line per-test protocol."""
        results: dict[str, bool] = {}
        for status, test_id in re.findall(
            r"(?m)^(PASSED|FAILED)\s+([^\s]+)\s*$", output
        ):
            passed = status == "PASSED"
            results[test_id] = results.get(test_id, True) and passed
        return results


    @staticmethod
    def _failed_test_ids_from_output(output: str) -> set[str]:
        failed: set[str] = set()
        patterns = (
            r"(?m)^FAILED\s+([^\s]+)",
            r"(?m)^test\s+(.+?)\s+\.\.\.\s+FAILED\s*$",
            r"(?m)^\s*\d+/\d+\s+Test\s+#\d+:\s+(.+?)\s+\.{2,}\*{3}Failed",
            r"(?m)^\s*---\s+FAIL:\s+(\S+)",
        )
        for pattern in patterns:
            failed.update(
                match.strip() for match in re.findall(pattern, output) if match.strip()
            )
        for line in output.splitlines():
            if not line.lstrip().startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(event, dict)
                and event.get("Action") == "fail"
                and str(event.get("Test") or "").strip()
            ):
                package = str(event.get("Package") or "").strip()
                name = str(event["Test"]).strip()
                failed.add(f"{package}::{name}" if package else name)
        return failed


    @staticmethod
    def _passed_test_ids_from_output(output: str) -> set[str]:
        passed: set[str] = set()
        for pattern in (
            r"(?m)^PASSED\s+([^\s]+)",
            r"(?m)^test\s+(.+?)\s+\.\.\.\s+PASSED\s*$",
            r"(?m)^\s*---\s+PASS:\s+(\S+)",
            r"(?m)^\s*test\s+(.+?)\s+\.\.\.\s+ok\s*$",
            r"(?m)^\s*\d+/\d+\s+Test\s+#\d+:\s+(.+?)\s+\.{2,}Passed",
        ):
            passed.update(
                match.strip() for match in re.findall(pattern, output) if match.strip()
            )
        return passed


