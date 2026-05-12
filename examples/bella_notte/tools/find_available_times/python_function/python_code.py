"""Executor tool: look up available reservation times."""

from typing import Any


def find_available_times(
    party_size: int,
    preferred_date: str,  # pylint: disable=unused-argument
) -> dict[str, Any]:
  """Find available time slots for the given party size and date.

  Args:
    party_size: Number of guests.
    preferred_date: Requested date in YYYY-MM-DD format.

  Returns:
    Dict with available_times string and success flag.
  """
  schedule = {
      2: ["6:00 PM", "7:30 PM", "9:00 PM"],
      4: ["7:00 PM", "8:30 PM"],
      6: ["6:00 PM"],
  }
  times = schedule.get(
      int(party_size),
      ["6:00 PM", "7:30 PM", "9:00 PM"],
  )
  return {"available_times": ", ".join(times), "success": True}
