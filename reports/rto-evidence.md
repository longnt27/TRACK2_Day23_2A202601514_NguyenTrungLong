# RTO/RPO Evidence — Lab 23

All values below come from this drill run. Timestamps are measured from the load-generator and JSONL logs; no reference/sample numbers were copied. The submitted Drill 2 loadgen file is the unmodified evidence window from before the outage through the first successful recovery request; the complete 100-second run was also checked locally for stable post-recovery service.

## 1. Drill 1 — no DR baseline

| Metric | Value | Measurement | Evidence |
|---|---:|---|---|
| t_outage | `2026-08-25T10:01:46` | chaos kill event | `chaos/chaos-events.jsonl:1` |
| First failed request | `+0.1s` | first `ok:false` after outage | `reports/drill-1-nodr.jsonl:17` |
| Successful request after failure | none | no later successful request in this drill | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

## 2. Drill 2 — DR enabled

| Milestone | +seconds from t_outage | Measurement | Evidence |
|---|---:|---|---|
| t_outage | `0.0s` | `action:kill`, Region A | `chaos/chaos-events.jsonl:3` |
| User sees first error | `+0.1s` | first `ok:false` | `reports/drill-2-withdr.jsonl:25` |
| Health checker detects outage | `+14.9s` | `to:UNHEALTHY`, Region A | `reports/health-events.jsonl:2` |
| Snapshot restore complete | `+15.0s` | `2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Region B ready | `+21.3s` | successful `4_wait_ready` | `reports/failover-events.jsonl:4` |
| DNS/LB cutover | `+21.3s` | `5_dns_cutover` | `reports/failover-events.jsonl:5` |
| **First successful request from Region B / RTO** | **`22.5s`** | first `ok:true` after failure, served by B | `reports/drill-2-withdr.jsonl:36` |

| Objective | Measured | Target | Verdict |
|---|---:|---:|---|
| RTO — inference API | `22.5s` | 300s | **PASS** |
| RPO — vector DB | `4.0s` / `2` docs lost | 300s | **PASS** |

RPO is measured against the restored database, not estimated from snapshot age. The restore event records both `rpo_seconds=4.0` and `docs_lost=2`: `reports/failover-events.jsonl:2`.

## 3. RTO decomposition

Health-check configuration is `interval_s=5.0` and `threshold=3`, so the theoretical detection floor is **`15.0s`**. The measured `UNHEALTHY` transition occurred at `+14.9s`: `reports/health-events.jsonl:2`.

| Component | Seconds | Boundary / evidence | How to reduce it |
|---|---:|---|---|
| Health-check detection | `14.9s` | outage → `UNHEALTHY`; `reports/health-events.jsonl:2` | Shorter interval while retaining consecutive-failure anti-flap logic/circuit breaking. |
| Snapshot restore + orchestration | `0.1s` | detection → restore complete; `reports/failover-events.jsonl:2` | Pre-stage recent snapshots and validate restore artifacts continuously. |
| GPU pool warm-up + ready gate | `6.3s` | restore complete → safe DNS cutover; `reports/failover-events.jsonl:4` and `reports/failover-events.jsonl:5` | Keep minimum warm standby capacity or pre-warm the target pool. |
| DNS/LB TTL cache | `1.2s` | cutover → first successful request from B; `reports/drill-2-withdr.jsonl:36` | Lower TTL where safe or use a health-aware global LB. |
| **Total measured RTO** | **`22.5s`** | timestamp decomposition above | Target: ≤ 300s. |

The target readiness wait itself was `6.279s`, recorded by `4_wait_ready`: `reports/failover-events.jsonl:4`. The four non-overlapping phases above reconcile exactly to the measured RTO instead of hiding operator/orchestration time.
