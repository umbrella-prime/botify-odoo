"""The argument contract of a business method: the same strict JSON-Schema
subset Botify uses for website actions (packages/widget/src/params.ts).
Unknown arguments are always refused (``additionalProperties: false``)."""

import math
import re

NAME = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.-]{0,63}$")
TYPES = {"string", "number", "integer", "boolean"}


def check_schema(env, schema):
    """The problem with a params schema, or None when it is well formed."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return env._('params must be {"type": "object", "properties": {...}}')
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return env._('params must be {"type": "object", "properties": {...}}')
    if len(properties) > 20:
        return env._("at most 20 params")
    for key, param in properties.items():
        if not NAME.match(key):
            return env._("invalid param name %s", key)
        if not isinstance(param, dict) or param.get("type") not in TYPES:
            return env._("param %s has no valid type", key)
        enum = param.get("enum")
        if enum is not None and not (
            isinstance(enum, list)
            and len(enum) <= 20
            and all(
                (isinstance(v, str) and len(v) <= 64)
                or (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v))
                for v in enum
            )
        ):
            return env._("param %s enum must be at most 20 strings or numbers", key)
    required = schema.get("required", [])
    if not isinstance(required, list) or any(k not in properties for k in required):
        return env._("required must list declared params")
    return None


def _check_value(env, param, value):
    kind = param["type"]
    if kind == "string":
        if not isinstance(value, str):
            return env._("must be a string")
        if len(value) > param.get("maxLength", 1000):
            return env._("is too long")
    elif kind == "boolean":
        if not isinstance(value, bool):
            return env._("must be a boolean")
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return env._("must be a number")
        if kind == "integer" and not float(value).is_integer():
            return env._("must be an integer")
        if "minimum" in param and value < param["minimum"]:
            return env._("must be >= %s", param["minimum"])
        if "maximum" in param and value > param["maximum"]:
            return env._("must be <= %s", param["maximum"])
    if "enum" in param and value not in param["enum"]:
        return env._("must be one of %s", ", ".join(str(v) for v in param["enum"]))
    return None


def validate(env, schema, args):
    """The problems with ``args`` (an empty list means valid)."""
    if not isinstance(args, dict):
        return [env._("args must be an object")]
    properties = schema.get("properties", {})
    problems = [env._("%s is required", key) for key in schema.get("required", []) if key not in args]
    for key, value in args.items():
        param = properties.get(key)
        if param is None:
            problems.append(env._("%s is not a declared param", key))
            continue
        problem = _check_value(env, param, value)
        if problem:
            problems.append("%s %s" % (key, problem))
    return problems
