from lxml import etree

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

STATIC_APPROVAL_GROUP_XMLID = "approval_center.group_approval_approver"


class ApprovalConfig(models.Model):
    _name = "approval.config"
    _description = "Approval Configuration"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "name"

    name = fields.Char(required=True, tracking=True)
    model_id = fields.Many2one(
        "ir.model",
        string="Model",
        required=True,
        ondelete="cascade",
        index=True,
        tracking=True,
    )
    # FIX [Technical]: domain động theo model_id để chỉ hiện form view đúng model
    view_id = fields.Many2one(
        "ir.ui.view",
        string="Form View",
        required=True,
        ondelete="cascade",
        domain="[('type', '=', 'form'), ('model', '=', model_id_name)]",
    )
    # Helper char field để dùng trong domain
    model_id_name = fields.Char(related="model_id.model", string="Model Name (tech)", store=False)

    approver_ids = fields.Many2many(
        "res.users",
        "approval_config_res_users_rel",
        "config_id",
        "user_id",
        string="Approvers",
        tracking=True,
    )

    # FIX [Missing]: Thêm require_all_approvers để hỗ trợ multi-level approval
    require_all_approvers = fields.Boolean(
        string="Require All Approvers",
        default=False,
        help="If checked, ALL approvers must approve. Otherwise, any single approver can approve.",
        tracking=True,
    )

    state = fields.Selection(
        [("draft", "Draft"), ("confirmed", "Confirmed")],
        default="draft",
        required=True,
        index=True,
        tracking=True,
    )

    # Metadata được tạo tự động khi confirm.
    submit_server_action_id = fields.Many2one(
        "ir.actions.server", readonly=True, ondelete="set null", string="Submit Server Action"
    )
    approve_server_action_id = fields.Many2one(
        "ir.actions.server", readonly=True, ondelete="set null", string="Approve Server Action"
    )
    # FIX [Missing]: Server action cho Reject
    reject_server_action_id = fields.Many2one(
        "ir.actions.server", readonly=True, ondelete="set null", string="Reject Server Action"
    )
    inherit_view_id = fields.Many2one(
        "ir.ui.view", readonly=True, ondelete="set null", string="Injected Inherited View"
    )
    view_approvals_server_action_id = fields.Many2one(
        "ir.actions.server", readonly=True, ondelete="set null", string="View Approvals Server Action"
    )

    # FIX [Technical]: Bỏ unique constraint theo model_id — quá restrictive.
    # Thay bằng unique theo (model_id, name) để hỗ trợ nhiều config trên cùng model.
    _sql_constraints = [
        (
            "approval_config_unique_model_name",
            "unique(model_id, name)",
            "An approval configuration with this name already exists for this model.",
        ),
    ]

    @api.constrains("view_id", "model_id")
    def _check_view_matches_model(self):
        for rec in self:
            if rec.view_id and rec.model_id and rec.view_id.model != rec.model_id.model:
                raise ValidationError(_("Selected view does not belong to the selected model."))

    @api.onchange("model_id")
    def _onchange_model_id(self):
        """Reset view_id khi đổi model để tránh chọn sai view."""
        self.view_id = False

    # -------------------------------------------------------------------------
    # Luồng trạng thái
    # -------------------------------------------------------------------------
    def action_draft(self):
        for cfg in self:
            if cfg.inherit_view_id:
                cfg.inherit_view_id.sudo().unlink()
            cfg.write({"state": "draft"})
        return True

    def action_confirm(self):
        for cfg in self:
            cfg._action_confirm()
        return True

    def unlink(self):
        actions = (
            self.mapped("submit_server_action_id")
            | self.mapped("approve_server_action_id")
            | self.mapped("reject_server_action_id")
            | self.mapped("view_approvals_server_action_id")
        )
        views = self.mapped("inherit_view_id")
        if actions:
            actions.sudo().unlink()
        if views:
            views.sudo().unlink()
        return super().unlink()

    def _action_confirm(self):
        self.ensure_one()

        if self.state != "draft":
            raise ValidationError(_("Only draft configurations can be confirmed."))
        if not self.approver_ids:
            raise ValidationError(_("Please select at least one approver."))
        if not self.model_id or not self.view_id:
            raise ValidationError(_("Model and View are required."))
        if self.view_id.model != self.model_id.model:
            raise ValidationError(_("Selected view does not belong to the selected model."))

        # FIX [Critical]: Chỉ sync approver của config này vào group, KHÔNG ảnh hưởng config khác.
        # Group dùng để phân quyền menu/view, còn kiểm tra approver thực sự dùng approver_ids.
        group = self.sudo().env.ref(STATIC_APPROVAL_GROUP_XMLID)
        # Gom toàn bộ approver từ tất cả confirmed config + config hiện tại
        all_confirmed_approver_ids = self.env["approval.config"].sudo().search([
            ("state", "=", "confirmed"),
            ("id", "!=", self.id),
        ]).mapped("approver_ids").ids
        new_approver_ids = list(set(all_confirmed_approver_ids + self.approver_ids.ids))
        group.write({"users": [(6, 0, new_approver_ids)]})

        self._ensure_metadata_created()
        self.write({"state": "confirmed"})

    def _ensure_metadata_created(self):
        self.ensure_one()
        sudo_cfg = self.sudo()

        submit_action = sudo_cfg._ensure_server_action_submit()
        approve_action = sudo_cfg._ensure_server_action_approve()
        reject_action = sudo_cfg._ensure_server_action_reject()
        view_approvals_action = sudo_cfg._ensure_server_action_view_approvals()
        inherit_view = sudo_cfg._ensure_inherited_view(
            submit_action, approve_action, reject_action, view_approvals_action
        )

        sudo_cfg.write(
            {
                "submit_server_action_id": submit_action.id,
                "approve_server_action_id": approve_action.id,
                "reject_server_action_id": reject_action.id,
                "view_approvals_server_action_id": view_approvals_action.id,
                "inherit_view_id": inherit_view.id,
            }
        )

    # -------------------------------------------------------------------------
    # FIX [Critical]: Server actions dùng binding thay vì code string eval
    # -------------------------------------------------------------------------
    def _ensure_server_action_submit(self):
        self.ensure_one()
        vals = {
            "name": _("AdecSol Submit Approval (%s)") % self.name,
            "model_id": self.model_id.id,
            "state": "code",
            "code": (
                "config = env['approval.config'].browse(%d)\n"
                "if config.exists():\n"
                "    config._server_action_submit(record)\n"
            ) % self.id,
        }
        if self.submit_server_action_id:
            self.submit_server_action_id.write(vals)
            return self.submit_server_action_id
        return self.env["ir.actions.server"].create(vals)

    def _ensure_server_action_approve(self):
        self.ensure_one()
        vals = {
            "name": _("AdecSol Approve (%s)") % self.name,
            "model_id": self.model_id.id,
            "state": "code",
            "code": (
                "config = env['approval.config'].browse(%d)\n"
                "if config.exists():\n"
                "    config._server_action_approve(record)\n"
            ) % self.id,
        }
        if self.approve_server_action_id:
            self.approve_server_action_id.write(vals)
            return self.approve_server_action_id
        return self.env["ir.actions.server"].create(vals)

    # FIX [Missing]: Server action Reject
    def _ensure_server_action_reject(self):
        self.ensure_one()
        vals = {
            "name": _("AdecSol Reject (%s)") % self.name,
            "model_id": self.model_id.id,
            "state": "code",
            "code": (
                "config = env['approval.config'].browse(%d)\n"
                "if config.exists():\n"
                "    config._server_action_reject(record)\n"
            ) % self.id,
        }
        if self.reject_server_action_id:
            self.reject_server_action_id.write(vals)
            return self.reject_server_action_id
        return self.env["ir.actions.server"].create(vals)

    def _ensure_server_action_view_approvals(self):
        self.ensure_one()
        config_id = self.id
        vals = {
            "name": _("AdecSol View Approvals (%s)") % self.name,
            "model_id": self.model_id.id,
            "state": "code",
            "code": (
                "req = env['approval.request'].search(\n"
                "    [('config_id', '=', %d), ('res_id', '=', record.id)],\n"
                "    order='id desc', limit=1\n"
                ")\n"
                "if req:\n"
                "    action = {\n"
                "        'type': 'ir.actions.act_window',\n"
                "        'name': 'Approval Request',\n"
                "        'res_model': 'approval.request',\n"
                "        'view_mode': 'form',\n"
                "        'res_id': req.id,\n"
                "        'target': 'current',\n"
                "    }\n"
            ) % config_id,
        }
        if self.view_approvals_server_action_id:
            self.view_approvals_server_action_id.write(vals)
            return self.view_approvals_server_action_id
        return self.env["ir.actions.server"].create(vals)

    def _ensure_inherited_view(self, submit_action, approve_action, reject_action, view_approvals_action):
        self.ensure_one()

        def _safe_btn(action_id, string, css_class, invisible_expr, groups=None):
            btn = etree.Element("button")
            btn.set("name", str(int(action_id)))
            btn.set("type", "action")
            btn.set("string", string)
            btn.set("class", css_class)
            btn.set("invisible", invisible_expr)
            if groups:
                btn.set("groups", groups)
            return etree.tostring(btn, encoding="unicode")

        submit_btn = _safe_btn(
            submit_action.id,
            _("Submit for Approval"),
            "btn-primary",
            "approval_state != 'draft'",
        )
        approve_btn = _safe_btn(
            approve_action.id,
            _("Approve"),
            "btn-success",
            "approval_state != 'waiting' or not approval_is_approver",
            groups=STATIC_APPROVAL_GROUP_XMLID,
        )
        reject_btn = _safe_btn(
            reject_action.id,
            _("Reject"),
            "btn-danger",
            "approval_state != 'waiting' or not approval_is_approver",
            groups=STATIC_APPROVAL_GROUP_XMLID,
        )

        va_id = int(view_approvals_action.id)

        def _view_btn(label, invisible_expr, css_extra=""):
            return (
                '<button name="{va_id}" type="action"'
                ' class="btn-light border ms-2 {css}"'
                ' invisible="{inv}"'
                ' string="{label}"/>'
            ).format(va_id=va_id, label=label, inv=invisible_expr, css=css_extra)

        view_waiting_btn  = _view_btn("⏳ Waiting",   "approval_state != 'waiting'",   "text-warning")
        view_approved_btn = _view_btn("✅ Approved",  "approval_state != 'approved'",  "text-success")
        view_rejected_btn = _view_btn("❌ Rejected",  "approval_state != 'rejected'",  "text-danger")
        view_cancel_btn   = _view_btn("🚫 Cancelled", "approval_state != 'cancelled'", "text-danger")

        arch_db = (
            "<data>\n"
            "  <xpath expr=\"//form/header\" position=\"inside\">\n"
            "    <field name=\"approval_state\" invisible=\"1\"/>\n"
            "    <field name=\"approval_is_approver\" invisible=\"1\"/>\n"
            "    <field name=\"approval_approved_by\" invisible=\"1\"/>\n"
            "    {submit}\n"
            "    {approve}\n"
            "    {reject}\n"
            "    {view_waiting}\n"
            "    {view_approved}\n"
            "    {view_rejected}\n"
            "    {view_cancel}\n"
            "  </xpath>\n"
            "</data>"
        ).format(
            submit=submit_btn, approve=approve_btn, reject=reject_btn,
            view_waiting=view_waiting_btn,
            view_approved=view_approved_btn,
            view_rejected=view_rejected_btn,
            view_cancel=view_cancel_btn,
        )

        view_name = "approval_center.inject.%s.%d" % (self.model_id.model, self.id)
        vals = {
            "name": view_name,
            "type": "form",
            "model": self.model_id.model,
            "inherit_id": self.view_id.id,
            "arch_db": arch_db,
            "active": True,
        }

        if self.inherit_view_id:
            self.inherit_view_id.write(vals)
            return self.inherit_view_id
        return self.env["ir.ui.view"].create(vals)

    # -------------------------------------------------------------------------
    # Nghiệp vụ được gọi bởi server action
    # -------------------------------------------------------------------------
    def _server_action_submit(self, record):
        self.ensure_one()
        if not record or not record.exists():
            return True
        if record._name != self.model_id.model:
            return True

        ApprovalRequest = self.env["approval.request"].sudo()

        # FIX [Critical]: Dùng SELECT FOR UPDATE để tránh race condition
        self.env.cr.execute(
            """
            SELECT id FROM approval_request
            WHERE model = %s AND res_id = %s AND state = 'waiting'
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """,
            (record._name, record.id),
        )
        if self.env.cr.fetchone():
            raise UserError(_("A pending approval request already exists for this record."))

        # request = ApprovalRequest.with_context(
        #     mail_auto_subscribe_no_notify=True,
        #     mail_create_nosubscribe=True,
        # ).create({
        #     "model": record._name,
        #     "res_id": record.id,
        #     "requester_id": self.env.user.id,
        #     "approver_ids": [(6, 0, self.approver_ids.ids)],
        #     "config_id": self.id,
        #     "state": "waiting",
        #     "require_all_approvers": self.require_all_approvers,
        # })

        request = ApprovalRequest.create({
            "model": record._name,
            "res_id": record.id,
            "requester_id": self.env.user.id,
            "approver_ids": [(6, 0, self.approver_ids.ids)],
            "config_id": self.id,
            "state": "waiting",
            "require_all_approvers": self.require_all_approvers,
        })

        # FIX [Missing]: Gửi notification email cho approvers
        request._notify_approvers()
        return True

    def _server_action_approve(self, record):
        self.ensure_one()
        if not record or not record.exists():
            return True
        if record._name != self.model_id.model:
            return True

        # Kiểm tra approver ở cả config level (không chỉ group)
        if self.env.user not in self.approver_ids:
            raise UserError(_("You are not authorized to approve this record."))

        ApprovalRequest = self.env["approval.request"]
        request = ApprovalRequest.search(
            [("model", "=", record._name), ("res_id", "=", record.id), ("state", "=", "waiting")],
            limit=1,
        )
        if not request:
            raise UserError(_("No pending approval request found for this record."))

        request.sudo()._do_approve(self.env.user)
        return True

    # FIX [Missing]: Logic Reject
    def _server_action_reject(self, record):
        self.ensure_one()
        if not record or not record.exists():
            return True
        if record._name != self.model_id.model:
            return True

        if self.env.user not in self.approver_ids:
            raise UserError(_("You are not authorized to reject this record."))

        ApprovalRequest = self.env["approval.request"]
        request = ApprovalRequest.search(
            [("model", "=", record._name), ("res_id", "=", record.id), ("state", "=", "waiting")],
            limit=1,
        )
        if not request:
            raise UserError(_("No pending approval request found for this record."))

        request.sudo()._do_reject(self.env.user)
        return True
