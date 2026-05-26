---
name: llm-lint
description: >-
  AI-driven semantic linter for GECX sub-agent instructions.
  Analyzes a single sub-agent's instruction.txt using Gemini to check for typos,
  style violations, task decomposition issues, ambiguity, and rule conflicts.
---

# Skill: LLM-based Instruction Linter (`llm-lint`)

This skill utilizes Gemini to perform high-fidelity semantic and style reviews of a single GECX sub-agent's instruction set. It aims to catch subtle writing, logical, and formatting bugs that standard static checkers cannot identify.

---

## Prerequisites & Setup

### 1. Check virtual environment
Ensure you have activated the project virtual environment:
```bash
source .venv/bin/activate
```

### 2. Authentication
Ensure you are authenticated with Google Cloud to access Vertex AI:
```bash
gcloud auth list
```
If needed, authenticate:
```bash
gcloud auth application-default login
```

---

## Usage & Execution

Run the standalone script against any sub-agent directory in your GECX app.

```bash
python .agents/skills/llm-lint/scripts/llm_lint.py --agent-dir <path_to_agent_directory> [options]
```

### Arguments

| Flag | Required | Default | Description |
| :--- | :--- | :--- | :--- |
| `--agent-dir` | Yes | - | Absolute or relative path to the sub-agent directory containing `instruction.txt`. |
| `--project-id` | No | Auto-detected | GCP Project ID to run Gemini in. Auto-detected from `gecx-config.json` or standard environment variables if omitted. |
| `--location` | No | `us-central1` | GCP Location/region to run Vertex AI Gemini queries in. |
| `--model` | No | `gemini-2.5-flash` | Gemini model name to use for linting. |
| `--output` | No | - | Optional path to save the generated markdown report. |

---

## Evaluation Criteria

The linter reviews the agent's instruction text based on three core groups of criteria:

### 1. Basic Errors
*   **Typos & Spelling**: Identifies misspellings, typing errors, and vocabulary mistakes.
*   **Grammar & Phrasing**: Flags confusing sentence structures or grammatical mistakes that could result in poor model outcomes.

### 2. Instruction Style Guide
*   **Task Decomposition**: Ensures complex workflows are broken down into logical, numbered steps (e.g., hierarchical structures: `1.`, `1.1.`, `1.2.`).
*   **Completeness & Edge Cases**: Searches for unhandled conditional flows (e.g., `if-then` rules missing a matching fallback `else` scenario).
*   **Clarity & Ambiguity**: Identifies abbreviations, idioms, jargon, or references that lack a clear, singular meaning.
*   **Contradictions**: Detects directives or rules that conflict with one another.

### 3. Examples & Turn-by-turn Demonstrations
*   **Redundant Examples**: Flags sample user-agent interactions that replicate existing instruction logic without showing new edge cases.
*   **Conflicting Examples**: Flags interaction logs or mock transcripts that directly violate rules declared in the primary instructions.
