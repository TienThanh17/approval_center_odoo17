import ast
import json

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
        required=True,
        tracking=True,
    )
    require_all_approvers = fields.Boolean(
        string="Require All Approvers",
        default=False,
        tracking=True,
    )

    # ------------------------------------------------------------------
    # Submit condition
    # ------------------------------------------------------------------
    submit_condition_domain = fields.Char(
        string="Submit Condition",
        default="[]",
        help="Condition domain for the record to be submitted. "
             "Leave empty or [] means no condition.",
    )

    # ------------------------------------------------------------------
    # Approve condition
    # ------------------------------------------------------------------
    approve_condition_domain = fields.Char(
        string="Approve Condition",
        default="[]",
        help="Domain definition for fields to be updated after approval. "
             "Each leaf (field, '=', value) will be written to the source record.",
    )

    # ------------------------------------------------------------------
    # Metadata (generated)
    # ------------------------------------------------------------------
    state = fields.Selection(
        [("draft", "Draft"), ("confirmed", "Confirmed")],
        default="draft",
        required=True,
        index=True,
        tracking=True,
    )
    submit_server_action_id = fields.Many2one(
        "ir.actions.server",
        readonly=True,
        ondelete="set null",
        string="Submit Server Action",
    )
    approve_server_action_id = fields.Many2one(
        "ir.actions.server",
        readonly=True,
        ondelete="set null",
        string="Approve Server Action",
    )
    reject_server_action_id = fields.Many2one(
        "ir.actions.server",
        readonly=True,
        ondelete="set null",
        string="Reject Server Action",
    )
    inherit_view_id = fields.Many2one(
        "ir.ui.view",
        readonly=True,
        ondelete="set null",
        string="Injected Inherited View",
    )
    view_approvals_server_action_id = fields.Many2one(
        "ir.actions.server",
        readonly=True,
        ondelete="set null",
        string="View Approvals Server Action",
    )

    _sql_constraints = [
        # (
        #     "approval_config_unique_model_name",
        #     "unique(model_id, name)",
        #     "An approval configuration with this name already exists for this model.",
        # ),
        (
            "approval_config_unique_model_id",
            "unique(model_id)",
            "An approval configuration already exists for this model. Each model can only have one configuration.",
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
        self.submit_condition_domain = "[]"
        self.approve_condition_domain = "[]"

    # -------------------------------------------------------------------------
    # Domain helpers
    # -------------------------------------------------------------------------
    def _parse_domain(self, domain_str):
        """Parse domain string thành list. Trả về [] nếu rỗng hoặc invalid.

        Domain widget của Odoo lưu dạng Python literal: [('field', '=', val)]
        (single quotes, tuples) nên phải dùng ast.literal_eval, không phải json.loads.
        """
        if not domain_str or domain_str.strip() in ("[]", "False", ""):
            return []
        # Thử json.loads trước (nếu đã là JSON chuẩn)
        try:
            domain = json.loads(domain_str)
            if isinstance(domain, list):
                return domain
        except (ValueError, TypeError):
            pass
        # Fallback: ast.literal_eval cho cú pháp Python (Odoo domain widget)
        try:
            domain = ast.literal_eval(domain_str)
            if isinstance(domain, list):
                return domain
        except (ValueError, SyntaxError, TypeError):
            pass
        return []

    def _parse_domain_leaves(self, domain):
        """Trích xuất danh sách (field_name, operator, value) từ domain.
        Bỏ qua các logic operator '&', '|', '!'.
        """
        leaves = []
        for item in domain:
            if (
                    isinstance(item, (list, tuple))
                    and len(item) == 3
                    and isinstance(item[0], str)
                    and item[0] not in ("&", "|", "!")
            ):
                leaves.append((item[0], item[1], item[2]))
        return leaves

    def _check_submit_condition(self, record):
        """Kiểm tra record có thỏa submit_condition_domain không.
        Nếu không thỏa → raise UserError liệt kê các field vi phạm
        kèm giá trị expected (đã convert display_name/label cho dễ đọc).
        """
        self.ensure_one()
        domain = self._parse_domain(self.submit_condition_domain)
        if not domain:
            return True

        matched = (
            self.env[record._name]
            .sudo()
            .search_count([("id", "=", record.id)] + domain)
        )
        if matched:
            return True

        # Tìm các field vi phạm
        IrModelFields = self.env["ir.model.fields"]
        leaves = self._parse_domain_leaves(domain)
        violations = []

        # Lấy thông tin fields_get một lần để xử lý field selection an toàn và tối ưu
        field_names = [leaf[0] for leaf in leaves]
        fields_info = self.env[record._name].sudo().fields_get(field_names)

        for field_name, operator, value in leaves:
            field_def = IrModelFields.search(
                [
                    ("model", "=", record._name),
                    ("name", "=", field_name),
                ],
                limit=1,
            )
            field_label = field_def.field_description if field_def else field_name
            display_value = value  # Đặt giá trị mặc định là value gốc

            if field_def:
                try:
                    if field_def.ttype == 'selection':
                        # Trích xuất dictionary các tuple (key, label) từ fields_info
                        if fields_info.get(field_name) and fields_info[field_name].get('selection'):
                            selection_dict = dict(fields_info[field_name]['selection'])
                            display_value = selection_dict.get(value, value)

                    elif field_def.ttype in ('many2one', 'many2many', 'one2many'):
                        rel_model = field_def.relation
                        if rel_model and rel_model in self.env:
                            # Value trong domain có thể là 1 ID (int) hoặc danh sách ID (list/tuple)
                            if isinstance(value, int):
                                browse_ids = [value]
                            elif isinstance(value, (list, tuple)):
                                browse_ids = [v for v in value if isinstance(v, int)]
                            else:
                                browse_ids = []

                            if browse_ids:
                                rel_records = self.env[rel_model].sudo().browse(browse_ids).exists()
                                if rel_records:
                                    # Nối các display_name lại với nhau cách nhau bằng dấu phẩy
                                    display_value = ", ".join(rel_records.mapped('display_name'))
                except Exception:
                    # Nếu có lỗi bất ngờ khi parse (ví dụ value dị thường), bỏ qua và dùng value gốc
                    pass

            violations.append(_("• %s: must be '%s'") % (field_label, display_value))

        msg = _("The record does not meet the conditions to submit for approval:\n%s") % "\n".join(
            violations
        )
        raise UserError(msg)

        # msg = _("The record does not meet the conditions to submit for approval")
        # raise UserError(msg)

    def _apply_approve_condition(self, record):
        """Parse approve_condition_domain và write value lên record nguồn.
        Chỉ xử lý các leaf có operator '=' hoặc '=='.
        Ví dụ: [('state', '=', 'done')] → record.write({'state': 'done'})
        """
        self.ensure_one()
        domain = self._parse_domain(self.approve_condition_domain)
        if not domain:
            return

        leaves = self._parse_domain_leaves(domain)
        vals = {}
        for field_name, operator, value in leaves:
            if operator in ("=", "=="):
                vals[field_name] = value
        if vals:
            record.sudo().write(vals)

    # -------------------------------------------------------------------------
    # State transitions
    # -------------------------------------------------------------------------
    def action_draft(self):
        for cfg in self:
            if cfg.inherit_view_id:
                cfg.inherit_view_id.sudo().unlink()
            cfg.write({"state": "draft"})
            cfg._ensure_approval_fields_removed_if_unused()

        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }

    def action_confirm(self):
        for cfg in self:
            cfg._action_confirm()

        return {
            'type': 'ir.actions.client',
            'tag': 'reload',
        }

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
        return bool(
            self.env["ir.model.fields"]
            .sudo()
            .search(
                [
                    ("model", "=", model_name),
                    ("name", "=", FIELD_STATE),
                ],
                limit=1,
            )
        )

    def _ensure_approval_fields_created(self):
        self.ensure_one()
        model_name = self.model_id.model
        IrModelFields = self.env["ir.model.fields"].sudo()

        if self._approval_fields_exist(model_name):
            return

        IrModelFields.create(
            {
                "model_id": self.model_id.id,
                "name": FIELD_STATE,
                "field_description": "Approval State",
                "ttype": "selection",
                "selection": APPROVAL_STATE_SELECTION,
                "store": True,
                "copied": False,
                "readonly": True,
            }
        )
        IrModelFields.create(
            {
                "model_id": self.model_id.id,
                "name": FIELD_IS_APPROVER,
                "field_description": "Is Approver",
                "ttype": "boolean",
                "store": False,
                "copied": False,
                "readonly": True,
            }
        )
        IrModelFields.create(
            {
                "model_id": self.model_id.id,
                "name": FIELD_APPROVED_BY,
                "field_description": "Approved By",
                "ttype": "char",
                "store": True,
                "copied": False,
                "readonly": True,
            }
        )

    def _ensure_approval_fields_removed_if_unused(self):
        self.ensure_one()
        # self._ensure_approval_fields_removed_if_unused_for(self.model_id.model)

    def _ensure_approval_fields_removed_if_unused_for(self, model_name):
        if not model_name:
            return
        # remaining = self.env["approval.config"].sudo().search([
        #     ("model_id.model", "=", model_name),
        #     ("state", "=", "confirmed"),
        # ], limit=1)
        # if remaining:
        #     return
        self.env["ir.model.fields"].sudo().search(
            [
                ("model", "=", model_name),
                ("name", "in", [FIELD_STATE, FIELD_IS_APPROVER, FIELD_APPROVED_BY]),
            ]
        ).unlink()

    # -------------------------------------------------------------------------
    # Sync approval state lên record nguồn
    # -------------------------------------------------------------------------
    @api.model
    def _update_approval_fields_on_record(
            self, model_name, res_id, state, approved_by=""
    ):
        if model_name not in self.env:
            return
        if not self._approval_fields_exist(model_name):
            return
        record = self.env[model_name].sudo().browse(res_id)
        if not record.exists():
            return
        record.write(
            {
                FIELD_STATE: state,
                FIELD_APPROVED_BY: approved_by or False,
            }
        )

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
        all_confirmed_approver_ids = (
            self.env["approval.config"]
            .sudo()
            .search(
                [
                    ("state", "=", "confirmed"),
                    ("id", "!=", self.id),
                ]
            )
            .mapped("approver_ids")
            .ids
        )
        new_approver_ids = list(set(all_confirmed_approver_ids + self.approver_ids.ids))
        group.write({"users": [(6, 0, new_approver_ids)]})

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
                    )
                    % self.id,
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
                    )
                    % self.id,
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
                    )
                    % self.id,
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
                    )
                    % self.id,
        }
        if self.view_approvals_server_action_id:
            self.view_approvals_server_action_id.write(vals)
            return self.view_approvals_server_action_id
        return self.env["ir.actions.server"].create(vals)

    def _ensure_inherited_view(
            self, submit_action, approve_action, reject_action, view_approvals_action
    ):
        self.ensure_one()

        # ---------------------------------------------------------------
        # 1. Build header action buttons (Submit / Approve / Reject)
        # ---------------------------------------------------------------
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

        header_buttons_xml = (
            '    <field name="{state}" invisible="1"/>\n'
            '    <field name="{approved_by}" invisible="1"/>\n'
            "    {submit}\n"
            "    {approve}\n"
            "    {reject}\n"
        ).format(
            state=FIELD_STATE,
            approved_by=FIELD_APPROVED_BY,
            submit=submit_btn,
            approve=approve_btn,
            reject=reject_btn,
        )

        # ---------------------------------------------------------------
        # 2. Build smart buttons (status indicators in oe_button_box)
        # ---------------------------------------------------------------
        va_id = int(view_approvals_action.id)

        def _smart_btn(label, invisible_expr, icon, text_class):
            """Return XML string for an oe_stat_button smart button (etree-safe)."""
            btn = etree.Element("button")
            btn.set("name", str(va_id))
            btn.set("type", "action")
            btn.set("class", "oe_stat_button")
            btn.set("icon", icon)
            btn.set("invisible", invisible_expr)
            div = etree.SubElement(btn, "div")
            div.set("class", "o_field_widget o_stat_info")
            span = etree.SubElement(div, "span")
            span.set("class", "o_stat_text " + text_class)
            span.text = label
            return etree.tostring(btn, encoding="unicode")

        smart_waiting = _smart_btn(
            _("Pending Approval"),
            "{f} != 'waiting'".format(f=FIELD_STATE),
            "fa-clock-o",
            "text-warning",
        )
        smart_approved = _smart_btn(
            _("Approved"),
            "{f} != 'approved'".format(f=FIELD_STATE),
            "fa-check-circle",
            "text-success",
        )
        smart_rejected = _smart_btn(
            _("Rejected"),
            "{f} != 'rejected'".format(f=FIELD_STATE),
            "fa-times-circle",
            "text-danger",
        )
        smart_cancelled = _smart_btn(
            _("Cancelled"),
            "{f} != 'cancelled'".format(f=FIELD_STATE),
            "fa-ban",
            "text-danger",
        )

        smart_buttons_xml = (
            "{waiting}\n"
            "{approved}\n"
            "{rejected}\n"
            "{cancelled}\n"
        ).format(
            waiting=smart_waiting,
            approved=smart_approved,
            rejected=smart_rejected,
            cancelled=smart_cancelled,
        )

        # ---------------------------------------------------------------
        # 3. Analyse source view to decide injection strategy
        # ---------------------------------------------------------------
        source_view = self.view_id
        try:
            arch_tree = etree.fromstring(source_view.arch_db.encode("utf-8"))
            has_header = arch_tree.find(".//header") is not None
            _bbox = arch_tree.find('.//div[@name="button_box"]')
            if _bbox is None:
                _bbox = arch_tree.find('.//div[contains(@class,"oe_button_box")]')
            has_button_box = _bbox is not None
            has_sheet = arch_tree.find(".//sheet") is not None
        except Exception:
            has_header = False
            has_button_box = False
            has_sheet = False
            _bbox = None

        # ---------------------------------------------------------------
        # 4. Build arch_db with separate xpath blocks
        # ---------------------------------------------------------------
        xpath_blocks = []

        # 4a. Header action buttons
        if has_header:
            xpath_blocks.append(
                '  <xpath expr="//form/header" position="inside">\n'
                + header_buttons_xml
                + "  </xpath>"
            )
        else:
            # Tao header moi truoc element dau tien cua form
            xpath_blocks.append(
                '  <xpath expr="//form/*[1]" position="before">\n'
                "    <header>\n"
                + header_buttons_xml
                + "    </header>\n"
                + "  </xpath>"
            )

        # 4b. Smart buttons vao oe_button_box
        if has_button_box:
            # Inject vao button_box hien co (uu tien name='button_box' truoc)
            if _bbox is not None and _bbox.get('name') == 'button_box':
                box_expr = "//div[@name='button_box']"
            else:
                box_expr = "//div[contains(@class,'oe_button_box')]"
            xpath_blocks.append(
                '  <xpath expr="{expr}" position="inside">\n'.format(expr=box_expr)
                + smart_buttons_xml
                + "  </xpath>"
            )
        elif has_sheet:
            # Sheet ton tai nhung chua co button_box -> them button_box dau tien trong sheet
            xpath_blocks.append(
                '  <xpath expr="//sheet" position="inside">\n'
                '    <div name="button_box" class="oe_button_box">\n'
                + smart_buttons_xml
                + "    </div>\n"
                + "  </xpath>"
            )
        else:
            # Khong co sheet, khong co button_box -> them div truoc noi dung form
            if has_header:
                xpath_blocks.append(
                    '  <xpath expr="//form/header" position="after">\n'
                    '    <div name="button_box" class="oe_button_box">\n'
                    + smart_buttons_xml
                    + "    </div>\n"
                    + "  </xpath>"
                )
            else:
                xpath_blocks.append(
                    '  <xpath expr="//form/*[1]" position="before">\n'
                    '    <div name="button_box" class="oe_button_box">\n'
                    + smart_buttons_xml
                    + "    </div>\n"
                    + "  </xpath>"
                )

        arch_db = "<data>\n" + "\n".join(xpath_blocks) + "\n</data>"

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

    def _server_action_submit(self, record):
        self.ensure_one()
        if not record or not record.exists():
            return True
        if record._name != self.model_id.model:
            return True

        # Kiểm tra điều kiện submit — popup liệt kê field vi phạm nếu không thỏa
        self._check_submit_condition(record)

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

        request = (
            self.env["approval.request"]
            .sudo()
            .create(
                {
                    "model": record._name,
                    "res_id": record.id,
                    "requester_id": self.env.user.id,
                    "approver_ids": [(6, 0, self.approver_ids.ids)],
                    "config_id": self.id,
                    "state": "waiting",
                    "require_all_approvers": self.require_all_approvers,
                }
            )
        )

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

        request = self.env["approval.request"].search(
            [
                ("model", "=", record._name),
                ("res_id", "=", record.id),
                ("config_id", "=", self.id),
                ("state", "=", "waiting"),
            ],
            limit=1,
        )
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

        request = self.env["approval.request"].search(
            [
                ("model", "=", record._name),
                ("res_id", "=", record.id),
                ("config_id", "=", self.id),
                ("state", "=", "waiting"),
            ],
            limit=1,
        )
        if not request:
            raise UserError(_("No pending approval request found for this record."))

        request.sudo()._do_reject(self.env.user)
        return True
