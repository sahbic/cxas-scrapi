# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Skill-local re-export of the promoted IR bundle module.

The implementation lives in :mod:`cxas_scrapi.migration.ir_bundle` so the
same logic is reachable from the CLI (``cxas migrate dfcx-cxas stage*``)
and any other caller. Skill scripts continue to ``import _bundle`` so
callsites don't need to change."""

from cxas_scrapi.migration.ir_bundle import (  # noqa: F401
    IRBundle,
    StageHistoryEntry,
    append_stage,
    attach_grouping,
    attach_version,
    find_default_bundle,
    load,
    save,
    save_for_target,
)
