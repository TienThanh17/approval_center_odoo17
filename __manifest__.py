{
    "name": "ADEC SOL Approval Center",
    "summary": "Generic, reusable approval workflow applicable to any model",
    "version": "17.0.2.0.0",
    "category": "Tools",
    'images': ['static/description/icon.png'],
    "license": "LGPL-3",
    "author": "ADEC SOL",
    "depends": [
        "base",
        "mail",
    ],
    "data": [
        # ============================== SECURITY =============================
        "security/approval_security.xml",
        "security/ir.model.access.csv",

        # ============================== VIEWS ================================
        "views/approval_config_views.xml",
        "views/approval_request_views.xml",
        "views/approval_menu.xml",
    ],
    "application": True,
    "installable": True,
    "uninstall_hook": "uninstall_hook",
}
