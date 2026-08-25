"""BƯỚC 3a — Health checker cho 2 region.

Poll /readyz của cả hai region, chống flapping bằng ngưỡng lỗi liên tiếp, và chỉ
log khi trạng thái thật sự thay đổi. Các field interval_s/threshold được giữ trong
log để RTO detection floor có thể được đo lại từ evidence.
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Probe readiness của một region và trả về ``(ready, reason)``.

    Readiness (không phải liveness) mới quyết định region có thể nhận inference
    traffic hay không. Timeout luôn được truyền vào để mô phỏng netblock không làm
    health checker treo vô hạn.
    """
    try:
        response = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
        try:
            body = response.json()
        except Exception:
            body = {}

        ready = response.status_code == 200 and bool(body.get("ready", True))
        if ready:
            return True, "ready"

        reasons = body.get("reasons")
        if isinstance(reasons, list) and reasons:
            return False, ",".join(str(x) for x in reasons)
        return False, f"http_{response.status_code}"
    except httpx.TimeoutException as exc:
        return False, type(exc).__name__
    except httpx.HTTPError as exc:
        return False, type(exc).__name__
    except Exception as exc:  # defensive: a probe failure must not kill the checker
        return False, type(exc).__name__


def _write_event(out: pathlib.Path, **fields) -> dict:
    ts = time.time()
    rec = {
        "ts": ts,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)),
        **fields,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Poll cả hai region và log state transition theo ngưỡng lỗi liên tiếp."""
    if interval <= 0:
        raise ValueError("interval must be > 0")
    if timeout <= 0:
        raise ValueError("timeout must be > 0")
    if threshold < 1:
        raise ValueError("threshold must be >= 1")
    if duration < 0:
        raise ValueError("duration must be >= 0")

    out.parent.mkdir(parents=True, exist_ok=True)

    # Stack baseline được khởi động với primary healthy. Không emit HEALTHY ở lần
    # poll đầu tiên; rubric chấm transition outage đầu tiên phải là UNHEALTHY.
    state = {region: "HEALTHY" for region in URL}
    consecutive_fails = {region: 0 for region in URL}
    started = time.monotonic()

    while time.monotonic() - started < duration:
        cycle_started = time.monotonic()
        for region in URL:
            ready, reason = probe(region, timeout)
            if ready:
                consecutive_fails[region] = 0
                if state[region] == "UNHEALTHY":
                    state[region] = "HEALTHY"
                    _write_event(
                        out,
                        event="state_change",
                        region=region,
                        to="HEALTHY",
                        reason=reason,
                        consecutive_fails=0,
                        interval_s=interval,
                        threshold=threshold,
                    )
                continue

            consecutive_fails[region] += 1
            if state[region] == "HEALTHY" and consecutive_fails[region] >= threshold:
                state[region] = "UNHEALTHY"
                _write_event(
                    out,
                    event="state_change",
                    region=region,
                    to="UNHEALTHY",
                    reason=reason,
                    consecutive_fails=consecutive_fails[region],
                    interval_s=interval,
                    threshold=threshold,
                )

        elapsed_cycle = time.monotonic() - cycle_started
        elapsed_total = time.monotonic() - started
        sleep_for = min(max(0.0, interval - elapsed_cycle), max(0.0, duration - elapsed_total))
        if sleep_for:
            time.sleep(sleep_for)

    return {
        "states": state,
        "consecutive_fails": consecutive_fails,
        "interval_s": interval,
        "threshold": threshold,
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
