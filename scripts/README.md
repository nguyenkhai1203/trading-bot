# Utility Scripts

Các công cụ hỗ trợ vận hành và kiểm tra Trading Bot.
Để chạy các script này từ thư mục gốc của dự án (`d:\code\tradingBot`), hãy dùng lệnh:

```bash
py scripts/<script_name>.py
```

## Danh sách Script

### 1. `run_audit.py`
**Công dụng:** Kiểm tra toàn diện trạng thái hệ thống.
- Check lệch giờ server.
- Check số dư USDT.
- Liệt kê các vị thế đang mở và lệnh pending (SL/TP breakdown).
**Cách dùng:** `py scripts/run_audit.py`

### 2. `check_orphans.py`
**Công dụng:** Quét và xử lý các lệnh "mồ côi" (orphaned orders) trên sàn mà không khớp với vị thế nào trong bot.
**Cách dùng:** `py scripts/check_orphans.py`

### 3. `diagnose_account.py`
**Công dụng:** Chẩn đoán nhanh thông tin tài khoản, quyền API, và cấu hình margin.
**Cách dùng:** `py scripts/diagnose_account.py`

### 4. `download_data.py`
**Công dụng:** Tải dữ liệu nến quá khứ từ Binance về để phục vụ backtest hoặc train AI.
**Cách dùng:** `py scripts/download_data.py`
