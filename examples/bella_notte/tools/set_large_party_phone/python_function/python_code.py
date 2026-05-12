"""Setter tool for the large_party_phone slot."""

import re
from typing import Any


def set_large_party_phone(phone: str) -> dict[str, Any]:
  """Record a contact phone number for a large party reservation.

  Accepts any reasonable phone format.

  Args:
    phone: Contact phone number string.

  Returns:
    Dict with stored=True and value on success, or error=True on failure.
  """
  phone = str(phone).strip()
  digits = re.sub(r'\D', '', phone)
  if len(digits) < 7:
    return {'error': True, 'error_code': 'invalid_phone'}

  return {'stored': True, 'value': phone}
