---
description: Your goal is to update tools.md based on latest google_cloud_ces python package.
---

* Step 1: Tell me all the tools type from google_cloud_ces python package in .venv/lib64/python3.13/site-packages
* Step 2: Ask me which tool I want to update reference for
* Step 3: add the tool to .agents/skills/cxas-agent-foundry/references/api-schemas/tools.md. Keep the existing styling, and make it short.
Sample formatting style

### ClientFunction
Client-side function -- agent invokes, client executes and returns result.

- **name** (string): [required]
- **description** (string)
- **parameters** (-> Schema): Parameter schema.
- **response** (-> Schema): Response schema.