#!/usr/bin/env python3
"""Manual review / curation service for the Dstill NovelAI backend.

This module is the *human-facing* side of the tool: it powers a WebUI where a
person reviews images produced by a batch run and acts on them one at a time
(regenerate, edit the prompt, delete, mark reviewed).  It is deliberately kept
**out of the agent skill**: the Codex/agent path only ever sees the CLI
(``dstill_novalai.py``), honoring SKILL.md's "front-end only, never implement a
web UI" rule.  All generation logic is reused from the kernel via ``core`` --
this module only adds single-item operations and review state on top.

Import contract: callers put the ``scripts/`` directory on ``sys.path`` and then
``import dstill_review``; this module does ``import dstill_novalai as core``.

Concurrency: the batch may run in a background thread while the user acts on
already-produced images.  Pass the same ``threading.Lock`` both to
``core.run_generation_job``/``core.generate_and_save_entry`` and to the mutating
functions here (``regenerate_entry``/``delete_artifact``) so reads/writes of
``progress.jsonl`` are serialized.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import dstill_novalai as core

__all__ = [
    "list_artifacts",
    "get_artifact_detail",
    "rebuild_entry",
    "scoped_params_for_artifact",
    "edit_prompt_source",
    "regenerate_entry",
    "delete_artifact",
    "set_review_status",
    "load_review_status",
]

PROGRESS_FILENAME = "progress.jsonl"
REVIEW_FILENAME = "review.jsonl"
VALID_REVIEW_STATUS = {"approved", "rejected", "pending"}
_ARTIFACT_EXTS = ("png", "webp", "jpg", "jpeg", "json", "txt")
_IMAGE_EXTS = {"png", "webp", "jpg", "jpeg"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timeout() -> float:
    return float(os.getenv("NOVELAI_TIMEOUT", "60"))


def _lock_cm(progress_lock: threading.Lock | None):
    return progress_lock if progress_lock is not None else contextlib.nullcontext()


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("ab") as file:
        file.write(data.encode("utf-8"))
        file.flush()
        os.fsync(file.fileno())


###############################################################################
# Listing / inspection (read-only over existing metadata.json artifacts).
###############################################################################


def list_artifacts(output_dir: str | Path) -> list[dict[str, Any]]:
    """Return one record per produced image (parsed metadata + review status).

    Reads the top-level ``{stem}.json`` artifact metadata files written by
    ``core.save_generated_artifact`` (the ``dry-run/`` subdir and the bookkeeping
    ``*.jsonl`` files are ignored).
    """
    output_dir = Path(output_dir)
    if not output_dir.is_dir():
        return []
    review = load_review_status(output_dir)
    items: list[dict[str, Any]] = []
    for meta_path in sorted(output_dir.glob("*.json")):
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or "output_stem" not in data or "path" not in data:
            continue
        stem = meta_path.stem
        data["artifact_stem"] = stem
        data["metadata_file"] = str(meta_path)
        data["review_status"] = review.get(stem, {}).get("status", "pending")
        items.append(data)
    return items


def get_artifact_detail(output_dir: str | Path, artifact_stem: str) -> dict[str, Any]:
    """Full metadata (incl. agent plan, payload, source line) for one image."""
    output_dir = Path(output_dir)
    meta_path = output_dir / f"{artifact_stem}.json"
    if not meta_path.is_file():
        raise FileNotFoundError(f"artifact metadata not found: {meta_path}")
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    review = load_review_status(output_dir)
    data["artifact_stem"] = artifact_stem
    data["metadata_file"] = str(meta_path)
    data["review_status"] = review.get(artifact_stem, {}).get("status", "pending")
    return data


###############################################################################
# Prompt source rebuild + write-back.
###############################################################################


def _exclude_set(params: core.GenerationJobParams, config: dict[str, Any]) -> set[str]:
    if params.exclude_tags is not None:
        raw = core._split_parts(params.exclude_tags)
    else:
        raw = config.get("prompt", {}).get("exclude_tags") or []
    return core.build_exclude_tag_set(raw)


def rebuild_entry(params: core.GenerationJobParams, *, config: dict[str, Any] | None = None) -> core.PromptEntry:
    """Re-derive the single ``PromptEntry`` that ``params`` points at.

    ``params`` must be scoped to exactly one prompt: for a per-file source use
    ``prompt_file`` + ``prompt_split_mode="single"``; for a line in a multi-line
    file use ``prompt_split_mode="nonempty-lines"`` with ``prompt_start_index``
    equal to that line's ``source_prompt_index`` and ``prompt_limit=1``.  The
    entry reflects the *current* on-disk ``.txt`` (so it picks up edits).
    """
    if config is None:
        config = core.load_config(params.config_file)
    entries = core.find_prompt_entries(
        Path(params.prompt_file) if params.prompt_file else None,
        Path(params.prompt_dir) if params.prompt_dir else None,
        prompt_split_mode=params.prompt_split_mode,
        exclude_tags=_exclude_set(params, config),
        prompt_start_index=params.prompt_start_index,
        prompt_limit=params.prompt_limit,
    )
    entries = core.filter_prompt_entries(entries, start_index=params.prompt_start_index, limit=params.prompt_limit)
    if not entries:
        raise ValueError("no prompt entry matched the given source/window")
    if len(entries) > 1:
        raise ValueError("source/window matched multiple prompts; narrow prompt_start_index/prompt_limit to one")
    return entries[0]


def scoped_params_for_artifact(
    run_params: core.GenerationJobParams,
    metadata: dict[str, Any],
) -> core.GenerationJobParams:
    """Clone the run's params, narrowed to the single prompt that produced ``metadata``.

    ``metadata`` is an artifact metadata dict (a ``list_artifacts`` item). Agent
    config, model, sampler, prefixes etc. are inherited from the original run;
    only the prompt source is scoped to that one entry so ``regenerate_entry``
    targets exactly it. ``seed`` is left unchanged -- the caller sets it to None
    for a fresh-seed rerun.
    """
    source = metadata.get("source_prompt_file")
    if not source:
        raise ValueError("metadata missing source_prompt_file")
    line_number = metadata.get("source_line_number")
    source_index = metadata.get("source_prompt_index")
    if line_number is not None:
        return dataclasses.replace(
            run_params,
            prompt_file=str(source),
            prompt_dir=None,
            prompt_split_mode="nonempty-lines",
            prompt_start_index=source_index,
            prompt_limit=1,
        )
    return dataclasses.replace(
        run_params,
        prompt_file=str(source),
        prompt_dir=None,
        prompt_split_mode="single",
        prompt_start_index=None,
        prompt_limit=1,
    )


def edit_prompt_source(
    source_path: str | Path,
    *,
    split_mode: str,
    new_tags: str,
    line_number: int | None = None,
) -> dict[str, Any]:
    """Write edited tags back to the originating ``.txt``.

    * ``single`` / directory sources (one prompt per file): overwrite the file.
    * ``nonempty-lines``: replace only the physical line ``line_number``
      (1-based, matching ``PromptEntry.source_line_number``); ``new_tags`` must
      be single-line.

    The text written is what the UI shows (already preprocessed: underscores ->
    spaces, excluded tags removed); discovery re-applies preprocessing on read,
    which is idempotent for these transforms.
    """
    source_path = Path(source_path)
    if split_mode == "nonempty-lines":
        if line_number is None:
            raise ValueError("line_number is required for nonempty-lines sources")
        if "\n" in new_tags or "\r" in new_tags:
            raise ValueError("new_tags must be single-line for nonempty-lines sources")
        # Use the same decode/split as discovery so line_number stays aligned.
        lines = core.read_prompt_lines(source_path)
        idx = line_number - 1
        if idx < 0 or idx >= len(lines):
            raise IndexError(f"line_number {line_number} out of range for {source_path} ({len(lines)} lines)")
        lines[idx] = new_tags
        payload = ("\n".join(lines) + "\n").encode("utf-8")
    else:
        payload = (new_tags.rstrip("\n") + "\n").encode("utf-8")
    core._atomic_write(source_path, payload)
    return {"source_path": str(source_path), "split_mode": split_mode, "line_number": line_number}


###############################################################################
# Single-image regenerate (replace semantics) + delete.
###############################################################################


def _prepare_for_regen(
    params: core.GenerationJobParams,
    config: dict[str, Any],
    entry: core.PromptEntry,
    *,
    force_replan: bool,
) -> core.PreparedPrompt | None:
    """Resolve prefix/negative/agent config exactly like the batch and prepare."""
    model = params.model
    if params.positive_prefix_file:
        positive_prefix_mode = "custom"
        positive_prefix_value = core.read_prompt_text(Path(params.positive_prefix_file))
    else:
        positive_prefix_mode = params.positive_prefix_mode
        positive_prefix_value = params.positive_prefix or ""
    if params.negative_prompt_file:
        negative_prompt_mode = "custom"
        negative_prompt_value = core.read_prompt_text(Path(params.negative_prompt_file))
    else:
        negative_prompt_mode = params.negative_prompt_mode
        negative_prompt_value = params.negative_prompt or ""

    positive_prefix = core.resolve_positive_prefix(
        mode=positive_prefix_mode, custom_prefix=positive_prefix_value, model=model
    )
    fixed_negative_prompt, negative_uc_preset = core.resolve_fixed_negative_prompt(
        mode=negative_prompt_mode, custom_negative=negative_prompt_value, uc_preset=params.negative_uc_preset, model=model
    )
    agent_config = core.resolve_agent_runtime_config(params, config)
    return core.prepare_entry_with_agent_policy(
        entry,
        mode=params.mode,
        positive_prefix_mode=positive_prefix_mode,
        positive_prefix=positive_prefix,
        negative_prompt_mode=negative_prompt_mode,
        negative_uc_preset=negative_uc_preset,
        fixed_negative_prompt=fixed_negative_prompt,
        model=model,
        sampler=params.sampler,
        steps=params.steps,
        scale=params.scale,
        cfg_rescale=params.cfg_rescale,
        noise_schedule=params.noise_schedule,
        seed=params.seed,
        resolution_preset_name=params.resolution_preset,
        n_samples=params.n_samples,
        agent_config=agent_config,
        dry_run=False,
        skip_recorder=None,
        force_replan=force_replan,
    )


def regenerate_entry(
    output_dir: str | Path,
    params: core.GenerationJobParams,
    *,
    token: str,
    config: dict[str, Any] | None = None,
    previous_output_stem: str | None = None,
    force_refresh: bool = False,
    emit: core.EventEmitter | None = None,
    progress_lock: threading.Lock | None = None,
    request_lock: threading.Lock | None = None,
) -> dict[str, Any]:
    """Regenerate one prompt and replace its artifact (new random seed if
    ``params.seed is None``).

    ``params`` must be scoped to a single prompt (see ``rebuild_entry``); it
    reflects the current ``.txt`` so an edited prompt re-plans automatically
    (multichar plans miss the cache and re-call the LLM).  Pass
    ``previous_output_stem`` (from the image being reviewed) so that, when an
    edit changed the tags and thus the stem, the stale artifact + progress
    record are cleaned up.  ``force_refresh=True`` re-plans even for unchanged
    tags.  Raises ``TokenMissingError`` if no token, ``ProviderError`` on
    generation failure, ``RuntimeError`` if agent policy skipped the prompt.
    """
    output_dir = Path(output_dir)
    if not token:
        raise core.TokenMissingError("a NOVELAI token is required to regenerate")
    if config is None:
        config = core.load_config(params.config_file)
    entry = rebuild_entry(params, config=config)
    prepared = _prepare_for_regen(params, config, entry, force_replan=force_refresh)
    if prepared is None:
        raise RuntimeError("prompt was skipped by agent policy; see skipped.jsonl")

    new_stem = core.entry_output_stem(entry)
    if previous_output_stem and previous_output_stem != new_stem:
        delete_artifact(output_dir, previous_output_stem, include_samples=True, progress_lock=progress_lock)
    # Replace: clear any existing artifact + progress for this stem before regen.
    delete_artifact(output_dir, new_stem, include_samples=True, progress_lock=progress_lock)

    artifacts = core.generate_and_save_entry(
        output_dir,
        prepared,
        token=token,
        timeout=_timeout(),
        proxy_url=core.resolve_network_proxy(config),
        emit=emit,
        progress_lock=progress_lock,
        request_lock=request_lock,
    )
    return {
        "output_stem": new_stem,
        "resume_key": core.entry_resume_key(entry),
        "raw_tags": entry.raw_tags,
        "artifacts": artifacts,
    }


def _remove_artifact_files(output_dir: Path, stem: str) -> dict[str, list[str]]:
    removed: dict[str, list[str]] = {"images": [], "files": []}
    for ext in _ARTIFACT_EXTS:
        path = output_dir / f"{stem}.{ext}"
        if path.is_file():
            if ext in _IMAGE_EXTS:
                removed["images"].append(str(path))
            removed["files"].append(str(path))
            path.unlink()
    return removed


def _remove_entry_artifacts(output_dir: Path, output_stem: str) -> dict[str, list[str]]:
    """Remove the base stem and all numeric sample variants (``_2``, ``_3`` ...)."""
    removed: dict[str, list[str]] = {"images": [], "files": []}
    if not output_dir.is_dir():
        return removed
    pattern = re.compile(rf"^{re.escape(output_stem)}(_\d+)?\.(png|webp|jpg|jpeg|json|txt)$")
    for child in sorted(output_dir.iterdir()):
        if not child.is_file() or not pattern.match(child.name):
            continue
        if child.suffix.lower().lstrip(".") in _IMAGE_EXTS:
            removed["images"].append(str(child))
        removed["files"].append(str(child))
        child.unlink()
    return removed


def _prune_progress(output_dir: Path, *, removed_image_paths: list[str], progress_lock: threading.Lock | None) -> None:
    """Drop the deleted image paths from progress.jsonl; remove a record whose
    outputs become empty (so that prompt is no longer 'done' and can be regenerated)."""
    if not removed_image_paths:
        return
    path = output_dir / PROGRESS_FILENAME
    removed_set = set(removed_image_paths)
    with _lock_cm(progress_lock):
        if not path.is_file():
            return
        kept: list[str] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                outputs = record.get("outputs")
                if isinstance(outputs, list):
                    new_outputs = [item for item in outputs if item not in removed_set]
                    if not new_outputs:
                        continue
                    record["outputs"] = new_outputs
            kept.append(json.dumps(record, ensure_ascii=False, sort_keys=True))
        data = ("\n".join(kept) + ("\n" if kept else "")).encode("utf-8")
        core._atomic_write(path, data)


def delete_artifact(
    output_dir: str | Path,
    stem: str,
    *,
    include_samples: bool = False,
    progress_lock: threading.Lock | None = None,
) -> dict[str, list[str]]:
    """Delete an image's files and prune its progress entry.

    ``stem`` is the artifact file stem. With ``include_samples=False`` only that
    exact image (+ its ``.json``/``.txt``) is removed -- use this for "delete the
    image under review" on a multi-sample entry. With ``include_samples=True``
    the base stem and all ``_N`` samples are removed -- used by regenerate to
    fully replace an entry. Removed image paths are pruned from progress.jsonl;
    a progress record left with no outputs is dropped (the prompt becomes
    regenerable on a future batch run).
    """
    output_dir = Path(output_dir)
    if include_samples:
        removed = _remove_entry_artifacts(output_dir, stem)
    else:
        removed = _remove_artifact_files(output_dir, stem)
    _prune_progress(output_dir, removed_image_paths=removed["images"], progress_lock=progress_lock)
    return removed


###############################################################################
# Review state (approve / reject / pending) -- separate from generation progress.
###############################################################################


def set_review_status(
    output_dir: str | Path,
    artifact_stem: str,
    status: str,
    *,
    note: str | None = None,
) -> dict[str, Any]:
    """Record a human review decision for one image (append to review.jsonl).

    "Mark done / next" maps to ``status="approved"``. Independent of
    progress.jsonl, which tracks *generation* completion, not human approval.
    """
    if status not in VALID_REVIEW_STATUS:
        raise ValueError(f"status must be one of {sorted(VALID_REVIEW_STATUS)}")
    record = {
        "stem": artifact_stem,
        "status": status,
        "note": note,
        "created_at": _now_iso(),
    }
    _append_jsonl(Path(output_dir) / REVIEW_FILENAME, record)
    return record


def load_review_status(output_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Latest review record per artifact stem (last write wins)."""
    path = Path(output_dir) / REVIEW_FILENAME
    result: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return result
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and isinstance(record.get("stem"), str):
            result[record["stem"]] = record
    return result
