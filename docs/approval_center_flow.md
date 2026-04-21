# ADEC SOL Approval Center — Technical Documentation

## Tổng quan

Module `approval_center` cung cấp luồng phê duyệt động (dynamic approval workflow) có thể áp dụng cho **bất kỳ model** nào trong Odoo 18 mà không cần sửa code của model đó.

---

## Kiến trúc

```
approval.config  (cấu hình — 1 per model/use-case)
    │
    ├── tạo ra → ir.actions.server (Submit / Approve / Reject)
    ├── tạo ra → ir.ui.view (inject nút vào form view của model đích)
    │
    └── liên kết → approval.request  (1 request per submission)
                        │
                        └── approved_by_ids / rejected_by_id
```

**`base` mixin** inject `approval_state` + `approval_is_approver` vào mọi model bằng `compute` (không store), dùng để điều khiển hiển thị nút trên view.

---

## Luồng nghiệp vụ

### Submit
1. User bấm **"Submit for Approval"** trên form record.
2. `ir.actions.server` gọi `approval.config._server_action_submit(record)`.
3. Kiểm tra không có request `waiting` nào tồn tại (dùng `SELECT FOR UPDATE SKIP LOCKED` để tránh race condition).
4. Tạo `approval.request` ở trạng thái `waiting`.
5. Gửi notification email + tạo `mail.activity` cho từng approver.

### Approve
1. Approver bấm **"Approve"** (chỉ hiện khi `approval_state == 'waiting'` và user là approver).
2. `ir.actions.server` gọi `approval.config._server_action_approve(record)`.
3. Kiểm tra user thuộc `approver_ids` của config.
4. Gọi `approval.request._do_approve(user)`.
5. Nếu `require_all_approvers = False`: chuyển ngay sang `approved`.
6. Nếu `require_all_approvers = True`: chỉ chuyển `approved` khi tất cả approver đã duyệt.

### Reject
1. Approver bấm **"Reject"** (chỉ hiện khi `approval_state == 'waiting'` và user là approver).
2. Gọi `approval.config._server_action_reject(record)`.
3. Chuyển request sang `rejected`, ghi nhận `rejected_by_id`, gửi notification cho requester.

### Cancel
- User (requester) hoặc admin bấm **"Cancel Request"** trên form `approval.request`.
- Chỉ được cancel khi state là `draft` hoặc `waiting`.
- Xóa `mail.activity` đang mở.

---

## Cấu hình (approval.config)

| Field | Mô tả |
|---|---|
| `name` | Tên định danh config (unique per model) |
| `model_id` | Model áp dụng (vd: `res.partner`) |
| `view_id` | Form view cần inject nút (phải thuộc model) |
| `approver_ids` | Danh sách approver |
| `require_all_approvers` | `True` = tất cả phải duyệt; `False` = 1 người là đủ |
| `state` | `draft` → `confirmed` |

**Lưu ý**: Nhiều config có thể tồn tại trên cùng 1 model (khác `name`). Khi đó mỗi config inject nút riêng vào view riêng của nó.

---

## Phân quyền

| Group | Quyền |
|---|---|
| `group_approval_user` | Đọc tất cả request; tạo/submit; sửa/cancel request của chính mình |
| `group_approval_approver` | Kế thừa User + sửa request mình là approver; truy cập menu Configurations |

---

## Hiệu năng

### `_compute_approval_state` (base mixin)
- **Batch query**: 1 lần search `approval.config` + 1 lần search `approval.request` cho toàn bộ recordset. Không có query trong vòng lặp.
- **Early exit**: Nếu model không có config confirmed → return ngay, không query `approval.request`.
- Field là `store=False` (computed, không lưu DB) nên không ảnh hưởng write performance.

### Race condition prevention
- `_server_action_submit` dùng `SELECT FOR UPDATE SKIP LOCKED` trước khi `create`.
- Partial unique index trên `(model, res_id, config_id) WHERE state = 'waiting'` là safety net tầng DB.

---

## Edge cases & Lưu ý

### Khi model nguồn bị xóa
- `approval.request` vẫn tồn tại với `res_name = "(Deleted)"`.
- Nút "Open Source" sẽ raise `UserError` thay vì crash.

### Khi config bị xóa
- `submit_server_action_id`, `approve_server_action_id`, `reject_server_action_id` → `ondelete='set null'`.
- `inherit_view_id` → `ondelete='set null'` + xóa trong `unlink()`.
- Server action code check `if config.exists()` trước khi chạy → không gây lỗi.

### Khi uninstall module
- `uninstall_hook` quét và xóa toàn bộ view + action được tạo tự động.
- Quét orphan theo naming convention `AdecSol Submit/Approve/Reject (%)` và `approval_center.inject.%`.

---

## Chạy tests

```bash
odoo-bin -d <db_name> --test-enable -u approval_center --test-tags approval
```

Các test class:
- `TestApprovalConfig` — validation, confirm/draft flow, metadata creation
- `TestApprovalSubmit` — submit, duplicate prevention, res_name
- `TestApprovalApproveReject` — approve (any/all), reject, cancel, outsider blocked
- `TestBaseInheritCompute` — compute state, is_approver flag, unconfigured model early exit

---

## Changelog

### v2.0.0
- **[Critical Fix]** Race condition trong submit: thêm `SELECT FOR UPDATE SKIP LOCKED` + uncommit partial unique index.
- **[Critical Fix]** Group approver bị ghi đè sai: sync toàn bộ approver từ tất cả confirmed config.
- **[Critical Fix]** N+1 query trong `_compute_approval_state`: batch query, early exit cho unconfigured model.
- **[Critical Fix]** Server action `exists()` check tránh lỗi khi config bị xóa.
- **[Missing]** Luồng Reject hoàn chỉnh: server action, nút, logic `_do_reject`, notification.
- **[Missing]** Multi-level approval: `require_all_approvers`, `approved_by_ids` tracking.
- **[Missing]** Email notification + `mail.activity` khi submit/reject.
- **[Missing]** Cancel request: action, button, xóa activity.
- **[Missing]** `deadline` field trên request.
- **[Technical]** `view_id` domain động theo `model_id`.
- **[Technical]** `onchange` reset `view_id` khi đổi `model_id`.
- **[Technical]** `res_name` computed field hiển thị tên record nguồn.
- **[Technical]** Bỏ unique constraint `unique(model_id)` → `unique(model_id, name)`.
- **[Technical]** Inject view name thêm config id để tránh conflict nhiều config trên 1 model.
- **[UX]** Kanban view cho `approval.request` grouped by state.
- **[UX]** Search view với filters: "To Approve", "Submitted by Me", "Overdue".
- **[UX]** Menu "My Requests" riêng cho requester.
- **[UX]** `approval_date`, `rejected_by_id`, `approved_by_ids` hiển thị trên form.
- **[Technical]** Test suite đầy đủ: 4 class, 18 test case.
