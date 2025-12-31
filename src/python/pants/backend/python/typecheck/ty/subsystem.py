# Copyright 2025 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from pants.backend.python.subsystems.python_tool_base import PythonToolBase
from pants.backend.python.target_types import ConsoleScript
from pants.core.util_rules.config_files import ConfigFilesRequest
from pants.option.option_types import ArgsListOption, FileOption, SkipOption
from pants.util.strutil import help_text, softwrap


class Ty(PythonToolBase):
    options_scope = "ty"
    name = "ty"
    help_short = help_text(
        """
        The ty utility for typechecking Python code (https://docs.astral.sh/ty).
        """
    )

    default_version = "ty==0.0.8"
    default_main = ConsoleScript("ty")
    default_requirements = ["ty==0.0.8"]
    default_lockfile_resource = ("pants.backend.python.typecheck.ty", "ty.lock")

    register_interpreter_constraints = True

    skip = SkipOption("check")
    args = ArgsListOption(example="--output-format=concise")
    config = FileOption(
        default=None,
        advanced=True,
        help=softwrap(
            """
            Path to a `ty.toml` file to use for configuration.

            Note: `ty` can also be configured via `pyproject.toml` using a `[tool.ty]` table.
            """
        ),
    )

    def config_request(self) -> ConfigFilesRequest:
        """ty looks for `ty.toml` or `pyproject.toml` (with a `[tool.ty]` section).

        `ty.toml` takes precedence if both are present.
        """
        return ConfigFilesRequest(
            specified=self.config,
            specified_option_name=f"[{self.options_scope}].config",
            discovery=True,
            check_existence=["ty.toml"],
            check_content={"pyproject.toml": b"[tool.ty"},
        )
