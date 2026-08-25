"""BƯỚC 3b — Cutover sang region phụ theo đúng thứ tự an toàn.

Thứ tự bắt buộc:
1_verify_target -> 2_restore_snapshot -> 3_scale_pool -> 4_wait_ready -> 5_dns_cutover.
DNS chỉ được đổi sau khi target thật sự ready.
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")
ACTIVE = pathlib.Path("edge/active_region")


def emit(**kw):
    """Append một record JSONL có timestamp và in record ra stdout."""
    ts = time.time()
    rec = {
        "ts": ts,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)),
        **kw,
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("FAILOVER", json.dumps(rec))
    return rec


def state_of(region: str, timeout: float = 2.0) -> dict:
    response = httpx.get(f"{URL[region]}/v1/state", timeout=timeout)
    if response.status_code != 200:
        raise RuntimeError(f"region-{region} state returned HTTP {response.status_code}")
    return response.json()


def _error(step: str, exc: BaseException, **extra) -> dict:
    emit(step=step, ok=False, error=type(exc).__name__, detail=str(exc), **extra)
    return {"ok": False, "failed_step": step, "error": type(exc).__name__, "detail": str(exc)}


def failover(target: str, backend: str, wait: float) -> dict:
    """Thực hiện failover 5 bước; abort nếu target không thể trở thành ready."""
    if target not in URL:
        raise ValueError(f"unknown target region: {target}")
    if backend not in {"fs", "minio"}:
        raise ValueError(f"unsupported backend: {backend}")
    if wait <= 0:
        raise ValueError("wait must be > 0")

    primary = "b" if target == "a" else "a"

    # 1. Target process phải truy cập được. Nó chưa cần ready vì snapshot/pool sẽ
    # được phục hồi ở các bước sau.
    try:
        before = state_of(target)
        emit(step="1_verify_target", ok=True, target=target, state=before)
    except Exception as exc:
        result = _error("1_verify_target", exc, target=target)
        result.update(target=target, primary=primary)
        return result

    # 2. Restore snapshot rồi đo RPO bằng primary hiện tại so với DB vừa restore.
    try:
        meta = snapshot.get(target, backend)
        rpo = snapshot.rpo(
            pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
            pathlib.Path(f"state/region-{target}/vectors.sqlite"),
        )
        restore_event = emit(
            step="2_restore_snapshot",
            ok=True,
            target=target,
            backend=backend,
            snapshot_at=meta.get("snapshot_at"),
            restored_at=meta.get("restored_at"),
            rpo_seconds=rpo.get("rpo_seconds"),
            docs_lost=rpo.get("docs_lost"),
            embed_model_version=meta.get("embed_model_version"),
        )
    except (Exception, SystemExit) as exc:
        result = _error("2_restore_snapshot", exc, target=target, backend=backend)
        result.update(target=target, primary=primary)
        return result

    # 3. Scale GPU pool. serving/app.py bắt đầu warm-up khi thấy transition này.
    pool_file = pathlib.Path(f"state/region-{target}/pool_state")
    try:
        pool_file.parent.mkdir(parents=True, exist_ok=True)
        pool_file.write_text("full\n")
        emit(step="3_scale_pool", ok=True, target=target, pool_state="full")
    except Exception as exc:
        result = _error("3_scale_pool", exc, target=target)
        result.update(target=target, primary=primary)
        return result

    # 4. Không được cutover cho tới khi /readyz 200. Warm-up thực tế nằm trong RTO.
    wait_started = time.monotonic()
    ready_body = None
    last_reason = "not_ready"
    while time.monotonic() - wait_started < wait:
        try:
            response = httpx.get(f"{URL[target]}/readyz", timeout=min(2.0, wait))
            try:
                body = response.json()
            except Exception:
                body = {}
            if response.status_code == 200 and bool(body.get("ready", True)):
                ready_body = body
                break
            reasons = body.get("reasons")
            last_reason = ",".join(map(str, reasons)) if isinstance(reasons, list) else f"http_{response.status_code}"
        except Exception as exc:
            last_reason = type(exc).__name__
        remaining = wait - (time.monotonic() - wait_started)
        if remaining > 0:
            time.sleep(min(0.25, remaining))

    waited_s = round(time.monotonic() - wait_started, 3)
    if ready_body is None:
        emit(
            step="4_wait_ready",
            ok=False,
            target=target,
            waited_s=waited_s,
            reason=last_reason,
        )
        return {
            "ok": False,
            "failed_step": "4_wait_ready",
            "target": target,
            "primary": primary,
            "waited_s": waited_s,
            "reason": last_reason,
            "rpo_seconds": restore_event.get("rpo_seconds"),
            "docs_lost": restore_event.get("docs_lost"),
        }

    try:
        final_state = state_of(target)
    except Exception:
        final_state = ready_body
    emit(
        step="4_wait_ready",
        ok=True,
        target=target,
        waited_s=waited_s,
        state=final_state,
    )

    # 5. Chỉ bây giờ mới đổi DNS/LB pointer.
    previous = ACTIVE.read_text().strip() if ACTIVE.exists() else primary
    try:
        ACTIVE.parent.mkdir(parents=True, exist_ok=True)
        ACTIVE.write_text(target + "\n")
        cutover_event = emit(
            step="5_dns_cutover",
            ok=True,
            target=target,
            previous_region=previous,
            active_region=target,
        )
    except Exception as exc:
        result = _error("5_dns_cutover", exc, target=target)
        result.update(target=target, primary=primary)
        return result

    return {
        "ok": True,
        "primary": primary,
        "target": target,
        "target_state": final_state,
        "restore": {
            "rpo_seconds": restore_event.get("rpo_seconds"),
            "docs_lost": restore_event.get("docs_lost"),
            "embed_model_version": restore_event.get("embed_model_version"),
        },
        "cutover": {
            "ok": cutover_event.get("ok", False),
            "from": previous,
            "to": target,
        },
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
