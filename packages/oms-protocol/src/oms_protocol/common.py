"""Small protocol primitives; no dependency on market-data or strategy packages."""

import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated
from uuid import uuid4

from pydantic import StringConstraints

Token = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=160)]


def utcnow():
    return datetime.now(timezone.utc)


def uid():
    return uuid4().hex


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()
