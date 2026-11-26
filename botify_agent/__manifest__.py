{
    "name": "Botify AI Assistant",
    "summary": "An AI assistant inside Odoo that works with each employee's own Odoo permissions",
    "version": "20.0.3.0.2",
    "category": "Productivity",
    "license": "LGPL-3",
    "author": "Umbrella Prime",
    "website": "https://botifyarabia.ai",
    "support": "hello@umbrellaprime.sa",
    "depends": ["base", "web", "mail", "bus"],
    "data": [
        "security/botify_security.xml",
        "data/botify_method_data.xml",
        "data/botify_cron.xml",
        "views/botify_views.xml",
        "views/res_config_settings_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "botify_agent/static/src/**/*",
        ],
    },
    "images": ["static/description/banner.png"],
    "installable": True,
    "application": False,
}
