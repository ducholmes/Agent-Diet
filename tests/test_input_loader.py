from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openhands_adapter.input_loader import (  # noqa: E402
    AmbiguousCaseError,
    InputLoadError,
    discover_cases,
    load_case,
)


def _write_case(root: Path, case_id: str, *, cwd: str = ".") -> None:
    project = root / case_id
    project.mkdir(parents=True)
    (project / "source.c").write_text("int main(void) { return 0; }\n")
    (root / f"{case_id}.failure.log").write_text("FAILED target-test\n")
    config = {
        "schema_version": 6,
        "setup": [["build-tool", "configure"]],
        "build": [{"command": "build-tool build --jobs 4", "cwd": cwd}],
        "target_test": [
            {
                "command": ["test-tool", "--test", "{test_id}"],
                "evidence_pattern": "^(?:PASSED|FAILED)\\s+\\S+",
                "failure_pattern": "^FAILED\\s+\\S+",
            }
        ],
        "regression_test": [["test-tool", "--all"]],
        "repair": {
            "failing_tests": ["target-test"],
            "source_extensions": [".c"],
        },
        "workspace": {
            "disposable": True,
            "initialize_git_if_missing": True,
        },
        "environment": {
            "mode": "image",
            "runtime": "docker",
            "image": "example/repair:latest",
        },
    }
    (root / f"{case_id}.debugging-framework.json").write_text(
        json.dumps(config), encoding="utf-8"
    )


def _write_directory_case(root: Path, case_id: str) -> Path:
    directory = root / case_id
    directory.mkdir(parents=True)
    _write_case(directory, case_id)
    config_path = directory / f"{case_id}.debugging-framework.json"
    config = json.loads(config_path.read_text())
    config["project_id"] = case_id
    config_path.write_text(json.dumps(config))
    config_path.rename(directory / "config.json")
    (directory / f"{case_id}.failure.log").rename(directory / "failure.log")
    return directory


class InputLoaderTests(unittest.TestCase):
    def test_directory_layout_loads_from_collection_or_case_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case_id = "fmtlib__fmt-2457"
            prepared = _write_directory_case(root, case_id)
            case = load_case(root, case_id)
            self.assertEqual(case.source_project, prepared / case_id)
            self.assertEqual(case.config_path, prepared / "config.json")
            self.assertEqual(case.failure_log, prepared / "failure.log")
            self.assertEqual(case.relative_id, case_id)
            self.assertEqual(load_case(prepared).source_project, case.source_project)
            self.assertEqual(case.target_test_commands[0].argv[-1], "{test_id}")
            self.assertTrue(case.regression_test_commands)

    def test_mixed_php_and_fmtlib_layouts_and_relative_selectors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            php = root / "php" / "inputs"
            php.mkdir(parents=True)
            _write_case(php, "CVE-2016-3132__28a6ed9f9a36")
            _write_directory_case(root / "fmtlib", "fmtlib__fmt-2457")
            cases = discover_cases(root)
            self.assertEqual([case.relative_id for case in cases], [
                "fmtlib/fmtlib__fmt-2457", "php/inputs/CVE-2016-3132__28a6ed9f9a36",
            ])
            self.assertEqual(load_case(root, "fmtlib/fmtlib__fmt-2457"), cases[0])
            self.assertEqual(load_case(root, "fmtlib__fmt-24"), cases[0])

    def test_directory_layout_without_project_id_uses_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = _write_directory_case(root, "fmtlib__fmt-2457")
            path = prepared / "config.json"
            config = json.loads(path.read_text())
            del config["project_id"]
            path.write_text(json.dumps(config))
            self.assertEqual(load_case(root).case_id, prepared.name)

    def test_directory_layout_reports_missing_failure_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prepared = _write_directory_case(root, "fmtlib__fmt-2457")
            (prepared / "failure.log").unlink()
            with self.assertRaisesRegex(InputLoadError, "missing .*failure.log"):
                discover_cases(root)

    def test_directory_layout_ignores_source_configs_and_deduplicates_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case_id = "fmtlib__fmt-2457"
            prepared = _write_directory_case(root, case_id)
            (prepared / case_id / "config.json").write_text("unrelated source config")
            self.assertEqual(len(discover_cases(root)), 1)
            (prepared / f"{case_id}.debugging-framework.json").write_bytes(
                (prepared / "config.json").read_bytes())
            (prepared / f"{case_id}.failure.log").write_bytes(
                (prepared / "failure.log").read_bytes())
            self.assertEqual(len(discover_cases(root)), 1)

    def test_directory_layout_rejects_unsafe_project_ids(self) -> None:
        for case_id in ("../outside", "/absolute", "..", "", "a\\b", 42):
            with self.subTest(case_id=case_id), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                prepared = _write_directory_case(root, "fmtlib__fmt-2457")
                path = prepared / "config.json"
                config = json.loads(path.read_text())
                config["project_id"] = case_id
                path.write_text(json.dumps(config))
                with self.assertRaisesRegex(InputLoadError, "Invalid config.project_id"):
                    discover_cases(root)

    def test_directory_layout_rejects_source_symlinks_outside_input_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            root = parent / "inputs"
            prepared = _write_directory_case(root, "fmtlib__fmt-2457")
            project = prepared / "fmtlib__fmt-2457"
            project.rename(parent / "outside")
            project.symlink_to(parent / "outside", target_is_directory=True)
            with self.assertRaisesRegex(InputLoadError, "outside input root"):
                discover_cases(root)

    def test_problem_statement_prefers_metadata_then_failure_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_case(root, "example-1")
            failure = "FAILED target-test\n  expected 1, got 0\n"
            (root / "example-1.failure.log").write_text(failure)
            self.assertEqual(load_case(root).problem_statement, failure)
            path = root / "example-1.debugging-framework.json"
            config = json.loads(path.read_text())
            config["problem_statement"] = "  Expected behavior differs from actual behavior.\n"
            path.write_text(json.dumps(config))
            self.assertEqual(load_case(root).problem_statement, config["problem_statement"])
            config["problem_statement"] = {"invalid": "type"}
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(InputLoadError, "nonempty issue text"):
                load_case(root)

    def test_failure_log_with_non_utf8_output_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_case(root, "example-1")
            (root / "example-1.failure.log").write_bytes(b"FAILED target-test\n\xff\n")
            with self.assertRaisesRegex(InputLoadError, "valid UTF-8"):
                load_case(root)

    def test_load_case_normalizes_prepared_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_case(root, "example-1")

            case = load_case(root)

            self.assertEqual(case.case_id, "example-1")
            self.assertEqual(case.relative_id, "example-1")
            self.assertEqual(case.environment.image, "example/repair:latest")
            self.assertEqual(
                case.build_commands[0].argv,
                ("build-tool", "build", "--jobs", "4"),
            )
            self.assertEqual(
                case.target_test_commands[0].failure_pattern,
                r"^FAILED\s+\S+",
            )
            self.assertEqual(case.failing_tests, ("target-test",))

    def test_discover_cases_recurses_and_loads_by_relative_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "php" / "inputs"
            nested.mkdir(parents=True)
            _write_case(nested, "example-2")

            cases = discover_cases(root)

            self.assertEqual(
                [case.relative_id for case in cases],
                ["php/inputs/example-2"],
            )
            self.assertEqual(load_case(root, "php/inputs/example-2"), cases[0])

    def test_load_case_rejects_workspace_escape_in_command_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_case(root, "bad-case", cwd="../outside")

            with self.assertRaisesRegex(
                InputLoadError, "must stay inside the project"
            ):
                load_case(root, "bad-case")

    def test_load_case_requires_an_unambiguous_selector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_case(root, "example-1")
            _write_case(root, "example-2")

            with self.assertRaises(AmbiguousCaseError):
                load_case(root)
            with self.assertRaises(AmbiguousCaseError):
                load_case(root, "example-")


if __name__ == "__main__":
    unittest.main()
