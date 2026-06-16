# Benchmark lựa chọn mô hình LLM cho Aurum

Tài liệu này ghi lại quá trình **đánh giá thực nghiệm** các mô hình LLM của GreenNode AI Platform cho tác vụ cốt lõi của Aurum: sinh **nhận định tài chính cá nhân hóa, 2–3 câu tiếng Việt** từ danh mục + tin tức + giá thị trường (RAG). Mục tiêu là chọn model **cân bằng chất lượng – độ trễ – độ ổn định** trong khuôn khổ cuộc thi (cộng đồng vote, không redeploy trong kỳ chấm).

> Số liệu đo ngày **16/06/2026**, region **HCM**, đo **đơn luồng tuần tự** qua endpoint OpenAI‑compatible `https://maas-llm-aiplatform-hcm.api.vngcloud.vn/v1`. Latency thực tế dưới tải đồng thời có thể cao hơn.

## 1. Phương pháp

- **Prompt:** dùng **đúng prompt production** (`AI_SYSTEM` + hàm `_ai_context()` trong `main.py`), không phải prompt rút gọn.
- **Dữ liệu vào:** một danh mục mẫu thực tế — SJC (lãi), nhẫn tròn trơn (lãi nhẹ), nữ trang (lỗ) — kèm 3 tin tức vàng thật.
- **Tham số:** giống app — `max_tokens` ≈ 220–320; với test chất lượng nới rộng `max_tokens` để tránh cắt cụt.
- **Đo:** mỗi cấu hình chạy 2–4 lần, lấy min/avg/max. Ghi nhận `latency`, `finish_reason`, `completion_tokens`, `reasoning_tokens`, độ dài output và kiểm tra output có bị lẫn "reasoning/thinking" hay rỗng không.
- **Tiêu chí đạt:** output **sạch** (không lộ chuỗi suy luận), **đủ 2–3 câu** (`finish_reason=stop`), **tiếng Việt chuẩn**, **đúng logic tài chính**, latency nằm trong `timeout` của app (18s).

## 2. Kết quả tổng hợp

| Model / cấu hình | Latency (đơn luồng) | reasoning_tokens | Output @ cấu hình app | Đánh giá |
|---|---|---|---|---|
| **Gemma 4 31B‑IT** (đang dùng) | **~1,5–2s** (0,53–2,66) | 0 | ✅ sạch, 2–3 câu, `stop` | Nhanh, ổn định, chất lượng tốt |
| **GPT‑5 · `reasoning_effort=minimal`** | **~3,1s** (2,76–3,62) | 0 | ✅ sạch, 2–3 câu, `stop` | **Chất lượng cao nhất ở tốc độ ~Gemma** |
| GPT‑5 · `low` | ~9s | ~512 | ✅ sạch | Chậm hơn, chất lượng tương đương minimal |
| GPT‑5 · `medium` (mặc định) | ~18–23s | ~1.300–1.600 | ✅ sạch | Vượt `timeout=18s` → loại |
| Gemini 3.1 Pro Preview | ~16–18s | ~1.550–1.960 | ⚠️ tại `max_tokens=220` chỉ ra **~13 ký tự** (`finish=length`) | "thinking model", **không chỉnh được effort** → loại |

**Ghi chú truy cập:** GPT‑5 ban đầu trả `404 model not found` (account chưa enable). Sau khi **enable model** và **bật type Chat** trên console, GPT‑5 gọi được qua `chat.completions` như app đang dùng (không cần đổi transport).

## 3. Output mẫu (thực tế, cùng một input)

**Gemma 4 31B‑IT** (~2s):
> Chào Na, danh mục tổng của bạn đang tăng trưởng ổn định (+2,07%) trong bối cảnh giá vàng thế giới và trong nước hồi phục ấn tượng. Với vị thế nữ trang đang tạm lỗ do chênh lệch phí, bạn nên kiên nhẫn nắm giữ vì vàng là tài sản tích sản dài hạn. Ưu tiên theo dõi nhịp tăng hiện tại để tối ưu hóa lợi nhuận cho các lệnh SJC và nhẫn.

**GPT‑5 · `reasoning_effort=minimal`** (~3s):
> Chào Na, danh mục tổng của bạn đang tăng trưởng tốt (+2,07%). Thị trường phục hồi khi vàng thế giới tăng 3 phiên và giá trong nước nhích thêm, nhưng chênh lệch mua–bán vẫn cao; lệnh nữ trang tạm lỗ chủ yếu do spread và phí gia công. Gợi ý: tiếp tục giữ vị thế hiện tại, theo dõi thêm; nếu tích sản, ưu tiên SJC/nhẫn để tối ưu chi phí.

**Gemini 3.1 Pro Preview** (~17s, cần nới `max_tokens` mới đủ):
> Chào Na, danh mục tổng của bạn đang tăng trưởng tốt (+2,07%) nhờ nhịp hồi phục ấn tượng của giá vàng trong nước và thế giới. Riêng phần vàng nữ trang đang tạm âm do đặc thù chênh lệch mua-bán và phí gia công, Na nên tiếp tục nắm giữ dài hạn thay vì bán ra lúc này. Nếu có kế hoạch giải ngân thêm, hãy ưu tiên tích sản vàng miếng SJC hoặc nhẫn trơn để tối ưu hiệu suất sinh lời.

→ Cả ba đều cho output **sạch, đúng logic tài chính** (giải thích nữ trang lỗ do spread + phí gia công, khuyến nghị giữ + ưu tiên DCA SJC/nhẫn). GPT‑5 nhỉnh hơn về độ tinh và sắc thái khuyến nghị.

## 4. Rủi ro & ràng buộc

- **Độ trễ vs `timeout`:** GPT‑5 (mặc định) và Gemini ~17–23s **vượt** `timeout=18s` của client → sẽ rơi về fallback. Chỉ **GPT‑5 `minimal` (~3s)** và **Gemma (~2s)** nằm trong ngưỡng an toàn.
- **Tải đồng thời:** benchmark đo đơn luồng. Khi nhiều người vote cùng lúc trên gateway dùng chung, latency/throughput có thể giảm — đã có `rule_insight` (fallback không cần LLM) đảm bảo app không vỡ.
- **Không redeploy trong kỳ vote:** model phải được chốt và kiểm thử kỹ **trước** giai đoạn chấm.
- **`reasoning_effort`** chỉ áp dụng cho model suy luận (GPT‑5); Gemma bỏ qua tham số này, Gemini không cho điều chỉnh.

## 5. Load test đồng thời (mô phỏng vote)

Đo đơn luồng đẹp chưa đủ — phép thử thật là **nhiều người vote cùng lúc** trên gateway dùng chung. Mỗi "wave" dùng `threading.Barrier` để C request bắn gần như đồng thời; prompt vary mỗi request để tránh cache.

| Cấu hình | Concurrency | OK | Lỗi | p95 latency | Output sạch |
|---|---|---|---|---|---|
| **Gemma 4 31B** | 20 (×2 vòng) | **40/40** | 0 | 3,38s | 40/40 |
| GPT‑5 `minimal` | 10 (×2 vòng) | 10/20 | **10× HTTP 429** | 3,78s | 10/10 |
| GPT‑5 `minimal` | 20 (×2 vòng) | **0/40** | **40× HTTP 429** | — | — |
| GPT‑5 `minimal` | 40 (×1 vòng) | **0/40** | **40× HTTP 429** | — | — |

→ **GPT‑5 bị rate‑limit rất gắt** (`AI rate limit exceeded`): latency đơn luồng tốt (~3s) nhưng **trần throughput thấp** — fail ngay ở concurrency 10, **chết hẳn ở 20+**. Đây đúng là kịch bản ~600 người vote đồng thời. Ngược lại **Gemma xử lý concurrency 20 mượt** (40/40, p95 3,4s, không lỗi).

## 6. Kết luận & quyết định

**Production chọn `google/gemma-4-31b-it`.** Căn cứ:
- Nhanh (~2s), output sạch, đã chạy ổn định nhiều ngày trong app.
- **Trụ tốt dưới tải đồng thời** (40/40 @ concurrency 20) — yếu tố quyết định cho kỳ vote.
- GPT‑5 `minimal` chất lượng cao + nhanh khi đơn luồng, **nhưng rate‑limit khiến nó sập dưới tải** → không phù hợp khi đông người + **không sửa được trong kỳ vote**.
- GPT‑5 `medium`/`low` và Gemini 3.1 Pro: quá chậm (vượt `timeout=18s`).
- `rule_insight` (fallback không cần LLM) đảm bảo app không vỡ ở các đỉnh tải.

> Quyết định dựa trên **đo thực nghiệm**, không cảm tính. Nếu sau này có hạn mức (rate limit) cao hơn cho GPT‑5, có thể đánh giá lại — code đã tách model qua biến môi trường để đổi không cần sửa logic.

## 7. Tái lập

```python
import os, time
from dotenv import load_dotenv; load_dotenv()
from openai import OpenAI
from main import AI_SYSTEM, _ai_context  # prompt production

c = OpenAI(api_key=os.environ["LLM_API_KEY"], base_url=os.environ["LLM_BASE_URL"],
           timeout=120, max_retries=0)
summary = {
    "totals": {"value": 905.0, "pnl": 18.4, "roi": 2.07, "avg_buy_price": 143.5, "qty": 6.0, "day_change": 1.2},
    "rows": [{"label": "Vàng miếng SJC", "qty": 3, "roi": 5.1},
             {"label": "Nhẫn tròn trơn", "qty": 2, "roi": 1.2},
             {"label": "Vàng nữ trang", "qty": 1, "roi": -6.8}],
    "prices": {"sjc": {"buy": 149.0, "sell": 151.5}, "nhan": {"buy": 146.0, "sell": 148.0}},
}
news = ["VnExpress: Giá vàng thế giới tăng 3 phiên liên tiếp",
        "Báo VietNamNet: SJC đắt thêm 1,5 triệu/lượng"]
usr = _ai_context(summary, "Na", news) + "\n\nHãy xuất câu nhận định chuẩn UX theo MẪU ĐẦU RA."
msgs = [{"role": "system", "content": AI_SYSTEM}, {"role": "user", "content": usr}]

for model, extra in [("google/gemma-4-31b-it", {}),
                     ("openai/gpt-5", {"reasoning_effort": "minimal"}),
                     ("gemini/gemini-3.1-pro-preview", {})]:
    t = time.time()
    r = c.chat.completions.create(model=model, max_completion_tokens=600, messages=msgs, **extra)
    print(model, round(time.time() - t, 2), "s →", (r.choices[0].message.content or "").strip()[:200])
```

> Liệt kê model đang bật: `bash .claude/skills/agentbase/scripts/aip.sh models list --status ENABLED`.
> Số liệu sẽ thay đổi theo thời điểm và tải hệ thống — chạy lại để cập nhật.
