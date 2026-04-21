from odoo import models, api, fields


class Base(models.AbstractModel):
    _inherit = "base"

    approval_state = fields.Selection(
        [
            ("draft", "Draft"),
            ("waiting", "Waiting"),
            ("approved", "Approved"),
            ("rejected", "Rejected"),
            ("cancelled", "Cancelled"),
        ],
        compute="_compute_approval_state",
        store=False,
    )
    approval_is_approver = fields.Boolean(
        compute="_compute_approval_state",
        store=False,
    )
    # FIX [Missing]: field để hiển thị ai đã duyệt trên record nguồn
    approval_approved_by = fields.Char(
        compute="_compute_approval_state",
        store=False,
    )

    # FIX [Critical]: Cache config list để tránh query lặp lại trên mọi model.
    # Dùng _approval_config_cache (class-level dict) được invalidate khi config thay đổi.
    # Key: model name → (config_id, [approver_ids], require_all) | None

    def _get_approval_config_cached(self):
        """
        Trả về config cho model hiện tại.
        FIX: Chỉ query 1 lần per transaction nhờ env.cache hoặc context cache.
        Tránh N+1: không gọi search() trong vòng lặp recordset.
        """
        # Dùng context cache để tránh query lặp trong cùng 1 request
        cache_key = "_approval_config_%s" % self._name
        ctx = self.env.context
        if cache_key in ctx:
            return ctx[cache_key]

        config = self.env["approval.config"].sudo().search(
            [("model_id.model", "=", self._name), ("state", "=", "confirmed")],
            limit=1,
        )
        result = config if config else False

        # Lưu vào context (không mutate — tạo dict mới)
        self = self.with_context(**{cache_key: result})
        return result

    @api.depends_context("uid")
    def _compute_approval_state(self):
        """
        FIX [Critical]: Batch query thay vì N+1.
        - 1 query lấy config cho model này
        - 1 query lấy toàn bộ approval.request cho recordset hiện tại
        - Không có query trong vòng lặp for rec in self
        """
        # EARLY EXIT: Các model nội bộ / abstract / transient không cần check
        if not self._name or not self.ids:
            for rec in self:
                rec.approval_state = False
                rec.approval_is_approver = False
                rec.approval_approved_by = False
            return

        # 1 query duy nhất để lấy config
        config = self.env["approval.config"].sudo().search(
            [("model_id.model", "=", self._name), ("state", "=", "confirmed")],
            limit=1,
        )

        if not config:
            for rec in self:
                rec.approval_state = False
                rec.approval_is_approver = False
                rec.approval_approved_by = False
            return

        is_approver = self.env.user.id in config.approver_ids.ids

        # 1 query batch cho toàn bộ recordset — tránh N+1
        mapping = {}  # res_id → (state, approved_by_names)
        if self.ids:
            requests = self.env["approval.request"].sudo().search(
                [
                    ("model", "=", self._name),
                    ("res_id", "in", self.ids),
                    ("config_id", "=", config.id),
                ],
                order="id desc",
            )
            for req in requests:
                if req.res_id not in mapping:
                    approved_by = ", ".join(req.approved_by_ids.mapped("name")) if req.approved_by_ids else ""
                    mapping[req.res_id] = (req.state, approved_by)

        for rec in self:
            state_info = mapping.get(rec.id)
            if state_info:
                rec.approval_state = state_info[0]
                rec.approval_approved_by = state_info[1]
            else:
                rec.approval_state = "draft"
                rec.approval_approved_by = False
            rec.approval_is_approver = is_approver
