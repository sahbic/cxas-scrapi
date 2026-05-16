# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Skill-local re-export of the promoted post-deploy lint module.

The implementation lives in :mod:`cxas_scrapi.migration.post_deploy_lint`."""

from cxas_scrapi.migration.post_deploy_lint import (  # noqa: F401
    run_post_deploy_lint,
)
