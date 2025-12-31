# Copyright 2025 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from collections.abc import Iterable
from textwrap import dedent

import pytest

from pants.backend.python import target_types_rules
from pants.backend.python.target_types import (
    PythonRequirementTarget,
    PythonSourcesGeneratorTarget,
    PythonSourceTarget,
)
from pants.backend.python.typecheck.ty.rules import (
    TyFieldSet,
    TyPartition,
    TyPartitions,
    TyRequest,
)
from pants.backend.python.typecheck.ty.rules import rules as ty_rules
from pants.backend.python.util_rules.interpreter_constraints import InterpreterConstraints
from pants.core.goals.check import CheckResult, CheckResults
from pants.engine.addresses import Address
from pants.engine.fs import EMPTY_DIGEST
from pants.engine.rules import QueryRule
from pants.engine.target import Target
from pants.testutil.python_rule_runner import PythonRuleRunner


@pytest.fixture
def rule_runner() -> PythonRuleRunner:
    return PythonRuleRunner(
        rules=[
            *ty_rules(),
            *target_types_rules.rules(),
            QueryRule(CheckResults, (TyRequest,)),
            QueryRule(TyPartitions, (TyRequest,)),
        ],
        target_types=[
            PythonRequirementTarget,
            PythonSourcesGeneratorTarget,
            PythonSourceTarget,
        ],
    )


PACKAGE = "src/py/project"
GOOD_FILE = dedent(
    """\
    def add(x: int, y: int) -> int:
        return x + y

    result = add(3, 3)
    """
)
BAD_FILE = dedent(
    """\
    def add(x: int, y: int) -> int:
        return x + y

    result = add(2.0, 3.0)
    """
)

IGNORE_INVALID_ARGUMENT_TYPE_PYPROJECT = dedent(
    """\
    [tool.ty.rules]
    invalid-argument-type = "ignore"
    """
)


def run_ty(
    rule_runner: PythonRuleRunner, targets: list[Target], *, extra_args: Iterable[str] | None = None
) -> tuple[CheckResult, ...]:
    rule_runner.set_options(extra_args or (), env_inherit={"PATH", "PYENV_ROOT", "HOME"})
    results = rule_runner.request(CheckResults, [TyRequest(TyFieldSet.create(tgt) for tgt in targets)])
    return results.results


def test_passing(rule_runner: PythonRuleRunner) -> None:
    rule_runner.write_files({f"{PACKAGE}/f.py": GOOD_FILE, f"{PACKAGE}/BUILD": "python_sources()"})
    tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="f.py"))
    results = run_ty(rule_runner, [tgt])

    assert len(results) == 1
    assert results[0].exit_code == 0
    assert "All checks passed!" in results[0].stdout
    assert results[0].report == EMPTY_DIGEST


def test_failing(rule_runner: PythonRuleRunner) -> None:
    rule_runner.write_files({f"{PACKAGE}/f.py": BAD_FILE, f"{PACKAGE}/BUILD": "python_sources()"})
    tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="f.py"))
    results = run_ty(rule_runner, [tgt])

    assert len(results) == 1
    assert results[0].exit_code == 1
    assert "error[invalid-argument-type]" in results[0].stdout
    assert f"{PACKAGE}/f.py:4:14" in results[0].stdout
    assert "Found 2 diagnostics" in results[0].stdout
    assert results[0].report == EMPTY_DIGEST


def test_config_file(rule_runner: PythonRuleRunner) -> None:
    rule_runner.write_files(
        {
            f"{PACKAGE}/f.py": BAD_FILE,
            f"{PACKAGE}/BUILD": "python_sources()",
            "pyproject.toml": IGNORE_INVALID_ARGUMENT_TYPE_PYPROJECT,
        }
    )
    tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="f.py"))
    results = run_ty(rule_runner, [tgt])

    assert len(results) == 1
    assert results[0].exit_code == 0


def test_skip(rule_runner: PythonRuleRunner) -> None:
    rule_runner.write_files({f"{PACKAGE}/f.py": BAD_FILE, f"{PACKAGE}/BUILD": "python_sources()"})
    tgt = rule_runner.get_target(Address(PACKAGE, relative_file_path="f.py"))
    results = run_ty(rule_runner, [tgt], extra_args=["--ty-skip"])

    assert not results


def test_partition_targets(rule_runner: PythonRuleRunner) -> None:
    def create_folder(folder: str, resolve: str, interpreter: str) -> dict[str, str]:
        return {
            f"{folder}/dep.py": "",
            f"{folder}/root.py": "",
            f"{folder}/BUILD": dedent(
                f"""\
                python_source(
                    name='dep',
                    source='dep.py',
                    resolve='{resolve}',
                    interpreter_constraints=['=={interpreter}.*'],
                )
                python_source(
                    name='root',
                    source='root.py',
                    resolve='{resolve}',
                    interpreter_constraints=['=={interpreter}.*'],
                    dependencies=[':dep'],
                )
                """
            ),
        }

    rule_runner.write_files(
        {
            **create_folder("resolveA_py38", "a", "3.8"),
            **create_folder("resolveA_py39", "a", "3.9"),
            **create_folder("resolveB_1", "b", "3.9"),
            **create_folder("resolveB_2", "b", "3.9"),
        }
    )
    rule_runner.set_options(
        ["--python-resolves={'a': '', 'b': ''}", "--python-enable-resolves"],
        env_inherit={"PATH", "PYENV_ROOT", "HOME"},
    )

    resolve_a_py38_root = rule_runner.get_target(Address("resolveA_py38", target_name="root"))
    resolve_a_py39_root = rule_runner.get_target(Address("resolveA_py39", target_name="root"))
    resolve_b_root1 = rule_runner.get_target(Address("resolveB_1", target_name="root"))
    resolve_b_root2 = rule_runner.get_target(Address("resolveB_2", target_name="root"))

    request = TyRequest(
        TyFieldSet.create(t)
        for t in (
            resolve_a_py38_root,
            resolve_a_py39_root,
            resolve_b_root1,
            resolve_b_root2,
        )
    )

    partitions = rule_runner.request(TyPartitions, [request])
    assert len(partitions) == 3

    def assert_partition(partition: TyPartition, *, interpreter: str, resolve: str) -> None:
        ics = [f"CPython=={interpreter}.*"]
        assert partition.interpreter_constraints == InterpreterConstraints(ics)
        assert partition.description() == f"{resolve}, {ics}"

    assert_partition(partitions[0], interpreter="3.8", resolve="a")
    assert_partition(partitions[1], interpreter="3.9", resolve="a")
    assert_partition(partitions[2], interpreter="3.9", resolve="b")

