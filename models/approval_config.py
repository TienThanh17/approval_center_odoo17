from lxml import etree

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

STATIC_APPROVAL_GROUP_XMLID = "approval_center.group_approval_approver"

# Tên 3 field được inject vào target model — dùng chung cho mọi model
FIELD_STATE = "x_approval_state"
FIELD_IS_APPROVER = "x_approval_is_approver"
FIELD_APPROVED_BY = "x_approval_approved_by"

APPROVAL_STATE_SELECTION = (
    "[('draft','Draft'),('waiting','Waiting'),"
    "('approved','Approved'),('rejected','Rejected'),('cancelled','Cancelled')]"
)

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
    view_id = fields.Many2one(
        "ir.ui.view",
        string="Form View",
        required=True,
        ondelete="cascade",
        domain="[('type', '=', 'form'), ('model', '=', model_id_name)]",
    )
    model_id_name = fields.Char(
        related="model_id.model", string="Model Name (tech)", store=False
    )
    approver_ids = fields.Many2many(
        "res.users",
        "approval_config_res_users_rel",
        "config_id",
        "user_id",
        string="Approvers",
        tracking=True,
        # domain=lambda self: [('groups_id', 'in', [self.env.ref(STATIC_APPROVAL_GROUP_XMLID).id])]
    )
    require_all_approvers = fields.Boolean(
        string="Require All Approvers",
        default=False,
        tracking=True,
    )
    state = fields.Selection(
        [("draft", "Draft"), ("confirmed", "Confirmed")],
        default="draft",
        required=True,
        index=True,
        tracking=True,
    )
    submit_server_action_id = fields.Many2one(
        "ir.actions.server", readonly=True, ondelete="set null",
        string="Submit Server Action",
    )
    approve_server_action_id = fields.Many2one(
        "ir.actions.server", readonly=True, ondelete="set null",
        string="Approve Server Action",
    )
    reject_server_action_id = fields.Many2one(
        "ir.actions.server", readonly=True, ondelete="set null",
        string="Reject Server Action",
    )
    inherit_view_id = fields.Many2one(
        "ir.ui.view", readonly=True, ondelete="set null",
        string="Injected Inherited View",
    )
    view_approvals_server_action_id = fields.Many2one(
        "ir.actions.server", readonly=True, ondelete="set null",
        string="View Approvals Server Action",
    )

    _sql_constraints = [
        (
            "approval_config_unique_model_name",
            "unique(model_id, name)",
            "An approval configuration with this name already exists for this model.",
        ),
    ]

    # -------------------------------------------------------------------------
    # Constraints / onchange
    # -------------------------------------------------------------------------
    @api.constrains("view_id", "model_id")
    def _check_view_matches_model(self):
        for rec in self:
            if rec.view_id and rec.model_id:
                if rec.view_id.model != rec.model_id.model:
                    raise ValidationError(
                        _("Selected view does not belong to the selected model.")
                    )

    @api.onchange("model_id")
    def _onchange_model_id(self):
        self.view_id = False

    # -------------------------------------------------------------------------
    # State transitions
    # -------------------------------------------------------------------------
    def action_draft(self):
        for cfg in self:
            if cfg.inherit_view_id:
                cfg.inherit_view_id.sudo().unlink()
            cfg.write({"state": "draft"})
            cfg._ensure_approval_fields_removed_if_unused()
        return True

    def action_confirm(self):
        for cfg in self:
            cfg._action_confirm()
        return True

    def unlink(self):
        model_names = self.mapped("model_id.model")
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

        res = super().unlink()

        for model_name in model_names:
            self._ensure_approval_fields_removed_if_unused_for(model_name)

        return res

    # -------------------------------------------------------------------------
    # ir.model.fields management
    # -------------------------------------------------------------------------
    def _approval_fields_exist(self, model_name):
        """Kiểm tra field x_approval_state đã tồn tại trên model chưa."""
        return bool(
            self.env["ir.model.fields"].sudo().search([
                ("model", "=", model_name),
                ("name", "=", FIELD_STATE),
            ], limit=1)
        )

    def _ensure_approval_fields_created(self):
        """
        Tạo 3 stored field trên target model qua ir.model.fields.
        - Nếu model đã có field (config thứ 2 trở đi) → bỏ qua, dùng chung.
        - Field được Odoo tạo cột DB tự động sau khi create ir.model.fields.
        """
        self.ensure_one()
        model_name = self.model_id.model
        IrModelFields = self.env["ir.model.fields"].sudo()

        if self._approval_fields_exist(model_name):
            return  # Đã có — config thứ 2, 3 cùng model không tạo lại

        # x_approval_state — Selection, stored
        IrModelFields.create({
            "model_id": self.model_id.id,
            "name": FIELD_STATE,
            "field_description": "Approval State",
            "ttype": "selection",
            "selection": APPROVAL_STATE_SELECTION,
            "store": True,
            "copied": False,
            "readonly": True,
        })

        # x_approval_is_approver — Boolean, NOT stored
        # Không lưu DB vì phụ thuộc user hiện tại,
        # được set qua _update_approval_fields_on_record với sudo
        IrModelFields.create({
            "model_id": self.model_id.id,
            "name": FIELD_IS_APPROVER,
            "field_description": "Is Approver",
            "ttype": "boolean",
            "store": False,
            "copied": False,
            "readonly": True,
        })

        # x_approval_approved_by — Char, stored
        IrModelFields.create({
            "model_id": self.model_id.id,
            "name": FIELD_APPROVED_BY,
            "field_description": "Approved By",
            "ttype": "char",
            "store": True,
            "copied": False,
            "readonly": True,
        })

    def _ensure_approval_fields_removed_if_unused(self):
        self.ensure_one()
        self._ensure_approval_fields_removed_if_unused_for(self.model_id.model)

    def _ensure_approval_fields_removed_if_unused_for(self, model_name):
        """
        Xóa 3 field khi không còn confirmed config nào trên model.
        Odoo tự drop cột DB khi unlink ir.model.fields.
        """
        if not model_name:
            return

        remaining = self.env["approval.config"].sudo().search([
            ("model_id.model", "=", model_name),
            ("state", "=", "confirmed"),
        ], limit=1)

        if remaining:
            return  # Còn config khác → giữ field

        self.env["ir.model.fields"].sudo().search([
            ("model", "=", model_name),
            ("name", "in", [FIELD_STATE, FIELD_IS_APPROVER, FIELD_APPROVED_BY]),
        ]).unlink()

    # -------------------------------------------------------------------------
    # Sync state lên record nguồn
    # -------------------------------------------------------------------------
    @api.model
    def _update_approval_fields_on_record(
        self, model_name, res_id, state, approved_by=""
    ):
        """
        Write x_approval_state và x_approval_approved_by lên record nguồn.
        Được gọi từ approval.request mỗi khi state thay đổi.
        Dùng sudo() vì user thường không có quyền write trên target model.
        """
        if model_name not in self.env:
            return
        if not self._approval_fields_exist(model_name):
            return

        record = self.env[model_name].sudo().browse(res_id)
        if not record.exists():
            return

        record.write({
            FIELD_STATE: state,
            FIELD_APPROVED_BY: approved_by or False,
        })

    # -------------------------------------------------------------------------
    # Confirm
    # -------------------------------------------------------------------------
    def _action_confirm(self):
        self.ensure_one()

        if self.state != "draft":
            raise ValidationError(_("Only draft configurations can be confirmed."))
        if not self.approver_ids:
            raise ValidationError(_("Please select at least one approver."))
        if not self.model_id or not self.view_id:
            raise ValidationError(_("Model and View are required."))
        if self.view_id.model != self.model_id.model:
            raise ValidationError(
                _("Selected view does not belong to the selected model.")
            )

        # Sync approvers vào group
        group = self.sudo().env.ref(STATIC_APPROVAL_GROUP_XMLID)
        all_confirmed_approver_ids = self.env["approval.config"].sudo().search([
            ("state", "=", "confirmed"),
            ("id", "!=", self.id),
        ]).mapped("approver_ids").ids
        new_approver_ids = list(
            set(all_confirmed_approver_ids + self.approver_ids.ids)
        )
        # gán người duyệt vào group approver
        group.write({"users": [(6, 0, new_approver_ids)]})

        # Tạo field trên target model (idempotent — bỏ qua nếu đã có)
        self.sudo()._ensure_approval_fields_created()

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

        sudo_cfg.write({
            "submit_server_action_id": submit_action.id,
            "approve_server_action_id": approve_action.id,
            "reject_server_action_id": reject_action.id,
            "view_approvals_server_action_id": view_approvals_action.id,
            "inherit_view_id": inherit_view.id,
        })

    # -------------------------------------------------------------------------
    # Server actions
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
            ) % self.id,
        }
        if self.view_approvals_server_action_id:
            self.view_approvals_server_action_id.write(vals)
            return self.view_approvals_server_action_id
        return self.env["ir.actions.server"].create(vals)

    def _ensure_inherited_view(
        self, submit_action, approve_action, reject_action, view_approvals_action
    ):
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

        # Submit: hiện khi chưa có state hoặc state = draft
        submit_btn = _safe_btn(
            submit_action.id,
            _("Submit for Approval"),
            "btn-primary",
            "{f} != False and {f} != 'draft'".format(f=FIELD_STATE),
        )
        approve_btn = _safe_btn(
            approve_action.id,
            _("Approve"),
            "btn-success",
            "{f} != 'waiting'".format(f=FIELD_STATE),
            groups=STATIC_APPROVAL_GROUP_XMLID,
        )
        reject_btn = _safe_btn(
            reject_action.id,
            _("Reject"),
            "btn-danger",
            "{f} != 'waiting'".format(f=FIELD_STATE),
            groups=STATIC_APPROVAL_GROUP_XMLID,
        )

        va_id = int(view_approvals_action.id)

        def _view_btn(label, invisible_expr, css_extra=""):
            return (
                '<button name="{va_id}" type="action"'
                ' class="btn-light border ms-2 {css}"'
                ' invisible="{inv}" string="{label}"/>'
            ).format(va_id=va_id, label=label, inv=invisible_expr, css=css_extra)

        view_waiting_btn = _view_btn(
            "⏳ Waiting",
            "{f} != 'waiting'".format(f=FIELD_STATE),
            "text-warning",
        )
        view_approved_btn = _view_btn(
            "✅ Approved",
            "{f} != 'approved'".format(f=FIELD_STATE),
            "text-success",
        )
        view_rejected_btn = _view_btn(
            "❌ Rejected",
            "{f} != 'rejected'".format(f=FIELD_STATE),
            "text-danger",
        )
        view_cancel_btn = _view_btn(
            "🚫 Cancelled",
            "{f} != 'cancelled'".format(f=FIELD_STATE),
            "text-danger",
        )

        buttons_xml = (
            "    <field name=\"{state}\" invisible=\"1\"/>\n"
            "    <field name=\"{approved_by}\" invisible=\"1\"/>\n"
            "    {submit}\n"
            "    {approve}\n"
            "    {reject}\n"
            "    {view_waiting}\n"
            "    {view_approved}\n"
            "    {view_rejected}\n"
            "    {view_cancel}\n"
        ).format(
            state=FIELD_STATE,
            approved_by=FIELD_APPROVED_BY,
            submit=submit_btn,
            approve=approve_btn,
            reject=reject_btn,
            view_waiting=view_waiting_btn,
            view_approved=view_approved_btn,
            view_rejected=view_rejected_btn,
            view_cancel=view_cancel_btn,
        )

        source_view = self.view_id
        try:
            arch_tree = etree.fromstring(source_view.arch_db.encode("utf-8"))
            has_header = bool(arch_tree.find(".//header"))
        except Exception:
            has_header = False

        if has_header:
            arch_db = (
                "<data>\n"
                "  <xpath expr=\"//form/header\" position=\"inside\">\n"
                "{buttons}"
                "  </xpath>\n"
                "</data>"
            ).format(buttons=buttons_xml)
        else:
            arch_db = (
                "<data>\n"
                "  <xpath expr=\"//form/*[1]\" position=\"before\">\n"
                "    <header>\n"
                "{buttons}"
                "    </header>\n"
                "  </xpath>\n"
                "</data>"
            ).format(buttons=buttons_xml)

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

        # Race condition guard — config_id vào query để tránh conflict nhiều config
        self.env.cr.execute(
            """
            SELECT id FROM approval_request
            WHERE model = %s AND res_id = %s AND config_id = %s AND state = 'waiting'
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """,
            (record._name, record.id, self.id),
        )
        if self.env.cr.fetchone():
            raise UserError(
                _("A pending approval request already exists for this record.")
            )

        request = self.env["approval.request"].sudo().create({
            "model": record._name,
            "res_id": record.id,
            "requester_id": self.env.user.id,
            "approver_ids": [(6, 0, self.approver_ids.ids)],
            "config_id": self.id,
            "state": "waiting",
            "require_all_approvers": self.require_all_approvers,
        })

        # Sync trạng thái lên record nguồn
        self._update_approval_fields_on_record(record._name, record.id, "waiting")
        request._notify_approvers()
        return True

    def _server_action_approve(self, record):
        self.ensure_one()
        if not record or not record.exists():
            return True
        if record._name != self.model_id.model:
            return True

        if self.env.user not in self.approver_ids:
            raise UserError(_("You are not authorized to approve this record."))

        request = self.env["approval.request"].search([
            ("model", "=", record._name),
            ("res_id", "=", record.id),
            ("config_id", "=", self.id),
            ("state", "=", "waiting"),
        ], limit=1)
        if not request:
            raise UserError(_("No pending approval request found for this record."))

        request.sudo()._do_approve(self.env.user)
        return True

    def _server_action_reject(self, record):
        self.ensure_one()
        if not record or not record.exists():
            return True
        if record._name != self.model_id.model:
            return True

        if self.env.user not in self.approver_ids:
            raise UserError(_("You are not authorized to reject this record."))

        request = self.env["approval.request"].search([
            ("model", "=", record._name),
            ("res_id", "=", record.id),
            ("config_id", "=", self.id),
            ("state", "=", "waiting"),
        ], limit=1)
        if not request:
            raise UserError(_("No pending approval request found for this record."))

        request.sudo()._do_reject(self.env.user)
        return True