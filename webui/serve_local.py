#!/usr/bin/env python3
"""本地一键启动 WebUI 后端（uvicorn :8000）。

**不读取任何凭据文件**（早期版本会从硬编码的 s1.txt 注入 token/key，已移除）。
密钥来源二选一：
  1) 在审片台发起表单里直接填「外部 LLM API key」「NovelAI token」（env 缺失时生效）；
  2) 启动本脚本前自行设置环境变量 NOVELAI_TOKEN / AGENT_API_KEY（env 优先）。

这是**本地人工使用**的便利脚本，不属于 agent skill（不要镜像到 ~/.codex）。
前端 dev 会把 /api 代理到本服务，所以先起它，再 `cd webui/frontend && npm run dev`。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn  # noqa: E402
from webui.server import app  # noqa: E402

print("NOVELAI_TOKEN present:", bool(os.getenv("NOVELAI_TOKEN")))
print("AGENT_API_KEY present:", bool(os.getenv("AGENT_API_KEY")))
print("serve_local: no creds file is read; set keys in the WebUI form or via env vars.")
print("WebUI backend -> http://127.0.0.1:8000  (frontend dev proxies /api here)")
uvicorn.run(app, host="127.0.0.1", port=8000)
