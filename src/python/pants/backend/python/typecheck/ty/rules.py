# Copyright 2025 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from pants.backend.python.subsystems.setup import PythonSetup
from pants.backend.python.target_types import (
    InterpreterConstraintsField,
    PythonResolveField,
    PythonSourceField,
)
from pants.backend.python.typecheck.ty.skip_field import SkipTyField
from pants.backend.python.typecheck.ty.subsystem import Ty
from pants.backend.python.util_rules import pex_from_targets
from pants.backend.python.util_rules.interpreter_constraints import InterpreterConstraints
from pants.backend.python.util_rules.partition import (
    _partition_by_interpreter_constraints_and_resolve,
)
from pants.backend.python.util_rules.pex import (
    PexRequest,
    VenvPexProcess,
    VenvPexRequest,
    create_pex,
    create_venv_pex,
)
from pants.backend.python.util_rules.pex_environment import PexEnvironment
from pants.backend.python.util_rules.pex_from_targets import RequirementsPexRequest
from pants.backend.python.util_rules.python_sources import (
    PythonSourceFilesRequest,
    prepare_python_sources,
)
from pants.core.goals.check import CheckRequest, CheckResult, CheckResults
from pants.core.goals.resolves import ExportableTool
from pants.core.util_rules import config_files
from pants.core.util_rules.config_files import find_config_file
from pants.core.util_rules.source_files import SourceFilesRequest, determine_source_files
from pants.engine.collection import Collection
from pants.engine.internals.graph import resolve_coarsened_targets as coarsened_targets_get
from pants.engine.internals.native_engine import MergeDigests
from pants.engine.internals.selectors import concurrently
from pants.engine.intrinsics import execute_process, merge_digests
from pants.engine.rules import Rule, collect_rules, implicitly, rule
from pants.engine.target import CoarsenedTargets, CoarsenedTargetsRequest, FieldSet, Target
from pants.engine.unions import UnionRule
from pants.util.logging import LogLevel
from pants.util.ordered_set import FrozenOrderedSet, OrderedSet
from pants.util.strutil import pluralize


@dataclass(frozen=True)
class TyFieldSet(FieldSet):
    required_fields = (PythonSourceField,)

    sources: PythonSourceField
    resolve: PythonResolveField
    interpreter_constraints: InterpreterConstraintsField

    @classmethod
    def opt_out(cls, tgt: Target) -> bool:
        return tgt.get(SkipTyField).value


class TyRequest(CheckRequest):
    field_set_type = TyFieldSet
    tool_name = Ty.options_scope


@dataclass(frozen=True)
class TyPartition:
    field_sets: FrozenOrderedSet[TyFieldSet]
    root_targets: CoarsenedTargets
    resolve_description: str | None
    interpreter_constraints: InterpreterConstraints

    def description(self) -> str:
        ics = str(sorted(str(c) for c in self.interpreter_constraints))
        return f"{self.resolve_description}, {ics}" if self.resolve_description else ics


class TyPartitions(Collection[TyPartition]):
    pass


@rule(
    desc="ty typecheck each partition based on its interpreter_constraints",
    level=LogLevel.DEBUG,
)
async def ty_typecheck_partition(
    partition: TyPartition,
    ty: Ty,
    pex_environment: PexEnvironment,
    python_setup: PythonSetup,
) -> CheckResult:
    root_sources_get = determine_source_files(
        SourceFilesRequest(fs.sources for fs in partition.field_sets)
    )
    transitive_sources_get = prepare_python_sources(
        PythonSourceFilesRequest(partition.root_targets.closure()), **implicitly()
    )
    requirements_pex_get = create_pex(
        **implicitly(
            RequirementsPexRequest(
                (fs.address for fs in partition.field_sets),
                hardcoded_interpreter_constraints=partition.interpreter_constraints,
            )
        )
    )
    ty_pex_get = create_pex(
        ty.to_pex_request(interpreter_constraints=partition.interpreter_constraints),
        **implicitly(),
    )
    config_files_get = find_config_file(ty.config_request())

    root_sources, transitive_sources, requirements_pex, ty_pex, config_files = await concurrently(
        root_sources_get,
        transitive_sources_get,
        requirements_pex_get,
        ty_pex_get,
        config_files_get,
    )

    input_digest = await merge_digests(
        MergeDigests((transitive_sources.source_files.snapshot.digest, config_files.snapshot.digest))
    )

    complete_pex_env = pex_environment.in_sandbox(working_directory=None)

    # Create a venv runner with both `ty` itself and the (third-party) requirements of the code.
    # We then point ty at this venv for third-party module discovery via `--python`.
    runner = await create_venv_pex(
        VenvPexRequest(
            PexRequest(
                output_filename="ty_runner.pex",
                interpreter_constraints=partition.interpreter_constraints,
                main=ty.main,
                internal_only=True,
                pex_path=[ty_pex, requirements_pex],
            ),
            complete_pex_env,
        ),
        **implicitly(),
    )

    venv_path = str(complete_pex_env.pex_root / runner.venv_rel_dir)

    # Give ty the Pants source roots so it can resolve first-party modules correctly.
    extra_search_paths = [
        arg
        for source_root in (sr or "." for sr in transitive_sources.source_roots)
        for arg in ("--extra-search-path", source_root)
    ]

    config_file_args = (
        ("--config-file", ty.config)
        if ty.config and not any(arg.startswith("--config-file") for arg in ty.args)
        else ()
    )

    python_version = partition.interpreter_constraints.minimum_python_version(
        python_setup.interpreter_versions_universe
    )
    python_version_args = (
        ("--python-version", python_version)
        if python_version
        and not any(
            arg.startswith(("--python-version", "--target-version")) for arg in ty.args
        )
        else ()
    )

    python_env_args = (
        ("--python", venv_path)
        if not any(arg.startswith(("--python", "--venv")) for arg in ty.args)
        else ()
    )

    # Avoid progress bars/spinners in Pants output.
    argv = (
        "check",
        "--no-progress",
        "--color=never",
        *config_file_args,
        *python_env_args,
        *python_version_args,
        *extra_search_paths,
        *ty.args,
        *root_sources.snapshot.files,
    )

    result = await execute_process(
        **implicitly(
            VenvPexProcess(
                runner,
                argv=argv,
                input_digest=input_digest,
                description=f"Run ty on {pluralize(len(root_sources.snapshot.files), 'file')}.",
                level=LogLevel.DEBUG,
            )
        )
    )
    return CheckResult.from_fallible_process_result(
        result,
        partition_description=partition.description(),
    )


@rule(
    desc="Determine if it is necessary to partition ty's input (interpreter_constraints and resolves)",
    level=LogLevel.DEBUG,
)
async def ty_determine_partitions(
    request: TyRequest,
    ty: Ty,
    python_setup: PythonSetup,
) -> TyPartitions:
    resolve_and_interpreter_constraints_to_field_sets = (
        _partition_by_interpreter_constraints_and_resolve(request.field_sets, python_setup)
    )

    coarsened_targets = await coarsened_targets_get(
        CoarsenedTargetsRequest(field_set.address for field_set in request.field_sets),
        **implicitly(),
    )
    coarsened_targets_by_address = coarsened_targets.by_address()

    return TyPartitions(
        TyPartition(
            FrozenOrderedSet(field_sets),
            CoarsenedTargets(
                OrderedSet(
                    coarsened_targets_by_address[field_set.address] for field_set in field_sets
                )
            ),
            resolve if len(python_setup.resolves) > 1 else None,
            interpreter_constraints or ty.interpreter_constraints,
        )
        for (resolve, interpreter_constraints), field_sets in sorted(
            resolve_and_interpreter_constraints_to_field_sets.items()
        )
    )


@rule(desc="Typecheck using ty", level=LogLevel.DEBUG)
async def ty_typecheck(
    request: TyRequest,
    ty: Ty,
) -> CheckResults:
    if ty.skip:
        return CheckResults([], checker_name=request.tool_name)

    # Explicitly excluding `ty` as a function argument to `ty_determine_partitions` and
    # `ty_typecheck_partition` as it throws "TypeError: unhashable type: 'Ty'".
    partitions = await ty_determine_partitions(request, **implicitly())
    partitioned_results = await concurrently(
        ty_typecheck_partition(partition, **implicitly()) for partition in partitions
    )
    return CheckResults(
        partitioned_results,
        checker_name=request.tool_name,
    )


def rules() -> Iterable[Rule | UnionRule]:
    return (
        *collect_rules(),
        *config_files.rules(),
        *pex_from_targets.rules(),
        UnionRule(CheckRequest, TyRequest),
        UnionRule(ExportableTool, Ty),
    )
