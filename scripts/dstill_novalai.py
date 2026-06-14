#!/usr/bin/env python3
"""Dstill NovelAI Skill v1 backend.

This script is intentionally small and self-contained:

* one public command: ``generate``;
* prompt input is ``.txt`` file(s) only;
* no web UI, database, gallery, workers, or user-defined resolution pools;
* NovelAI tokens are consumed only from environment/CLI and are never written.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import copy
import hashlib
import io
import json
import os
import random
import re
import sys
import threading
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Literal
from urllib.parse import urlsplit, urlunsplit

try:  # Pillow is used only for embedded metadata and image size.
    from PIL import Image
except Exception:  # pragma: no cover - keep dry-run usable without Pillow.
    Image = None  # type: ignore[assignment]


###############################################################################
# Built-in generation defaults migrated from the legacy backend.
###############################################################################

FIELD_MAP = {"quality_toggle": "qualityToggle", "uc_preset": "ucPreset"}

WEB_COMPAT_DEFAULTS: dict[str, Any] = {
    # Defaults normally present in NovelAI Web UI requests even when not exposed
    # as first-class fields by this CLI.
    "cfg_rescale": 0,
    "noise_schedule": "karras",
    "sm": False,
    "sm_dyn": False,
    "dynamic_thresholding": False,
    "legacy_v3_extend": False,
    "deliberate_euler_ancestral_bug": False,
    "stream": "msgpack",
    "reference_information_extracted_multiple": [],
    "reference_strength_multiple": [],
    "controlnet_strength": 1,
}

SUPPORTED_IMAGE_MODELS = [
    "nai-diffusion-4-5-full",
    "nai-diffusion-4-5-curated",
    "nai-diffusion-4-full",
    "nai-diffusion-4-curated",
    "nai-diffusion-4-curated-preview",
    "nai-diffusion-3",
    "nai-diffusion-furry-3",
]

SUPPORTED_SAMPLERS = [
    "k_euler",
    "k_euler_ancestral",
    "k_dpmpp_2s_ancestral",
    "k_dpmpp_2m",
    "k_dpmpp_sde",
    "ddim_v3",
    "uni_pc",
    "uni_pc_bh2",
]

SUPPORTED_IMAGE_FORMATS = ["png", "webp"]
SUPPORTED_UC_PRESETS = [0, 1, 2, 3, 4]

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "dstill_novalai.json"
DEFAULT_CONFIG: dict[str, Any] = {
    "network": {
        # Host:port is accepted here and normalized to http://host:port
        # before it is passed to httpx.
        "proxy": "127.0.0.1:7890",
    },
    "agent": {
        # Non-secret defaults only.  API keys are read from environment
        # variables at runtime and are never persisted to config/cache/metadata.
        # External prompt-refinement is mandatory for configured runs; leave
        # these blank here so callers must explicitly provide/confirm them.
        "api_format": "",
        "base_url": "",
        "model": "",
        "api_key_env": "",
        "timeout_seconds": 60,
        "max_retries": 3,
        "temperature": 0.2,
        "cache_enabled": True,
        "cache_dir": "",
    },
    "prompt": {
        # Tags removed from both the request prompt and the agent input before
        # a run. Matching is normalized: case-insensitive and underscore/space
        # equivalent (e.g. "white space" == "white_space").
        "exclude_tags": [],
    },
}

AGENT_PROMPT_VERSION = "dstill-agent-plan-v3"
SUPPORTED_AGENT_API_FORMATS = ["openai", "gemini"]
SUPPORTED_AGENT_PROVIDERS = ["external"]
SUPPORTED_AGENT_FAILURE_POLICIES = ["skip", "abort", "fallback-internal"]

DEFAULT_AGENT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
}
DEFAULT_AGENT_KEY_ENVS = {
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

FIRST_CLASS_PARAMETER_FIELDS = [
    "image",
    "mask",
    "strength",
    "noise",
    "color_correct",
    "extra_noise_seed",
    "img2img",
    "controlnet_condition",
    "controlnet_model",
    "controlnet_strength",
    "skip_cfg_above_sigma",
    "deliberate_euler_ancestral_bug",
]

QUALITY_TAGS_BY_MODEL: dict[str, str] = {
    "nai-diffusion-4-5-full": "location, very aesthetic, masterpiece, no text",
    "nai-diffusion-4-5-curated": "location, masterpiece, no text, -0.8::feet::, rating:general",
    "nai-diffusion-4-full": "no text, best quality, very aesthetic, absurdres",
    "nai-diffusion-4-curated": "rating:general, amazing quality, very aesthetic, absurdres",
    "nai-diffusion-4-curated-preview": "rating:general, amazing quality, very aesthetic, absurdres",
    "nai-diffusion-3": "best quality, amazing quality, very aesthetic, absurdres",
    "nai-diffusion-furry-3": "{best quality}, {amazing quality}",
}

UC_PRESET_TAGS_BY_MODEL: dict[str, dict[int, str]] = {
    "nai-diffusion-4-5-full": {
        1: "lowres, artistic error, scan artifacts, worst quality, bad quality, jpeg artifacts, multiple views, very displeasing, too many watermarks, negative space, blank page",
        2: "lowres, artistic error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, very displeasing, chromatic aberration, dithering, halftone, screentone, multiple views, logo, too many watermarks, negative space, blank page, @_@, mismatched pupils, glowing eyes, bad anatomy",
        3: "lowres, artistic error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, very displeasing, chromatic aberration, dithering, halftone, screentone, multiple views, logo, too many watermarks, negative space, blank page",
        4: "{worst quality}, distracting watermark, unfinished, bad quality, {widescreen}, upscale, {sequence}, {{grandfathered content}}, blurred foreground, chromatic aberration, sketch, everyone, [sketch background], simple, [flat colors], ych (character), outline, multiple scenes, [[horror (theme)]], comic",
    },
    "nai-diffusion-4-5-curated": {
        1: "blurry, lowres, upscaled, artistic error, scan artifacts, jpeg artifacts, logo, too many watermarks, negative space, blank page",
        2: "blurry, lowres, upscaled, artistic error, film grain, scan artifacts, bad anatomy, bad hands, worst quality, bad quality, jpeg artifacts, very displeasing, chromatic aberration, halftone, multiple views, logo, too many watermarks, @_@, mismatched pupils, glowing eyes, negative space, blank page",
        3: "blurry, lowres, upscaled, artistic error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, very displeasing, chromatic aberration, halftone, multiple views, logo, too many watermarks, negative space, blank page",
    },
    "nai-diffusion-4-full": {
        1: "blurry, lowres, error, worst quality, bad quality, jpeg artifacts, very displeasing",
        3: "blurry, lowres, error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, very displeasing, chromatic aberration, multiple views, logo, too many watermarks",
    },
    "nai-diffusion-4-curated": {
        1: "blurry, lowres, error, worst quality, bad quality, jpeg artifacts, very displeasing, logo, dated, signature",
        3: "blurry, lowres, error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, very displeasing, chromatic aberration, logo, dated, signature, multiple views, gigantic breasts",
    },
    "nai-diffusion-4-curated-preview": {
        1: "blurry, lowres, error, worst quality, bad quality, jpeg artifacts, very displeasing, logo, dated, signature",
        3: "blurry, lowres, error, film grain, scan artifacts, worst quality, bad quality, jpeg artifacts, very displeasing, chromatic aberration, logo, dated, signature, multiple views, gigantic breasts",
    },
    "nai-diffusion-3": {
        1: "lowres, jpeg artifacts, worst quality, watermark, blurry, very displeasing",
        2: "lowres, {bad}, error, fewer, extra, missing, worst quality, jpeg artifacts, bad quality, watermark, unfinished, displeasing, chromatic aberration, signature, extra digits, artistic error, username, scan, [abstract], bad anatomy, bad hands, @_@, mismatched pupils, heart-shaped pupils, glowing eyes",
        3: "lowres, {bad}, error, fewer, extra, missing, worst quality, jpeg artifacts, bad quality, watermark, unfinished, displeasing, chromatic aberration, signature, extra digits, artistic error, username, scan, [abstract]",
    },
    "nai-diffusion-furry-3": {
        1: "{worst quality}, guide lines, unfinished, bad, url, tall image, widescreen, compression artifacts, unknown text",
        3: "{{worst quality}}, [displeasing], {unusual pupils}, guide lines, {{unfinished}}, {bad}, url, artist name, {{tall image}}, mosaic, {sketch page}, comic panel, impact (font), [dated], {logo}, ych, {what}, {where is your god now}, {distorted text}, repeated text, {floating head}, {1994}, {widescreen}, absolutely everyone, sequence, {compression artifacts}, hard translated, {cropped}, {commissioner name}, unknown text, high contrast",
    },
}

MULTICHAR_DEFAULT_NEGATIVE_PROMPT = (
    "bad anatomy, bad hands, fused bodies, merged faces, extra limbs, extra arms, "
    "extra legs, duplicate face, malformed limbs, incorrect hands, bad proportions"
)

MULTICHAR_AGENT_SYSTEM_PROMPT = f"""你是 NovelAI V4/V4.5 多人图 prompt 构造器。输入是 Danbooru/NovelAI tag 文本，输出多人结构 JSON。只输出 JSON，不要 Markdown。

【决定一切规则的事实（来自 NovelAI 官方多人教程）】
- base_caption 是全局 caption，决定整张图的场景、画风、画质与构图。
- char_captions[i] 只作用到第 i 个角色区域，用来单独描述一个角色、最小化角色间信息串扰。
- 人数 tag（1boy、2girls、3others 等）只能出现在 base_caption；每个 char_captions[i].prompt 必须以不带数字的 girl / boy / other 开头（官方明确要求，绝不要在角色里写 1girl/1boy/1other）。
- char_captions 的顺序必须和 center 坐标一致：按 center.x 从小到大（从左到右）排列，第 0 个是最左边的角色。

【第一步 规范化（先清洗，再拆分）】
1. 人数收敛：base_caption 必须且只保留一个正确总人数 tag（如 2girls，或 1girl, 1boy），与 expected_character_count 一致。删除与多人冲突的 tag：solo、solo focus，以及当总人数>=2 时多余的 1girl/1boy/1other。
2. 解决矛盾：输入若同时含 solo 和 2girls 这类冲突，一律以 expected_character_count 为准按多人处理，丢弃 solo。
3. 不无中生有：规范化只做"删冲突、去重、归类"，绝不发明输入里没有的发色/服装/表情等具体特征。
4. 把删除的冲突 tag 写进 dropped_tags 字段，便于审计。

【第二步 归属分配（除第一步删掉的，其余 tag 不丢）】
- base_caption：总人数 tag + 全局共享信息——场景/背景、构图/镜头（cowboy shot、from side 等）、光照、画质/风格/画师 tag、所有角色共有的特征、概括全图的互动 tag（hug、holding hands 可留在 base）。只属于某一个角色的特征不要留在 base_caption。
- char_captions[i].prompt：以不带数字的 girl / boy / other 开头，后接只属于第 i 个角色的特征——角色名+作品名、发色/瞳色/发型/身材、该角色的服装、该角色的表情/单人动作。绝不写 1girl/2girls/3girls/multiple 等任何带数量的 tag。
- 互动动作官方语法：在相关角色的 prompt 里给动作 tag 加 source# / target# / mutual# 前缀表达主动/被动/相互。例：A 抱 B -> A 写 source#hug，B 写 target#hug；互相拥抱 -> 双方都写 mutual#hug。
- 既无法归类又非全局共享的 tag：保守放 base_caption，但优先尝试归类到角色。

【第三步 角色负面词（默认留空）】
官方定位：角色级 Undesired Content 是出现串色后的补救手段，不是默认预防。
- char_captions[i].negative_prompt 默认输出空字符串 ""。
- 仅当角色间某维度强对立、极易串色（双胞胎、同款服装不同色、同色系相近发型）时，才写 1-3 个"其他角色"的区分性 tag。
- 绝不把全局负面词复制进角色 negative_prompt。

【字段与硬约束】
- detected_character_count 必须等于 expected_character_count；char_captions 长度也必须等于它。
- 角色信息不足也要补足，用 `girl, unspecified appearance, same scene`、`boy, unspecified appearance, same scene` 或 `other, unspecified appearance, same scene`，不要只写 girl/boy/other。
- tag 里的反斜杠、括号转义原样保留，不要按 Markdown 处理；角色名/作品名原样复制，不改成自然语言。
- global_negative_prompt（用户通用负面词）只作整图全局负面，不复制到每个角色的 negative_prompt。
- llm_result.negative_prompt 是整图多人全局负面，默认包含：{MULTICHAR_DEFAULT_NEGATIVE_PROMPT}
- resolution_preset_name 只能从内置固定比例池中选择。

【示例】expected_character_count=2
输入：2girls, solo, hatsune miku, kasane teto, aqua hair, twintails, red hair, drill hair, school uniform, classroom, hug, masterpiece
正确输出：
- base_caption: "2girls, school uniform, classroom, hug, masterpiece"   // 删了 solo；uniform 两人都穿、hug 概括全图，留全局
- char_captions[0] 最左 center 约 [0.3,0.5]: prompt "girl, hatsune miku, aqua hair, twintails, mutual#hug"，negative_prompt ""
- char_captions[1] 右 center 约 [0.7,0.5]: prompt "girl, kasane teto, red hair, drill hair, mutual#hug"，negative_prompt ""
- dropped_tags: ["solo"]
"""

SINGLE_AGENT_SYSTEM_PROMPT = """你是 NovelAI V4/V4.5 单人图 prompt 规划器。

任务：在不改写用户原始 tag 的前提下理解场景，只从内置固定比例池中选择 resolution_preset_name。
不要输出自定义 width/height；不要删除用户 tag；只返回结构化 plan。
"""


###############################################################################
# Fixed resolution pool.
###############################################################################

MAX_PIXELS = 1024 * 1024
SINGLE_DEFAULT_RESOLUTION = "portrait_default_832x1216"
MULTI_DEFAULT_TENDENCY = [
    "multi_landscape_1024x768",
    "landscape_default_1216x832",
    "square_1024x1024",
]


@dataclass(frozen=True)
class ResolutionPreset:
    name: str
    width: int
    height: int

    @property
    def orientation(self) -> str:
        if self.width > self.height:
            return "landscape"
        if self.height > self.width:
            return "portrait"
        return "square"

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "width": self.width,
            "height": self.height,
            "orientation": self.orientation,
        }


BUILTIN_RESOLUTION_PRESETS: tuple[ResolutionPreset, ...] = (
    ResolutionPreset("square_1024x1024", 1024, 1024),
    ResolutionPreset("square_896x896", 896, 896),
    ResolutionPreset("portrait_default_832x1216", 832, 1216),
    ResolutionPreset("portrait_768x1152", 768, 1152),
    ResolutionPreset("portrait_896x1152", 896, 1152),
    ResolutionPreset("tall_portrait_704x1472", 704, 1472),
    ResolutionPreset("tall_portrait_640x1536", 640, 1536),
    ResolutionPreset("landscape_default_1216x832", 1216, 832),
    ResolutionPreset("landscape_1152x896", 1152, 896),
    ResolutionPreset("landscape_1344x768", 1344, 768),
    ResolutionPreset("wide_landscape_1472x704", 1472, 704),
    ResolutionPreset("cinematic_1536x640", 1536, 640),
    ResolutionPreset("multi_landscape_1024x768", 1024, 768),
    ResolutionPreset("multi_portrait_768x1024", 768, 1024),
)

PRESETS_BY_NAME = {preset.name: preset for preset in BUILTIN_RESOLUTION_PRESETS}


def validate_builtin_resolution_presets() -> None:
    for preset in BUILTIN_RESOLUTION_PRESETS:
        if preset.width % 64 != 0 or preset.height % 64 != 0:
            raise RuntimeError(f"invalid built-in resolution preset {preset.name}: size must be multiple of 64")
        if preset.width * preset.height > MAX_PIXELS:
            raise RuntimeError(f"invalid built-in resolution preset {preset.name}: pixel count exceeds 1024^2")
    for required in [SINGLE_DEFAULT_RESOLUTION, *MULTI_DEFAULT_TENDENCY, "multi_portrait_768x1024"]:
        if required not in PRESETS_BY_NAME:
            raise RuntimeError(f"missing built-in resolution preset: {required}")


validate_builtin_resolution_presets()


###############################################################################
# Runtime configuration.
###############################################################################


def _merge_dict_config(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(config_file: str | os.PathLike[str] | Path | None = None) -> dict[str, Any]:
    path = Path(config_file) if config_file else DEFAULT_CONFIG_PATH
    if not path.exists():
        return copy.deepcopy(DEFAULT_CONFIG)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON config file: {path}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"config file must contain a JSON object: {path}")
    return _merge_dict_config(DEFAULT_CONFIG, loaded)


def normalize_proxy_url(proxy: Any) -> str | None:
    if proxy is None:
        return None
    value = str(proxy).strip()
    if not value:
        return None
    if value.lower() in {"none", "null", "false", "off", "direct", "no_proxy", "no-proxy"}:
        return None
    if "://" not in value:
        value = f"http://{value}"
    return value


def resolve_network_proxy(config: dict[str, Any]) -> str | None:
    env_override = os.getenv("NOVELAI_PROXY")
    if env_override is not None:
        return normalize_proxy_url(env_override)
    network = config.get("network")
    if not isinstance(network, dict):
        return normalize_proxy_url(DEFAULT_CONFIG["network"]["proxy"])
    return normalize_proxy_url(network.get("proxy", network.get("proxy_url")))


###############################################################################
# Prompt composition and multi-character planning.
###############################################################################

COUNT_TAG_RE = re.compile(
    r"(?<![a-z0-9])([2-9]\d*|1\d+)\s*(girls?|boys?|others?|people|characters?)(?![a-z0-9])",
    re.IGNORECASE,
)
MULTIPLE_TAG_RE = re.compile(
    r"(?<![a-z0-9])multiple\s+(girls?|boys?|others?|people|characters?)(?![a-z0-9])",
    re.IGNORECASE,
)
SINGLE_GIRL_RE = re.compile(r"(?<![a-z0-9])1\s*girl(?![a-z0-9])", re.IGNORECASE)
SINGLE_BOY_RE = re.compile(r"(?<![a-z0-9])1\s*boy(?![a-z0-9])", re.IGNORECASE)
SINGLE_OTHER_RE = re.compile(r"(?<![a-z0-9])1\s*other(?![a-z0-9])", re.IGNORECASE)

TRIVIAL_CHARACTER_PROMPTS = {"girl", "boy", "other", "character", "person", "female", "male"}


@dataclass(frozen=True)
class PromptEntry:
    source_path: Path
    relative_path: Path
    raw_tags: str
    detected_multichar_tags: list[str]
    expected_character_count: int
    source_prompt_index: int | None = None
    source_line_number: int | None = None


@dataclass(frozen=True)
class MultiCharPrompt:
    raw_prompt: str
    base_caption: str
    negative_prompt: str
    char_captions: list[dict[str, Any]]
    source: dict[str, Any]
    canvas_orientation: str | None = None


@dataclass
class GenerationRequest:
    prompt: str
    negative_prompt: str | None = None
    action: Literal["generate", "img2img", "infill"] = "generate"
    model: str = "nai-diffusion-4-5-full"
    width: int = 832
    height: int = 1216
    steps: int = 28
    prompt_guidance: float = 5.0
    prompt_guidance_rescale: float | None = None
    sampler: str = "k_euler"
    noise_schedule: str | None = None
    seed: int | None = None
    n_samples: int = 1
    image_format: Literal["png", "webp"] = "png"
    quality_toggle: bool | None = True
    uc_preset: int | None = 0
    image: str | None = None
    mask: str | None = None
    strength: float | None = None
    noise: float | None = None
    color_correct: bool | None = None
    extra_noise_seed: int | None = None
    img2img: dict[str, Any] | None = None
    controlnet_condition: str | None = None
    controlnet_model: str | None = None
    controlnet_strength: float | None = None
    skip_cfg_above_sigma: float | None = None
    deliberate_euler_ancestral_bug: bool | None = None
    vibe_transfer: list[Any] = field(default_factory=list)
    precise_reference: dict[str, Any] | None = None
    extra_options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedPrompt:
    entry: PromptEntry
    mode: Literal["auto-multichar", "all-agent"]
    positive_prefix_mode: Literal["none", "preset", "custom"]
    positive_prefix: str
    negative_prompt_mode: Literal["none", "preset", "custom"]
    negative_uc_preset: int | None
    fixed_negative_prompt: str
    multichar_detected: bool
    agent_used: bool
    agent_plan: dict[str, Any]
    resolution_plan: dict[str, Any]
    multichar_prompt: MultiCharPrompt | None
    request: GenerationRequest
    training_caption: str
    agent_status: dict[str, Any] = field(default_factory=dict)


def _split_parts(text: str | None) -> list[str]:
    if not text:
        return []
    chars = " ,\t\r\n"
    return [p.strip(chars) for p in text.split(",") if p.strip(chars)]


def dedupe_prompt_tags(prompt: str) -> str:
    seen: set[str] = set()
    deduped: list[str] = []
    for part in _split_parts(prompt):
        if part in seen:
            continue
        seen.add(part)
        deduped.append(part)
    return ", ".join(deduped)


def merge_prompt_tags(*parts: str | None, dedupe: bool = True) -> str | None:
    merged = ", ".join(part for text in parts for part in _split_parts(text))
    if dedupe:
        merged = dedupe_prompt_tags(merged)
    return merged or None


def _normalize_tag(tag: str) -> str:
    # Case-insensitive, with underscores and spaces treated as equivalent.
    return re.sub(r"\s+", " ", tag.strip().lower().replace("_", " "))


def build_exclude_tag_set(tags: Iterable[str] | None) -> set[str]:
    if not tags:
        return set()
    return {normalized for tag in tags if (normalized := _normalize_tag(tag))}


def filter_excluded_tags(prompt: str, exclude: set[str]) -> str:
    if not exclude:
        return prompt
    return ", ".join(
        part for part in _split_parts(prompt) if _normalize_tag(part) not in exclude
    )


def preprocess_prompt_tags(prompt: str, exclude: set[str]) -> str:
    """在送往请求与 agent 之前预处理原始提示词：
    先剔除被排除的 tag，再把 tag 内部的下划线转成空格。"""
    return filter_excluded_tags(prompt, exclude).replace("_", " ")


def compose_prompt(
    random_prompt: str,
    positive_prefix: str | None = None,
    negative_prompt: str | None = None,
    *,
    dedupe_positive: bool = False,
) -> tuple[str, str | None]:
    prefix = _split_parts(positive_prefix)
    random_parts = _split_parts(random_prompt)
    positive = ", ".join(prefix + random_parts)
    if dedupe_positive:
        positive = dedupe_prompt_tags(positive)
    negative = merge_prompt_tags(negative_prompt, dedupe=True)
    return positive, negative


def resolve_positive_prefix(
    *,
    mode: Literal["none", "preset", "custom"],
    custom_prefix: str,
    model: str,
) -> str:
    if mode == "none":
        return ""
    if mode == "preset":
        return QUALITY_TAGS_BY_MODEL.get(model, "")
    value = (custom_prefix or "").strip()
    if not value:
        raise ValueError("--positive-prefix is required when --positive-prefix-mode custom")
    return value


def resolve_fixed_negative_prompt(
    *,
    mode: Literal["none", "preset", "custom"],
    custom_negative: str,
    uc_preset: int | None,
    model: str,
) -> tuple[str, int | None]:
    if mode == "none":
        return "", None
    if mode == "preset":
        if uc_preset not in {1, 2, 3}:
            raise ValueError("--negative-uc-preset must be one of 1, 2, 3 when --negative-prompt-mode preset")
        value = UC_PRESET_TAGS_BY_MODEL.get(model, {}).get(uc_preset, "")
        if not value:
            raise ValueError(f"UC preset {uc_preset} is not available for model {model}")
        return value, uc_preset
    value = (custom_negative or "").strip()
    if not value:
        raise ValueError("--negative-prompt is required when --negative-prompt-mode custom")
    return value, None


def decode_prompt_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").strip()


def read_prompt_text(path: Path) -> str:
    return decode_prompt_bytes(path.read_bytes())


def read_prompt_lines(path: Path) -> list[str]:
    return decode_prompt_bytes(path.read_bytes()).splitlines()


def dedupe_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        normalized = item.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def detect_multichar_tags(raw_prompt: str) -> list[str]:
    normalized = raw_prompt.lower().replace("_", " ")
    tags: list[str] = []

    for match in COUNT_TAG_RE.finditer(normalized):
        count, kind = match.groups()
        tags.append(f"{count}{kind.replace(' ', '')}")

    for match in MULTIPLE_TAG_RE.finditer(normalized):
        tags.append(f"multiple {match.group(1).strip()}")

    has_1girl = SINGLE_GIRL_RE.search(normalized) is not None
    has_1boy = SINGLE_BOY_RE.search(normalized) is not None
    has_1other = SINGLE_OTHER_RE.search(normalized) is not None

    if has_1girl and has_1boy:
        tags.append("1girl+1boy")
    if has_1girl and has_1other:
        tags.append("1girl+1other")
    if has_1boy and has_1other:
        tags.append("1boy+1other")

    return dedupe_preserve_order(tags)


def expected_character_count_from_tags(detected_tags: Iterable[str]) -> int:
    max_count = 0

    for raw_tag in detected_tags:
        tag = raw_tag.strip().lower().replace("_", " ")
        if not tag:
            continue

        explicit = re.match(r"^(\d+)\s*(girls?|boys?|others?|people|characters?)$", tag)
        if explicit:
            max_count = max(max_count, int(explicit.group(1)))
            continue

        if re.match(r"^multiple\s+(girls?|boys?|others?|people|characters?)$", tag):
            max_count = max(max_count, 2)
            continue

        combo_matches = re.findall(r"(\d+)\s*(?:girls?|boys?|others?)", tag)
        if combo_matches:
            max_count = max(max_count, sum(int(count) for count in combo_matches))

    return max_count


def find_prompt_entries(
    prompt_file: Path | None,
    prompt_dir: Path | None,
    *,
    prompt_split_mode: Literal["single", "nonempty-lines"] = "single",
    exclude_tags: set[str] | None = None,
    prompt_start_index: int | None = None,
    prompt_limit: int | None = None,
) -> list[PromptEntry]:
    exclude = exclude_tags or set()
    if bool(prompt_file) == bool(prompt_dir):
        raise ValueError("--prompt-file and --prompt-dir must be mutually exclusive and one is required")
    if prompt_split_mode not in {"single", "nonempty-lines"}:
        raise ValueError("--prompt-split-mode must be one of single, nonempty-lines")

    if prompt_file is not None:
        if prompt_file.suffix.lower() != ".txt":
            raise ValueError("--prompt-file must point to a .txt file")
        if not prompt_file.is_file():
            raise ValueError(f"prompt file not found: {prompt_file}")
        if prompt_split_mode == "nonempty-lines":
            entries: list[PromptEntry] = []
            prompt_index = 0
            for line_number, raw_line in enumerate(read_prompt_lines(prompt_file), start=1):
                raw_tags = preprocess_prompt_tags(raw_line.strip(), exclude)
                if not raw_tags:
                    continue
                prompt_index += 1
                detected = detect_multichar_tags(raw_tags)
                entries.append(
                    PromptEntry(
                        source_path=prompt_file,
                        relative_path=Path(f"{prompt_file.stem}_line_{prompt_index:06d}.txt"),
                        raw_tags=raw_tags,
                        detected_multichar_tags=detected,
                        expected_character_count=expected_character_count_from_tags(detected) or 1,
                        source_prompt_index=prompt_index,
                        source_line_number=line_number,
                    )
                )
            return entries
        raw_tags = preprocess_prompt_tags(read_prompt_text(prompt_file), exclude)
        detected = detect_multichar_tags(raw_tags)
        return [
            PromptEntry(
                source_path=prompt_file,
                relative_path=Path(prompt_file.name),
                raw_tags=raw_tags,
                detected_multichar_tags=detected,
                expected_character_count=expected_character_count_from_tags(detected) or 1,
            )
        ]

    assert prompt_dir is not None
    if not prompt_dir.is_dir():
        raise ValueError(f"prompt dir not found: {prompt_dir}")

    entries: list[PromptEntry] = []
    # 给目录里的每个 .txt 按排序后的顺序编 1-based 序号，并在读取文件内容之前就按
    # start-index/limit 窗口裁剪——这样 --prompt-limit 对 --prompt-dir 也生效，且
    # 限量小批时不必读完整个目录（19 万文件）。
    for index, path in enumerate((p for p in sorted(prompt_dir.rglob("*.txt")) if p.is_file()), start=1):
        if not _index_in_prompt_window(index, start_index=prompt_start_index, limit=prompt_limit):
            continue
        raw_tags = preprocess_prompt_tags(read_prompt_text(path), exclude)
        detected = detect_multichar_tags(raw_tags)
        entries.append(
            PromptEntry(
                source_path=path,
                relative_path=path.relative_to(prompt_dir),
                raw_tags=raw_tags,
                detected_multichar_tags=detected,
                expected_character_count=expected_character_count_from_tags(detected) or 1,
                source_prompt_index=index,
            )
        )
    return entries


def fallback_centers(count: int) -> list[list[float]]:
    if count <= 1:
        return [[0.5, 0.5]]
    if count == 2:
        xs = [0.3, 0.7]
    elif count == 3:
        xs = [0.2, 0.5, 0.8]
    elif count == 4:
        xs = [0.15, 0.38, 0.62, 0.85]
    else:
        step = 0.8 / max(count - 1, 1)
        xs = [round(0.1 + step * index, 4) for index in range(count)]
    return [[x, 0.5] for x in xs]


def _kind_for_index(raw_prompt: str, index: int, count: int) -> str:
    text = raw_prompt.casefold()
    has_girl = "girl" in text
    has_boy = "boy" in text
    has_other = "other" in text
    if has_girl and not has_boy and not has_other:
        return "girl"
    if has_boy and not has_girl and not has_other:
        return "boy"
    if has_other and not has_girl and not has_boy:
        return "other"
    if has_girl and has_boy and count == 2:
        return "girl" if index == 0 else "boy"
    if has_girl and has_other and count == 2:
        return "girl" if index == 0 else "other"
    if has_boy and has_other and count == 2:
        return "boy" if index == 0 else "other"
    return "character"


def _is_short_or_empty_character_prompt(prompt: Any) -> bool:
    if not isinstance(prompt, str):
        return True
    normalized = re.sub(r"\s+", " ", prompt.strip().lower())
    if not normalized:
        return True
    if normalized in TRIVIAL_CHARACTER_PROMPTS:
        return True
    parts = [part.strip().lower() for part in normalized.split(",") if part.strip()]
    return len(parts) == 1 and parts[0] in TRIVIAL_CHARACTER_PROMPTS


def validate_center(value: Any, index: int, total: int) -> list[float]:
    if (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(item, (int, float)) for item in value)
    ):
        x = min(1.0, max(0.0, float(value[0])))
        y = min(1.0, max(0.0, float(value[1])))
        return [x, y]
    return fallback_centers(total)[index]


def choose_resolution_name_from_agent_plan(agent_plan: dict[str, Any]) -> str:
    name = str(agent_plan.get("resolution_preset_name") or "").strip()
    if not name:
        llm_result = agent_plan.get("llm_result")
        if isinstance(llm_result, dict):
            name = str(llm_result.get("resolution_preset_name") or "").strip()
    if name not in PRESETS_BY_NAME:
        raise ValueError(f"agent selected unknown resolution preset: {name or '<empty>'}")
    return name


def _stable_index(text: str, modulo: int) -> int:
    if modulo <= 0:
        return 0
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).digest()
    return int.from_bytes(digest[:4], "big") % modulo


def _orientation_hint(raw_prompt: str, *, multichar: bool) -> str:
    text = raw_prompt.casefold()
    if any(term in text for term in ("wide shot", "panorama", "landscape", "scenery", "wide angle")):
        return "landscape"
    if any(term in text for term in ("square", "icon", "profile picture", "头像")):
        return "square"
    if any(term in text for term in ("portrait", "upper body", "cowboy shot", "bust", "close-up", "closeup")):
        return "portrait"
    return "landscape" if multichar else "portrait"


def build_single_agent_plan(entry: PromptEntry) -> dict[str, Any]:
    orientation = _orientation_hint(entry.raw_tags, multichar=False)
    if orientation == "landscape":
        preset_name = "landscape_default_1216x832"
    elif orientation == "square":
        preset_name = "square_1024x1024"
    else:
        preset_name = SINGLE_DEFAULT_RESOLUTION
    return {
        "agent_type": "single",
        "source": "dstill_internal_agent_plan",
        "system_prompt": "SINGLE_AGENT_SYSTEM_PROMPT",
        "base_caption": entry.raw_tags,
        "negative_prompt": "",
        "recommended_canvas": {
            "orientation": orientation,
            "reason": "single-character scene understanding from raw tags",
        },
        "resolution_preset_name": preset_name,
        "notes": ["Single prompt entered agent because mode=all-agent."],
    }


def build_multichar_agent_plan(entry: PromptEntry, fixed_negative_prompt: str) -> dict[str, Any]:
    count = max(2, entry.expected_character_count)
    centers = fallback_centers(count)
    char_captions = []
    for index, center in enumerate(centers):
        kind = _kind_for_index(entry.raw_tags, index, count)
        char_captions.append(
            {
                "id": index + 1,
                "label": f"character {index + 1}",
                "center": center,
                "prompt": f"{kind}, unspecified appearance, same scene",
                "negative_prompt": "",
            }
        )

    orientation = _orientation_hint(entry.raw_tags, multichar=True)
    if orientation == "portrait":
        preset_name = "multi_portrait_768x1024"
    elif orientation == "square":
        preset_name = "square_1024x1024"
    else:
        preset_name = MULTI_DEFAULT_TENDENCY[_stable_index(entry.raw_tags, len(MULTI_DEFAULT_TENDENCY))]

    return {
        "agent_type": "multichar",
        "source": "dstill_internal_agent_plan",
        "system_prompt": "MULTICHAR_AGENT_SYSTEM_PROMPT",
        "detected_tags": entry.detected_multichar_tags,
        "expected_character_count": count,
        "global_negative_prompt": fixed_negative_prompt,
        "negative_prompt_policy": "global_negative_only_in_base_caption",
        "llm_result": {
            "detected_character_count": count,
            "recommended_canvas": {
                "orientation": orientation,
                "reason": "multi-character tags require structured V4 prompt",
            },
            "base_caption": entry.raw_tags,
            "char_captions": char_captions,
            "negative_prompt": MULTICHAR_DEFAULT_NEGATIVE_PROMPT,
            "resolution_preset_name": preset_name,
            "notes": [
                "Generated without an external LLM call; character captions are conservative placeholders.",
                "The caller agent may refine raw .txt tags before running this CLI if stronger per-character separation is needed.",
            ],
        },
    }


def parse_multichar_agent_plan(entry: PromptEntry, agent_plan: dict[str, Any]) -> MultiCharPrompt:
    llm_result = agent_plan.get("llm_result")
    if not isinstance(llm_result, dict):
        raise ValueError("agent_plan.llm_result is missing or not an object")

    base_caption = str(llm_result.get("base_caption") or "").strip()
    if not base_caption:
        raise ValueError("agent_plan.llm_result.base_caption is empty")

    raw_chars = llm_result.get("char_captions")
    if not isinstance(raw_chars, list) or not raw_chars:
        raise ValueError("agent_plan.llm_result.char_captions is empty or invalid")

    expected = max(2, entry.expected_character_count)
    if len(raw_chars) != expected:
        raise ValueError(f"agent char_captions length {len(raw_chars)} != expected {expected}")

    char_captions: list[dict[str, Any]] = []
    global_negative_prompt = str(agent_plan.get("global_negative_prompt") or "").strip()
    for index, raw_char in enumerate(raw_chars):
        if not isinstance(raw_char, dict):
            raise ValueError(f"agent char_captions[{index + 1}] is not an object")
        prompt = str(raw_char.get("prompt") or "").strip()
        if _is_short_or_empty_character_prompt(prompt):
            raise ValueError(f"agent char_captions[{index + 1}].prompt is missing or too short")
        char_negative_prompt = str(raw_char.get("negative_prompt") or "").strip()
        if char_negative_prompt and global_negative_prompt and char_negative_prompt == global_negative_prompt:
            char_negative_prompt = ""
            notes = agent_plan.setdefault("notes", [])
            if isinstance(notes, list):
                notes.append(
                    f"Cleared char_captions[{index + 1}].negative_prompt because it duplicated global_negative_prompt."
                )
        char_captions.append(
            {
                "id": raw_char.get("id", index + 1),
                "label": str(raw_char.get("label") or f"character {index + 1}").strip(),
                "center": validate_center(raw_char.get("center"), index, len(raw_chars)),
                "prompt": prompt,
                "negative_prompt": char_negative_prompt,
            }
        )

    canvas = llm_result.get("recommended_canvas")
    orientation = None
    if isinstance(canvas, dict):
        raw_orientation = str(canvas.get("orientation") or "").lower()
        if raw_orientation in {"portrait", "landscape", "square"}:
            orientation = raw_orientation

    return MultiCharPrompt(
        raw_prompt=entry.raw_tags,
        base_caption=base_caption,
        negative_prompt=str(llm_result.get("negative_prompt") or "").strip(),
        char_captions=char_captions,
        source=agent_plan,
        canvas_orientation=orientation,
    )


###############################################################################
# External prompt-refinement agent adapters and cache.
###############################################################################


@dataclass(frozen=True)
class AgentRuntimeConfig:
    provider: Literal["external"]
    api_format: Literal["openai", "gemini"]
    base_url: str
    model: str
    api_key_env: str
    timeout_seconds: float = 60.0
    max_retries: int = 3
    temperature: float = 0.2
    cache_enabled: bool = True
    cache_dir: Path | None = None
    cache_file: Path | None = None
    failure_policy: Literal["skip", "abort", "fallback-internal"] = "skip"
    run_in_dry_run: bool = False


class AgentError(RuntimeError):
    """External prompt-refinement agent failed."""


class AgentNonRetryableError(AgentError):
    """Deterministic agent failure (content filter, blocked prompt, client error).

    Retrying the identical request will not help and only burns quota/tokens, so
    the retry loop must propagate this immediately instead of looping.
    """


class AgentKeyMissingError(AgentError):
    """External agent API key was not supplied via the configured environment variable."""


class AgentValidationError(AgentError):
    """External agent returned a plan that failed local validation."""


def bool_from_config(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def normalize_agent_base_url(api_format: str, base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    if api_format != "openai":
        return normalized

    parts = urlsplit(normalized)
    if not parts.scheme or not parts.netloc:
        return normalized

    path = parts.path.rstrip("/")
    chat_suffix = "/chat/completions"
    if path.lower().endswith(chat_suffix):
        path = path[: -len(chat_suffix)].rstrip("/")
    if not path:
        path = "/v1"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def resolve_agent_runtime_config(args: argparse.Namespace, config: dict[str, Any]) -> AgentRuntimeConfig:
    agent_config = config.get("agent")
    if not isinstance(agent_config, dict):
        agent_config = {}

    provider = str(getattr(args, "agent_provider", None) or "external").strip().lower()
    if provider not in SUPPORTED_AGENT_PROVIDERS:
        raise ValueError("--agent-provider must be external")

    api_format = str(getattr(args, "agent_api_format", None) or agent_config.get("api_format") or "").strip().lower()
    if not api_format:
        raise ValueError("--agent-api-format is required")
    if api_format not in SUPPORTED_AGENT_API_FORMATS:
        raise ValueError("--agent-api-format must be one of openai, gemini")

    base_url = str(getattr(args, "agent_base_url", None) or agent_config.get("base_url") or "").strip()
    if not base_url:
        raise ValueError("--agent-base-url is required")

    model = str(getattr(args, "agent_model", None) or agent_config.get("model") or "").strip()
    if not model:
        raise ValueError("--agent-model is required")

    api_key_env = str(getattr(args, "agent_api_key_env", None) or agent_config.get("api_key_env") or "").strip()
    if not api_key_env:
        raise ValueError("--agent-api-key-env is required")

    cache_dir_raw = getattr(args, "agent_cache_dir", None) or agent_config.get("cache_dir") or ""
    cache_file_raw = getattr(args, "agent_cache_file", None) or ""
    max_retries = int(agent_config.get("max_retries") or 3)
    timeout_seconds = float(agent_config.get("timeout_seconds") or 60)
    temperature = float(agent_config.get("temperature") if agent_config.get("temperature") is not None else 0.2)

    return AgentRuntimeConfig(
        provider=provider,  # type: ignore[arg-type]
        api_format=api_format,  # type: ignore[arg-type]
        base_url=normalize_agent_base_url(api_format, base_url),
        model=model,
        api_key_env=api_key_env,
        timeout_seconds=timeout_seconds,
        max_retries=max(1, max_retries),
        temperature=temperature,
        cache_enabled=bool_from_config(agent_config.get("cache_enabled"), True),
        cache_dir=Path(cache_dir_raw) if str(cache_dir_raw).strip() else None,
        cache_file=Path(cache_file_raw) if str(cache_file_raw).strip() else None,
        failure_policy=getattr(args, "agent_failure_policy", "skip"),
        run_in_dry_run=bool(getattr(args, "agent_run_in_dry_run", False)),
    )


def should_use_agent_for_entry(entry: PromptEntry, mode: Literal["auto-multichar", "all-agent"]) -> bool:
    return mode == "all-agent" or (mode == "auto-multichar" and bool(entry.detected_multichar_tags))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def agent_base_url_hash(base_url: str) -> str:
    return sha256_text(base_url.rstrip("/"))[:16]


def agent_cache_key_payload(
    entry: PromptEntry,
    *,
    mode: str,
    agent_config: AgentRuntimeConfig,
    fixed_negative_prompt: str,
) -> dict[str, Any]:
    return {
        "raw_tags_sha256": sha256_text(entry.raw_tags),
        "source_prompt_index": entry.source_prompt_index,
        "mode": mode,
        "agent_api_format": agent_config.api_format,
        "agent_base_url_hash": agent_base_url_hash(agent_config.base_url),
        "agent_model": agent_config.model,
        "agent_prompt_version": AGENT_PROMPT_VERSION,
        "fixed_negative_prompt_sha256": sha256_text(fixed_negative_prompt),
    }


def agent_cache_key(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_text(encoded)


def default_agent_cache_file_for_entry(entry: PromptEntry, agent_config: AgentRuntimeConfig) -> Path:
    if agent_config.cache_file is not None:
        return agent_config.cache_file
    if agent_config.cache_dir is not None:
        if entry.source_prompt_index is not None:
            stem = safe_stem(entry.source_path.stem)
        else:
            stem = safe_stem(str(entry.relative_path.with_suffix("")))
        return agent_config.cache_dir / f"{stem}.agent_cache.jsonl"
    return entry.source_path.with_suffix(".agent_cache.jsonl")


def load_agent_cache(path: Path, key: str) -> dict[str, Any] | None:
    if not path.exists() or not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict) and record.get("cache_key") == key and isinstance(record.get("agent_plan"), dict):
            return record["agent_plan"]
    return None


def append_agent_cache(path: Path, record: dict[str, Any], *, token: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(scrub_secrets(record, token), ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("ab") as file:
        file.write(data.encode("utf-8"))
        file.flush()
        os.fsync(file.fileno())


def agent_plan_schema(*, multichar: bool) -> dict[str, Any]:
    resolution_names = [item.name for item in BUILTIN_RESOLUTION_PRESETS]
    char_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "label": {"type": "string"},
            "center": {
                "type": "array",
                "items": {"type": "number"},
                "minItems": 2,
                "maxItems": 2,
            },
            "prompt": {"type": "string"},
            "negative_prompt": {"type": "string"},
        },
        "required": ["id", "label", "center", "prompt", "negative_prompt"],
        "additionalProperties": True,
    }
    llm_result_schema = {
        "type": "object",
        "properties": {
            "detected_character_count": {"type": "integer"},
            "recommended_canvas": {
                "type": "object",
                "properties": {
                    "orientation": {"type": "string", "enum": ["portrait", "landscape", "square"]},
                    "reason": {"type": "string"},
                },
                "additionalProperties": True,
            },
            "base_caption": {"type": "string"},
            "char_captions": {"type": "array", "items": char_schema},
            "negative_prompt": {"type": "string"},
            "resolution_preset_name": {"type": "string", "enum": resolution_names},
            "dropped_tags": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["base_caption", "char_captions", "negative_prompt", "resolution_preset_name"],
        "additionalProperties": True,
    }
    properties = {
        "agent_type": {"type": "string", "enum": ["single", "multichar"]},
        "source": {"type": "string"},
        "base_caption": {"type": "string"},
        "negative_prompt": {"type": "string"},
        "global_negative_prompt": {"type": "string"},
        "negative_prompt_policy": {"type": "string"},
        "recommended_canvas": {
            "type": "object",
            "properties": {
                "orientation": {"type": "string", "enum": ["portrait", "landscape", "square"]},
                "reason": {"type": "string"},
            },
            "additionalProperties": True,
        },
        "resolution_preset_name": {"type": "string", "enum": resolution_names},
        "llm_result": llm_result_schema,
        "notes": {"type": "array", "items": {"type": "string"}},
    }
    required = ["agent_type", "llm_result"] if multichar else ["agent_type", "resolution_preset_name"]
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": True,
    }


def build_agent_user_payload(
    entry: PromptEntry,
    *,
    mode: str,
    fixed_negative_prompt: str,
    multichar: bool,
) -> dict[str, Any]:
    return {
        "task": "Return one canonical Dstill NovelAI agent plan JSON object. Do not return Markdown.",
        "agent_prompt_version": AGENT_PROMPT_VERSION,
        "mode": mode,
        "agent_type": "multichar" if multichar else "single",
        "raw_tags": entry.raw_tags,
        "detected_multichar_tags": entry.detected_multichar_tags,
        "expected_character_count": max(2, entry.expected_character_count) if multichar else 1,
        "fixed_negative_prompt": fixed_negative_prompt,
        "allowed_resolution_presets": [preset.snapshot() for preset in BUILTIN_RESOLUTION_PRESETS],
        "output_contract": {
            "single": {
                "agent_type": "single",
                "resolution_preset_name": "one allowed preset name",
                "base_caption": "copy raw_tags unchanged unless only adding explanatory fields",
            },
            "multichar": {
                "agent_type": "multichar",
                "global_negative_prompt": "copy fixed_negative_prompt",
                "llm_result": {
                    "detected_character_count": "must equal expected_character_count",
                    "base_caption": "the single subject count tag (e.g. 2girls) plus global/shared content only: scene, composition, lighting, quality/style, attributes shared by all characters; move per-character attributes into char_captions; drop solo/solo focus and count tags that contradict expected_character_count",
                    "char_captions": "one item per character ordered left-to-right by center.x; prompt starts with girl/boy/other WITHOUT a number and holds that character's own appearance/name/clothes; interaction action tags use source#/target#/mutual# prefixes; negative_prompt is usually an empty string, only 1-3 distinguishing tags of OTHER characters when traits strongly conflict",
                    "negative_prompt": f"global multichar negative prompt, normally include: {MULTICHAR_DEFAULT_NEGATIVE_PROMPT}",
                    "resolution_preset_name": "one allowed preset name",
                    "dropped_tags": "tags removed during normalization, e.g. solo when rendering multiple characters",
                },
            },
        },
    }


def parse_json_from_model_text(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise AgentError("external agent returned non-JSON content") from exc
    if not isinstance(parsed, dict):
        raise AgentError("external agent JSON must be an object")
    if isinstance(parsed.get("agent_plan"), dict):
        parsed = parsed["agent_plan"]
    return parsed


def extract_openai_message_text(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AgentError("OpenAI response missing choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise AgentError("OpenAI response missing message")
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for part in content:
            if isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    pieces.append(text)
        if pieces:
            return "\n".join(pieces)
    raise AgentError("OpenAI response message content is empty")


def extract_gemini_message_text(data: dict[str, Any]) -> str:
    candidates = data.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise AgentError("Gemini response missing candidates")
    content = candidates[0].get("content") if isinstance(candidates[0], dict) else None
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list):
        raise AgentError("Gemini response missing content parts")
    pieces = [part.get("text") for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str)]
    if not pieces:
        raise AgentError("Gemini response text is empty")
    return "\n".join(pieces)


def normalize_external_agent_plan(
    raw_plan: dict[str, Any],
    entry: PromptEntry,
    *,
    agent_config: AgentRuntimeConfig,
    fixed_negative_prompt: str,
    multichar: bool,
) -> dict[str, Any]:
    plan = copy.deepcopy(raw_plan)
    plan["agent_type"] = "multichar" if multichar else "single"
    plan["source"] = "external_agent"
    plan["agent_prompt_version"] = AGENT_PROMPT_VERSION
    plan["agent_provider"] = {
        "api_format": agent_config.api_format,
        "base_url_hash": agent_base_url_hash(agent_config.base_url),
        "model": agent_config.model,
    }

    if multichar:
        if not isinstance(plan.get("llm_result"), dict):
            llm_fields = {
                key: plan.pop(key)
                for key in list(plan.keys())
                if key
                in {
                    "detected_character_count",
                    "recommended_canvas",
                    "base_caption",
                    "char_captions",
                    "negative_prompt",
                    "resolution_preset_name",
                    "notes",
                }
            }
            plan["llm_result"] = llm_fields
        llm_result = plan["llm_result"]
        if isinstance(llm_result, dict):
            llm_result.setdefault("detected_character_count", max(2, entry.expected_character_count))
            llm_result.setdefault("negative_prompt", MULTICHAR_DEFAULT_NEGATIVE_PROMPT)
        plan.setdefault("detected_tags", entry.detected_multichar_tags)
        plan.setdefault("expected_character_count", max(2, entry.expected_character_count))
        plan.setdefault("global_negative_prompt", fixed_negative_prompt)
        plan.setdefault("negative_prompt_policy", "global_negative_only_in_base_caption")
    else:
        if not plan.get("resolution_preset_name") and isinstance(plan.get("llm_result"), dict):
            plan["resolution_preset_name"] = str(plan["llm_result"].get("resolution_preset_name") or "")
        plan.setdefault("base_caption", entry.raw_tags)
        plan.setdefault("negative_prompt", "")
    return plan


def validate_agent_plan_for_entry(
    entry: PromptEntry,
    agent_plan: dict[str, Any],
    *,
    multichar: bool,
) -> None:
    try:
        if multichar:
            parsed = parse_multichar_agent_plan(entry, agent_plan)
            choose_resolution_name_from_agent_plan(agent_plan)
            if len(parsed.char_captions) != max(2, entry.expected_character_count):
                raise ValueError("external agent returned wrong character count")
        else:
            choose_resolution_name_from_agent_plan(agent_plan)
    except AgentValidationError:
        raise
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        raise AgentValidationError(f"external agent validation failed: {message}") from exc


def call_openai_agent(
    entry: PromptEntry,
    *,
    agent_config: AgentRuntimeConfig,
    api_key: str,
    mode: str,
    fixed_negative_prompt: str,
    multichar: bool,
) -> dict[str, Any]:
    try:
        import httpx
    except Exception as exc:  # pragma: no cover - environment-specific.
        raise AgentError("httpx is required for external OpenAI-format agent") from exc

    system_prompt = MULTICHAR_AGENT_SYSTEM_PROMPT if multichar else SINGLE_AGENT_SYSTEM_PROMPT
    payload = build_agent_user_payload(
        entry,
        mode=mode,
        fixed_negative_prompt=fixed_negative_prompt,
        multichar=multichar,
    )
    auth = api_key.strip()
    if not auth.lower().startswith("bearer "):
        auth = f"Bearer {auth}"
    request_json = {
        "model": agent_config.model,
        "temperature": agent_config.temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "dstill_agent_plan",
                "strict": False,
                "schema": agent_plan_schema(multichar=multichar),
            },
        },
    }
    url = f"{agent_config.base_url.rstrip('/')}/chat/completions"
    try:
        response = httpx.post(
            url,
            json=request_json,
            headers={"Authorization": auth, "Content-Type": "application/json"},
            timeout=agent_config.timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        raise AgentError("external OpenAI-format agent timeout") from exc
    except httpx.HTTPError as exc:
        raise AgentError(mask_secret_text(str(exc), api_key)) from exc
    if response.status_code >= 400:
        detail = mask_secret_text(response.text[:500] if response.content else "", api_key)
        message = f"external OpenAI-format agent rejected request {response.status_code}: {detail}"
        if response.status_code in (400, 401, 403, 404):
            raise AgentNonRetryableError(message)
        raise AgentError(message)
    try:
        data = response.json()
    except Exception as exc:
        raise AgentError("external OpenAI-format agent returned invalid JSON response") from exc
    choice0 = data.get("choices")[0] if isinstance(data.get("choices"), list) and data.get("choices") else None
    finish_reason = choice0.get("finish_reason") if isinstance(choice0, dict) else None
    if finish_reason == "content_filter":
        raise AgentNonRetryableError(
            "external OpenAI-format agent response blocked (finish_reason=content_filter, empty content)"
        )
    try:
        message_text = extract_openai_message_text(data)
    except AgentError as exc:
        if finish_reason in ("content_filter", "safety"):
            raise AgentNonRetryableError(
                f"external OpenAI-format agent returned no usable content (finish_reason={finish_reason})"
            ) from exc
        raise
    parsed = parse_json_from_model_text(message_text)
    return normalize_external_agent_plan(
        parsed,
        entry,
        agent_config=agent_config,
        fixed_negative_prompt=fixed_negative_prompt,
        multichar=multichar,
    )


def call_gemini_agent(
    entry: PromptEntry,
    *,
    agent_config: AgentRuntimeConfig,
    api_key: str,
    mode: str,
    fixed_negative_prompt: str,
    multichar: bool,
) -> dict[str, Any]:
    try:
        import httpx
    except Exception as exc:  # pragma: no cover - environment-specific.
        raise AgentError("httpx is required for external Gemini agent") from exc

    system_prompt = MULTICHAR_AGENT_SYSTEM_PROMPT if multichar else SINGLE_AGENT_SYSTEM_PROMPT
    payload = build_agent_user_payload(
        entry,
        mode=mode,
        fixed_negative_prompt=fixed_negative_prompt,
        multichar=multichar,
    )
    request_json = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [
            {
                "role": "user",
                "parts": [{"text": json.dumps(payload, ensure_ascii=False)}],
            }
        ],
        "generationConfig": {
            "temperature": agent_config.temperature,
            "responseMimeType": "application/json",
        },
    }
    url = f"{agent_config.base_url.rstrip('/')}/models/{agent_config.model}:generateContent"
    try:
        response = httpx.post(
            url,
            params={"key": api_key},
            json=request_json,
            timeout=agent_config.timeout_seconds,
        )
    except httpx.TimeoutException as exc:
        raise AgentError("external Gemini agent timeout") from exc
    except httpx.HTTPError as exc:
        raise AgentError(mask_secret_text(str(exc), api_key)) from exc
    if response.status_code >= 400:
        detail = mask_secret_text(response.text[:500] if response.content else "", api_key)
        message = f"external Gemini agent rejected request {response.status_code}: {detail}"
        if response.status_code in (400, 401, 403, 404):
            raise AgentNonRetryableError(message)
        raise AgentError(message)
    try:
        data = response.json()
    except Exception as exc:
        raise AgentError("external Gemini agent returned invalid JSON response") from exc
    block_reason = None
    prompt_feedback = data.get("promptFeedback")
    if isinstance(prompt_feedback, dict):
        block_reason = prompt_feedback.get("blockReason")
    candidate0 = data.get("candidates")[0] if isinstance(data.get("candidates"), list) and data.get("candidates") else None
    finish_reason = candidate0.get("finishReason") if isinstance(candidate0, dict) else None
    blocking = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "RECITATION", "SPII"}
    if block_reason or (finish_reason in blocking):
        raise AgentNonRetryableError(
            f"external Gemini agent response blocked (blockReason={block_reason}, finishReason={finish_reason})"
        )
    try:
        message_text = extract_gemini_message_text(data)
    except AgentError as exc:
        if finish_reason in blocking or block_reason:
            raise AgentNonRetryableError(
                f"external Gemini agent returned no usable content (finishReason={finish_reason})"
            ) from exc
        raise
    parsed = parse_json_from_model_text(message_text)
    return normalize_external_agent_plan(
        parsed,
        entry,
        agent_config=agent_config,
        fixed_negative_prompt=fixed_negative_prompt,
        multichar=multichar,
    )


def call_external_agent_once(
    entry: PromptEntry,
    *,
    agent_config: AgentRuntimeConfig,
    api_key: str,
    mode: str,
    fixed_negative_prompt: str,
    multichar: bool,
) -> dict[str, Any]:
    if agent_config.api_format == "gemini":
        return call_gemini_agent(
            entry,
            agent_config=agent_config,
            api_key=api_key,
            mode=mode,
            fixed_negative_prompt=fixed_negative_prompt,
            multichar=multichar,
        )
    return call_openai_agent(
        entry,
        agent_config=agent_config,
        api_key=api_key,
        mode=mode,
        fixed_negative_prompt=fixed_negative_prompt,
        multichar=multichar,
    )


def resolve_external_agent_plan(
    entry: PromptEntry,
    *,
    mode: Literal["auto-multichar", "all-agent"],
    fixed_negative_prompt: str,
    agent_config: AgentRuntimeConfig,
    dry_run: bool,
    force_refresh: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return an external agent plan override plus a small non-secret status.

    ``force_refresh`` bypasses the cache *read* (a fresh plan is still written
    back), so the manual review UI can re-plan a prompt without changing tags.
    """
    multichar = bool(entry.detected_multichar_tags)
    if not should_use_agent_for_entry(entry, mode):
        return None, {"agent_needed": False, "agent_source": "not-needed"}

    cache_payload = agent_cache_key_payload(
        entry,
        mode=mode,
        agent_config=agent_config,
        fixed_negative_prompt=fixed_negative_prompt,
    )
    cache_key = agent_cache_key(cache_payload)
    cache_file = default_agent_cache_file_for_entry(entry, agent_config)
    if agent_config.cache_enabled and not force_refresh:
        cached = load_agent_cache(cache_file, cache_key)
        if cached is not None:
            try:
                validate_agent_plan_for_entry(entry, cached, multichar=multichar)
                return cached, {
                    "agent_needed": True,
                    "agent_source": "cache",
                    "cache_file": str(cache_file),
                    "cache_key": cache_key,
                }
            except AgentValidationError:
                # Cache files may contain stale/bad plans from an older agent
                # prompt.  Treat a bad cache hit as a miss so a fresh external
                # plan can repair the batch instead of aborting it.
                pass

    if dry_run and not agent_config.run_in_dry_run:
        return None, {
            "agent_needed": True,
            "agent_source": "internal",
            "reason": "dry_run_external_agent_disabled",
            "cache_file": str(cache_file),
            "cache_key": cache_key,
        }

    api_key = os.getenv(agent_config.api_key_env)
    if not api_key:
        raise AgentKeyMissingError(f"external agent API key environment variable is not set: {agent_config.api_key_env}")

    last_error: Exception | None = None
    for _attempt in range(1, agent_config.max_retries + 1):
        try:
            plan = call_external_agent_once(
                entry,
                agent_config=agent_config,
                api_key=api_key,
                mode=mode,
                fixed_negative_prompt=fixed_negative_prompt,
                multichar=multichar,
            )
            validate_agent_plan_for_entry(entry, plan, multichar=multichar)
            if agent_config.cache_enabled:
                append_agent_cache(
                    cache_file,
                    {
                        "kind": "dstill_external_agent_cache",
                        "cache_key": cache_key,
                        "key": cache_payload,
                        "agent_plan": plan,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                    token=api_key,
                )
            return plan, {
                "agent_needed": True,
                "agent_source": "external",
                "cache_file": str(cache_file),
                "cache_key": cache_key,
            }
        except AgentNonRetryableError:
            # Deterministic failure (content filter, blocked prompt, 4xx): retrying
            # the identical request cannot succeed and only burns quota/tokens.
            raise
        except Exception as exc:  # Keep retry boundary around request + validation.
            last_error = exc
    message = mask_secret_text(str(last_error or "external agent failed"), api_key)
    if isinstance(last_error, AgentValidationError):
        raise AgentValidationError(message) from last_error
    raise AgentError(message) from last_error


def multichar_v4_extra_options(parsed: MultiCharPrompt) -> dict[str, Any]:
    char_captions = [
        {
            "char_caption": char["prompt"],
            "centers": [{"x": char["center"][0], "y": char["center"][1]}],
        }
        for char in parsed.char_captions
    ]
    negative_char_captions = [
        {
            "char_caption": char["negative_prompt"],
            "centers": [{"x": char["center"][0], "y": char["center"][1]}],
        }
        for char in parsed.char_captions
    ]
    return {
        "v4_prompt": {
            "caption": {
                "base_caption": parsed.base_caption,
                "char_captions": char_captions,
            },
            "use_coords": True,
            "use_order": True,
        },
        "v4_negative_prompt": {
            "caption": {
                "base_caption": parsed.negative_prompt,
                "char_captions": negative_char_captions,
            },
            "use_coords": True,
            "use_order": True,
            "legacy_uc": False,
        },
    }


def build_resolution_plan(
    *,
    entry: PromptEntry,
    agent_used: bool,
    agent_plan: dict[str, Any],
    multichar_detected: bool,
    resolution_preset_name: str | None = None,
) -> dict[str, Any]:
    if resolution_preset_name:
        if resolution_preset_name not in PRESETS_BY_NAME:
            raise ValueError(f"unknown resolution preset override: {resolution_preset_name}")
        selected_name = resolution_preset_name
        reason = "user override from --resolution-preset"
    elif agent_used:
        selected_name = choose_resolution_name_from_agent_plan(agent_plan)
        reason = "selected by agent from built-in fixed pool"
    else:
        selected_name = SINGLE_DEFAULT_RESOLUTION
        reason = "auto-multichar single prompt bypassed agent; using single portrait default"
    preset = PRESETS_BY_NAME[selected_name]
    return {
        "selected_name": preset.name,
        "width": preset.width,
        "height": preset.height,
        "orientation": preset.orientation,
        "reason": reason,
        "multichar_detected": multichar_detected,
        "allowed_pool": [item.name for item in BUILTIN_RESOLUTION_PRESETS],
        "source_prompt_file": str(entry.source_path),
    }


def prepare_prompt(
    entry: PromptEntry,
    *,
    mode: Literal["auto-multichar", "all-agent"],
    positive_prefix_mode: Literal["none", "preset", "custom"],
    positive_prefix: str,
    negative_prompt_mode: Literal["none", "preset", "custom"],
    negative_uc_preset: int | None,
    fixed_negative_prompt: str,
    model: str = "nai-diffusion-4-5-full",
    sampler: str = "k_euler",
    steps: int = 28,
    scale: float = 5.0,
    cfg_rescale: float | None = None,
    noise_schedule: str | None = None,
    seed: int | None = None,
    resolution_preset_name: str | None = None,
    n_samples: int = 1,
    agent_plan_override: dict[str, Any] | None = None,
    agent_status: dict[str, Any] | None = None,
) -> PreparedPrompt:
    multichar_detected = bool(entry.detected_multichar_tags)
    agent_used = mode == "all-agent" or (mode == "auto-multichar" and multichar_detected)

    multichar_prompt: MultiCharPrompt | None = None
    if agent_plan_override is not None:
        if not agent_used:
            raise ValueError("agent_plan_override was provided for a prompt that does not use agent mode")
        agent_plan = copy.deepcopy(agent_plan_override)
        if multichar_detected:
            multichar_prompt = parse_multichar_agent_plan(entry, agent_plan)
    elif agent_used and multichar_detected:
        agent_plan = build_multichar_agent_plan(entry, fixed_negative_prompt)
        multichar_prompt = parse_multichar_agent_plan(entry, agent_plan)
    elif agent_used:
        agent_plan = build_single_agent_plan(entry)
    else:
        agent_plan = {}

    resolution_plan = build_resolution_plan(
        entry=entry,
        agent_used=agent_used,
        agent_plan=agent_plan,
        multichar_detected=multichar_detected,
        resolution_preset_name=resolution_preset_name,
    )

    request_seed = seed if seed is not None else random.SystemRandom().randrange(0, 2**32)
    if multichar_prompt is None:
        positive, negative = compose_prompt(
            entry.raw_tags,
            positive_prefix,
            fixed_negative_prompt,
            dedupe_positive=False,
        )
        request = GenerationRequest(
            prompt=positive,
            negative_prompt=negative,
            model=model,
            width=resolution_plan["width"],
            height=resolution_plan["height"],
            steps=steps,
            prompt_guidance=scale,
            prompt_guidance_rescale=cfg_rescale,
            sampler=sampler,
            noise_schedule=noise_schedule,
            seed=request_seed,
            n_samples=n_samples,
            quality_toggle=False,
            uc_preset=0,
        )
    else:
        base_caption, _ = compose_prompt(
            multichar_prompt.base_caption,
            positive_prefix,
            None,
            dedupe_positive=False,
        )
        negative = merge_prompt_tags(
            fixed_negative_prompt,
            multichar_prompt.negative_prompt,
            MULTICHAR_DEFAULT_NEGATIVE_PROMPT,
            dedupe=True,
        )
        effective_multichar = MultiCharPrompt(
            raw_prompt=multichar_prompt.raw_prompt,
            base_caption=base_caption,
            negative_prompt=negative or "",
            char_captions=multichar_prompt.char_captions,
            source=multichar_prompt.source,
            canvas_orientation=multichar_prompt.canvas_orientation,
        )
        request = GenerationRequest(
            prompt=base_caption,
            negative_prompt=negative,
            model=model,
            width=resolution_plan["width"],
            height=resolution_plan["height"],
            steps=steps,
            prompt_guidance=scale,
            prompt_guidance_rescale=cfg_rescale,
            sampler=sampler,
            noise_schedule=noise_schedule,
            seed=request_seed,
            n_samples=n_samples,
            quality_toggle=False,
            uc_preset=0,
            extra_options=multichar_v4_extra_options(effective_multichar),
        )
        multichar_prompt = effective_multichar

    validate_generation_request(request)
    training_caption = build_training_caption(entry, request, multichar_prompt, positive_prefix)
    return PreparedPrompt(
        entry=entry,
        mode=mode,
        positive_prefix_mode=positive_prefix_mode,
        positive_prefix=positive_prefix,
        negative_prompt_mode=negative_prompt_mode,
        negative_uc_preset=negative_uc_preset,
        fixed_negative_prompt=fixed_negative_prompt,
        multichar_detected=multichar_detected,
        agent_used=agent_used,
        agent_plan=agent_plan,
        resolution_plan=resolution_plan,
        multichar_prompt=multichar_prompt,
        request=request,
        training_caption=training_caption,
        agent_status=agent_status or {},
    )


###############################################################################
# NovelAI payload and response parsing.
###############################################################################


class ProviderError(RuntimeError):
    code = "provider_error"


class ProviderAuthError(ProviderError):
    code = "provider_auth_error"


class ProviderValidationError(ProviderError):
    code = "provider_validation_error"


class ProviderResponseError(ProviderError):
    code = "provider_response_error"


class ProviderTimeoutError(ProviderError):
    code = "provider_timeout"


def _join_prompt_parts(*parts: str | None) -> str:
    cleaned = [p.strip(" \t\r\n,") for p in parts if p and p.strip(" \t\r\n,")]
    return ", ".join(cleaned)


def effective_prompt_pair(request: GenerationRequest) -> tuple[str, str]:
    prompt = request.prompt
    negative = request.negative_prompt or ""
    if request.quality_toggle:
        prompt = _join_prompt_parts(prompt, QUALITY_TAGS_BY_MODEL.get(request.model))
    if request.uc_preset:
        preset = UC_PRESET_TAGS_BY_MODEL.get(request.model, {}).get(request.uc_preset)
        negative = _join_prompt_parts(negative, preset)
    return prompt, negative


def build_vibe_transfer_params(request: GenerationRequest) -> dict[str, Any]:
    refs = [v for v in (request.vibe_transfer or []) if isinstance(v, dict) and v.get("reference_image")]
    if not refs:
        return {}
    return {
        "reference_image_multiple": [v["reference_image"] for v in refs],
        "reference_strength_multiple": [v.get("reference_strength", 0.6) for v in refs],
        "reference_information_extracted_multiple": [v.get("information_extracted", 1.0) for v in refs],
        "normalize_reference_strength_multiple": True,
    }


def build_precise_reference_params(request: GenerationRequest) -> dict[str, Any]:
    pr = request.precise_reference
    if not pr or not pr.get("reference_image"):
        return {}
    fidelity = max(0.0, min(1.0, float(pr.get("fidelity", 1.0))))
    mode = pr.get("mode") or "character&style"
    return {
        "director_reference_images": [pr["reference_image"]],
        "director_reference_descriptions": [
            {
                "use_coords": False,
                "use_order": False,
                "legacy_uc": False,
                "caption": {"base_caption": mode, "char_captions": []},
            }
        ],
        "director_reference_strength_values": [pr.get("reference_strength", 1.0)],
        "director_reference_secondary_strength_values": [1.0 - fidelity],
        "director_reference_information_extracted": [pr.get("reference_information_extracted", 1.0)],
    }


def build_payload(request: GenerationRequest) -> dict[str, Any]:
    cfg_rescale = request.prompt_guidance_rescale
    if cfg_rescale is None:
        cfg_rescale = WEB_COMPAT_DEFAULTS["cfg_rescale"]
    prompt, negative_prompt = effective_prompt_pair(request)
    params: dict[str, Any] = {
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "width": request.width,
        "height": request.height,
        "steps": request.steps,
        "scale": request.prompt_guidance,
        "cfg_rescale": cfg_rescale,
        "sampler": request.sampler,
        "noise_schedule": request.noise_schedule,
        "seed": request.seed,
        "n_samples": request.n_samples,
        "image_format": request.image_format,
        "quality_toggle": request.quality_toggle,
        "uc_preset": request.uc_preset,
    }
    for field_name in FIRST_CLASS_PARAMETER_FIELDS:
        params[field_name] = getattr(request, field_name)

    out: dict[str, Any] = {}
    for key, value in params.items():
        if value is not None:
            out[FIELD_MAP.get(key, key)] = value

    for key, value in WEB_COMPAT_DEFAULTS.items():
        out.setdefault(key, copy.deepcopy(value))

    if request.model.startswith("nai-diffusion-4"):
        out.setdefault("params_version", 3)
        out.setdefault("legacy", False)
        out.setdefault("prefer_brownian", True)
        out.setdefault(
            "v4_prompt",
            {
                "caption": {"base_caption": prompt, "char_captions": []},
                "use_coords": False,
                "use_order": True,
            },
        )
        out.setdefault(
            "v4_negative_prompt",
            {
                "caption": {"base_caption": negative_prompt, "char_captions": []},
                "use_coords": False,
                "use_order": False,
                "legacy_uc": False,
            },
        )

    out.update(build_vibe_transfer_params(request))
    out.update(build_precise_reference_params(request))
    extra_options = {key: value for key, value in (request.extra_options or {}).items() if value is not None}
    structured_v4_prompt = extra_options.pop("v4_prompt", None)
    structured_v4_negative_prompt = extra_options.pop("v4_negative_prompt", None)
    extra_options.pop("characterPrompts", None)
    extra_options.pop("use_coords", None)
    extra_options.pop("use_order", None)
    action = extra_options.pop("action", request.action)
    out.update(extra_options)
    if structured_v4_prompt is not None:
        out["v4_prompt"] = structured_v4_prompt
    if structured_v4_negative_prompt is not None:
        out["v4_negative_prompt"] = structured_v4_negative_prompt
    return {"action": action, "input": prompt, "model": request.model, "parameters": out}


def validate_generation_request(request: GenerationRequest) -> None:
    if not request.prompt.strip():
        raise ProviderValidationError("prompt is empty")
    if request.model not in SUPPORTED_IMAGE_MODELS:
        raise ProviderValidationError(f"unsupported NovelAI image model: {request.model}")
    if request.sampler not in SUPPORTED_SAMPLERS:
        raise ProviderValidationError(f"unsupported NovelAI sampler: {request.sampler}")
    if request.image_format not in SUPPORTED_IMAGE_FORMATS:
        raise ProviderValidationError(f"unsupported NovelAI image_format: {request.image_format}")
    if request.uc_preset is not None and request.uc_preset not in SUPPORTED_UC_PRESETS:
        raise ProviderValidationError(f"unsupported NovelAI uc_preset: {request.uc_preset}")
    if request.width % 64 != 0 or request.height % 64 != 0:
        raise ProviderValidationError("width and height must be multiples of 64")
    if request.width * request.height > MAX_PIXELS:
        raise ProviderValidationError("width * height must be <= 1024 * 1024")
    if request.steps <= 0:
        raise ProviderValidationError("steps must be positive")
    if not 1 <= request.n_samples <= 4:
        raise ProviderValidationError("n_samples must be in 1..4")


def _jsonable_image_info_value(value: Any) -> Any:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return base64.b64encode(value).decode("ascii")
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _extract_embedded_image_metadata(image_bytes: bytes) -> dict[str, Any]:
    if Image is None:
        return {}
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            info = {key: _jsonable_image_info_value(value) for key, value in img.info.items()}
    except Exception:
        return {}
    meta: dict[str, Any] = {}
    comment = info.pop("Comment", None)
    if isinstance(comment, str) and comment:
        try:
            parsed = json.loads(comment)
            if isinstance(parsed, dict):
                meta.update(parsed)
            else:
                meta["Comment"] = parsed
        except json.JSONDecodeError:
            meta["Comment"] = comment
    if info:
        meta["image_info"] = info
    return meta


def parse_zip_response(content: bytes) -> list[tuple[bytes, dict[str, Any]]]:
    results: list[tuple[bytes, dict[str, Any]]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            meta_by_stem: dict[str, dict[str, Any]] = {}
            for name in zf.namelist():
                if name.lower().endswith(".json"):
                    try:
                        raw = zf.read(name).decode("utf-8")
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            meta_by_stem[name.rsplit(".", 1)[0]] = parsed
                    except Exception:
                        pass
            for name in zf.namelist():
                if name.lower().endswith((".png", ".jpg", ".jpeg", ".webp")):
                    image_bytes = zf.read(name)
                    meta = _extract_embedded_image_metadata(image_bytes)
                    meta.update(meta_by_stem.get(name.rsplit(".", 1)[0], {}))
                    results.append((image_bytes, meta))
    except zipfile.BadZipFile as exc:
        raise ProviderResponseError("invalid zip response") from exc
    if not results:
        raise ProviderResponseError("zip response contains no images")
    return results


###############################################################################
# Metadata, storage, and secret scrubbing.
###############################################################################


SENSITIVE_KEY_RE = re.compile(r"(authorization|bearer|(^|[_-])token($|[_-]))", re.IGNORECASE)
BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
AUTHORIZATION_RE = re.compile(r"Authorization\s*[:=]\s*[^,;\r\n]+", re.IGNORECASE)


def mask_secret_text(text: str, token: str | None = None) -> str:
    masked = AUTHORIZATION_RE.sub("[redacted auth header]", text)
    masked = BEARER_RE.sub("[redacted auth token]", masked)
    if token:
        raw = token.strip()
        if raw:
            masked = masked.replace(raw, "[redacted]")
            if raw.lower().startswith("bearer "):
                masked = masked.replace(raw[7:].strip(), "[redacted]")
    return masked


def scrub_secrets(value: Any, token: str | None = None) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if SENSITIVE_KEY_RE.search(key_str):
                out[key_str] = "[redacted]"
            else:
                out[key_str] = scrub_secrets(item, token)
        return out
    if isinstance(value, list):
        return [scrub_secrets(item, token) for item in value]
    if isinstance(value, str):
        return mask_secret_text(value, token)
    return value


def _atomic_write(path: Path, data: bytes) -> None:
    # The legacy service used a temp-file + os.replace flow.  Some managed
    # Windows sandboxes deny Python's temp unlink/replace operations even
    # inside the writable workspace, so this standalone skill keeps the same
    # single helper but writes directly to the final path.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())


def build_skip_record(
    entry: PromptEntry,
    *,
    phase: Literal["agent", "provider"],
    reason: str,
    error: BaseException | str,
    token: str | None = None,
) -> dict[str, Any]:
    message = str(error)
    record = {
        "source_prompt_file": str(entry.source_path),
        "source_prompt_index": entry.source_prompt_index,
        "source_line_number": entry.source_line_number,
        "phase": phase,
        "reason": reason,
        "error_type": error.__class__.__name__ if isinstance(error, BaseException) else "Error",
        "message": message,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return scrub_secrets(record, token)


def append_skip_jsonl(path: Path, record: dict[str, Any], *, token: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(scrub_secrets(record, token), ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("ab") as file:
        file.write(data.encode("utf-8"))
        file.flush()
        os.fsync(file.fileno())


SkipRecorder = Callable[
    [PromptEntry, Literal["agent", "provider"], str, BaseException | str, str | None],
    dict[str, Any],
]


def call_skip_recorder(
    skip_recorder: SkipRecorder,
    entry: PromptEntry,
    phase: Literal["agent", "provider"],
    reason: str,
    error: BaseException | str,
    token: str | None = None,
) -> dict[str, Any] | None:
    try:
        return skip_recorder(entry, phase, reason, error, token)
    except TypeError as exc:
        try:
            return skip_recorder(entry, phase, reason, error)  # type: ignore[misc,call-arg]
        except TypeError:
            raise exc


def make_skip_recorder(output_dir: Path, skipped_items: list[dict[str, Any]]) -> SkipRecorder:
    skip_path = output_dir / "skipped.jsonl"

    def record_skip(
        entry: PromptEntry,
        phase: Literal["agent", "provider"],
        reason: str,
        error: BaseException | str,
        token: str | None = None,
    ) -> dict[str, Any]:
        record = build_skip_record(entry, phase=phase, reason=reason, error=error, token=token)
        append_skip_jsonl(skip_path, record, token=token)
        skipped_items.append(record)
        return record

    return record_skip


def safe_stem(text: str, fallback: str = "prompt") -> str:
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip())
    stem = stem.strip("._-")
    return (stem or fallback)[:64]


def entry_resume_key(entry: PromptEntry) -> str:
    return hashlib.sha256(entry.raw_tags.encode("utf-8")).hexdigest()


def entry_output_stem(entry: PromptEntry) -> str:
    return f"{safe_stem(entry.relative_path.stem)}_{entry_resume_key(entry)[:8]}"


def build_progress_record(
    entry: PromptEntry,
    *,
    key: str,
    outputs: list[str],
    token: str | None = None,
) -> dict[str, Any]:
    record = {
        "key": key,
        "raw_tags_sha256": key,
        "source_prompt_file": str(entry.source_path),
        "source_prompt_index": entry.source_prompt_index,
        "source_line_number": entry.source_line_number,
        "outputs": outputs,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return scrub_secrets(record, token)


def append_progress_jsonl(path: Path, record: dict[str, Any], *, token: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(scrub_secrets(record, token), ensure_ascii=False, sort_keys=True) + "\n"
    with path.open("ab") as file:
        file.write(data.encode("utf-8"))
        file.flush()
        os.fsync(file.fileno())


def load_done_keys(output_dir: Path) -> set[str]:
    path = output_dir / "progress.jsonl"
    if not path.is_file():
        return set()
    done: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            key = record.get("key")
            if isinstance(key, str) and key:
                done.add(key)
    return done


def image_size(image_bytes: bytes, fallback: tuple[int, int]) -> tuple[int, int]:
    if Image is None:
        return fallback
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            return int(img.width), int(img.height)
    except Exception:
        return fallback


def extension_for_format(image_format: str) -> str:
    lower = image_format.lower()
    if lower in {"webp", "jpg", "jpeg", "png"}:
        return "jpg" if lower == "jpeg" else lower
    return "png"


def build_training_caption(
    entry: PromptEntry,
    request: GenerationRequest,
    multichar_prompt: MultiCharPrompt | None,
    positive_prefix: str,
) -> str:
    effective_positive, _ = effective_prompt_pair(request)
    if multichar_prompt is not None:
        pieces = [effective_positive, *(char["prompt"] for char in multichar_prompt.char_captions)]
        return merge_prompt_tags(*pieces, dedupe=False) or ""
    if positive_prefix:
        return effective_positive
    return entry.raw_tags


def build_metadata(
    prepared: PreparedPrompt,
    *,
    payload: dict[str, Any],
    provider_metadata: dict[str, Any] | None,
    seed: int | None,
    sha256: str | None,
    width: int | None = None,
    height: int | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    request = prepared.request
    effective_prompt, effective_negative_prompt = effective_prompt_pair(request)
    data = {
        "source_prompt_file": str(prepared.entry.source_path),
        "source_prompt_index": prepared.entry.source_prompt_index,
        "source_line_number": prepared.entry.source_line_number,
        "raw_tags": prepared.entry.raw_tags,
        "mode": prepared.mode,
        "multichar_detected": prepared.multichar_detected,
        "detected_multichar_tags": prepared.entry.detected_multichar_tags,
        "positive_prefix_mode": prepared.positive_prefix_mode,
        "positive_prefix": prepared.positive_prefix,
        "negative_prompt_mode": prepared.negative_prompt_mode,
        "negative_uc_preset": prepared.negative_uc_preset,
        "fixed_negative_prompt": prepared.fixed_negative_prompt,
        "agent_plan": prepared.agent_plan,
        "agent_status": prepared.agent_status,
        "resolution_plan": prepared.resolution_plan,
        "effective_prompt": effective_prompt,
        "effective_negative_prompt": effective_negative_prompt,
        "training_caption": prepared.training_caption,
        "novelai_request_payload": payload,
        "model": request.model,
        "sampler": request.sampler,
        "steps": request.steps,
        "seed": seed if seed is not None else request.seed,
        "width": width if width is not None else request.width,
        "height": height if height is not None else request.height,
        "sha256": sha256,
        "provider_metadata": provider_metadata or {},
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return scrub_secrets(data, token)


def save_generated_artifact(
    output_dir: Path,
    *,
    output_stem: str,
    prepared: PreparedPrompt,
    image_bytes: bytes,
    provider_metadata: dict[str, Any],
    payload: dict[str, Any],
    token: str | None,
    image_index: int = 0,
) -> dict[str, Any]:
    ext = extension_for_format(prepared.request.image_format)
    suffix = f"_{image_index + 1}" if image_index else ""
    stem = f"{output_stem}{suffix}"
    image_path = output_dir / f"{stem}.{ext}"
    meta_path = output_dir / f"{stem}.json"
    caption_path = output_dir / f"{stem}.txt"
    digest = hashlib.sha256(image_bytes).hexdigest()
    actual_width, actual_height = image_size(image_bytes, (prepared.request.width, prepared.request.height))
    seed = provider_metadata.get("seed", prepared.request.seed) if isinstance(provider_metadata, dict) else prepared.request.seed
    metadata = build_metadata(
        prepared,
        payload=payload,
        provider_metadata=provider_metadata,
        seed=seed,
        sha256=digest,
        width=actual_width,
        height=actual_height,
        token=token,
    )
    metadata.update(
        {
            "path": str(image_path),
            "metadata_path": str(meta_path),
            "caption_path": str(caption_path),
            "output_stem": output_stem,
            "resume_key": entry_resume_key(prepared.entry),
            "image_index": image_index,
        }
    )
    metadata = scrub_secrets(metadata, token)
    _atomic_write(image_path, image_bytes)
    _atomic_write(caption_path, prepared.training_caption.encode("utf-8"))
    _atomic_write(meta_path, (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return {
        "image": str(image_path),
        "metadata": str(meta_path),
        "caption": str(caption_path),
        "sha256": digest,
        "seed": seed,
        "width": actual_width,
        "height": actual_height,
    }


###############################################################################
# Provider call and command handling.
###############################################################################


def token_from_args(args: argparse.Namespace) -> str | None:
    return os.getenv("NOVELAI_TOKEN") or args.novelai_token or args.novalai_token


def novelai_generate(
    payload: dict[str, Any],
    token: str,
    *,
    timeout: float,
    proxy_url: str | None = None,
) -> list[tuple[bytes, dict[str, Any]]]:
    try:
        import httpx
    except Exception as exc:  # pragma: no cover - environment-specific.
        raise ProviderError("httpx is required for real NovelAI generation") from exc

    base_url = os.getenv("NOVELAI_BASE_URL", "https://image.novelai.net").rstrip("/")
    url = f"{base_url}/ai/generate-image"
    auth = token.strip()
    if not auth.lower().startswith("bearer "):
        auth = f"Bearer {auth}"
    headers = {"Authorization": auth}

    request_kwargs: dict[str, Any] = {"json": payload, "headers": headers, "timeout": timeout}
    if proxy_url:
        request_kwargs["proxy"] = proxy_url

    try:
        response = httpx.post(url, **request_kwargs)
    except httpx.TimeoutException as exc:
        raise ProviderTimeoutError("provider timeout") from exc
    except httpx.HTTPError as exc:
        raise ProviderError(mask_secret_text(str(exc), token)) from exc

    if response.status_code == 401:
        raise ProviderAuthError("provider authentication failed")
    if response.status_code >= 500:
        detail = mask_secret_text(response.text[:300] if response.content else "", token)
        raise ProviderResponseError(f"provider temporary error {response.status_code}: {detail}")
    if response.status_code >= 400:
        detail = mask_secret_text(response.text[:300] if response.content else "", token)
        raise ProviderValidationError(f"provider rejected request {response.status_code}: {detail}")
    return parse_zip_response(response.content)


def should_retry_provider_error(error: ProviderError) -> bool:
    return not isinstance(error, (ProviderAuthError, ProviderValidationError))


def novelai_generate_with_retry(
    payload: dict[str, Any],
    token: str,
    *,
    timeout: float,
    proxy_url: str | None = None,
    max_attempts: int = 3,
) -> list[tuple[bytes, dict[str, Any]]]:
    attempts = max(1, max_attempts)
    last_error: ProviderError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return novelai_generate(payload, token, timeout=timeout, proxy_url=proxy_url)
        except ProviderError as exc:
            last_error = exc
            if attempt >= attempts or not should_retry_provider_error(exc):
                raise
    assert last_error is not None
    raise last_error


def write_dry_run(
    output_dir: Path,
    prepared_items: list[PreparedPrompt],
    *,
    token: str | None,
) -> list[dict[str, Any]]:
    dry_dir = output_dir / "dry-run"
    written: list[dict[str, Any]] = []
    for prepared in prepared_items:
        payload = build_payload(prepared.request)
        metadata_draft = build_metadata(
            prepared,
            payload=payload,
            provider_metadata={},
            seed=prepared.request.seed,
            sha256=None,
            token=token,
        )
        stem = f"{entry_output_stem(prepared.entry)}.dry_run.json"
        path = dry_dir / stem
        doc = scrub_secrets(
            {
                "kind": "dstill_novalai_dry_run",
                "source_prompt_file": str(prepared.entry.source_path),
                "mode": prepared.mode,
                "agent_used": prepared.agent_used,
                "plan": {
                    "agent_plan": prepared.agent_plan,
                    "agent_status": prepared.agent_status,
                    "resolution_plan": prepared.resolution_plan,
                },
                "payload": payload,
                "metadata_draft": metadata_draft,
            },
            token,
        )
        _atomic_write(path, (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        written.append(
            {
                "source_prompt_file": str(prepared.entry.source_path),
                "source_prompt_index": prepared.entry.source_prompt_index,
                "source_line_number": prepared.entry.source_line_number,
                "dry_run_path": str(path),
            }
        )
    manifest = {
        "kind": "dstill_novalai_dry_run_manifest",
        "count": len(written),
        "items": written,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write(dry_dir / "manifest.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return written


def _index_in_prompt_window(index: int, *, start_index: int | None, limit: int | None) -> bool:
    if start_index is None and limit is None:
        return True
    if start_index is not None and index < start_index:
        return False
    if start_index is not None and limit is not None and index >= start_index + limit:
        return False
    if start_index is None and limit is not None and index > limit:
        return False
    return True


def _entry_in_prompt_window(entry: PromptEntry, *, start_index: int | None, limit: int | None) -> bool:
    index = entry.source_prompt_index
    if index is None:
        index = 1
    return _index_in_prompt_window(index, start_index=start_index, limit=limit)


def filter_prompt_entries(
    entries: list[PromptEntry],
    *,
    start_index: int | None,
    limit: int | None,
) -> list[PromptEntry]:
    if start_index is not None and start_index < 1:
        raise ValueError("--prompt-start-index must be >= 1")
    if limit is not None and limit < 1:
        raise ValueError("--prompt-limit must be >= 1")
    return [entry for entry in entries if _entry_in_prompt_window(entry, start_index=start_index, limit=limit)]


def prepare_entry_with_agent_policy(
    entry: PromptEntry,
    *,
    mode: Literal["auto-multichar", "all-agent"],
    positive_prefix_mode: Literal["none", "preset", "custom"],
    positive_prefix: str,
    negative_prompt_mode: Literal["none", "preset", "custom"],
    negative_uc_preset: int | None,
    fixed_negative_prompt: str,
    model: str,
    sampler: str,
    steps: int,
    scale: float,
    cfg_rescale: float | None,
    noise_schedule: str | None,
    seed: int | None,
    resolution_preset_name: str | None,
    n_samples: int,
    agent_config: AgentRuntimeConfig,
    dry_run: bool,
    skip_recorder: SkipRecorder | None = None,
    force_replan: bool = False,
) -> PreparedPrompt | None:
    try:
        agent_plan, agent_status = resolve_external_agent_plan(
            entry,
            mode=mode,
            fixed_negative_prompt=fixed_negative_prompt,
            agent_config=agent_config,
            dry_run=dry_run,
            force_refresh=force_replan,
        )
    except AgentKeyMissingError:
        raise
    except AgentError as exc:
        if agent_config.failure_policy == "fallback-internal":
            agent_plan, agent_status = None, {
                "agent_needed": should_use_agent_for_entry(entry, mode),
                "agent_source": "internal",
                "reason": "external_agent_failed_fallback_internal",
            }
        elif agent_config.failure_policy == "skip":
            if skip_recorder is not None:
                api_key = os.getenv(agent_config.api_key_env)
                reason = (
                    "external_agent_validation_failed"
                    if isinstance(exc, AgentValidationError)
                    else "external_agent_failed"
                )
                call_skip_recorder(skip_recorder, entry, "agent", reason, exc, api_key)
            return None
        else:
            raise

    return prepare_prompt(
        entry,
        mode=mode,
        positive_prefix_mode=positive_prefix_mode,
        positive_prefix=positive_prefix,
        negative_prompt_mode=negative_prompt_mode,
        negative_uc_preset=negative_uc_preset,
        fixed_negative_prompt=fixed_negative_prompt,
        model=model,
        sampler=sampler,
        steps=steps,
        scale=scale,
        cfg_rescale=cfg_rescale,
        noise_schedule=noise_schedule,
        seed=seed,
        resolution_preset_name=resolution_preset_name,
        n_samples=n_samples,
        agent_plan_override=agent_plan,
        agent_status=agent_status,
    )


###############################################################################
# Backend job interface.
#
# ``run_generation_job`` is the single orchestration entry point shared by the
# CLI (``run_generate``) and the manual review WebUI.  It takes typed params,
# never prints (callers render the returned summary dict), and emits optional
# progress events.  Keeping the CLI a thin adapter over this function means the
# skill path and the WebUI can never diverge.
###############################################################################

JobEvent = dict[str, Any]
EventEmitter = Callable[[JobEvent], None]


def _emit(emit: EventEmitter | None, event_type: str, **fields: Any) -> None:
    """Best-effort progress event.  A misbehaving UI callback never aborts a run."""
    if emit is None:
        return
    event: JobEvent = {"type": event_type}
    event.update(fields)
    try:
        emit(event)
    except Exception:
        pass


class TokenMissingError(RuntimeError):
    """Real (non dry-run) generation was requested without a NovelAI token."""


@dataclass
class GenerationJobParams:
    """Typed mirror of the ``generate`` CLI knobs (``build_parser`` dests).

    Field names and types match the argparse namespace so ``run_generate`` is a
    direct adapter and helpers like ``resolve_agent_runtime_config`` accept this
    object unchanged.  Secrets (the NovelAI token, the agent API key) are never
    stored here: the token is a separate argument to ``run_generation_job`` and
    the agent key is read from the environment via ``agent_api_key_env``.
    """

    prompt_file: str | None = None
    prompt_dir: str | None = None
    prompt_split_mode: str = "single"
    output_dir: str | None = None
    config_file: str = str(DEFAULT_CONFIG_PATH)
    exclude_tags: str | None = None
    mode: str = "auto-multichar"
    model: str = "nai-diffusion-4-5-full"
    sampler: str = "k_euler"
    noise_schedule: str | None = None
    steps: int = 28
    scale: float = 5.0
    cfg_rescale: float | None = None
    seed: int | None = None
    resolution_preset: str | None = None
    n_samples: int = 1
    positive_prefix_mode: str = "none"
    positive_prefix: str = ""
    positive_prefix_file: str | None = None
    negative_prompt_mode: str = "none"
    negative_uc_preset: int | None = None
    negative_prompt: str = ""
    negative_prompt_file: str | None = None
    dry_run: bool = False
    agent_provider: str = "external"
    agent_api_format: str = ""
    agent_base_url: str = ""
    agent_model: str = ""
    agent_api_key_env: str = ""
    agent_cache_file: str | None = None
    agent_cache_dir: str | None = None
    agent_failure_policy: str = "skip"
    agent_run_in_dry_run: bool = False
    prompt_start_index: int | None = None
    prompt_limit: int | None = None
    overwrite: bool = False


def _job_params_from_args(args: argparse.Namespace) -> GenerationJobParams:
    return GenerationJobParams(
        prompt_file=args.prompt_file,
        prompt_dir=args.prompt_dir,
        prompt_split_mode=args.prompt_split_mode,
        output_dir=args.output_dir,
        config_file=args.config_file,
        exclude_tags=args.exclude_tags,
        mode=args.mode,
        model=args.model,
        sampler=args.sampler,
        noise_schedule=args.noise_schedule,
        steps=args.steps,
        scale=args.scale,
        cfg_rescale=args.cfg_rescale,
        seed=args.seed,
        resolution_preset=args.resolution_preset,
        n_samples=args.n_samples,
        positive_prefix_mode=args.positive_prefix_mode,
        positive_prefix=args.positive_prefix,
        positive_prefix_file=args.positive_prefix_file,
        negative_prompt_mode=args.negative_prompt_mode,
        negative_uc_preset=args.negative_uc_preset,
        negative_prompt=args.negative_prompt,
        negative_prompt_file=args.negative_prompt_file,
        dry_run=args.dry_run,
        agent_provider=args.agent_provider,
        agent_api_format=args.agent_api_format,
        agent_base_url=args.agent_base_url,
        agent_model=args.agent_model,
        agent_api_key_env=args.agent_api_key_env,
        agent_cache_file=args.agent_cache_file,
        agent_cache_dir=args.agent_cache_dir,
        agent_failure_policy=args.agent_failure_policy,
        agent_run_in_dry_run=args.agent_run_in_dry_run,
        prompt_start_index=args.prompt_start_index,
        prompt_limit=args.prompt_limit,
        overwrite=args.overwrite,
    )


def generate_and_save_entry(
    output_dir: Path,
    prepared: PreparedPrompt,
    *,
    token: str,
    timeout: float,
    proxy_url: str | None = None,
    emit: EventEmitter | None = None,
    progress_lock: threading.Lock | None = None,
    request_lock: threading.Lock | None = None,
) -> list[dict[str, Any]]:
    """Generate one prepared prompt and persist its artifacts.

    Shared by the batch loop and the WebUI single-image rerun.  Raises
    ``ProviderError`` on generation failure (the caller decides skip vs surface).
    The progress record is appended (guarded by ``progress_lock`` when supplied)
    only after at least one image is saved.  When ``request_lock`` is supplied it
    is held across the NovelAI call (incl. retries) so a single token never has
    more than one in-flight request — the batch loop and the WebUI rerun share
    one lock, so a manual rerun queues behind any in-flight batch image.
    """
    # Payload contains no token; keep it byte-for-byte as constructed for
    # NovelAI.  Metadata is scrubbed separately before persistence.
    payload = build_payload(prepared.request)
    request_cm = request_lock if request_lock is not None else contextlib.nullcontext()
    with request_cm:
        provider_results = novelai_generate_with_retry(payload, token, timeout=timeout, proxy_url=proxy_url)
    entry = prepared.entry
    key = entry_resume_key(entry)
    output_stem = entry_output_stem(entry)
    artifacts: list[dict[str, Any]] = []
    entry_outputs: list[str] = []
    for image_index, (image_bytes, provider_metadata) in enumerate(provider_results):
        artifact = save_generated_artifact(
            output_dir,
            output_stem=output_stem,
            prepared=prepared,
            image_bytes=image_bytes,
            provider_metadata=scrub_secrets(provider_metadata, token),
            payload=payload,
            token=token,
            image_index=image_index,
        )
        artifacts.append(artifact)
        entry_outputs.append(artifact["image"])
        _emit(emit, "image_saved", key=key, output_stem=output_stem, image_index=image_index, image=artifact["image"])
    if entry_outputs:
        record = build_progress_record(entry, key=key, outputs=entry_outputs, token=token)
        lock_cm = progress_lock if progress_lock is not None else contextlib.nullcontext()
        with lock_cm:
            append_progress_jsonl(output_dir / "progress.jsonl", record, token=token)
    return artifacts


def run_generation_job(
    params: GenerationJobParams,
    *,
    token: str | None = None,
    config: dict[str, Any] | None = None,
    emit: EventEmitter | None = None,
    progress_lock: threading.Lock | None = None,
    request_lock: threading.Lock | None = None,
) -> dict[str, Any]:
    """Run a generate job from typed params and return the summary dict.

    Never prints.  Raises ``TokenMissingError`` for a real run without a token;
    propagates ``ValueError`` / ``ProviderError`` / ``AgentError`` as before.
    ``request_lock``, when supplied, serializes the NovelAI call so a manual
    WebUI rerun sharing the same lock cannot run concurrently with the batch.
    """
    prompt_file = Path(params.prompt_file) if params.prompt_file else None
    prompt_dir = Path(params.prompt_dir) if params.prompt_dir else None
    output_dir = Path(params.output_dir) if params.output_dir else default_output_dir()
    if config is None:
        config = load_config(params.config_file)
    if params.exclude_tags is not None:
        exclude_raw = _split_parts(params.exclude_tags)
    else:
        exclude_raw = config.get("prompt", {}).get("exclude_tags") or []
    exclude_tags = build_exclude_tag_set(exclude_raw)
    entries = find_prompt_entries(
        prompt_file,
        prompt_dir,
        prompt_split_mode=params.prompt_split_mode,
        exclude_tags=exclude_tags,
        prompt_start_index=params.prompt_start_index,
        prompt_limit=params.prompt_limit,
    )
    entries = filter_prompt_entries(entries, start_index=params.prompt_start_index, limit=params.prompt_limit)
    if not entries:
        raise ValueError("no .txt prompt files found")

    mode: Literal["auto-multichar", "all-agent"] = params.mode
    positive_prefix_mode: Literal["none", "preset", "custom"] = params.positive_prefix_mode
    negative_prompt_mode: Literal["none", "preset", "custom"] = params.negative_prompt_mode
    model = params.model

    if params.positive_prefix_file:
        positive_prefix_mode = "custom"
        positive_prefix_value = read_prompt_text(Path(params.positive_prefix_file))
    else:
        positive_prefix_value = params.positive_prefix or ""
    if params.negative_prompt_file:
        negative_prompt_mode = "custom"
        negative_prompt_value = read_prompt_text(Path(params.negative_prompt_file))
    else:
        negative_prompt_value = params.negative_prompt or ""

    positive_prefix = resolve_positive_prefix(
        mode=positive_prefix_mode,
        custom_prefix=positive_prefix_value,
        model=model,
    )
    fixed_negative_prompt, negative_uc_preset = resolve_fixed_negative_prompt(
        mode=negative_prompt_mode,
        custom_negative=negative_prompt_value,
        uc_preset=params.negative_uc_preset,
        model=model,
    )

    if not params.dry_run and not token:
        raise TokenMissingError(
            "NOVELAI_TOKEN is not set; provide a token for this run or rerun with --dry-run."
        )

    agent_config = resolve_agent_runtime_config(params, config)
    skipped_items: list[dict[str, Any]] = []
    skip_recorder = make_skip_recorder(output_dir, skipped_items)
    done_keys: set[str] = set() if params.overwrite else load_done_keys(output_dir)
    resumed_skipped = 0

    _emit(
        emit,
        "job_start",
        dry_run=bool(params.dry_run),
        output_dir=str(output_dir),
        total_entries=len(entries),
        resumable_done=len(done_keys),
    )

    if params.dry_run:
        prepared_items: list[PreparedPrompt] = []
        for entry in entries:
            key = entry_resume_key(entry)
            if key in done_keys:
                resumed_skipped += 1
                _emit(
                    emit,
                    "entry_resumed",
                    key=key,
                    source_line_number=entry.source_line_number,
                    source_prompt_index=entry.source_prompt_index,
                    source_prompt_file=str(entry.source_path),
                )
                continue
            prepared = prepare_entry_with_agent_policy(
                entry,
                mode=mode,
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
                dry_run=True,
                skip_recorder=skip_recorder,
            )
            if prepared is None:
                continue
            prepared_items.append(prepared)
        written = write_dry_run(output_dir, prepared_items, token=token)
        summary = {
            "ok": True,
            "dry_run": True,
            "output_dir": str(output_dir),
            "count": len(written),
            "skipped_count": len(skipped_items),
            "resumed_skipped_count": resumed_skipped,
            "items": written,
            "skipped": skipped_items,
        }
        _emit(emit, "job_complete", summary=summary)
        return summary

    assert token is not None
    timeout = float(os.getenv("NOVELAI_TIMEOUT", "60"))
    proxy_url = resolve_network_proxy(config)
    saved: list[dict[str, Any]] = []
    prepared_count = 0
    for entry in entries:
        key = entry_resume_key(entry)
        if key in done_keys:
            resumed_skipped += 1
            # Resume-skipped entries used to be silent — for a long-running
            # batch with a partial prior run, this looked like "进度卡死" because
            # progress.total counts these but no event ticks the counter.
            _emit(
                emit,
                "entry_resumed",
                key=key,
                source_line_number=entry.source_line_number,
                source_prompt_index=entry.source_prompt_index,
                source_prompt_file=str(entry.source_path),
            )
            continue
        # Long-running phases (agent planning + NAI generation) emit no events
        # by themselves; without this start tick, the UI looks frozen for a
        # multi-minute multichar entry.
        _emit(
            emit,
            "entry_started",
            key=key,
            phase="prepare",
            source_line_number=entry.source_line_number,
            source_prompt_index=entry.source_prompt_index,
            source_prompt_file=str(entry.source_path),
            multichar=bool(entry.detected_multichar_tags),
        )
        skipped_before = len(skipped_items)
        prepared = prepare_entry_with_agent_policy(
            entry,
            mode=mode,
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
            skip_recorder=skip_recorder,
        )
        if prepared is None:
            # prepare_entry_with_agent_policy swallows AgentError under failure_policy=skip
            # and just returns None — without an explicit emit here the UI would tick
            # nothing for this entry and look frozen. Recover the skip record the
            # recorder just appended so we can surface phase/reason/line to the client.
            new_skip = skipped_items[-1] if len(skipped_items) > skipped_before else None
            _emit(
                emit,
                "entry_skipped",
                key=key,
                phase=(new_skip or {}).get("phase", "agent"),
                reason=(new_skip or {}).get("reason", "agent_skipped"),
                message=(new_skip or {}).get("message"),
                source_line_number=entry.source_line_number,
                source_prompt_index=entry.source_prompt_index,
                source_prompt_file=str(entry.source_path),
            )
            continue
        prepared_count += 1
        try:
            artifacts = generate_and_save_entry(
                output_dir,
                prepared,
                token=token,
                timeout=timeout,
                proxy_url=proxy_url,
                emit=emit,
                progress_lock=progress_lock,
                request_lock=request_lock,
            )
        except ProviderError as exc:
            if isinstance(exc, ProviderAuthError):
                reason = "provider_auth_failed"
            elif isinstance(exc, ProviderValidationError):
                reason = "provider_validation_failed"
            else:
                reason = "provider_failed"
            record = call_skip_recorder(skip_recorder, entry, "provider", reason, exc, token)
            _emit(
                emit,
                "entry_skipped",
                key=key,
                phase="provider",
                reason=reason,
                message=(record or {}).get("message") if record else mask_secret_text(str(exc), token),
                source_line_number=entry.source_line_number,
                source_prompt_index=entry.source_prompt_index,
                source_prompt_file=str(entry.source_path),
            )
            continue
        if artifacts:
            saved.extend(artifacts)
            done_keys.add(key)
            _emit(emit, "entry_done", key=key, outputs=[artifact["image"] for artifact in artifacts])

    summary = {
        "ok": True,
        "dry_run": False,
        "output_dir": str(output_dir),
        "prompt_count": prepared_count,
        "skipped_count": len(skipped_items),
        "resumed_skipped_count": resumed_skipped,
        "image_count": len(saved),
        "items": saved,
        "skipped": skipped_items,
    }
    _emit(emit, "job_complete", summary=summary)
    return summary


def run_generate(args: argparse.Namespace) -> int:
    """Thin CLI adapter over ``run_generation_job`` (stdout/exit-code unchanged)."""
    params = _job_params_from_args(args)
    token = token_from_args(args)
    try:
        summary = run_generation_job(params, token=token)
    except TokenMissingError:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": "token_missing",
                    "message": "NOVELAI_TOKEN is not set; provide a token for this run or rerun with --dry-run.",
                },
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def default_output_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path("output") / f"run_{stamp}"


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dstill NovelAI Skill v1 backend")
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate", help="Generate NovelAI images from .txt prompt file(s)")
    source = generate.add_mutually_exclusive_group(required=True)
    source.add_argument("--prompt-file", help="Path to one .txt prompt file")
    source.add_argument("--prompt-dir", help="Directory containing .txt prompt files; searched recursively")
    generate.add_argument(
        "--prompt-split-mode",
        default="single",
        choices=["single", "nonempty-lines"],
        help="For --prompt-file only: single keeps whole file as one prompt; nonempty-lines treats each non-empty line as one prompt",
    )
    generate.add_argument("--output-dir", help="Output directory; defaults to output/run_YYYYMMDD_HHMMSS")
    generate.add_argument(
        "--config-file",
        default=str(DEFAULT_CONFIG_PATH),
        help="JSON config file; defaults to config/dstill_novalai.json",
    )
    generate.add_argument(
        "--exclude-tags",
        default=None,
        help="Comma-separated tags removed from the request prompt and the agent input "
        "before the run; when provided it overrides config prompt.exclude_tags "
        "(normalized exact match, case-insensitive and underscore/space equivalent)",
    )
    generate.add_argument("--mode", required=True, choices=["auto-multichar", "all-agent"])
    generate.add_argument(
        "--model",
        default="nai-diffusion-4-5-full",
        choices=SUPPORTED_IMAGE_MODELS,
        help="NovelAI image model",
    )
    generate.add_argument(
        "--sampler",
        default="k_euler",
        choices=SUPPORTED_SAMPLERS,
        help="NovelAI sampler",
    )
    generate.add_argument(
        "--noise-schedule",
        help="NovelAI noise_schedule parameter; omit to keep the Web-compatible default",
    )
    generate.add_argument("--steps", type=int, default=28, help="Sampling steps")
    generate.add_argument("--scale", type=float, default=5.0, help="Prompt guidance scale")
    generate.add_argument(
        "--cfg-rescale",
        type=float,
        default=None,
        help="Prompt guidance rescale; omitted keeps NovelAI Web UI compatible default",
    )
    generate.add_argument("--seed", type=int, help="Generation seed; omit for a random seed")
    generate.add_argument(
        "--resolution-preset",
        choices=[preset.name for preset in BUILTIN_RESOLUTION_PRESETS],
        help="Built-in fixed resolution preset; overrides agent/auto selection",
    )
    generate.add_argument("--n-samples", type=int, default=1, help="Number of samples per prompt; must be 1..4")
    generate.add_argument("--positive-prefix-mode", required=True, choices=["none", "preset", "custom"])
    generate.add_argument("--positive-prefix", default="", help="Custom fixed positive prefix tags")
    generate.add_argument(
        "--positive-prefix-file",
        default=None,
        help="Read the custom positive prefix from a file (avoids shell quoting of multi-line "
        "text); implies --positive-prefix-mode custom and overrides --positive-prefix",
    )
    generate.add_argument("--negative-prompt-mode", required=True, choices=["none", "preset", "custom"])
    generate.add_argument("--negative-uc-preset", type=int, choices=[1, 2, 3], help="UC preset id for preset negative prompt")
    generate.add_argument("--negative-prompt", default="", help="Custom fixed negative prompt tags")
    generate.add_argument(
        "--negative-prompt-file",
        default=None,
        help="Read the custom negative prompt from a file (avoids shell quoting of multi-line "
        "text); implies --negative-prompt-mode custom and overrides --negative-prompt",
    )
    generate.add_argument(
        "--dry-run",
        nargs="?",
        const=True,
        default=False,
        type=parse_bool,
        help="true/false; when true writes plan/payload/metadata drafts only",
    )
    generate.add_argument("--agent-provider", default="external", choices=SUPPORTED_AGENT_PROVIDERS)
    generate.add_argument("--agent-api-format", required=True, choices=SUPPORTED_AGENT_API_FORMATS)
    generate.add_argument("--agent-base-url", required=True, help="External agent base URL; OpenAI-compatible URLs keep OpenAI schema")
    generate.add_argument("--agent-model", required=True, help="External prompt-refinement model id")
    generate.add_argument("--agent-api-key-env", required=True, help="Environment variable containing the external agent API key")
    generate.add_argument("--agent-cache-file", help="JSONL cache file for external agent plans")
    generate.add_argument("--agent-cache-dir", help="Directory for per-source external agent JSONL caches")
    generate.add_argument("--agent-failure-policy", default="skip", choices=SUPPORTED_AGENT_FAILURE_POLICIES)
    generate.add_argument(
        "--agent-run-in-dry-run",
        nargs="?",
        const=True,
        default=False,
        type=parse_bool,
        help="true/false; when true dry-run may call the external refinement API and write cache",
    )
    generate.add_argument("--prompt-start-index", type=int, help="For split prompt collections: first source_prompt_index to process")
    generate.add_argument("--prompt-limit", type=int, help="Maximum number of prompts to process")
    generate.add_argument(
        "--overwrite",
        action="store_true",
        help="Regenerate and overwrite prompts already completed in this output dir (ignore progress.jsonl)",
    )
    generate.add_argument("--novelai-token", default="", help=argparse.SUPPRESS)
    generate.add_argument("--novalai_token", default="", help=argparse.SUPPRESS)
    generate.set_defaults(func=run_generate)
    return parser


def main(argv: list[str] | None = None) -> int:
    # 强制 UTF-8 输出，避免 Windows 默认 GBK 控制台遇到中文/特殊字符时
    # 'gbk' codec can't encode 直接崩掉（脚本输出本就是 ensure_ascii=False 的 JSON/日志）。
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ProviderError as exc:
        token = token_from_args(args) if hasattr(args, "novelai_token") else None
        message = mask_secret_text(str(exc), token)
        code = getattr(exc, "code", "provider_error")
        print(json.dumps({"ok": False, "code": code, "message": message}, ensure_ascii=False), file=sys.stderr)
        return 1
    except AgentError as exc:
        message = mask_secret_text(str(exc))
        print(json.dumps({"ok": False, "code": "agent_error", "message": message}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception as exc:
        token = token_from_args(args) if hasattr(args, "novelai_token") else None
        message = mask_secret_text(str(exc), token)
        print(json.dumps({"ok": False, "code": "error", "message": message}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
