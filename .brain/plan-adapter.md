AI AGENT INSTRUCTION: BYBIT TRADING BOT INTEGRATION
Nhu cầu: cần thêm 1 sàn nữa là bybit để tối ưu việc giao dịch, gửi TP-SL-Leverage ngon hơn, nhưng vẫn muốn giữ binance vì uy tín. Nên cần thêm 1 sàn này và có thể các sàn khác nữa, vì data và cách đặt lệnh khác nhau, nên cần làm adapter để tối ưu việc giao dịch và tối ưu hiệu quả của bot.
1. Kiến trúc dự án (Architecture)
Áp dụng Adapter Pattern. Phải tách biệt logic phân tích (Analysis) và logic thực thi sàn (Exchange Execution).

Core: Dùng chung logic tính toán tín hiệu từ nến (OHLCV).

Adapters: Tạo BybitAdapter kế thừa từ BaseAdapter.

Data Normalization: Mọi dữ liệu trả về từ Adapter (nến, giá, trạng thái lệnh) phải được chuẩn hóa về cùng một định dạng (Standard Object) trước khi đưa vào Core.

2. Module: Data Acquisition (Nến & Giá)
Thông số: 25 Tokens | 8 Timeframes | 2000 nến/bộ.

Kỹ thuật Fetch Nến (Bybit V5): * Bybit giới hạn 1000 nến/request. Để lấy 2000 nến, Agent phải thực hiện 2 lần fetch (phân trang bằng tham số since hoặc startCursor).

Sử dụng asyncio để fetch song song 25 tokens nhằm tránh bottleneck, nhưng phải giới hạn Rate Limit < 10 req/s.

Vòng lặp 5 giây (Monitoring):

Không gọi lẻ tẻ. Sử dụng fetch_tickers() (không truyền symbol) để lấy giá của toàn bộ sàn trong 1 request duy nhất.

3. Module: Quản lý Vị thế & Lệnh (Execution)
Thay vì logic rời rạc của Binance, Agent phải chuyển sang cơ chế Parent-Child của Bybit:

Setup: Phải gọi set_margin_mode('ISOLATED') và set_leverage() trước khi đặt lệnh.

Đặt lệnh (Order): Sử dụng create_order với tham số params:

Gắn trực tiếp takeProfit và stopLoss.

tpslMode='Full': Để đảm bảo khi chạm TP/SL là đóng sạch vị thế.

tpOrderType='Market' / slOrderType='Market': Ưu tiên thoát hàng nhanh.

Cơ chế Tự dọn rác: AI Agent không cần code xóa TP/SL khi lệnh Entry bị hủy. Chỉ cần ra lệnh cancel_order(entry_id), Bybit sẽ tự hủy các lệnh con đính kèm.

4. Module: Đồng bộ hóa (Synchronization)
Fetch Open Orders: Mỗi 5-10 giây, gọi fetch_open_orders() để lấy danh sách lệnh thực tế trên sàn.

Mapping: So sánh order_id từ sàn với Database cục bộ.

Nếu ID trên sàn biến mất mà bot chưa ghi nhận: Cập nhật trạng thái (Khớp/Hủy).

Nếu có biến động thị trường (tín hiệu đảo chiều): Dùng set_trading_stop để dời TP/SL cho các vị thế đang mở.

🛠️ YÊU CẦU CỤ THỂ CHO AI AGENT (PROMPT ĐIÈM CHỈ)
*"Hãy viết một Class BybitAdapter bằng Python/CCXT. Class này phải có các phương thức:

get_historical_candles(symbol, timeframe, count=2000): Sử dụng phân trang để lấy đủ 2000 nến.

place_smart_order(symbol, side, amount, price, tp, sl, leverage): Thực hiện chỉnh leverage, set isolated mode và đặt lệnh limit kèm TP/SL đính kèm (Attached).

sync_local_data(): Fetch tất cả open orders và trả về định dạng JSON chuẩn hóa để đối chiếu với database.

quick_price_check(): Lấy giá toàn sàn qua fetch_tickers để feed cho hệ thống phân tích mỗi 5s."