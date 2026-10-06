import json
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID


def encode(value: object) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    raise TypeError(f'Cannot serialize {type(value).__name__}')


def json_ready(value: object):
    return json.loads(json.dumps(value, ensure_ascii=False, default=encode, allow_nan=False))
