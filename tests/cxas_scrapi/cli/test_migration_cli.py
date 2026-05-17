# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for :class:`MigrationCLI`.

Most of MigrationCLI is interactive (rich.Prompt loops), but the new
:meth:`MigrationCLI._run_post_migration_opt_ins` helper is pure async
plumbing — the right place to verify that the three opt-in flags
(consolidate / run_stage3 / persist_bundle) wire through to
:meth:`MigrationService.run_stage1` / :meth:`run_stage3` /
:meth:`persist_bundle` with the expected arguments.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from cxas_scrapi.cli.migration_cli import MigrationCLI
from cxas_scrapi.migration.data_models import (
    DFCXAgentIR,
    IRMetadata,
    MigrationConfig,
    MigrationIR,
)


def _make_config(**overrides) -> MigrationConfig:
    base = {
        "project_id": "test-project",
        "target_name": "test_target",
        "model": "gemini-2.5-flash-001",
        "optimize_for_cxas": True,
    }
    base.update(overrides)
    return MigrationConfig(**base)


def _make_source() -> DFCXAgentIR:
    return DFCXAgentIR(
        name="projects/p/locations/us/agents/src",
        display_name="Test Source",
        default_language_code="en",
    )


def _make_service_mock():
    service = MagicMock()
    service.location = "us"
    service.ir = MigrationIR(
        metadata=IRMetadata(
            app_name="test-app",
            app_id="11111111-1111-1111-1111-111111111111",
            app_resource_name="projects/p/locations/us/apps/X",
        ),
    )
    service.run_stage1 = AsyncMock(return_value=None)
    service.run_stage3 = AsyncMock(return_value=(1, 0, 0))
    service.persist_bundle = MagicMock(return_value="bundle.json")
    return service


@pytest.mark.asyncio
async def test_post_migration_opt_ins_all_off_skips_everything():
    """With all three flags off, no service methods are invoked."""
    cli = MigrationCLI()
    service = _make_service_mock()
    config = _make_config(
        consolidate=False, run_stage3=False, persist_bundle=False
    )

    await cli._run_post_migration_opt_ins(service, config, _make_source())

    service.run_stage1.assert_not_called()
    service.run_stage3.assert_not_called()
    service.persist_bundle.assert_not_called()


@pytest.mark.asyncio
async def test_post_migration_opt_ins_persist_only_calls_persist_bundle():
    cli = MigrationCLI()
    service = _make_service_mock()
    config = _make_config(persist_bundle=True)

    await cli._run_post_migration_opt_ins(service, config, _make_source())

    service.persist_bundle.assert_called_once()
    call = service.persist_bundle.call_args
    assert call.args[1] == "test_target_ir.json"
    assert call.kwargs["phase"] == "migrate"
    assert call.kwargs["status"] == "ok"
    service.run_stage1.assert_not_called()
    service.run_stage3.assert_not_called()


@pytest.mark.asyncio
async def test_post_migration_opt_ins_consolidate_calls_run_stage1():
    cli = MigrationCLI()
    service = _make_service_mock()
    config = _make_config(consolidate=True, persist_bundle=False)

    await cli._run_post_migration_opt_ins(service, config, _make_source())

    service.run_stage1.assert_awaited_once()
    kwargs = service.run_stage1.call_args.kwargs
    assert kwargs["consolidate"] is True
    assert kwargs["grouping_callback"] is None  # auto-accept (MigrationCLI)
    assert kwargs["version_label"] == "0.0.4"
    # persist_bundle is off → no persist path passed
    assert kwargs["persist_bundle_path"] is None
    service.run_stage3.assert_not_called()


@pytest.mark.asyncio
async def test_post_migration_opt_ins_stage3_calls_run_stage3():
    cli = MigrationCLI()
    service = _make_service_mock()
    config = _make_config(
        consolidate=True, run_stage3=True, persist_bundle=False
    )

    await cli._run_post_migration_opt_ins(service, config, _make_source())

    service.run_stage1.assert_awaited_once()
    service.run_stage3.assert_awaited_once()
    stage3_kwargs = service.run_stage3.call_args.kwargs
    assert stage3_kwargs["mode"] == "hub"
    assert stage3_kwargs["persist_bundle_path"] is None


@pytest.mark.asyncio
async def test_post_migration_opt_ins_full_stack_passes_persist_paths():
    """With all three flags on, run_stage1 + run_stage3 each get the
    bundle path so they persist after their respective stages."""
    cli = MigrationCLI()
    service = _make_service_mock()
    config = _make_config(
        consolidate=True, run_stage3=True, persist_bundle=True
    )

    await cli._run_post_migration_opt_ins(service, config, _make_source())

    expected_path = "test_target_ir.json"
    # Initial migrate-phase persist
    service.persist_bundle.assert_called_once()
    assert service.persist_bundle.call_args.kwargs["phase"] == "migrate"

    # Stage 1 + Stage 3 both received the bundle path
    assert (
        service.run_stage1.call_args.kwargs["persist_bundle_path"]
        == expected_path
    )
    assert (
        service.run_stage3.call_args.kwargs["persist_bundle_path"]
        == expected_path
    )


@pytest.mark.asyncio
async def test_post_migration_opt_ins_consolidate_failure_no_block():
    """If consolidation raises, stage3 is still attempted (each opt-in
    step is independent — failures log but don't abort the chain)."""
    cli = MigrationCLI()
    service = _make_service_mock()
    service.run_stage1 = AsyncMock(side_effect=RuntimeError("Gemini timeout"))
    config = _make_config(consolidate=True, run_stage3=True)

    # Should NOT raise — failures are logged + surfaced via console, not raised.
    await cli._run_post_migration_opt_ins(service, config, _make_source())

    service.run_stage1.assert_awaited_once()
    service.run_stage3.assert_awaited_once()
