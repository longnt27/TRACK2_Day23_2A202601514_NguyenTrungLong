"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Append one timestamped runbook step."""
    LOG.parent.mkdir(parents=True, exist_ok=True)
    record = {"ts": time.time(),
              "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
              "step": n, "name": name, **kw}
    with LOG.open("a") as log:
        log.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps(record, ensure_ascii=False), flush=True)
    return record


def confirm(auto: bool, msg: str) -> bool:
    if auto:
        return True
    return input(f"{msg} [y/N] ").strip().lower() in {"y", "yes"}


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Execute the semi-automated seven-step regional outage runbook."""
    if primary == target or primary not in URL or target not in URL:
        raise ValueError("primary and target must be distinct known regions")
    started = time.time()

    probes = []
    for attempt in range(3):
        status = {}
        for region in (primary, target):
            try:
                response = httpx.get(f"{URL[region]}/readyz", timeout=2.0)
                status[region] = {"ready": response.status_code == 200,
                                  "status_code": response.status_code}
            except Exception as exc:
                status[region] = {"ready": False, "error": type(exc).__name__}
        probes.append(status)
        if attempt < 2:
            time.sleep(0.25)
    outage_confirmed = all(not probe[primary]["ready"] for probe in probes)
    target_available = any(probe[target]["ready"] for probe in probes)
    step(1, "xac_nhan_outage", primary=primary, target=target,
         outage_confirmed=outage_confirmed, target_alive_or_ready=target_available,
         probes=probes)
    if not outage_confirmed:
        return {"ok": False, "failed_step": 1, "reason": "primary outage not confirmed"}

    if not confirm(auto, f"Region {primary} failed 3 probes. Fail over to {target}?"):
        step(2, "thong_bao_incident", confirmed=False, aborted=True)
        return {"ok": False, "failed_step": 2, "reason": "operator declined"}

    chaos_file = pathlib.Path("chaos/chaos-events.jsonl")
    outage_ts = None
    if chaos_file.exists():
        for line in chaos_file.read_text().splitlines():
            event = json.loads(line)
            if event.get("action") == "kill" and event.get("region") == primary:
                outage_ts = event.get("ts")
    incident = step(2, "thong_bao_incident", confirmed=True, outage_ts=outage_ts,
                    notification_delay_s=None if outage_ts is None else round(time.time() - outage_ts, 2))

    # The health checker is the alerting authority for this drill. If it is running,
    # let its debounced transition land before cutover so the timeline is causal.
    health_file = pathlib.Path("reports/health-events.jsonl")
    detection_deadline = time.monotonic() + 20.0
    while health_file.exists() and time.monotonic() < detection_deadline:
        detected = any(
            event.get("region") == primary and event.get("to") == "UNHEALTHY"
            and event.get("ts", 0) >= (outage_ts or started)
            for event in (json.loads(line) for line in health_file.read_text().splitlines() if line.strip())
        )
        if detected:
            break
        time.sleep(0.25)

    result = fo.failover(target, backend, wait=60.0)  # exactly one invocation
    step(3, "scale_gpu_pool", failover_ok=result.get("ok"),
         failed_step=result.get("failed_step"), waited_s=result.get("waited_s"))
    if not result.get("ok"):
        step(7, "post_incident", ok=False, elapsed_s=round(time.time() - started, 2),
             failed_step=result.get("failed_step"))
        return result

    state = result.get("state", {})
    state_ok = bool(state.get("weights")) and state.get("count", 0) > 0
    step(4, "verify_state_replica", ok=state_ok, vector_count=state.get("count"),
         weights=state.get("weights"), embed_model_version=result.get("embed_model_version"),
         rpo_seconds=result.get("rpo_seconds"), docs_lost=result.get("docs_lost"))
    step(5, "dns_cutover", ok=result.get("active_region") == target,
         active_region=result.get("active_region"))

    latencies = []
    errors = 0
    for _ in range(10):
        t0 = time.monotonic()
        try:
            response = httpx.get(f"{URL[target]}/v1/infer", timeout=3.0)
            if response.status_code != 200 or response.json().get("region") != target:
                errors += 1
        except Exception:
            errors += 1
        latencies.append((time.monotonic() - t0) * 1000)
    ordered = sorted(latencies)
    p95 = ordered[max(0, min(len(ordered) - 1, int(0.95 * len(ordered)) - 1))]
    error_rate = errors / len(latencies)
    golden_ok = error_rate == 0 and p95 < 1000
    step(6, "verify_golden_signals", ok=golden_ok, requests=10,
         p95_latency_ms=round(p95, 2), error_rate=error_rate)

    elapsed = round(time.time() - started, 2)
    command = ("python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
               "--target-rto 300")
    step(7, "post_incident", ok=state_ok and golden_ok, elapsed_s=elapsed,
         measure_rto_command=command, incident_ts=incident["ts"])
    return {**result, "ok": state_ok and golden_ok, "golden_signals": {
        "p95_latency_ms": round(p95, 2), "error_rate": error_rate}, "elapsed_s": elapsed}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
