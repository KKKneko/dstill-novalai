#!/usr/bin/env python3
"""FastAPI service backing the Dstill NovelAI review WebUI (电影暗房·审片台).

Reuses the kernel (``dstill_novalai``) + review service (``dstill_review``):
- the batch runs in a background thread; progress events stream to the browser via SSE;
- single-image review ops (regenerate / edit / delete / review) are plain REST;
- secrets (NOVELAI_TOKEN / the agent key) resolve env-first; if a var is absent the
  value may be supplied per-run via the POST /run body (this is a local single-user
  tool) and is promoted into the process env. Secrets are never sent to the client.

Run (optionally export NOVELAI_TOKEN + the agent key env var first; otherwise enter
them in the WebUI form):
    uvicorn webui.server:app --port 8000
"""
from __future__ import annotations

import dataclasses
import json
import os
import queue
import re
import sys
import threading
from pathlib import Path
from typing import Any

# The kernel + review modules live in ../scripts.
_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import dstill_novalai as core  # noqa: E402
import dstill_review as review  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, StreamingResponse  # noqa: E402

app = FastAPI(title="Dstill NovelAI Review")

_PARAM_FIELDS = {f.name for f in dataclasses.fields(core.GenerationJobParams)}

# 发起表单预设：存仓库 config/presets/*.json。绝不写密钥——保存时只保留
# GenerationJobParams 字段（agent_api_key / novelai_token 不是其字段，天然被滤掉），
# 再显式排除一次以防万一。
_PRESETS_DIR = (_SCRIPTS.parent / "config" / "presets").resolve()
_PRESET_NAME_RE = re.compile(r"^[\w\- 一-鿿]{1,64}$")
_PRESET_SECRET_KEYS = {"agent_api_key", "novelai_token"}


def _preset_path(name: str) -> Path:
    name = (name or "").strip()
    if not _PRESET_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="预设名只能含字母/数字/中文/下划线/连字符/空格，且不超过 64 字")
    path = (_PRESETS_DIR / f"{name}.json").resolve()
    if path.parent != _PRESETS_DIR:
        raise HTTPException(status_code=400, detail="bad preset name")
    return path


class _RunState:
    """Single-run server state (this is a local, single-user tool)."""

    def __init__(self) -> None:
        self.params: core.GenerationJobParams | None = None
        self.output_dir: Path | None = None
        self.progress_lock = threading.Lock()
        # One in-flight NovelAI request at a time: a single token rejects
        # concurrent jobs, so the batch worker and every manual rerun share
        # this lock — a rerun queues until the current request finishes.
        self.request_lock = threading.Lock()
        self.events: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.worker: threading.Thread | None = None


STATE = _RunState()


def _token() -> str | None:
    return os.getenv("NOVELAI_TOKEN") or None


def _emit(event: dict[str, Any]) -> None:
    STATE.events.put(event)


def _require_run() -> Path:
    if STATE.output_dir is None:
        raise HTTPException(status_code=409, detail="no run started yet")
    return STATE.output_dir


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "running": bool(STATE.worker and STATE.worker.is_alive()),
        "output_dir": str(STATE.output_dir) if STATE.output_dir else None,
        "token_present": bool(_token()),
    }


@app.get("/options")
def options() -> dict[str, Any]:
    return {
        "models": list(core.SUPPORTED_IMAGE_MODELS),
        "samplers": list(core.SUPPORTED_SAMPLERS),
        "resolution_presets": [p.name for p in core.BUILTIN_RESOLUTION_PRESETS],
        "uc_presets": [1, 2, 3],  # 负面 preset 模式合法值（见 resolve_fixed_negative_prompt）
    }


@app.get("/presets")
def list_presets() -> list[str]:
    if not _PRESETS_DIR.is_dir():
        return []
    return sorted(p.stem for p in _PRESETS_DIR.glob("*.json"))


@app.get("/presets/{name}")
def get_preset(name: str) -> dict[str, Any]:
    path = _preset_path(name)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="preset not found")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"bad preset file: {exc}")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="bad preset file")
    for key in _PRESET_SECRET_KEYS:
        data.pop(key, None)
    return data


@app.put("/presets/{name}")
def save_preset(name: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    path = _preset_path(name)
    body = body or {}
    data = {k: v for k, v in body.items() if k in _PARAM_FIELDS and k not in _PRESET_SECRET_KEYS}
    _PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "name": path.stem}


@app.delete("/presets/{name}")
def delete_preset(name: str) -> dict[str, Any]:
    path = _preset_path(name)
    if path.is_file():
        path.unlink()
    return {"ok": True}


@app.post("/run")
def start_run(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    if STATE.worker is not None and STATE.worker.is_alive():
        raise HTTPException(status_code=409, detail="a run is already in progress")

    # Keep kernel defaults for any field the caller omits.
    kwargs = {k: v for k, v in body.items() if k in _PARAM_FIELDS}
    try:
        params = core.GenerationJobParams(**kwargs)
    except TypeError as exc:
        raise HTTPException(status_code=400, detail=f"bad params: {exc}")

    # The server owns an absolute output_dir so it can list artifacts / serve images.
    if params.output_dir:
        out = Path(params.output_dir).resolve()
    else:
        out = (_SCRIPTS.parent / "output" / core.default_output_dir().name).resolve()
    params.output_dir = str(out)
    params.dry_run = bool(body.get("dry_run", False))
    out.mkdir(parents=True, exist_ok=True)

    # Secrets resolve env-first; if a var is absent we take the value from the
    # (localhost) request body and promote it into the process env, so the kernel
    # (which reads the agent key via os.getenv) and the review endpoints all see
    # it. Form values are never logged, echoed back, or written to disk.
    api_key_env = (params.agent_api_key_env or "AGENT_API_KEY").strip()
    params.agent_api_key_env = api_key_env
    body_token = str(body.get("novelai_token") or "").strip()
    if body_token and not os.getenv("NOVELAI_TOKEN"):
        os.environ["NOVELAI_TOKEN"] = body_token
    body_agent_key = str(body.get("agent_api_key") or "").strip()
    if body_agent_key and not os.getenv(api_key_env):
        os.environ[api_key_env] = body_agent_key

    token = _token()
    if not params.dry_run and not token:
        raise HTTPException(status_code=400, detail="缺少 NovelAI token：请在表单填写，或设置服务端环境变量 NOVELAI_TOKEN")
    # The agent key is hard-required by the kernel (AgentKeyMissingError is NOT
    # swallowed by failure-policy=skip — a missing key aborts the whole batch), so
    # validate up-front whenever the run could call the agent.
    agent_capable = params.mode in ("auto-multichar", "all-agent")
    needs_agent_key = agent_capable and (not params.dry_run or params.agent_run_in_dry_run)
    if needs_agent_key and not os.getenv(api_key_env):
        raise HTTPException(status_code=400, detail=f"缺少外部 LLM API key：请在表单填写，或设置服务端环境变量 {api_key_env}")

    STATE.params = params
    STATE.output_dir = out
    # Drop any stale events from a previous run.
    with STATE.events.mutex:
        STATE.events.queue.clear()

    def _worker() -> None:
        try:
            core.run_generation_job(
                params,
                token=token,
                emit=_emit,
                progress_lock=STATE.progress_lock,
                request_lock=STATE.request_lock,
            )
        except core.TokenMissingError:
            _emit({"type": "job_error", "code": "token_missing", "message": "NOVELAI_TOKEN not set"})
        except Exception as exc:  # noqa: BLE001 - surface to UI, scrubbed
            _emit({"type": "job_error", "message": core.mask_secret_text(str(exc), token)})

    STATE.worker = threading.Thread(target=_worker, name="dstill-batch", daemon=True)
    STATE.worker.start()
    return {"ok": True, "output_dir": str(out), "dry_run": params.dry_run}


@app.get("/events")
def events() -> StreamingResponse:
    def stream():
        yield ": connected\n\n"
        while True:
            try:
                event = STATE.events.get(timeout=15.0)
            except queue.Empty:
                yield ": keepalive\n\n"
                continue
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(stream(), media_type="text/event-stream", headers=headers)


@app.get("/artifacts")
def artifacts() -> list[dict[str, Any]]:
    return review.list_artifacts(_require_run())


@app.get("/artifacts/{stem}")
def artifact_detail(stem: str) -> dict[str, Any]:
    try:
        return review.get_artifact_detail(_require_run(), stem)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="artifact not found")


@app.post("/artifacts/{stem}/regenerate")
def regenerate(stem: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    out = _require_run()
    if STATE.params is None:
        raise HTTPException(status_code=409, detail="no run params on server")
    token = _token()
    if not token:
        raise HTTPException(status_code=400, detail="NOVELAI_TOKEN not set in server environment")
    try:
        meta = review.get_artifact_detail(out, stem)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="artifact not found")

    scoped = review.scoped_params_for_artifact(STATE.params, meta)
    scoped.seed = None  # rerun = replace with a fresh random seed
    try:
        result = review.regenerate_entry(
            out,
            scoped,
            token=token,
            previous_output_stem=meta.get("output_stem", stem),
            force_refresh=bool(body.get("force_refresh", False)),
            emit=_emit,
            progress_lock=STATE.progress_lock,
            request_lock=STATE.request_lock,
        )
    except core.ProviderError as exc:
        raise HTTPException(status_code=502, detail=core.mask_secret_text(str(exc), token))
    except (core.AgentError, RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=core.mask_secret_text(str(exc)))
    return result


@app.post("/artifacts/{stem}/edit")
def edit(stem: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    out = _require_run()
    new_tags = body.get("new_tags")
    if not isinstance(new_tags, str) or not new_tags.strip():
        raise HTTPException(status_code=400, detail="new_tags required")
    try:
        meta = review.get_artifact_detail(out, stem)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="artifact not found")

    source = meta.get("source_prompt_file")
    line_number = meta.get("source_line_number")
    split_mode = "nonempty-lines" if line_number is not None else "single"
    try:
        review.edit_prompt_source(source, split_mode=split_mode, new_tags=new_tags, line_number=line_number)
    except (ValueError, IndexError, OSError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Two-step: report the rebuilt entry (new tags/stem) but do NOT regenerate yet.
    payload: dict[str, Any] = {"ok": True, "regenerated": False}
    if STATE.params is not None:
        scoped = review.scoped_params_for_artifact(STATE.params, meta)
        entry = review.rebuild_entry(scoped)
        payload["raw_tags"] = entry.raw_tags
        payload["new_output_stem"] = core.entry_output_stem(entry)
    return payload


@app.delete("/artifacts/{stem}")
def delete(stem: str) -> dict[str, Any]:
    out = _require_run()
    removed = review.delete_artifact(out, stem, include_samples=False, progress_lock=STATE.progress_lock)
    return {"ok": True, "removed": removed}


@app.post("/artifacts/{stem}/review")
def set_review(stem: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or {}
    out = _require_run()
    try:
        record = review.set_review_status(out, stem, body.get("status"), note=body.get("note"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "review": record}


@app.get("/image/{stem}")
def image(stem: str) -> FileResponse:
    out = _require_run().resolve()
    if "/" in stem or "\\" in stem or ".." in stem:
        raise HTTPException(status_code=400, detail="bad stem")
    for ext in ("png", "webp", "jpg", "jpeg"):
        candidate = (out / f"{stem}.{ext}").resolve()
        if not str(candidate).startswith(str(out)):
            continue
        if candidate.is_file():
            return FileResponse(candidate)
    raise HTTPException(status_code=404, detail="image not found")
