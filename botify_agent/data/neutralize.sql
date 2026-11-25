DELETE FROM ir_config_parameter
WHERE key IN ('botify_agent.enabled', 'botify_agent.installation_key', 'botify_agent.secret');
