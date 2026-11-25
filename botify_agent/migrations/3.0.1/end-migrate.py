"""Remove the retired Time Off addon's stale Apps entry."""

import logging

from odoo import SUPERUSER_ID
from odoo.api import Environment
from odoo.modules.module import get_module_path

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    name = "botify_agent_hr"
    env = Environment(cr, SUPERUSER_ID, {})
    module = env["ir.module.module"].search([("name", "=", name), ("state", "=", "uninstalled")], limit=1)
    if module and not get_module_path(name, display_warning=False):
        module.unlink()
        _logger.info("Botify 3.0.1: removed stale botify_agent_hr module record")
