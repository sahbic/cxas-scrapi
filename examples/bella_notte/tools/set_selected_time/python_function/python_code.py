"""Setter tool for the selected_time slot."""

from typing import Any


def set_selected_time(time: str) -> dict[str, Any]:
  """Record the guest's chosen time in HH:MM 24-hour format.

  Converts natural language like '6 PM' to '18:00', '7:30 PM' to '19:30'.

  Args:
    time: Time string in HH:MM 24-hour format.

  Returns:
    Dict with stored=True and value on success, or error=True on failure.
  """
  time = str(time).strip()

  try:
    parts = time.split(':')
    h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    period = 'AM' if h < 12 else 'PM'
    h12 = h % 12 or 12
    display_value = f'{h12}:{m:02d} {period}'
  except (ValueError, IndexError):
    return {'error': True, 'error_code': 'not_available'}

  return {'stored': True, 'value': time, 'display_value': display_value}
