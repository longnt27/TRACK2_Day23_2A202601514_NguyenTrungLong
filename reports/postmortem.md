# Postmortem — DR Drill Lab 23

This is a blameless game-day postmortem. The injected outage is the trigger; the useful question is which system properties determine customer impact, RTO, and RPO.

## 1. Timeline

| ISO time | Event | Evidence |
|---|---|---|
| 2026-08-25T10:02:53 | Region A outage begins | `chaos/chaos-events.jsonl:3` |
| request timestamp | First user-visible failure at `+0.1s` | `reports/drill-2-withdr.jsonl:25` |
| 2026-08-25T10:03:08 | Health checker marks Region A `UNHEALTHY` at `+14.9s` | `reports/health-events.jsonl:2` |
| 2026-08-25T10:03:08 | Runbook confirms the incident and proceeds | `reports/runbook-run.jsonl:2` |
| request timestamp | Resolved: first successful request served by Region B at `22.5s` RTO | `reports/drill-2-withdr.jsonl:36` |

## 2. RTO/RPO vs objective — gap analysis

- **RTO target:** 300s. **Measured:** `22.5s`. **Headroom/gap to target:** `277.5s`.
- **RPO target:** 300s. **Measured:** `4.0s` / `2` documents lost. **Headroom/gap to target:** `296.0s`.
- **Largest measured RTO component:** health-check detection at `14.9s`.
- The configured health-check floor is `5.0s × 3 = 15.0s`; therefore detection dominates the recovery budget. Evidence: `reports/health-events.jsonl:2`.
- The safe-order invariant held: restore → scale → wait-ready → DNS cutover. Evidence: `reports/failover-events.jsonl:2`, `reports/failover-events.jsonl:3`, `reports/failover-events.jsonl:4`, `reports/failover-events.jsonl:5`.

## 3. Root cause — 5 whys

1. **Why did users receive errors?** Region A stopped responding while the edge still routed requests to A.
2. **Why did traffic remain on A after the outage?** The edge pointer/TTL does not move merely because the process is unhealthy; a health-driven failover decision is required.
3. **Why was recovery not immediate after detection?** Region B had to restore vector state/model weights and transition its pool from warm to full before it was safe to receive traffic.
4. **Why is `/healthz` insufficient?** A live process is not a ready AI region. Readiness also depends on vector count, compatible model weights/version, pool state, and warm-up completion.
5. **Why can permanent primary loss still lose data?** Failover can only restore data captured by the latest usable replica. Replication cadence and successful restore validation therefore bound customer-visible RPO.

The baseline demonstrates the resilience gap directly: without DR, the same outage produced `NO_RECOVERY` (`reports/measure-drill-1.json`). With health detection, replication, readiness gating, and cutover automation, the drill recovered within the objective (`reports/measure-drill-2.json`).

## 4. Action items

| # | Action item | Owner | Deadline | Expected effect |
|---|---|---|---|---|
| 1 | Alert on replication age and run scheduled restore-validation drills so stale/corrupt replicas are found before an incident. | Platform/SRE | 2026-08-27 | Protects RPO and prevents restore-step failure. |
| 2 | Test `interval=1s, threshold=3` behind a circuit breaker and compare false-positive rate and RTO before adopting it. | On-call/SRE | 2026-08-28 | Theoretical detection floor drops from `15.0s` to `3.0s`, up to ~`12.0s` RTO reduction if other phases stay unchanged. |
| 3 | Maintain a minimum warm standby pool and continuously verify `/readyz` dependencies without routing user traffic to the standby. | AI Platform | 2026-08-29 | Reduces the measured `6.3s` warm-up/ready phase. |

## 5. Three required questions

1. **What is `interval × threshold`, and how much of RTO is it?** `5.0s × 3 = 15.0s`, which is **66.7%** of the measured `22.5s` RTO. The observed transition was at `+14.9s`; sub-second scheduling alignment explains the difference from the theoretical floor.
2. **What if interval becomes 1s?** With threshold 3, the theoretical floor becomes `3.0s`; best-case RTO would drop by about `12.0s` to roughly `10.5s` if all other phases remain unchanged. The trade-off is 5× more probing and a shorter observation window, increasing sensitivity to transient latency unless anti-flap logic/circuit breaking remains in place.
3. **What does `2` docs lost mean if Region A is permanently gone?** Those are documents written after the latest restorable replica point. They would be absent from Region B after recovery, so retrieval can omit recent facts or answer from stale state. The recovery process therefore needs reconciliation/replay from an authoritative source where available; RPO is a customer-data property, not merely a backup timestamp.

Golden-signal verification sent 10 real requests to Region B with `error_rate=0.0` and `p95_latency_ms=43.1`: `reports/runbook-run.jsonl:6`.
