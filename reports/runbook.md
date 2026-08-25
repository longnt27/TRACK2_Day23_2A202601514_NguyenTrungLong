# Runbook — Primary Region Down

Purpose: recover inference traffic from Region A to Region B without cutting over before the target is ready. This is the operator procedure for a real incident; the graded drill uses the same flow through `dr/runbook.py --auto`.

| # | Step | Copy-paste command | Completion signal | Owner |
|---|---|---|---|---|
| 1 | Confirm outage | `for i in 1 2 3; do python3 chaos/kill_region.py status; sleep 2; done` | Region A is not ready/alive on all three checks while Region B remains alive. Do **not** continue if Region B is also down. | On-call SRE |
| 2 | Open incident + start RTO clock | `date -u +"%Y-%m-%dT%H:%M:%SZ"` | Incident timestamp is recorded; `reports/runbook-run.jsonl` will contain step `2 thong_bao_incident` when automation starts. | Incident Commander |
| 3 | Restore state to Region B | `python3 dr/failover.py --target b --backend fs` | `reports/failover-events.jsonl` contains `2_restore_snapshot` with non-null `rpo_seconds`, `docs_lost`, and `embed_model_version`. If restore fails, **abort cutover**. | On-call SRE |
| 4 | Wait for Region B readiness / GPU warm-up | `until curl -sf http://127.0.0.1:8002/readyz; do sleep 1; done` | `/readyz` returns HTTP 200 and the failover log contains `4_wait_ready`. | On-call SRE |
| 5 | Verify DNS/LB cutover | `curl -sf http://127.0.0.1:8080/edge/state` | Response shows `"active_region":"b"`; failover log contains `5_dns_cutover`. Never edit `edge/active_region` manually during the graded drill. | On-call SRE |
| 6 | Verify golden signals | `python3 - <<'PY'
import statistics, time, httpx
lat=[]; errors=0
with httpx.Client(timeout=3) as c:
    for i in range(10):
        t=time.perf_counter()
        try:
            r=c.get('http://127.0.0.1:8002/v1/infer', params={'q':f'golden-{i}'})
            errors += int(r.status_code != 200)
        except Exception:
            errors += 1
        lat.append((time.perf_counter()-t)*1000)
lat.sort(); p95=lat[max(0, int(len(lat)*0.95)-1)]
print({'p95_ms':round(p95,1),'error_rate':errors/10})
PY` | 10 real requests complete, error rate = `0.0`, and p95 is recorded in `reports/runbook-run.jsonl` by the automated runbook. | On-call SRE |
| 7 | Measure RTO/RPO + start postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | Output has `"valid": true`, empty `warnings`, `"rto_verdict":"PASS"`, non-null `rpo_at_restore_s`, and non-null `docs_lost`. | Incident Commander |

## Preferred graded-drill command

After the health checker has actually emitted Region A `UNHEALTHY`, execute:

```bash
python3 dr/runbook.py --primary a --target b --backend fs --auto
```

The runbook calls `failover.failover(...)` exactly once, verifies restored state, checks the cutover result, sends 10 real golden-signal requests, and writes the incident timeline to `reports/runbook-run.jsonl`.

## Abort / rollback conditions

**Abort before cutover** if Region B is not alive, the snapshot restore fails, model weights/version are missing, `/readyz` does not become 200 within the failover timeout, or the restored state fails validation. In all of these cases, do not write or change the active-region pointer.

**Fail back to Region A only after** Region A passes `/readyz` for at least 3 consecutive checks, its state/model version has been validated, replication is current enough to meet the RPO objective, and the Incident Commander explicitly approves the change. The Incident Commander owns the decision; the on-call SRE executes it during a controlled window with `python3 dr/failover.py --target a --backend fs`, then repeats the golden-signal checks. Do not enable automatic bidirectional failover: without a circuit breaker it can flap traffic between regions.
