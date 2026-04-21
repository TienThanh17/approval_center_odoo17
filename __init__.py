# -*- coding: utf-8 -*-

from . import models


def uninstall_hook(env):
    """
    Dọn dẹp toàn bộ view và action được tạo tự động khi uninstall module.
    """
    configs = env["approval.config"].with_context(active_test=False).search([])

    actions = (
        configs.mapped("submit_server_action_id")
        | configs.mapped("approve_server_action_id")
        | configs.mapped("reject_server_action_id")  # FIX: thêm reject action
    )
    views = configs.mapped("inherit_view_id")

    if actions:
        actions.sudo().unlink()
    if views:
        views.sudo().unlink()

    # Quét orphan (đề phòng ondelete='set null' đã tách liên kết)
    orphan_actions = env["ir.actions.server"].sudo().with_context(active_test=False).search([
        "|", "|",
        ("name", "=like", "AdecSol Submit Approval (%)"),
        ("name", "=like", "AdecSol Approve (%)"),
        ("name", "=like", "AdecSol Reject (%)"),  # FIX: thêm reject
    ])
    if orphan_actions:
        orphan_actions.unlink()

    orphan_views = env["ir.ui.view"].sudo().with_context(active_test=False).search([
        ("name", "=like", "approval_center.inject.%")
    ])
    if orphan_views:
        orphan_views.unlink()
