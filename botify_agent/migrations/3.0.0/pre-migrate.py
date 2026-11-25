"""Transfer the optional Time Off addon's data before loading the merged addon."""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    # Both config parameters keep their original keys. The Many2one already
    # stores an integer, so keep the column and remove only its old foreign key.
    cr.execute("""
        DO $$
        DECLARE fk record;
        BEGIN
            FOR fk IN
                SELECT conname
                  FROM pg_constraint
                 WHERE contype = 'f' AND conrelid = to_regclass('botify_agent_event')
                   AND conkey = ARRAY[
                       (SELECT attnum FROM pg_attribute
                         WHERE attrelid = to_regclass('botify_agent_event')
                           AND attname = 'botify_leave_type_id' AND NOT attisdropped)
                   ]
            LOOP
                EXECUTE format('ALTER TABLE botify_agent_event DROP CONSTRAINT %I', fk.conname);
            END LOOP;
        END;
        $$;
    """)

    # The new settings block is on the parent view. Keep any custom views that
    # inherited the old extension attached to that parent.
    cr.execute("""
        UPDATE ir_ui_view AS child
           SET inherit_id = obsolete.inherit_id
          FROM ir_ui_view AS obsolete, ir_model_data AS data
         WHERE data.module = 'botify_agent_hr' AND data.model = 'ir.ui.view'
           AND data.name = 'res_config_settings_view_form'
           AND obsolete.id = data.res_id AND child.inherit_id = obsolete.id
    """)
    cr.execute("""
        DELETE FROM ir_ui_view
         WHERE id IN (
             SELECT res_id FROM ir_model_data
              WHERE module = 'botify_agent_hr' AND model = 'ir.ui.view'
                AND name = 'res_config_settings_view_form'
         )
    """)
    cr.execute("""
        DELETE FROM ir_model_fields
         WHERE model = 'res.config.settings' AND name = 'botify_excluded_leave_type_ids'
    """)
    cr.execute("DROP TABLE IF EXISTS botify_settings_excluded_leave_type_rel")
    cr.execute("DELETE FROM ir_model_relation WHERE name = 'botify_settings_excluded_leave_type_rel'")
    cr.execute("""
        UPDATE ir_model_data AS old
           SET module = 'botify_agent'
         WHERE old.module = 'botify_agent_hr' AND old.model = 'ir.model.fields'
           AND old.name IN (
               'field_botify_agent_event__botify_leave_type_id',
               'field_res_config_settings__botify_leave_notifications_enabled'
           )
           AND NOT EXISTS (
               SELECT FROM ir_model_data AS existing
                WHERE existing.module = 'botify_agent' AND existing.name = old.name
           )
    """)
    cr.execute("DELETE FROM ir_model_data WHERE module = 'botify_agent_hr'")
    cr.execute("UPDATE ir_module_module SET state = 'uninstalled' WHERE name = 'botify_agent_hr'")
    _logger.info("Botify 3.0.0: merged Time Off addon data")
