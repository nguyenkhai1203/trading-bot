# Thư Mục Đặc Tả

Thư mục này chứa tất cả đặc tả tính năng (Feature Specifications) cho dự án Trading Bot.

## Cấu Trúc

Mỗi tính năng được tổ chức trong một thư mục riêng:

```
specs/
├── 001-automated-futures-trading/
│   ├── spec.md          ← Đặc tả chi tiết (bắt buộc)
│   ├── research.md      ← Nghiên cứu kỹ thuật (sau khi plan)
│   ├── data-model.md    ← Mô hình dữ liệu (sau khi plan)
│   ├── quickstart.md    ← Hướng dẫn nhanh (sau khi plan)
│   └── tasks.md         ← Danh sách nhiệm vụ (sau khi tasks command)
```

## Danh Sách Tính Năng Hiện Tại

### ✅ 001-automated-futures-trading
**Status**: Draft  
**Mô Tả**: Bot tự động giao dịch future trên Binance & Bybit với AI signals  
**Lần Cập Nhật**: 2026-02-13  
**Phạm Vi**: 5 user stories, 20 yêu cầu, 12 tiêu chí thành công

## Quy Trình Phát Triển Tính Năng

### 1. Specify Phase (Hiện Tại)
Định nghĩa rõ ràng **WHAT** (yêu cầu), **WHY** (giá trị), **HOW to test** (kiểm thử)

```bash
# Tạo spec mới
/speckit.specify "Mô tả tính năng..."
```

### 2. Plan Phase (Tiếp Theo)
Thiết kế kỹ thuật, research, data model, tasks breakdown

```bash
# Lên kế hoạch chi tiết
/speckit.plan
```

### 3. Implement Phase
Code theo chi tiết plan, test, commit

### 4. Verify Phase
Backtest, dry-run 24-48h, review, merge to main

## Chú Ý Quan Trọng

- **Mỗi spec PHẢI tuân thủ Constitution** (.specify/memory/constitution.md)
- **User Stories PHẢI P1/P2/P3 priority** - không có "nice to have" không đánh giá
- **Yêu Cầu PHẢI testable** - không có requirement mơ hồ
- **Tiêu Chí Thành Công PHẢI đo lường được** - không có "user satisfied"
- **Trường Hợp Biên PHẢI xác định rõ** - network down, crash, invalid token, v.v.

## Tài Liệu Tham Khảo

- Template: `.specify/templates/spec-template.md`
- Constitution: `.specify/memory/constitution.md`
- README: `README.md`
