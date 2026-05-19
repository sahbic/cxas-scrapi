# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Skill-local re-export of the promoted stage-runner module.

The implementation lives in :mod:`cxas_scrapi.migration.stage_runner`."""

from cxas_scrapi.migration.stage_runner import (  # noqa: F401
    merge_optimizer_logs_into_ir,
    run_stage1,
    run_stage2,
    run_stage_with_redeploy,
)
