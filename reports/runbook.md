# Runbook — Region chính down

Phạm vi: bare mode local, primary A, target B, backend snapshot `fs`. Chạy từ repository root. Mục tiêu RTO/RPO: 300s. Incident Commander (IC) giữ quyền cutover và rollback; on-call không tự sửa `edge/active_region`.

| # | Bước | Lệnh copy-paste | Biết là xong khi | Owner |
|---|---|---|---|---|
| 1 | Xác nhận outage | `for i in 1 2 3; do python3 chaos/kill_region.py status; sleep 2; done` | A không ready cả 3 lần; B còn `alive:true`. Nếu B không alive, dừng: double outage. | Serving on-call |
| 2 | Mở incident và bấm giờ | `date -u +%FT%TZ; python3 dr/runbook.py --primary a --target b --backend fs` | On-call nhập `y`; `reports/runbook-run.jsonl` có `thong_bao_incident` và `confirmed:true`. | IC |
| 3 | Restore state target | `tail -n 5 reports/failover-events.jsonl` | Có `2_restore_snapshot`, `embed_model_version`, `rpo_seconds` và `docs_lost` khác null. Lệnh ở bước 2 thực hiện restore đúng một lần. | Data on-call |
| 4 | Scale và chờ GPU pool | `curl -fsS http://127.0.0.1:8002/readyz` | HTTP 200, `ready:true`, pool `full`, vectors > 0, không còn warm-up. | ML platform on-call |
| 5 | Xác nhận DNS/LB cutover | `curl -fsS http://127.0.0.1:8080/edge/state` | `active_region` là `b`; failover log có `5_dns_cutover` sau `4_wait_ready`. | IC |
| 6 | Verify golden signals | `tail -n 2 reports/runbook-run.jsonl` | Bước 6 ghi 10 request thật, `error_rate:0.0`, `p95_latency_ms < 1000`, `ok:true`. | SRE on-call |
| 7 | Đo RTO/RPO và mở postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `valid:true`, warnings rỗng, `recovered_by_region:b`, RTO/RPO khác null và verdict `PASS`. | IC |

## Abort và rollback

- Abort trước cutover nếu snapshot thiếu, model version không khớp, B không ready trong 60s, hoặc B có vectors = 0. `dr/failover.py` sẽ không ghi bước 5; giữ pointer ở A và IC chuyển sang phục hồi primary.
- Sau cutover, rollback về A khi error rate của B > 1%, p95 > 1000ms trong 3 cửa sổ liên tiếp, lỗi dữ liệu/model-version, hoặc A đã ready ổn định 3 probe và B suy giảm. Chỉ IC được phê duyệt.
- Lệnh rollback sau phê duyệt: `python3 dr/runbook.py --primary b --target a --backend fs` (không sửa pointer bằng tay). Xác nhận A ready trước cutover và chạy lại 10 golden requests. Nếu cả hai region không ready, không rollback qua lại; tuyên bố double outage và phục hồi state trước.

## Liên lạc

IC ghi timestamp, quyết định và owner vào incident channel; Data on-call xác nhận snapshot/RPO; ML Platform xác nhận model/pool; SRE theo dõi latency và error rate. Sau resolution, lưu nguyên các JSONL và hoàn tất postmortem trong 24 giờ.
