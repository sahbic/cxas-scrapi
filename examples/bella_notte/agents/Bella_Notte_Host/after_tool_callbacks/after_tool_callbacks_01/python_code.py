"""After-tool callback — setter and executor tool state management.

Pure framework callback with no agent-specific knowledge. Reads config
from sm (populated by before_model_callback from _get_config()):
  _setter_slots:    tool name → slot name mapping
  _slot_requires:   slot name → list of prerequisite slot names
  _slot_validates:  slot name → {response_field, filled_slot, error_code}
  _executor_tasks:  tool name → {task_name, outputs, success_check, terminal}

Setter tools return {stored, value} or {error, error_code} directly to
the LLM. This callback reads those results as side effects: writing
sm["pending"] on success and sm["_slot_errors"] on failure.

Executor tools (fired by before_model_callback via preemption) return
task results. This callback stores results in sm["task_results"],
maps outputs to sm["filled"], and sets sm["_task_just_completed"]
for before_model_callback to process on re-invocation.
"""

from typing import Any, Optional


def after_tool_callback(  # pylint: disable=undefined-variable
    tool: Tool,
    tool_input: dict[str, Any],  # pylint: disable=unused-argument
    callback_context: CallbackContext,
    tool_response: dict[str, Any],
) -> Optional[dict[str, Any]]:
  """Route setter tool results to sm state."""
  sm = callback_context.state.get("sm", {})
  setter_slots = sm.get("_setter_slots", {})

  slot_name = setter_slots.get(tool.name)
  if slot_name is None:
    executor_tasks = sm.get("_executor_tasks", {})
    task_info = executor_tasks.get(tool.name)
    if task_info:
      response_data = tool_response.get("result", tool_response)
      task_results_map = sm.setdefault("task_results", {})
      task_results_map[task_info["task_name"]] = response_data

      success_key = task_info.get("success_check", "success")
      if response_data.get(success_key):
        outputs = task_info.get("outputs", {})
        if all(k in response_data for k in outputs):
          filled_map = sm.setdefault("filled", {})
          for result_key, slot_nm in outputs.items():
            filled_map[slot_nm] = response_data[result_key]

      sm["_task_just_completed"] = task_info["task_name"]
    return None

  response_data = tool_response.get("result", tool_response)
  filled = sm.get("filled", {})

  if response_data.get("error"):
    error_code = response_data.get("error_code", "unknown")
    sm.setdefault("_slot_errors", []).append(
        {"slot": slot_name, "code": error_code}
    )
    return None

  if response_data.get("stored"):
    value = response_data.get("value")

    slot_requires = sm.get("_slot_requires", {})
    for req in slot_requires.get(slot_name, []):
      if req not in filled:
        sm.setdefault("_slot_errors", []).append(
            {"slot": slot_name, "code": "prereq_not_met"}
        )
        return None

    slot_validates = sm.get("_slot_validates", {})
    validation = slot_validates.get(slot_name)
    if validation:
      check_value = response_data.get(validation["response_field"], "")
      against_raw = filled.get(validation["filled_slot"], "")
      valid_options = [t.strip() for t in against_raw.split(",")]
      if check_value not in valid_options:
        sm.setdefault("_slot_errors", []).append(
            {"slot": slot_name, "code": validation.get("error_code", "invalid")}
        )
        return None

    sm.setdefault("pending", {})[slot_name] = value

  return None
