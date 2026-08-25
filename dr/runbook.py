"""BƯỚC 3c — Tự động hoá runbook region chính down.

Runbook bán tự động: mặc định cần người vận hành xác nhận; ``--auto`` chỉ dành cho
CI / graded drill. Failover orchestration chỉ được gọi đúng một lần.
"""
import argparse
import json
import math
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402
from dr import health_checker as hc  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
HEALTH_LOG = pathlib.Path("reports/health-events.jsonl")
CHAOS_LOG = pathlib.Path("chaos/chaos-events.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    """Ghi một bước runbook vào JSONL và trả lại record vừa ghi."""
    ts = time.time()
    rec = {
        "ts": ts,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)),
        "step": n,
        "name": name,
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("RUNBOOK", json.dumps(rec))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """Trong auto mode luôn đồng ý; bình thường dùng default-safe ``y/N``."""
    if auto:
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        return False


def _jsonl(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _latest_kill(primary: str) -> dict | None:
    kills = [
        e for e in _jsonl(CHAOS_LOG)
        if e.get("action") == "kill" and e.get("region") == primary
    ]
    return kills[-1] if kills else None


def _health_detection(primary: str, after_ts: float | None) -> dict | None:
    events = [
        e for e in _jsonl(HEALTH_LOG)
        if e.get("event") == "state_change"
        and e.get("region") == primary
        and e.get("to") == "UNHEALTHY"
        and (after_ts is None or e.get("ts", 0) >= after_ts)
    ]
    return events[0] if events else None


def _target_alive(region: str, timeout: float = 1.0) -> bool:
    try:
        return httpx.get(f"{URL[region]}/healthz", timeout=timeout).status_code == 200
    except Exception:
        return False


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return round(ordered[idx], 2)


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """Thực thi 7 bước runbook và trả summary JSON-serializable."""
    if primary not in URL or target not in URL or primary == target:
        raise ValueError("primary and target must be different regions from {a,b}")

    started = time.time()
    outage = _latest_kill(primary)
    outage_ts = outage.get("ts") if outage else None
    outage_iso = outage.get("iso") if outage else None

    # 1. Không tin một probe duy nhất: primary phải fail 3 lần liên tiếp. Đồng thời
    # kiểm tra process target còn sống, dù target chưa ready do chưa restore state.
    primary_checks = []
    target_alive_checks = []
    for i in range(3):
        ready, reason = hc.probe(primary, timeout=1.5)
        primary_checks.append({"ready": ready, "reason": reason})
        target_alive_checks.append(_target_alive(target, timeout=1.0))
        if i < 2:
            time.sleep(0.5)

    primary_failed_three = all(not x["ready"] for x in primary_checks)
    target_alive = any(target_alive_checks)

    # Graded drill chạy health_checker.py song song. Chờ event UNHEALTHY thật sự để
    # không tạo cutover trước t_detect (measure_rto coi đó là warning/hard fail).
    detection = _health_detection(primary, outage_ts)
    detection_wait_started = time.monotonic()
    while primary_failed_three and detection is None and time.monotonic() - detection_wait_started < 20:
        time.sleep(0.2)
        detection = _health_detection(primary, outage_ts)

    step(
        1,
        "xac_nhan_outage",
        primary=primary,
        target=target,
        primary_failed_three=primary_failed_three,
        primary_checks=primary_checks,
        target_alive=target_alive,
        health_detection_ts=detection.get("ts") if detection else None,
        health_detection_iso=detection.get("iso") if detection else None,
    )

    if not primary_failed_three:
        return {"ok": False, "failed_step": 1, "reason": "primary outage not confirmed"}
    if not target_alive:
        return {"ok": False, "failed_step": 1, "reason": "target process is not alive"}

    # 2. Operator acknowledgement must be after the outage timestamp.
    incident = step(
        2,
        "thong_bao_incident",
        primary=primary,
        target=target,
        outage_ts=outage_ts,
        outage_iso=outage_iso,
        notification_delay_s=None if outage_ts is None else round(time.time() - outage_ts, 3),
        message=f"Region {primary} outage confirmed; proposed failover to region {target}",
    )

    if not confirm(auto, f"Fail over region {primary} -> {target}?"):
        return {"ok": False, "aborted": True, "failed_step": 2, "reason": "operator declined"}

    # 3. Gọi orchestration đúng MỘT lần. Hàm này tự restore/scale/wait/cutover.
    result = fo.failover(target=target, backend=backend, wait=60)
    step(
        3,
        "scale_gpu_pool",
        primary=primary,
        target=target,
        failover_ok=bool(result.get("ok")),
        failed_step=result.get("failed_step"),
        rpo_seconds=(result.get("restore") or {}).get("rpo_seconds", result.get("rpo_seconds")),
        docs_lost=(result.get("restore") or {}).get("docs_lost", result.get("docs_lost")),
    )
    if not result.get("ok"):
        return {"ok": False, "failed_step": 3, "failover": result}

    # 4. Chỉ đọc kết quả của lần failover trên; không gọi lại.
    state = result.get("target_state") or {}
    vector_count = state.get("count")
    if vector_count is None and isinstance(state.get("vectors"), dict):
        vector_count = state["vectors"].get("count")
    weights = state.get("weights")
    if weights is None:
        weights = pathlib.Path(f"state/region-{target}/weights/model.bin").exists()
    step(
        4,
        "verify_state_replica",
        target=target,
        vector_count=vector_count,
        weights=bool(weights),
        pool_state=state.get("pool_state"),
        embed_model_version=(result.get("restore") or {}).get("embed_model_version"),
    )

    # 5. Cũng chỉ đọc lại kết quả cutover đã thực hiện trong fo.failover().
    cutover = result.get("cutover") or {}
    step(
        5,
        "dns_cutover",
        target=target,
        ok=bool(cutover.get("ok")),
        previous_region=cutover.get("from"),
        active_region=cutover.get("to"),
    )

    # 6. Golden signals: 10 request thật vào target, đo p95 và error rate.
    latencies_ms = []
    errors = 0
    with httpx.Client(timeout=3.0) as client:
        for i in range(10):
            t0 = time.perf_counter()
            try:
                response = client.get(f"{URL[target]}/v1/infer", params={"q": f"golden-{i}"})
                if response.status_code != 200:
                    errors += 1
            except Exception:
                errors += 1
            latencies_ms.append((time.perf_counter() - t0) * 1000)

    p95_ms = _p95(latencies_ms)
    error_rate = round(errors / 10, 3)
    step(
        6,
        "verify_golden_signals",
        target=target,
        requests=10,
        p95_latency_ms=p95_ms,
        error_rate=error_rate,
        passed=errors == 0,
    )

    # 7. Tóm tắt incident và ghi nguyên lệnh đo để người khác chạy lại được.
    ended = time.time()
    measure_command = (
        "python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
        "--target-rto 300"
    )
    step(
        7,
        "post_incident",
        elapsed_s=round(ended - (outage_ts or started), 3),
        incident_opened_ts=incident["ts"],
        measure_command=measure_command,
        golden_p95_ms=p95_ms,
        golden_error_rate=error_rate,
    )

    return {
        "ok": errors == 0,
        "primary": primary,
        "target": target,
        "outage_ts": outage_ts,
        "health_detection_ts": detection.get("ts") if detection else None,
        "failover": result,
        "golden_signals": {"requests": 10, "p95_latency_ms": p95_ms, "error_rate": error_rate},
        "elapsed_s": round(ended - started, 3),
        "measure_command": measure_command,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))
