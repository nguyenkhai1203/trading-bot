# Đặc Tả Tính Năng: Bot Giao Dịch Tương Lai Tự Động

**Nhánh Tính Năng**: `001-automated-futures-trading`  
**Ngày Tạo**: 2026-02-13  
**Trạng Thái**: Nháp  
**Mô Tả Người Dùng**: Bot tự động giao dịch future trên sàn Binance và Bybit, tự động phân tích 40+ chỉ báo kỹ thuật, đặt lệnh, quản lý rủi ro, và gửi thông báo qua Telegram

---

## Các Kịch Bản Người Dùng & Kiểm Thử *(bắt buộc)*

### Kịch Bản 1: Trader Khởi Động Bot & Giám Sát Giao Dịch (Ưu Tiên: P1)

**Mô Tả Hành Trình:**
Một trader muốn khởi động bot để tự động giao dịch 3 cặp tiền (BTC/USDT, ETH/USDT, XRP/USDT) trên 7 khung thời gian khác nhau (15m, 30m, 1h, 2h, 4h, 8h, 1d). Bot sẽ phân tích tín hiệu kỹ thuật, mở vị trí khi điều kiện phù hợp, đặt lệnh dừng lỗ (SL) và chốt lời (TP), đồng thời gửi thông báo về mỗi giao dịch qua Telegram.

**Lý Do Ưu Tiên**: Đây là tính năng cốt lõi của bot - nếu không có, bot không thể giao dịch. Nó cung cấp giá trị trực tiếp: tự động hóa quy trình giao dịch 24/7 mà không cần can thiệp thủ công.

**Kiểm Thử Độc Lập**: Có thể kiểm thử bằng cách (1) khởi động bot ở chế độ demo (dry_run=True), (2) chạy 12-24 giờ, (3) kiểm tra log giao dịch xem có vị trí nào được mở/đóng hay không, (4) xác nhận Telegram nhận được thông báo. Giá trị là: giao dịch tự động hoạt động mà không crash.

**Các Kịch Bản Chấp Nhận:**

1. **Given** bot được khởi động ở chế độ demo với DRY_RUN=True, **When** 1 giờ trôi qua, **Then** bot vẫn chạy, không bị crash, và cập nhật log mỗi 5 giây (heartbeat)

2. **Given** bot phát hiện tín hiệu BUY từ 40+ chỉ báo kỹ thuật với độ tin cậy ≥ 0.5, **When** không có vị trí nào mở trên cặp tiền đó, **Then** bot mở vị trí SHORT với margin cố định (ví dụ $3) và đặt SL + TP

3. **Given** vị trí được mở, **When** giá chạm mức TP hoặc SL, **Then** bot tự động đóng vị trí và ghi lại P&L (lợi nhuận/lỗ)

4. **Given** mỗi khi có giao dịch (mở/đóng/hủy), **When** sự kiện xảy ra, **Then** bot gửi thông báo đầy đủ qua Telegram (symbol, timeframe, side, entry/exit price, P&L)

---

### Kịch Bản 2: Tối Ưu Hóa Trọng Số Chiến Lược & Backtest (Ưu Tiên: P2)

**Mô Tả Hành Trình:**
Trader muốn kiểm tra xem chiến lược của mình có lợi nhuận trước khi triển khai live. Anh ta chạy `analyzer.py` để tối ưu hóa trọng số các chỉ báo kỹ thuật dựa trên dữ liệu lịch sử 6 tháng, sau đó chạy `backtester.py` để xác nhận tỷ lệ thắng tối thiểu 55%. Bot chỉ cho phép giao dịch live nếu backtest pass.

**Lý Do Ưu Tiên**: Ngăn chặn giao dịch lỗ do chiến lược kém. Đây là bước validation quan trọng trước triển khai.

**Kiểm Thử Độc Lập**: Chạy analyzer, xác nhận `strategy_config.json` được cập nhật với trọng số tối ưu. Chạy backtester, xác nhận báo cáo CSV được tạo với tỷ lệ thắng ≥ 55%. Giá trị: có bằng chứng toán học bot sẽ sinh lời.

**Các Kịch Bản Chấp Nhận:**

1. **Given** dữ liệu lịch sử 6 tháng cho BTC/USDT 1h, **When** analyzer chạy, **Then** `strategy_config.json` cập nhật, trọng số được tối ưu

2. **Given** chiến lược được tối ưu, **When** backtester chạy trên dữ liệu test, **Then** báo cáo CSV hiển thị tỷ lệ thắng ≥ 55% (hoặc ≥ 53% nếu cài đặt cho phép)

3. **Given** tỷ lệ thắng < 55%, **When** bot khởi động, **Then** cặp tiền đó bị vô hiệu hóa (set `enabled: false` trong config) và bot bỏ qua nó

---

### Kịch Bản 3: Quản Lý Rủi Ro & Circuit Breaker (Ưu Tiên: P2)

**Mô Tả Hành Trình:**
Trader lo ngại rủi ro nếu chiến lược xảy ra lỗi. Bot phải có hệ thống dừng toàn bộ giao dịch nếu (1) thua lỗ 10% tư vốn từ đỉnh (drawdown), hoặc (2) thua lỗ > 3% trong 1 ngày. Bot cũng phải có cooldown 2 giờ sau mỗi SL để tránh tái vào ngay lập tức.

**Lý Do Ưu Tiên**: Bảo vệ vốn là ưu tiên hàng đầu. Nếu không có circuit breaker, bot có thể xóa sạch tài khoản trong lúc ngủ.

**Kiểm Thử Độc Lập**: Có thể kiểm thử bằng cách giả lập khoản lỗ lớn, xác nhận bot dừng tất cả giao dịch và gửi cảnh báo Telegram.

**Các Kịch Bản Chấp Nhận:**

1. **Given** balance giảm từ $1000 (đỉnh) xuống $900 (drawdown 10%), **When** check circuit breaker, **Then** bot dừng mở vị trí mới và gửi cảnh báo

2. **Given** hôm nay bot thua lỗ > 3% so với đầu ngày, **When** check daily loss, **Then** bot dừng giao dịch cho đến hết ngày

3. **Given** vị trí bị stop loss, **When** 2 giờ chưa qua, **Then** bot vô hiệu hóa cặp tiền đó, không mở vị trí mới

---

### Kịch Bản 4: Thông Báo Telegram Real-Time (Ưu Tiên: P1)

**Mô Tả Hành Trình:**
Trader muốn nhận thông báo ngay lập tức khi bot mở/đóng vị trí hoặc gặp lỗi. Thông báo phải bao gồm: symbol, timeframe, side (BUY/SELL), entry price, current price, P&L (%), SL, TP, và status (PENDING/FILLED).

**Lý Do Ưu Tiên**: Nếu trader không biết bot đang làm gì, anh ta không thể tin tưởng. Telegram notifications là cách chủ yếu để giám sát.

**Kiểm Thử Độc Lập**: Khởi động bot, xác nhận mỗi lệnh được gửi qua Telegram trong vòng 5 giây. Kiểm tra format thông báo đúng: rõ ràng, dễ đọc, không lỗi.

**Các Kịch Bản Chấp Nhận:**

1. **Given** bot mở vị trí BUY BTC/USDT 1h, **When** lệnh được tạo, **Then** Telegram nhận thông báo trong < 5s với đầy đủ chi tiết

2. **Given** vị trí BUY đóng lỗ, **When** SL bị hit, **Then** Telegram nhận thông báo: "🔴 STOP LOSS hit" + symbol + P&L (%)

3. **Given** vị trí mở chốt lời, **When** TP bị hit, **Then** Telegram nhận: "🟢 TAKE PROFIT hit" + P&L (%)

---

### Kịch Bản 5: Chế Độ Demo & Triển Khai Dần (Ưu Tiên: P2)

**Mô Tả Hành Trình:**
Trader muốn test bot trên "giấy" (paper trading) trước khi dùng tiền thật. Bot chế độ demo (dry_run=True) giả lập tất cả giao dịch mà không gửi lệnh thực đến sàn. Sau khi test 24-48 giờ, nếu kết quả tốt, trader có thể chuyển sang live (dry_run=False).

**Lý Do Ưu Tiên**: Giảm rủi ro khi triển khai. Hầu hết người dùng sẽ bắt đầu từ demo.

**Kiểm Thử Độc Lập**: Chạy bot ở chế độ demo 48 giờ, xác nhận: (1) không có lệnh thực được gửi, (2) giao dịch giả lập được ghi log đúng, (3) P&L được tính toán, (4) Telegram vẫn nhận thông báo. Giá trị: bạn có thể test toàn bộ hệ thống mà không cần tiền thật.

**Các Kịch Bản Chấp Nhận:**

1. **Given** bot được khởi động với DRY_RUN=True, **When** tín hiệu được phát hiện, **Then** bot mở vị trí giả lập, không gửi lệnh thực đến sàn

2. **Given** chế độ demo, **When** vị trí đóng, **Then** P&L được tính, trade_history.json được cập nhật, Telegram nhận thông báo

3. **Given** trader quyết định chuyển sang live, **When** set DRY_RUN=False, **Then** bot gửi lệnh thực đến Binance/Bybit

---

### Trường Hợp Biên

- **Nếu không có kết nối mạng**? Bot lưu trữ trạng thái cục bộ (positions.json), khi kết nối lại sẽ đồng bộ với sàn
- **Nếu bot crash giữa chừng**? Deep sync reconciliation kiểm tra tất cả vị trị trên sàn mỗi 10 phút, tự động khôi phục
- **Nếu Telegram không sẵn sàng (token không hợp lệ)**? Bot vẫn giao dịch bình thường, chỉ không gửi thông báo
- **Nếu tín hiệu đảo chiều nhanh (trong 1 phút)**? Bot hủy lệnh pending và gửi cảnh báo
- **Nếu multiple positions trên cùng một symbol**? Bot chỉ cho phép 1 vị trí/symbol, các timeframe khác bị block

---

## Yêu Cầu *(bắt buộc)*

### Yêu Cầu Chức Năng

- **YC-001**: Bot PHẢI phân tích 40+ chỉ báo kỹ thuật (EMA, MACD, RSI, Ichimoku, VWAP, Volume Spike, v.v.)
- **YC-002**: Bot PHẢI gán trọng số cho mỗi chỉ báo, tính điểm tin cậy (0-10)
- **YC-003**: Bot PHẢI yêu cầu điểm tin cậy ≥ 0.5 để mở vị trí
- **YC-004**: Bot PHẢI hỗ trợ 7 khung thời gian: 15m, 30m, 1h, 2h, 4h, 8h, 1d
- **YC-005**: Bot PHẢI hỗ trợ tối thiểu 3 cặp tiền (BTC/USDT, ETH/USDT, XRP/USDT)
- **YC-006**: Bot PHẢI tính toán SL & TP cho mỗi vị trí dựa trên tỷ lệ rủi ro:ro = 1:3 (SL 1.7%, TP 4%)
- **YC-007**: Bot PHẢI đặt lệnh dừng lỗ (stop loss) tự động trên sàn nếu live mode
- **YC-008**: Bot PHẢI đóng vị trí khi giá chạm SL hoặc TP
- **YC-009**: Bot PHẢI ghi lại mỗi giao dịch vào trade_history.json với P&L
- **YC-010**: Bot PHẢI hỗ trợ hai chế độ: dry_run (giấy) và live (tiền thật)
- **YC-011**: Bot PHẢI gửi thông báo Telegram khi: vị trí mở, vị trí đóng, SL hit, TP hit, circuit breaker triggered
- **YC-012**: Bot PHẢI có circuit breaker: dừng nếu drawdown ≥ 10% hoặc daily loss ≥ 3%
- **YC-013**: Bot PHẢI áp dụng cooldown 2 giờ sau mỗi stop loss
- **YC-014**: Bot PHẢI tối ưu hóa trọng số bằng `analyzer.py` trên dữ liệu lịch sử 6 tháng
- **YC-015**: Bot PHẢI chạy backtest trước khi mở vị trí trên từng cặp tiền
- **YC-016**: Bot PHẢI yêu cầu tỷ lệ thắng ≥ 55% trên cả train & test set
- **YC-017**: Bot PHẢI vô hiệu hóa cặp tiền nếu backtest fail
- **YC-018**: Bot PHẢI hỗ trợ cấu hình JSON (strategy_config.json) - hot reloadable
- **YC-019**: Bot PHẢI đồng bộ thời gian với sàn mỗi 1 giờ để tránh lỗi timestamp
- **YC-020**: Bot PHẢI reconcile (đối sánh) tất cả vị trí với sàn mỗi 10 phút

### Các Thực Thể Chính

- **Position (Vị Trí)**: Đại diện một giao dịch mở, bao gồm: symbol, timeframe, side (BUY/SELL), entry_price, quantity, leverage, sl, tp, status (pending/filled), entry_confidence
- **Trade (Giao Dịch)**: Ghi lại giao dịch đã đóng: symbol, side, entry_price, exit_price, quantity, pnl_usdt, pnl_pct, entry_time, exit_time, exit_reason (SL/TP/manual)
- **Signal (Tín Hiệu)**: Kết quả phân tích: side (BUY/SELL/SKIP), confidence (0-10), comment (danh sách chỉ báo kích hoạt)
- **Strategy Config (Cấu Hình Chiến Lược)**: JSON file chứa trọng số, ngưỡng, tiers (minimum/low/high confidence)
- **Circuit Breaker State**: Trạng thái giới hạn rủi ro: peak_balance, daily_loss, drawdown %, last_reset_date

---

## Tiêu Chí Thành Công *(bắt buộc)*

### Kết Quả Đo Lường Được

- **TC-001**: Bot khởi động thành công trong < 30 giây, không bị crash trong 24 giờ liên tục
- **TC-002**: Mỗi vị trí mở bắt đầu trong < 5 giây từ khi tín hiệu phát hiện
- **TC-003**: Telegram nhận thông báo mỗi sự kiện giao dịch trong < 5 giây
- **TC-004**: Backtest win rate ≥ 55% trên cả train & test set của tất cả cặp tiền được bật
- **TC-005**: Bot không mở vị trí mới nếu drawdown ≥ 10% hoặc daily loss ≥ 3%
- **TC-006**: Mỗi giao dịch ghi lại chính xác P&L vào trade_history.json trong ± 0.01%
- **TC-007**: Deep sync reconciliation hoàn tất trong < 30 giây mỗi 10 phút
- **TC-008**: Bot xử lý 21+ bots (3 symbols × 7 timeframes) đồng thời mà CPU < 80%, memory < 500MB
- **TC-009**: Chế độ demo ghi lại 100% giao dịch giả lập, không gửi lệnh thực
- **TC-010**: Nếu Telegram unavailable, bot vẫn giao dịch bình thường (graceful degradation)
- **TC-011**: Tỷ lệ successful backtest ≥ 80% cho các cặp tiền được chọn (ít nhất 2/3 hoặc 3/5)
- **TC-012**: Bot tự động khôi phục từ network disconnect trong < 2 phút

---

## Ghi Chú & Giả Định

**Giả Định 1**: Dữ liệu lịch sử từ Binance API qua CCXT là chính xác và có sẵn  
**Giả Định 2**: Margin (vốn giao dịch) có sẵn trên sàn (hoặc được mô phỏng ở chế độ demo)  
**Giả Định 3**: Telegram Bot Token được cấu hình đúng trong .env (nếu muốn thông báo)  
**Giả Định 4**: Network connection ổn định ≥ 99% uptime  
**Giả Định 5**: Các công cụ: Python 3.10+, CCXT, pandas, numpy, python-telegram-bot

---

## Phụ Thuộc & Rủi Ro

- **PH-001**: Phụ thuộc Binance/Bybit API - nếu API bị thay đổi, cần cập nhật CCXT
- **PH-002**: Phụ thuộc dữ liệu thị trường - nếu dữ liệu delay, tín hiệu có thể lag
- **RUI-001**: Nếu backtest overfitting, bot sẽ mất tiền live → giải pháp: strict train/test split, consistency check
- **RUI-002**: Nếu circuit breaker tắt, bot có thể xóa sạch account → giải pháp: circuit breaker non-negotiable, bắt buộc bật
- **RUI-003**: Nếu Telegram token sai, trader không biết bot đang làm gì → giải pháp: log đầy đủ, fallback to console output

