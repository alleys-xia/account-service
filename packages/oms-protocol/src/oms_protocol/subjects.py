"""Names shared by service provisioning and SDK binding; identities are validated tokens."""

from pydantic import TypeAdapter

from .common import Token


def stream_name(environment, account):
    for value in (environment, account):
        TypeAdapter(Token).validate_python(value)
    return f"OMS_{environment}_{account}"


def consumer_name(environment, account, strategy):
    TypeAdapter(Token).validate_python(strategy)
    return f"strategy_{environment}_{account}_{strategy}"
