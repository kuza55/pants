# Copyright 2025 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

resources(name="lockfile", sources=["ty.lock*"])

python_sources(
    overrides={"subsystem.py": {"dependencies": [":lockfile"]}},
)

python_tests(
    name="tests",
    overrides={
        "rules_integration_test.py": {
            "timeout": 240,
            "tags": ["platform_specific_behavior"],
        },
    },
)

