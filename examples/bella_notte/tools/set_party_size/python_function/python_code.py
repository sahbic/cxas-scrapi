"""Setter tool for the party_size slot."""

from typing import Any


def set_party_size(size: int) -> dict[str, Any]:
  """Record the number of guests.

  Parse natural language: 'just me'=1, 'a couple'=2, 'four of us'=4. Call
  immediately when party size is mentioned, even alongside other info in the
  same message.

  Args:
    size: Number of guests for the reservation.

  Returns:
    Dict with stored=True and value on success, or error=True on failure.
  """
  if not isinstance(size, int):
    try:
      size = int(size)
    except (ValueError, TypeError):
      return {"error": True, "error_code": "parse_error"}

  if not (1 <= size <= 8):
    return {"error": True, "error_code": "out_of_range"}

  return {"stored": True, "value": size}
