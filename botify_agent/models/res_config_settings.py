from odoo import Command, api, fields, models

from ..services import config
from ..services.find import time_off_type_field


class BotifyLeaveTypeOption(models.TransientModel):
    _name = "botify.agent.leave.type.option"
    _description = "Botify leave type option"
    _order = "name, id"

    name = fields.Char(required=True, readonly=True)
    type_id = fields.Integer(required=True, readonly=True)


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    botify_enabled = fields.Boolean("Botify agent", config_parameter="botify_agent.enabled")
    botify_api_url = fields.Char("Botify API URL", config_parameter="botify_agent.api_url")
    botify_installation_key = fields.Char("Installation key", config_parameter="botify_agent.installation_key")
    botify_secret = fields.Char("Installation secret", config_parameter="botify_agent.secret", groups="base.group_system")
    botify_allow_low_write = fields.Boolean("Low-risk changes", config_parameter="botify_agent.allow_low_write")
    botify_allow_high_write = fields.Boolean("High-impact changes", config_parameter="botify_agent.allow_high_write")
    botify_allow_financial = fields.Boolean("Financial operations", config_parameter="botify_agent.allow_financial")
    botify_allow_destructive = fields.Boolean("Deletions and cancellations", config_parameter="botify_agent.allow_destructive")
    botify_allow_external_communication = fields.Boolean(
        "Messages to external people", config_parameter="botify_agent.allow_external_communication"
    )
    botify_allow_simple_changes_without_confirmation = fields.Boolean(
        "Allow simple changes without confirmation",
        config_parameter="botify_agent.allow_simple_changes_without_confirmation",
        help="Allow enabled low-risk changes without asking employees to confirm when Botify also allows it.",
    )
    botify_refused_write_models = fields.Char(
        "Additional models protected from writes", config_parameter="botify_agent.refused_write_models",
        help="Comma-separated technical model names. Botify refuses changes and business actions on these models.",
    )
    botify_personal_data_privacy = fields.Selection(
        [("strict", "Strict"), ("follow_odoo", "Follow Odoo only")],
        string="Personal data privacy",
        default="strict",
        config_parameter="botify_agent.personal_data_privacy",
        groups="base.group_system",
        help="Strict narrows personal data to the employee and native roles with access. Both modes enforce Odoo access rights and the Botify deny lists.",
    )
    botify_allow_team_private_data = fields.Boolean(
        "Managers can see their team's private data",
        default=True,
        config_parameter=config.TEAM_PRIVATE_DATA,
        groups="base.group_system",
        help="Allow direct managers to see private data for direct reports where Odoo permits it. This does not grant access to pay or approval records.",
    )
    botify_refused_read_models = fields.Char(
        "Additional models hidden from the agent", config_parameter="botify_agent.refused_read_models",
        help="Comma-separated technical model names. Botify cannot discover or read these models.",
    )
    botify_refused_read_fields = fields.Char(
        "Additional fields hidden from the agent", config_parameter="botify_agent.refused_read_fields",
        help="Comma-separated model.field names. Botify cannot see, read, sort, filter or group by these fields.",
    )
    botify_retention_days = fields.Integer("Keep the operation log (days)", config_parameter="botify_agent.retention_days", default=90)
    botify_time_off_installed = fields.Boolean(compute="_compute_botify_time_off_installed", groups="base.group_system")
    botify_leave_notifications_enabled = fields.Boolean(
        "Send time-off notifications", config_parameter=config.LEAVE_NOTIFICATIONS_ENABLED, groups="base.group_system",
    )
    botify_leave_type_options = fields.Many2many(
        "botify.agent.leave.type.option", relation="botify_settings_leave_type_options_rel", groups="base.group_system",
    )
    botify_excluded_leave_type_options = fields.Many2many(
        "botify.agent.leave.type.option", relation="botify_settings_excluded_type_options_rel",
        string="Excluded leave types", groups="base.group_system",
    )

    def _compute_botify_time_off_installed(self):
        for settings in self:
            settings.botify_time_off_installed = "hr.leave" in self.env

    @api.model
    def get_values(self):
        values = super().get_values()
        leave = self.env.get("hr.leave")
        if leave is None or not self.env.is_system():
            return values

        # Native tags backed by wizard options. Only settings admins may read
        # these names and IDs, including those without a Time Off role.
        types = self.env[leave._fields[time_off_type_field(leave)].comodel_name].sudo().with_context(active_test=False)
        options = self.env["botify.agent.leave.type.option"].create([
            {"name": leave_type.display_name, "type_id": leave_type.id}
            for leave_type in types.search([])
        ])
        excluded = config.excluded_leave_type_ids(self.env)
        values.update(
            botify_leave_type_options=[Command.set(options.ids)],
            botify_excluded_leave_type_options=[Command.set(options.filtered(lambda option: option.type_id in excluded).ids)],
        )
        return values

    def set_values(self):
        super().set_values()
        if self.env.user.has_group("base.group_system"):
            # Odoo 19 deletes a bool parameter on False, which would restore our True default.
            config.write_parameter(self.env, config.TEAM_PRIVATE_DATA, str(self.botify_allow_team_private_data))
            if self.botify_time_off_installed:
                options = self.botify_excluded_leave_type_options & self.botify_leave_type_options
                config.write_parameter(self.env, config.EXCLUDED_LEAVE_TYPES, ",".join(map(str, sorted(options.mapped("type_id")))))
