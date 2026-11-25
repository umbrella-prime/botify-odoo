"""Installation settings (``ir.config_parameter``, written from Settings).

Reading them needs ``sudo()``: parameters are admin-only and the shared
secret must never be readable by employees. This is configuration access,
not the execution path: operations never run with elevated rights.
"""

PREFIX = "botify_agent."
TEAM_PRIVATE_DATA = PREFIX + "allow_team_private_data"
LEAVE_NOTIFICATIONS_ENABLED = PREFIX + "leave_notifications_enabled"
EXCLUDED_LEAVE_TYPES = PREFIX + "excluded_leave_type_ids"


def read_parameter(env, key, default=False):
    params = env["ir.config_parameter"].sudo()
    if hasattr(params, "get_str"):
        return params.get_str(key, default)
    return params.get_param(key, default)


def write_parameter(env, key, value):
    params = env["ir.config_parameter"].sudo()
    if hasattr(params, "set_str"):
        return params.set_str(key, None if value is None or value is False else str(value))
    return params.set_param(key, value)


def leave_notifications_enabled(env):
    return read_parameter(env, LEAVE_NOTIFICATIONS_ENABLED) == "True"


def excluded_leave_type_ids(env):
    value = read_parameter(env, EXCLUDED_LEAVE_TYPES) or ""
    return {int(item) for item in value.split(",") if item.isdecimal()}


def _param(env, key, default=None):
    return read_parameter(env, PREFIX + key, default)


def get_settings(env):
    from .classify import RISK_CLASSES

    enabled_classes = ["read"]
    for risk_class in RISK_CLASSES[1:]:
        if _param(env, "allow_" + risk_class) == "True":
            enabled_classes.append(risk_class)
    return {
        "enabled": _param(env, "enabled") == "True",
        "api_url": (_param(env, "api_url") or "").rstrip("/"),
        "installation_key": _param(env, "installation_key") or "",
        "enabled_classes": enabled_classes,
        "allow_simple_changes_without_confirmation": _param(env, "allow_simple_changes_without_confirmation") == "True",
        "retention_days": int(_param(env, "retention_days") or 90),
    }


def _csv_param(env, key):
    names = (_param(env, key) or "").split(",")
    return {name.strip() for name in names if name.strip()}


def get_refused_write_models(env):
    return _csv_param(env, "refused_write_models")


def get_read_denials(env):
    return _csv_param(env, "refused_read_models"), _csv_param(env, "refused_read_fields")


def get_privacy_settings(env):
    return (
        _param(env, "personal_data_privacy") != "follow_odoo",
        read_parameter(env, TEAM_PRIVATE_DATA, "True").lower() not in {"false", "0", "off", "no", "f", "n"},
    )


def get_secret(env):
    return _param(env, "secret") or ""


def configured(env):
    """Enabled and fully configured (Settings -> Botify Agent)."""
    settings = get_settings(env)
    return bool(settings["enabled"] and get_secret(env) and settings["installation_key"] and settings["api_url"])
