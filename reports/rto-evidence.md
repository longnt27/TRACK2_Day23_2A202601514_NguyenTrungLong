# RTO/RPO Evidence — Lab 23

Mọi số dưới đây được tính từ log của drill ngày 2026-08-25. Máy đo xác nhận `valid:true`, warnings rỗng tại `reports/measure-drill-2.json:2` và `reports/measure-drill-2.json:4`; RTO/RPO tại `reports/measure-drill-2.json:20` và `reports/measure-drill-2.json:23`.

## 1. Drill 1 — không có DR

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---:|---|---|
| t_outage | 2026-08-25T10:05:50Z | chaos kill Region A | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | +0.1s | `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:17` |
| Request thành công sau đó | Không có | 16 request còn lại đều fail | `reports/drill-1-nodr.jsonl:32` |
| RTO | NO_RECOVERY | máy đo không tìm thấy recovery | `reports/measure-drill-1.json:25` |

Baseline có 32 request; 16/16 request sau outage thất bại và Region A không tự phục hồi.

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---:|---|---|
| t_outage | 0.0s | `action:kill`, Region A | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | 0.1s | `ok:false` đầu tiên | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | 14.9s | Region A → `UNHEALTHY`, 3 fail liên tiếp | `reports/health-events.jsonl:2` |
| Snapshot restore xong | 15.0s | `2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region B ready | 21.2s | `4_wait_ready`, `ok:true` | `reports/failover-events.jsonl:4` |
| DNS cutover | 21.2s | `5_dns_cutover`, active Region B | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **22.3s** | request `ok:true` đầu tiên sau lỗi, served by B | `reports/drill-2-withdr.jsonl:36` |

| Chỉ số | Đo được | Mục tiêu | Verdict |
|---|---:|---:|---|
| RTO — Inference API | 22.3s | 300s | PASS, dư 277.7s |
| RPO — Vector DB | 4.01s / 2 docs | 300s | PASS, dư 295.99s |

RPO được tính bằng chênh lệch timestamp document mới nhất giữa primary và bản restore, đồng thời đếm document có ở primary nhưng không có trong snapshot. Evidence: `reports/failover-events.jsonl:2`.

## 3. Breakdown RTO

| Thành phần | Giây | Nguồn | Cách giảm |
|---|---:|---|---|
| Health-check detection floor | 15.0s | interval 5.0s × threshold 3 tại `reports/health-events.jsonl:2` | Chạy probe song song và hạ interval sau khi đánh giá tải/flapping. |
| Snapshot restore | 0.0s (0.003s thô) | restore 1787652449.733105 → scale 1787652449.733389 tại `reports/failover-events.jsonl:2` và `reports/failover-events.jsonl:3` | Snapshot incremental/gần target hơn; phần này hiện không phải bottleneck. |
| GPU pool warm-up | 6.18s | `waited_s` tại `reports/failover-events.jsonl:4` | Giữ warm capacity hoặc pre-warm pool dự phòng. |
| DNS/LB TTL cache | 1.1s | recovery 1787652456.981310 − cutover 1787652455.917001; `reports/drill-2-withdr.jsonl:36`, `reports/failover-events.jsonl:5` | TTL thấp hơn hoặc LB health routing chủ động. |
| **Tổng** | **22.3s** | 15.0 + 0.003 + 6.18 + 1.064 = 22.247s, làm tròn một chữ số | Khớp RTO máy đo 22.3s. |
