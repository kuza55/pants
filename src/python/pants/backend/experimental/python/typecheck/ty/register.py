# Copyright 2025 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

"""Static type checker for Python.

See https://docs.astral.sh/ty/ for details.
"""

from pants.backend.python.typecheck.ty import rules as ty_rules
from pants.backend.python.typecheck.ty import skip_field


def rules():
    return (*ty_rules.rules(), *skip_field.rules())

