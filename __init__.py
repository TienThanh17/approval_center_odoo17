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
        | configs.mapped("reject_server_action_id")
        | configs.mapped("view_approvals_server_action_id")
    )
    views = configs.mapped("inherit_view_id")

    if actions:
        actions.sudo().unlink()
    if views:
        views.sudo().unlink()

    # Quét orphan (đề phòng ondelete='set null' đã tách liên kết)
    orphan_actions = env["ir.actions.server"].sudo().with_context(active_test=False).search([
        "|", "|", "|",
        ("name", "=like", "AdecSol Submit Approval (%)"),
        ("name", "=like", "AdecSol Approve (%)"),
        ("name", "=like", "AdecSol Reject (%)"),
        ("name", "=like", "AdecSol View Approvals (%)"),
    ])
    if orphan_actions:
        orphan_actions.unlink()

    orphan_views = env["ir.ui.view"].sudo().with_context(active_test=False).search([
        ("name", "=like", "approval_center.inject.%")
    ])
    if orphan_views:
        orphan_views.unlink()

    # Xóa các field động đã inject vào các model khác
    # Lưu ý: FIELD_STATE, FIELD_IS_APPROVER, FIELD_APPROVED_BY được định nghĩa trong approval_config.py
    dynamic_fields = env["ir.model.fields"].sudo().search([
        ("name", "in", ["x_approval_state", "x_approval_is_approver", "x_approval_approved_by"])
    ])
    if dynamic_fields:
        dynamic_fields.unlink()
