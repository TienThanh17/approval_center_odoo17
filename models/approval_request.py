from markupsafe import Markup
from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ApprovalRequest(models.Model):
    _name = "approval.request"
    _description = "Approval Request"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "id desc"

    model = fields.Char(required=True, index=True)
    res_id = fields.Integer(required=True, index=True)
    res_name = fields.Char(
        string="Source Record",
        compute="_compute_res_name",
        store=True,
    )
    requester_id = fields.Many2one(
        "res.users",
        string="Requester",
        default=lambda self: self.env.user,
        required=True,
        index=True,
        tracking=True,
    )
    approver_ids = fields.Many2many(
        "res.users",
        "approval_request_res_users_rel",
        "request_id",
        "user_id",
        string="Approvers",
    )
    approved_by_ids = fields.Many2many(
        "res.users",
        "approval_request_approved_by_rel",
        "request_id",
        "user_id",
        string="Approved By",
        readonly=True,
    )
    rejected_by_id = fields.Many2one(
        "res.users",
        string="Rejected By",
        readonly=True,
    )
    config_id = fields.Many2one(
        "approval.config",
        string="Configuration",
        ondelete="cascade",
        index=True,
    )
    name = fields.Char(related="config_id.name", string="Approval Name", store=True)
    require_all_approvers = fields.Boolean(
        string="Require All Approvers",
        default=False,
    )
    state = fields.Selection(
        [
            ("draft", "Draft"),
            ("waiting", "Waiting"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("cancelled", "Cancelled"),
        ],
        default="draft",
        required=True,
        index=True,
        tracking=True,
        group_expand="_read_group_states",
    )
    deadline = fields.Date(
        string="Deadline",
        index=True,
        tracking=True,
    )
    approval_date = fields.Datetime(
        string="Approved/Rejected On",
        readonly=True,
    )

    @api.model
    def _read_group_states(self, stages, domain, order):
        return ["draft", "waiting", "approved", "rejected", "cancelled"]

    # -------------------------------------------------------------------------
    # Compute
    # -------------------------------------------------------------------------
    @api.depends("model", "res_id")
    def _compute_res_name(self):
        for rec in self:
            if rec.model and rec.res_id and rec.model in self.env:
                try:
                    source = self.env[rec.model].browse(rec.res_id)
                    rec.res_name = source.display_name if source.exists() else _("(Deleted)")
                except Exception:
                    rec.res_name = False
            else:
                rec.res_name = False

    def init(self):
        super().init()
        self.env.cr.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS approval_request_unique_waiting_idx
            ON approval_request (model, res_id, config_id)
            WHERE state = 'waiting'
            """
        )

    # -------------------------------------------------------------------------
    # Helper: sync x_approval_* lên record nguồn
    # -------------------------------------------------------------------------
    def _sync_state_to_source(self, state, approved_by=""):
        """
        Gọi approval.config._update_approval_fields_on_record để write
        x_approval_state và x_approval_approved_by lên record nguồn.
        Tách thành method riêng để dễ override.
        """
        self.ensure_one()
        self.env["approval.config"]._update_approval_fields_on_record(
            self.model, self.res_id, state, approved_by
        )

    # -------------------------------------------------------------------------
    # Actions
    # -------------------------------------------------------------------------
    def action_open_source_record(self):
        self.ensure_one()
        if not self.model or not self.res_id:
            raise UserError(_("Missing model or res_id to open source record."))
        if self.model not in self.env:
            raise UserError(_("Source model no longer exists in the system."))
        source_record = self.env[self.model].browse(self.res_id)
        if not source_record.exists():
            raise UserError(_("Source record has been deleted."))

        view_id = self.config_id.view_id.id if self.config_id and self.config_id.view_id else False
        return {
            "type": "ir.actions.act_window",
            "name": _("Source Record"),
            "res_model": self.model,
            "view_mode": "form",
            "res_id": self.res_id,
            "target": "current",
            "views": [(view_id, "form")],
        }

    def action_cancel(self):
        for req in self:
            if req.state not in ("draft", "waiting"):
                raise UserError(_("Only draft or waiting requests can be cancelled."))
            if self.env.user not in req.approver_ids:
                raise UserError(_("Only approvers can cancel this request."))
            if req.state == "waiting":
                req.activity_ids.unlink()
            req.write({"state": "cancelled"})
            req._sync_state_to_source("cancelled")
            req.message_post(
                body=_("Approval request cancelled by %s.") % self.env.user.name
            )
        return True

    def action_approve_request(self):
        self.ensure_one()
        if self.env.user not in self.approver_ids:
            raise UserError(_("You are not an approver for this request."))
        self._do_approve(self.env.user)
        return True

    def action_reject_request(self):
        self.ensure_one()
        if self.env.user not in self.approver_ids:
            raise UserError(_("You are not an approver for this request."))
        self._do_reject(self.env.user)
        return True

    def action_withdraw(self):
        for req in self:
            if req.state not in ("approved", "rejected"):
                raise UserError(_("Only approved or rejected requests can be withdrawn."))
            if self.env.user not in req.approver_ids:
                raise UserError(_("Only approvers can withdraw this request."))
            req.write({
                "state": "waiting",
                "approved_by_ids": [(5, 0, 0)],
                "rejected_by_id": False,
                "approval_date": False,
            })
            req._sync_state_to_source("waiting")
            req._notify_approvers()
            req.message_post(
                body=_("Approval request withdrawn by %s and reverted to waiting.")
                % self.env.user.name
            )
        return True

    def action_back_to_draft(self):
        for req in self:
            if req.state != "cancelled":
                raise UserError(_("Only cancelled requests can be reverted to draft."))
            if self.env.user != req.requester_id and self.env.user not in req.approver_ids:
                raise UserError(
                    _("Only the requester or an approver can revert this request to draft.")
                )
            req.write({"state": "draft"})
            req._sync_state_to_source("draft")
            req.message_post(
                body=_("Approval request reverted to draft by %s.") % self.env.user.name
            )
        return True

    # -------------------------------------------------------------------------
    # Core approve / reject
    # -------------------------------------------------------------------------
    def _do_approve(self, user):
        self.ensure_one()
        if self.state != "waiting":
            raise UserError(_("This request is no longer waiting for approval."))
        if user not in self.approver_ids:
            raise UserError(_("You are not an approver for this request."))
        if user in self.approved_by_ids:
            raise UserError(_("You have already approved this request."))

        self.approved_by_ids = [(4, user.id)]

        if self.require_all_approvers:
            remaining = self.approver_ids - self.approved_by_ids
            if remaining:
                self.message_post(
                    body=_("Approved by %s. Waiting for: %s")
                    % (user.name, ", ".join(remaining.mapped("name")))
                )
                return  # Chưa đủ

        approved_by_names = ", ".join(self.approved_by_ids.mapped("name"))
        self.write({
            "state": "approved",
            "approval_date": fields.Datetime.now(),
        })
        self.activity_ids.action_done()

        # Sync state lên record nguồn
        self._sync_state_to_source("approved", approved_by_names)

        # Áp dụng approve_condition_domain: write các giá trị từ domain lên record nguồn
        if self.config_id and self.model and self.res_id and self.model in self.env:
            source_record = self.env[self.model].browse(self.res_id)
            if source_record.exists():
                self.config_id._apply_approve_condition(source_record)

        self.message_post(
            body=_("✅ Request approved by %s.") % user.name,
            subtype_xmlid="mail.mt_note",
        )

    def _do_reject(self, user):
        self.ensure_one()
        if self.state != "waiting":
            raise UserError(_("This request is no longer waiting for approval."))
        if user not in self.approver_ids:
            raise UserError(_("You are not an approver for this request."))

        self.write({
            "state": "rejected",
            "rejected_by_id": user.id,
            "approval_date": fields.Datetime.now(),
        })
        self.activity_ids.action_done()

        # Sync lên record nguồn
        self._sync_state_to_source("rejected")

        self.message_post(
            body=_("❌ Request rejected by %s.") % user.name,
            subtype_xmlid="mail.mt_note",
        )
        self._notify_requester_rejected(user)

    # -------------------------------------------------------------------------
    # Notifications
    # -------------------------------------------------------------------------
    def _notify_approvers(self):
        self.ensure_one()
        if not self.approver_ids:
            return

        activity_type = self.env.ref(
            "mail.mail_activity_data_todo", raise_if_not_found=False
        )
        if activity_type:
            for approver in self.approver_ids:
                self.activity_schedule(
                    activity_type_id=activity_type.id,
                    summary=_("Approval Required: %s") % (self.name or self.model),
                    note=_("Record '%s' submitted by %s requires your approval.")
                    % (self.res_name or self.res_id, self.requester_id.name),
                    user_id=approver.id,
                    date_deadline=self.deadline or fields.Date.today(),
                )

        body_html = _(
            "📋 New approval request for <b>%s</b> (record: %s) submitted by %s.<br/>"
            "Approvers: %s"
        ) % (
            self.name or self.model,
            self.res_name or str(self.res_id),
            self.requester_id.name,
            ", ".join(self.approver_ids.mapped("name")),
        )
        self.message_post(
            body=Markup(body_html),
            partner_ids=self.approver_ids.mapped("partner_id").ids,
            subtype_xmlid="mail.mt_comment",
        )

    def _notify_requester_rejected(self, rejected_by):
        self.ensure_one()
        self.message_post(
            body=_("Your approval request for '%s' has been rejected by %s.")
            % (self.res_name or str(self.res_id), rejected_by.name),
            partner_ids=self.requester_id.partner_id.ids,
            subtype_xmlid="mail.mt_comment",
        )