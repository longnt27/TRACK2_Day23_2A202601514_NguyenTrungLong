# Postmortem — DR Drill Lab 23

Ngày drill: 2026-08-25. Đây là phân tích blameless về khả năng phục hồi của hệ thống.

## 1. Timeline

| ISO time (UTC) | Sự kiện | Evidence |
|---|---|---|
| 2026-08-25T10:07:14 | Netblock Region A, bắt đầu outage | `chaos/chaos-events.jsonl:3` |
| 2026-08-25T10:07:14 | Request đầu tiên lỗi (+0.1s) | `reports/drill-2-withdr.jsonl:25` |
| 2026-08-25T10:07:21 | Operator xác nhận sau 3 probe; notification delay 6.7s | `reports/runbook-run.jsonl:2` |
| 2026-08-25T10:07:29 | Health checker alert sau 3 fail liên tiếp (+14.9s) | `reports/health-events.jsonl:2` |
| 2026-08-25T10:07:35 | Region B ready rồi DNS cutover | `reports/failover-events.jsonl:4`, `reports/failover-events.jsonl:5` |
| 2026-08-25T10:07:36 | Request đầu tiên OK từ B; resolved (+22.3s) | `reports/drill-2-withdr.jsonl:36` |

## 2. RTO/RPO so với mục tiêu và gap analysis

- RTO mục tiêu 300s; đo được 22.3s; positive gap (headroom) 277.7s.
- RPO mục tiêu 300s; đo được 4.01s và 2 documents lost; positive gap 295.99s.
- Bước tốn nhiều nhất là health-check detection floor 15.0s (67.3% RTO), vì ba probe cách nhau 5s là circuit breaker chống flapping. GPU warm-up đứng thứ hai với 6.18s (27.7%). Restore 0.003s và TTL quan sát 1.064s không phải bottleneck.

## 3. Root cause — 5 whys

1. User nhận 503 vì edge vẫn trỏ tới A trong lúc A bị netblock.
2. Edge chưa đổi target vì failover chỉ được phép cutover sau alert và sau khi B ready.
3. Alert cần ba lỗi liên tiếp để tránh flapping; mỗi probe có interval 5s và timeout hữu hạn.
4. B ban đầu không ready vì pool warm, vector DB rỗng và model weights chưa có; snapshot restore và GPU warm-up phải hoàn tất trước DNS.
5. Thiết kế active-passive tối ưu chi phí bằng cách không giữ B full/warm đầy đủ. Nếu snapshot/manifest không tồn tại, đây là điểm runbook sẽ thất bại ở `2_restore_snapshot`; failover hiện abort an toàn và không cutover.

Root cause hệ thống là Region A không phản hồi kết hợp với active-passive cold state. Guard readiness, snapshot replication và ordered cutover đã giới hạn ảnh hưởng còn 22.3s.

## 4. Action items

| # | Action item | Owner | Deadline | Tác động dự kiến |
|---|---|---|---|---|
| 1 | Chạy health probes A/B song song và thử interval 3s, threshold 3 qua game day chống flapping. | SRE | 2026-09-08 | Giảm detection floor 6s; RTO dự kiến ~16.3s. |
| 2 | Duy trì B ở pool `full` với canary readiness và kiểm tra model version mỗi replication. | ML Platform | 2026-09-15 | Giảm warm-up khoảng 6.18s; đổi lại tốn warm capacity. |
| 3 | Alert nếu snapshot age > 30s hoặc manifest/model version thiếu. | Data Platform | 2026-09-08 | Giữ RPO có biên ≤30s và tránh restore-time abort. |

## 5. Câu hỏi bắt buộc

1. `interval × threshold = 5s × 3 = 15s`, chiếm 67.3% của RTO 22.3s.
2. Nếu interval xuống 1s, detection floor lý thuyết giảm 12s và RTO gần 10.3s nếu phần khác giữ nguyên. Giá phải trả là số probe tăng 5 lần, nhiều false positive hơn khi có jitter/GC pause, và nguy cơ flapping; vẫn giữ threshold/circuit breaker và thử nghiệm trước.
3. Với outage 6 giờ và primary mất vĩnh viễn, `docs_lost = 2` nghĩa là hai document khách hàng ingest sau điểm snapshot không có ở B: tìm kiếm/inference không thấy dữ liệu đó và cần replay từ nguồn hoặc khách gửi lại. `4.01s` mô tả cửa sổ thời gian mất dữ liệu của drill, không phải cam kết rằng outage dài hơn sẽ chỉ mất hai document.
