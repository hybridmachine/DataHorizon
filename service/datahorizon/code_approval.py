# SPDX-License-Identifier: GPL-3.0-only
"""Operator activation key syntax shared with the thin REST client."""
import re
import uuid


def validate_approvals(values):
    if not isinstance(values,(list,tuple,set,frozenset)) or len(values)>256:
        raise ValueError('Provide at most 256 explicit code activation keys')
    for key in values:
        if not isinstance(key,str) or re.fullmatch(r'[0-9a-f-]{36}:[0-9a-f]{64}',key) is None:
            raise ValueError('Code approval requires workbook UUID:SHA256')
        try:valid=str(uuid.UUID(key[:36]))==key[:36]
        except ValueError:valid=False
        if not valid:raise ValueError('Code approval requires a canonical workbook UUID')
    return frozenset(values)
