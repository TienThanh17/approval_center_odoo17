# -*- coding: utf-8 -*-
"""
Tests cho approval_center.

Chạy: odoo-bin -d <db> --test-enable -u approval_center
"""
from unittest.mock import patch

from odoo.exceptions import UserError, ValidationError
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestApprovalConfig(TransactionCase):
    """Tests cho approval.config."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.approver1 = cls.env["res.users"].create({
            "name": "Approver One",
            "login": "approver1_test@test.com",
            "groups_id": [(4, cls.env.ref("approval_center.group_approval_approver").id)],
        })
        cls.approver2 = cls.env["res.users"].create({
            "name": "Approver Two",
            "login": "approver2_test@test.com",
            "groups_id": [(4, cls.env.ref("approval_center.group_approval_approver").id)],
        })
        cls.requester = cls.env["res.users"].create({
            "name": "Requester",
            "login": "requester_test@test.com",
            "groups_id": [(4, cls.env.ref("approval_center.group_approval_user").id)],
        })

        # Lấy model res.partner để test
        cls.partner_model = cls.env["ir.model"].search([("model", "=", "res.partner")], limit=1)
        cls.partner_form_view = cls.env["ir.ui.view"].search([
            ("model", "=", "res.partner"),
            ("type", "=", "form"),
            ("inherit_id", "=", False),
        ], limit=1)

    def _make_config(self, name="Test Config", require_all=False, state="draft"):
        cfg = self.env["approval.config"].create({
            "name": name,
            "model_id": self.partner_model.id,
            "view_id": self.partner_form_view.id,
            "approver_ids": [(6, 0, [self.approver1.id, self.approver2.id])],
            "require_all_approvers": require_all,
        })
        return cfg

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------
    def test_view_must_match_model(self):
        """ValidationError khi view không thuộc model."""
        other_view = self.env["ir.ui.view"].search([
            ("model", "!=", "res.partner"),
            ("type", "=", "form"),
        ], limit=1)
        if not other_view:
            self.skipTest("No other form view available")
        with self.assertRaises(ValidationError):
            self.env["approval.config"].create({
                "name": "Bad Config",
                "model_id": self.partner_model.id,
                "view_id": other_view.id,
                "approver_ids": [(4, self.approver1.id)],
            })

    def test_confirm_requires_approver(self):
        """Không thể confirm nếu không có approver."""
        cfg = self.env["approval.config"].create({
            "name": "No Approver Config",
            "model_id": self.partner_model.id,
            "view_id": self.partner_form_view.id,
        })
        with self.assertRaises(ValidationError):
            cfg.action_confirm()

    def test_confirm_creates_metadata(self):
        """Confirm tạo server actions và inherited view."""
        cfg = self._make_config()
        cfg.action_confirm()
        self.assertEqual(cfg.state, "confirmed")
        self.assertTrue(cfg.submit_server_action_id)
        self.assertTrue(cfg.approve_server_action_id)
        self.assertTrue(cfg.reject_server_action_id)
        self.assertTrue(cfg.inherit_view_id)

    def test_reset_to_draft_removes_view(self):
        """Reset to draft xóa inherited view."""
        cfg = self._make_config()
        cfg.action_confirm()
        view_id = cfg.inherit_view_id.id
        cfg.action_draft()
        self.assertEqual(cfg.state, "draft")
        self.assertFalse(self.env["ir.ui.view"].browse(view_id).exists())

    def test_unique_name_per_model(self):
        """Không thể có 2 config cùng tên trên cùng model."""
        self._make_config(name="Unique Config")
        with self.assertRaises(Exception):
            self._make_config(name="Unique Config")

    def test_different_name_same_model_allowed(self):
        """Được phép có nhiều config khác tên trên cùng model."""
        cfg1 = self._make_config(name="Config Alpha")
        cfg2 = self._make_config(name="Config Beta")
        self.assertTrue(cfg1 and cfg2)

    def test_onchange_model_resets_view(self):
        """Đổi model phải reset view_id."""
        cfg = self._make_config()
        cfg._onchange_model_id()
        self.assertFalse(cfg.view_id)

    def test_unlink_cleans_metadata(self):
        """Xóa config xóa luôn server actions và view."""
        cfg = self._make_config()
        cfg.action_confirm()
        submit_id = cfg.submit_server_action_id.id
        view_id = cfg.inherit_view_id.id
        cfg.unlink()
        self.assertFalse(self.env["ir.actions.server"].browse(submit_id).exists())
        self.assertFalse(self.env["ir.ui.view"].browse(view_id).exists())


@tagged("post_install", "-at_install")
class TestApprovalSubmit(TransactionCase):
    """Tests cho luồng Submit."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.approver1 = cls.env["res.users"].create({
            "name": "Approver A",
            "login": "approver_a@test.com",
            "groups_id": [(4, cls.env.ref("approval_center.group_approval_approver").id)],
        })
        cls.requester = cls.env["res.users"].create({
            "name": "Requester X",
            "login": "requester_x@test.com",
            "groups_id": [(4, cls.env.ref("approval_center.group_approval_user").id)],
        })
        cls.partner_model = cls.env["ir.model"].search([("model", "=", "res.partner")], limit=1)
        cls.partner_form_view = cls.env["ir.ui.view"].search([
            ("model", "=", "res.partner"),
            ("type", "=", "form"),
            ("inherit_id", "=", False),
        ], limit=1)
        cls.cfg = cls.env["approval.config"].create({
            "name": "Partner Approval",
            "model_id": cls.partner_model.id,
            "view_id": cls.partner_form_view.id,
            "approver_ids": [(6, 0, [cls.approver1.id])],
        })
        cls.cfg.action_confirm()
        cls.partner = cls.env["res.partner"].create({"name": "Test Partner For Approval"})

    def test_submit_creates_request(self):
        """Submit tạo approval.request ở trạng thái waiting."""
        self.cfg.with_user(self.requester)._server_action_submit(self.partner)
        req = self.env["approval.request"].search([
            ("model", "=", "res.partner"),
            ("res_id", "=", self.partner.id),
            ("state", "=", "waiting"),
        ])
        self.assertEqual(len(req), 1)
        req.unlink()  # cleanup

    def test_submit_no_duplicate(self):
        """Submit lần 2 khi đã có waiting request phải báo lỗi."""
        self.cfg.with_user(self.requester)._server_action_submit(self.partner)
        with self.assertRaises(UserError):
            self.cfg.with_user(self.requester)._server_action_submit(self.partner)
        # cleanup
        self.env["approval.request"].search([
            ("res_id", "=", self.partner.id),
            ("model", "=", "res.partner"),
        ]).unlink()

    def test_submit_sets_res_name(self):
        """Request phải có res_name sau khi tạo."""
        self.cfg.with_user(self.requester)._server_action_submit(self.partner)
        req = self.env["approval.request"].search([
            ("res_id", "=", self.partner.id),
            ("model", "=", "res.partner"),
            ("state", "=", "waiting"),
        ], limit=1)
        self.assertTrue(req.res_name)
        req.unlink()


@tagged("post_install", "-at_install")
class TestApprovalApproveReject(TransactionCase):
    """Tests cho luồng Approve và Reject."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.approver1 = cls.env["res.users"].create({
            "name": "Approver P",
            "login": "approver_p@test.com",
            "groups_id": [(4, cls.env.ref("approval_center.group_approval_approver").id)],
        })
        cls.approver2 = cls.env["res.users"].create({
            "name": "Approver Q",
            "login": "approver_q@test.com",
            "groups_id": [(4, cls.env.ref("approval_center.group_approval_approver").id)],
        })
        cls.outsider = cls.env["res.users"].create({
            "name": "Outsider",
            "login": "outsider@test.com",
            "groups_id": [(4, cls.env.ref("approval_center.group_approval_user").id)],
        })
        cls.partner_model = cls.env["ir.model"].search([("model", "=", "res.partner")], limit=1)
        cls.partner_form_view = cls.env["ir.ui.view"].search([
            ("model", "=", "res.partner"),
            ("type", "=", "form"),
            ("inherit_id", "=", False),
        ], limit=1)

    def _make_waiting_request(self, require_all=False):
        cfg = self.env["approval.config"].create({
            "name": "Test Approval %s" % require_all,
            "model_id": self.partner_model.id,
            "view_id": self.partner_form_view.id,
            "approver_ids": [(6, 0, [self.approver1.id, self.approver2.id])],
            "require_all_approvers": require_all,
        })
        cfg.action_confirm()
        partner = self.env["res.partner"].create({"name": "Partner %s" % require_all})
        req = self.env["approval.request"].create({
            "model": "res.partner",
            "res_id": partner.id,
            "requester_id": self.outsider.id,
            "approver_ids": [(6, 0, [self.approver1.id, self.approver2.id])],
            "config_id": cfg.id,
            "state": "waiting",
            "require_all_approvers": require_all,
        })
        return cfg, req

    # ------------------------------------------------------------------
    # Approve
    # ------------------------------------------------------------------
    def test_approve_any_approver_is_enough(self):
        """Khi require_all=False, 1 approver duyệt là đủ."""
        cfg, req = self._make_waiting_request(require_all=False)
        req._do_approve(self.approver1)
        self.assertEqual(req.state, "approved")

    def test_approve_require_all_needs_both(self):
        """Khi require_all=True, phải đủ tất cả approver."""
        cfg, req = self._make_waiting_request(require_all=True)
        req._do_approve(self.approver1)
        self.assertEqual(req.state, "waiting")  # chưa đủ
        req._do_approve(self.approver2)
        self.assertEqual(req.state, "approved")  # đủ rồi

    def test_approve_duplicate_blocked(self):
        """Cùng approver không thể duyệt 2 lần."""
        _, req = self._make_waiting_request(require_all=True)
        req._do_approve(self.approver1)
        with self.assertRaises(UserError):
            req._do_approve(self.approver1)

    def test_approve_outsider_blocked(self):
        """Người không phải approver không thể duyệt."""
        cfg, _ = self._make_waiting_request()
        partner = self.env["res.partner"].create({"name": "Partner Outsider"})
        with self.assertRaises(UserError):
            cfg._server_action_approve(partner)

    # ------------------------------------------------------------------
    # Reject
    # ------------------------------------------------------------------
    def test_reject_changes_state(self):
        """Reject chuyển request sang rejected."""
        _, req = self._make_waiting_request()
        req._do_reject(self.approver1)
        self.assertEqual(req.state, "rejected")
        self.assertEqual(req.rejected_by_id, self.approver1)

    def test_reject_outsider_blocked(self):
        """Người không phải approver không thể reject."""
        cfg, _ = self._make_waiting_request()
        partner = self.env["res.partner"].create({"name": "Partner Reject Test"})
        with self.assertRaises(UserError):
            cfg.with_user(self.outsider)._server_action_reject(partner)

    # ------------------------------------------------------------------
    # Cancel
    # ------------------------------------------------------------------
    def test_cancel_waiting_request(self):
        """Cancel request đang waiting."""
        _, req = self._make_waiting_request()
        req.action_cancel()
        self.assertEqual(req.state, "cancelled")

    def test_cancel_approved_blocked(self):
        """Không thể cancel request đã approved."""
        _, req = self._make_waiting_request()
        req._do_approve(self.approver1)
        with self.assertRaises(UserError):
            req.action_cancel()

    def test_withdraw_from_approved(self):
        """Approve xong -> approver withdraw -> state == 'waiting', cleared approved_by_ids."""
        _, req = self._make_waiting_request()
        req._do_approve(self.approver1)
        req.with_user(self.approver1).action_withdraw()
        self.assertEqual(req.state, "waiting")
        self.assertFalse(req.approved_by_ids)
        self.assertFalse(req.approval_date)

    def test_withdraw_from_rejected(self):
        """Reject xong -> approver withdraw -> state == 'waiting', cleared rejected_by_id."""
        _, req = self._make_waiting_request()
        req._do_reject(self.approver1)
        req.with_user(self.approver1).action_withdraw()
        self.assertEqual(req.state, "waiting")
        self.assertFalse(req.rejected_by_id)

    def test_withdraw_outsider_blocked(self):
        """User khong phai approver withdraw -> UserError."""
        _, req = self._make_waiting_request()
        req._do_approve(self.approver1)
        with self.assertRaises(UserError):
            req.with_user(self.outsider).action_withdraw()

    def test_withdraw_wrong_state_blocked(self):
        """Withdraw khi state waiting -> UserError."""
        _, req = self._make_waiting_request()
        with self.assertRaises(UserError):
            req.with_user(self.approver1).action_withdraw()

    def test_back_to_draft_from_cancelled(self):
        """Cancel xong -> requester back to draft -> state == 'draft'."""
        _, req = self._make_waiting_request()
        req.with_user(self.outsider).action_cancel()
        req.with_user(self.outsider).action_back_to_draft()
        self.assertEqual(req.state, "draft")

    def test_back_to_draft_outsider_blocked(self):
        """User khong phai requester hay approver -> UserError."""
        _, req = self._make_waiting_request()
        req.with_user(self.outsider).action_cancel()
        outsider2 = self.env["res.users"].create({
            "name": "Outsider 2",
            "login": "outsider2@test.com",
            "groups_id": [(4, self.env.ref("approval_center.group_approval_user").id)],
        })
        with self.assertRaises(UserError):
            req.with_user(outsider2).action_back_to_draft()

    def test_back_to_draft_wrong_state_blocked(self):
        """Back to draft khi waiting -> UserError."""
        _, req = self._make_waiting_request()
        with self.assertRaises(UserError):
            req.with_user(self.outsider).action_back_to_draft()


@tagged("post_install", "-at_install")
class TestBaseInheritCompute(TransactionCase):
    """Tests cho base mixin — approval_state compute."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.approver = cls.env["res.users"].create({
            "name": "Approver Mixin",
            "login": "approver_mixin@test.com",
            "groups_id": [(4, cls.env.ref("approval_center.group_approval_approver").id)],
        })
        cls.partner_model = cls.env["ir.model"].search([("model", "=", "res.partner")], limit=1)
        cls.partner_form_view = cls.env["ir.ui.view"].search([
            ("model", "=", "res.partner"),
            ("type", "=", "form"),
            ("inherit_id", "=", False),
        ], limit=1)

    def test_approval_state_draft_when_no_request(self):
        """Record chưa có request → approval_state = 'draft'."""
        cfg = self.env["approval.config"].create({
            "name": "Mixin Test Config",
            "model_id": self.partner_model.id,
            "view_id": self.partner_form_view.id,
            "approver_ids": [(4, self.approver.id)],
        })
        cfg.action_confirm()
        partner = self.env["res.partner"].create({"name": "Mixin Partner"})
        self.assertEqual(partner.approval_state, "draft")

    def test_approval_state_waiting_after_submit(self):
        """Sau submit → approval_state = 'waiting'."""
        cfg = self.env["approval.config"].create({
            "name": "Mixin Test Config 2",
            "model_id": self.partner_model.id,
            "view_id": self.partner_form_view.id,
            "approver_ids": [(4, self.approver.id)],
        })
        cfg.action_confirm()
        partner = self.env["res.partner"].create({"name": "Mixin Partner 2"})
        self.env["approval.request"].create({
            "model": "res.partner",
            "res_id": partner.id,
            "requester_id": self.approver.id,
            "approver_ids": [(4, self.approver.id)],
            "config_id": cfg.id,
            "state": "waiting",
        })
        partner.invalidate_recordset()
        self.assertEqual(partner.approval_state, "waiting")

    def test_approval_state_false_for_unconfigured_model(self):
        """Model không có config → approval_state = False (không query thêm)."""
        # res.currency thường không có config
        currency = self.env["res.currency"].search([], limit=1)
        if not currency:
            self.skipTest("No currency found")
        self.assertFalse(currency.approval_state)

    def test_is_approver_flag(self):
        """approval_is_approver đúng với người có trong approver_ids của config."""
        cfg = self.env["approval.config"].create({
            "name": "Is Approver Test",
            "model_id": self.partner_model.id,
            "view_id": self.partner_form_view.id,
            "approver_ids": [(4, self.approver.id)],
        })
        cfg.action_confirm()
        partner = self.env["res.partner"].create({"name": "Is Approver Partner"})
        partner_as_approver = partner.with_user(self.approver)
        self.assertTrue(partner_as_approver.approval_is_approver)


@tagged("post_install", "-at_install")
class TestApprovalRequestDirectActions(TransactionCase):
    """Tests for direct Approve/Reject actions on approval.request."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.approver1 = cls.env["res.users"].create({
            "name": "Direct Approver 1",
            "login": "direct1@test.com",
            "groups_id": [(4, cls.env.ref("approval_center.group_approval_approver").id)],
        })
        cls.approver2 = cls.env["res.users"].create({
            "name": "Direct Approver 2",
            "login": "direct2@test.com",
            "groups_id": [(4, cls.env.ref("approval_center.group_approval_approver").id)],
        })
        cls.outsider = cls.env["res.users"].create({
            "name": "Direct Outsider",
            "login": "direct_out@test.com",
            "groups_id": [(4, cls.env.ref("approval_center.group_approval_user").id)],
        })
        cls.partner_model = cls.env["ir.model"].search([("model", "=", "res.partner")], limit=1)
        cls.partner_form_view = cls.env["ir.ui.view"].search([
            ("model", "=", "res.partner"),
            ("type", "=", "form"),
            ("inherit_id", "=", False),
        ], limit=1)

    def _make_waiting_request(self, require_all=False):
        cfg = self.env["approval.config"].create({
            "name": "Direct Config %s" % require_all,
            "model_id": self.partner_model.id,
            "view_id": self.partner_form_view.id,
            "approver_ids": [(6, 0, [self.approver1.id, self.approver2.id])],
            "require_all_approvers": require_all,
        })
        cfg.action_confirm()
        partner = self.env["res.partner"].create({"name": "Direct Partner %s" % require_all})
        req = self.env["approval.request"].create({
            "model": "res.partner",
            "res_id": partner.id,
            "requester_id": self.outsider.id,
            "approver_ids": [(6, 0, [self.approver1.id, self.approver2.id])],
            "config_id": cfg.id,
            "state": "waiting",
            "require_all_approvers": require_all,
        })
        return cfg, req

    def test_approve_request_direct(self):
        _, req = self._make_waiting_request(require_all=False)
        req.with_user(self.approver1).action_approve_request()
        self.assertEqual(req.state, "approved")
        self.assertIn(self.approver1, req.approved_by_ids)

    def test_reject_request_direct(self):
        _, req = self._make_waiting_request(require_all=False)
        req.with_user(self.approver1).action_reject_request()
        self.assertEqual(req.state, "rejected")
        self.assertEqual(req.rejected_by_id, self.approver1)

    def test_approve_require_all_partial(self):
        _, req = self._make_waiting_request(require_all=True)
        req.with_user(self.approver1).action_approve_request()
        self.assertEqual(req.state, "waiting")

    def test_approve_require_all_full(self):
        _, req = self._make_waiting_request(require_all=True)
        req.with_user(self.approver1).action_approve_request()
        req.with_user(self.approver2).action_approve_request()
        self.assertEqual(req.state, "approved")

    def test_approve_outsider_blocked(self):
        _, req = self._make_waiting_request()
        with self.assertRaises(UserError):
            req.with_user(self.outsider).action_approve_request()

    def test_reject_outsider_blocked(self):
        _, req = self._make_waiting_request()
        with self.assertRaises(UserError):
            req.with_user(self.outsider).action_reject_request()

    def test_approve_duplicate_blocked(self):
        _, req = self._make_waiting_request(require_all=True)
        req.with_user(self.approver1).action_approve_request()
        with self.assertRaises(UserError):
            req.with_user(self.approver1).action_approve_request()

    def test_approve_wrong_state_blocked(self):
        _, req = self._make_waiting_request(require_all=False)
        req.with_user(self.approver1).action_approve_request()
        with self.assertRaises(UserError):
            req.with_user(self.approver2).action_approve_request()
