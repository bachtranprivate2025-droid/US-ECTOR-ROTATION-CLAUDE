# US Sector Rotation (Claude)

Dashboard xoay vòng ngành Mỹ: 11 ETF sector chính + 48 ETF sub-section, có chế độ **Pre-market** và **Phiên chính**.

- Performance ngày / 3D / 1W / 1M, RS vs SPY, điểm RS đa khung, Sức mạnh /5, nhãn Rotation
- RS vs SPY 10 phiên, Relative RVOL, A/D proxy (price-volume)
- ETF Net Flow 10 phiên (USD thực) từ ETFIQ
- Bản đồ leader kiểu Finbox: ~10 top holdings/ETF ngành, RS Rating 1–99, tab RS Rating / Biến động giá, gắn nhãn BYBIT nếu có Stock Perp
- Ngày BCTC của cổ phiếu đại diện (TradingView), mã perpetual Bybit (Stock / ETF)
- Pre-market: Perf pre, RS pre cùng nến 5 phút với SPY, rotation tạm thời, điểm PRE trên chart

## Nguồn dữ liệu

| Dữ liệu | Nguồn |
|---|---|
| Giá ETF ngày + nến 5 phút pre-market | Yahoo Finance chart API |
| ETF Net Flow | [ETFIQ Open Data](https://etfiq.com/data/) (`/data/flows/funds/<ticker>.json`) |
| Ngày BCTC | TradingView scanner |
| Perpetual | Bybit v5 instruments-info (nếu bị chặn IP thì dùng snapshot trong `config/universe.json`) |

## Cách chạy

Trang tĩnh (`index.html`) đọc `data/rotation.json`. File dữ liệu do **GitHub Actions** tạo (`.github/workflows/update.yml`):

- mỗi 5 phút, thứ 2–6, 04:00 ET → 16:00 ET (lịch của GitHub có thể trễ vài phút)
- 1 lần sau đóng cửa và 1 lần thứ 7

Mỗi lần chạy: `python scripts/build_data.py` → build `_site` → deploy GitHub Pages. Nếu Yahoo lỗi, job dừng và bản cũ vẫn online.

### Bật GitHub Pages (một lần)

Settings → Pages → **Build and deployment → Source: GitHub Actions**. Sau đó vào tab Actions → *Update rotation data* → **Run workflow**.

Trang: `https://bachtranprivate2025-droid.github.io/US-ECTOR-ROTATION-CLAUDE/`

### Chạy thử offline

```bash
python scripts/build_data.py --mock --now 2026-11-16T07:50   # dữ liệu giả, giờ New York
python -m http.server 8000                                    # mở http://localhost:8000
```

## Tuỳ chỉnh

- Danh mục ETF, cổ phiếu đại diện, `leaders` (mã cho bản đồ leader), chủ đề catalyst, mã BCTC: `config/universe.json`
- Ngưỡng rotation, công thức điểm: đầu file `scripts/build_data.py`

Lưu ý: GitHub tự tắt lịch chạy của repo public nếu repo không có commit nào trong 60 ngày — khi đó vào Actions bấm *Enable workflow*.
