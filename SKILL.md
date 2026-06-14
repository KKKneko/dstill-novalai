---
name: dstill-novelai
description: Generate NovelAI/NAI images from Danbooru-style .txt tag prompts with the Dstill NovelAI CLI. Use when Codex needs to plan, dry-run, batch-generate, resume, overwrite, or troubleshoot generation jobs from a prompt file/directory; collect NovelAI parameters, external OpenAI/Gemini prompt-refinement agent settings, and environment-variable token/key names, then launch scripts/dstill_novalai.py generate without writing secrets.
---

# Dstill NovelAI

只充当前台：从用户需求提取参数、追问缺失项、组装命令并启动固定 CLI。不要实现前端、Gallery、API 服务、旧 JSON prompt 池、自定义生图逻辑，或另写生图脚本。

普通生成只读本文件。维护脚本、排查 cache/metadata/续跑细节时再读 `DevDoc/dstill_novelai_reference.md`。需要把 `832x1216` 这类尺寸映射为内置 preset 时读 `references/default-resolution-presets.json`。

## 固定入口

- 把 `<SKILL_DIR>` 解析为包含本 `SKILL.md` 的目录。不要在当前工作目录、输出目录或用户数据目录里搜索脚本。
- 先 `cd "<SKILL_DIR>"`，再运行 `python scripts/dstill_novalai.py generate ...`；不切目录时使用脚本绝对路径。
- 由于命令在 `<SKILL_DIR>` 执行，`--prompt-file`、`--prompt-dir`、`--output-dir`、前缀文件和负面词文件都用用户数据的绝对路径。
- 默认配置由脚本自动定位到 `config/dstill_novalai.json`。不要传 `--config-file`，除非用户明确要换配置。
- 默认 NovelAI 代理来自配置 `127.0.0.1:7890`。只有用户要求直连或换代理时才设置 `NOVELAI_PROXY=direct|off|URL`。
- 每条命令都传 `--agent-provider external` 和 `--agent-failure-policy skip`。不要询问 `abort` 或 `fallback-internal`。
- 直接传 CLI flags。不要为拼参数写临时脚本，也不要用复杂内联 PowerShell 读文件再拼命令。
- 正面前缀或负面词包含换行、`::`、`{}`、`[]`、逗号等复杂字符时，用 `--positive-prefix-file` 或 `--negative-prompt-file`，不要硬塞进命令行。
- token 和 API key 只走本次进程的环境变量。不要写入命令行参数、配置、日志、cache、metadata 或回复正文。

## 参数收集

只追问无法从用户请求和默认值安全推出的必需项；用户说“默认”“推荐”“随便”就采用推荐值。

### 输入

- 输入二选一：`--prompt-file <ABS_TXT>` 或 `--prompt-dir <ABS_DIR>`。只支持 `.txt`。
- 单文件默认 `--prompt-split-mode single`；用户说“一行一个 prompt/tag 组合”时用 `nonempty-lines`。
- 批量窗口用 `--prompt-start-index N` 和 `--prompt-limit N`。它们对单文件 `nonempty-lines` 和目录排序后的 `.txt` 序号都生效。
- 默认配置会剔除 `prompt.exclude_tags` 中的 tag；用户要求保留所有 tag 时传 `--exclude-tags ""` 清空本次过滤。用户给自定义过滤列表时用逗号分隔传给 `--exclude-tags`。

### 输出与续跑

- 必须确认 `--output-dir <ABS_DIR>`。可推荐带时间戳的新目录。
- 指向同一 `--output-dir` 重跑相同命令会自动续跑：已完成项跳过，失败项重试。
- 用户说“补完”“接着上次”“只重试失败的”时，使用同一输出目录重跑。
- 用户说“全部重新生成”时，使用同一命令加 `--overwrite`，或换新输出目录。
- 修改 prompt tag 会生成新的续跑 key；只改 model、seed、steps 等生成参数不会触发重刷，必须加 `--overwrite` 或换目录。

### 生成模式

- 默认推荐 `--mode auto-multichar`：单人 prompt 不调用外部修词 agent，多人 prompt 调用外部 agent 生成 V4/V4.5 structured plan。
- 用户要求所有 prompt 都经外部 agent 修词时用 `--mode all-agent`。
- 不要手动复刻多人检测；只传 `--mode`，让脚本判断。

### NovelAI 参数

- 必须确认或采用推荐值：`--model`、`--sampler`、`--steps`、`--scale`、`--cfg-rescale`。
- 推荐值：`nai-diffusion-4-5-full`、`k_euler`、`28`、`5.0`、`0`。
- `--noise-schedule` 用户没指定时省略；脚本保持 Web 兼容默认。
- `--seed` 用户确认固定种子才传；随机则省略。
- `--n-samples` 仅用户要求每个 prompt 多张采样时传，范围 1..4。
- `--resolution-preset` 仅用户明确要求固定尺寸或 preset 时传。尺寸必须完全匹配内置 preset；无法匹配就追问，不要发明 width/height。

### 前缀、负面词与过滤

- 正面前缀必选模式：`--positive-prefix-mode none|preset|custom`。custom 用 `--positive-prefix` 或 `--positive-prefix-file`。
- 负面词必选模式：`--negative-prompt-mode none|preset|custom`。preset 配 `--negative-uc-preset 1|2|3`；custom 用 `--negative-prompt` 或 `--negative-prompt-file`。
- 推荐负面词默认：`--negative-prompt-mode preset --negative-uc-preset 3`，除非用户要求不用或自定义。

### 外部修词 agent

每条命令都必须带：

- `--agent-provider external`
- `--agent-api-format openai|gemini`
- `--agent-base-url URL`
- `--agent-model MODEL_ID`
- `--agent-api-key-env ENV_NAME`
- `--agent-failure-policy skip`

用户没给格式、base URL、模型或 key 环境变量名时逐项追问。只把环境变量名传给 `--agent-api-key-env`，不要把明文 key 当参数值。

推荐配置需用户确认：

- OpenAI: `--agent-api-format openai --agent-base-url https://api.openai.com/v1 --agent-model gpt-4.1-mini --agent-api-key-env OPENAI_API_KEY`
- Gemini: `--agent-api-format gemini --agent-base-url https://generativelanguage.googleapis.com/v1beta --agent-model gemini-2.5-flash --agent-api-key-env GEMINI_API_KEY`

dry-run 默认不调用外部 API，也不需要 agent key 实际存在。用户要求在 dry-run 中验证修词结果时，才加 `--agent-run-in-dry-run true`，并先确认对应 key 环境变量存在。

### NovelAI token

- 用户说“预览”“检查”“不消耗额度”时传 `--dry-run true`。
- 用户说“开始”“跑批量”“真实生成”，或已提供/确认 `NOVELAI_TOKEN` 时传 `--dry-run false`。
- 真实生成前确认 `NOVELAI_TOKEN` 环境变量存在，或在用户明确授权后为本次进程临时设置并在运行后清理。
- 请求文件或用户消息已给明文 key/token 且要求你运行时，视为本次授权：只临时写入环境变量，运行后清理，不在回复中复述密钥。

## 命令骨架

PowerShell 示例：

```powershell
cd "<SKILL_DIR>"
python scripts/dstill_novalai.py generate `
  --prompt-file "<ABS_PROMPT_TXT>" `
  --prompt-split-mode nonempty-lines `
  --output-dir "<ABS_OUTPUT_DIR>" `
  --mode auto-multichar `
  --model nai-diffusion-4-5-full `
  --sampler k_euler `
  --steps 28 `
  --scale 5.0 `
  --cfg-rescale 0 `
  --positive-prefix-mode none `
  --negative-prompt-mode preset `
  --negative-uc-preset 3 `
  --agent-provider external `
  --agent-api-format openai `
  --agent-base-url https://api.openai.com/v1 `
  --agent-model gpt-4.1-mini `
  --agent-api-key-env OPENAI_API_KEY `
  --agent-failure-policy skip `
  --dry-run true
```

目录批量时把 `--prompt-file ... --prompt-split-mode ...` 换成 `--prompt-dir "<ABS_PROMPT_DIR>"`，按需追加 `--prompt-limit N` 或 `--prompt-start-index N`。真实生成时先确保 `NOVELAI_TOKEN` 存在，把 `--dry-run true` 改为 `--dry-run false`。如果由你临时设置环境变量，结束后执行 `Remove-Item Env:\NOVELAI_TOKEN` 和对应 agent key 清理。

## 输出与安全检查

- dry-run 写入：`<output_dir>/dry-run/*.dry_run.json` 和 `manifest.json`。
- 真实生成写入：图片、同名 metadata JSON、同名训练 caption TXT。
- 续跑账本：`progress.jsonl` 记录完成项，`skipped.jsonl` 记录失败项。
- 运行后检查 stdout/stderr JSON 摘要中的 `ok`、`dry_run`、`count`/`image_count`、`skipped_count`、`resumed_skipped_count`。
- 若报 `token_missing`，补 `NOVELAI_TOKEN` 或改 dry-run。若报 `agent_error` 且任务会调用外部 agent，先检查 `--agent-api-key-env` 指向的环境变量是否存在。
- 不要在最终回复、日志或输出中泄露 `NOVELAI_TOKEN`、外部 API key、`Authorization` 或 `Bearer`。
