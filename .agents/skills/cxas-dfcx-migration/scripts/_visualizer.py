# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0

"""Skill-local re-export of the promoted HTML preview module.

The implementation lives in :mod:`cxas_scrapi.migration.html_preview` so
the same logic is reachable from the CLI, notebooks, and any other
caller. Skill scripts continue to ``import _visualizer`` so existing
callsites don't need to change.
"""

from cxas_scrapi.migration.html_preview import (  # noqa: F401
    StageReport,
    _rich_to_html,
    build_mermaid_tools_per_agent,
    build_mermaid_topology,
    collect_resource_rows,
    collect_stats,
    generate_html_report,
    render_flow_trees_html,
    render_playbook_trees_html,
    rich_to_html,
    topology_svg,
    write_mermaid_files,
)
