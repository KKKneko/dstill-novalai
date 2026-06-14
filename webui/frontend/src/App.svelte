<script>
  import { onMount } from 'svelte';
  import { api } from './lib/api.js';
  import { connectEvents } from './lib/sse.js';
  import { playEnter, playApprove, playDelete, dissolveImage, revealText, filmIn } from './lib/anim.js';

  // ---- 状态 ----
  let started = $state(false);
  let busy = $state(false);
  let editing = $state(false);
  let editText = $state('');
  let dirty = $state(false); // 当前图已改词、待重生
  let toast = $state('');
  let queue = $state([]); // 待审 artifacts（list_artifacts 项）
  let index = $state(0);
  let progress = $state({ total: 0, generated: 0, skipped: 0, resumed: 0, done: false });
  // 当前正在处理的 entry（agent / NAI 这种长调用没有自己的事件，靠这个让 UI 不像死掉）
  let inflight = $state(null);

  const current = $derived(queue[index] ?? null);
  // 重跑队列：可多张排队，串行处理（NovelAI 单 token 同时只允许一个在途请求，故无并发收益）。
  // 视觉按 artifact 绑定——被排队/重跑的图显示溶解态 +「排队中 / 重跑中」浮层，
  // 切到别的图自然不显示、切回来仍是溶解态（而非命令式涂在屏幕上）。
  let rerunQueue = $state([]); // 等待重跑的 artifact_stem（FIFO，尚未开始）
  let rerunActive = $state(null); // 正在重跑的 artifact_stem
  let rerunBusy = false; // worker 循环是否在跑（纯控制流，非响应式）
  const isStemRerunning = (stem) => !!stem && (stem === rerunActive || rerunQueue.includes(stem));
  const currentRerunState = $derived(
    !current
      ? null
      : current.artifact_stem === rerunActive
        ? 'active'
        : rerunQueue.includes(current.artifact_stem)
          ? 'queued'
          : null,
  );
  const rerunCount = $derived(rerunQueue.length + (rerunActive ? 1 : 0));

  let stageImgEl = null;
  let hudPromptEl = $state(null);
  let es = null;
  let toastTimer = null;

  // ---- 发起表单（最小入口）----
  let form = $state({
    prompt_file: '',
    prompt_dir: '',
    prompt_split_mode: 'nonempty-lines',
    prompt_limit: 4,
    output_dir: '',
    mode: 'auto-multichar',
    model: 'nai-diffusion-4-5-full',
    resolution_preset: 'portrait_default_832x1216',
    positive_prefix_mode: 'none',
    negative_prompt_mode: 'preset',
    negative_uc_preset: 3,
    agent_api_format: 'openai',
    agent_base_url: '',
    agent_model: 'gemini-3.1-pro-preview',
    agent_api_key_env: 'AGENT_API_KEY', // 固定环境变量名，不在表单展示
    agent_api_key: '', // 外部 LLM API key：留空则读环境变量 AGENT_API_KEY
    novelai_token: '', // NovelAI token：留空则读环境变量 NOVELAI_TOKEN
    dry_run: false,
  });

  function flash(message) {
    toast = message;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (toast = ''), 2800);
  }

  function imgSrc(art) {
    const v = art?.sha256 ? art.sha256.slice(0, 12) : '';
    return api.imageUrl(art.artifact_stem) + (v ? `?v=${v}` : '');
  }

  function baseName(p) {
    if (!p) return '';
    return String(p).split(/[\\/]/).pop();
  }

  function enterAction(node) {
    stageImgEl = node;
    // 切到/切回正在排队或重跑的那张图：直接呈现溶解态、不播放进场
    // （否则切回来会把已溶解的图又重新进场显示出来）。
    if (isStemRerunning(current?.artifact_stem)) dissolveImage(node, { instant: true });
    else playEnter(node);
    revealText(hudPromptEl);
    return {
      destroy() {
        if (stageImgEl === node) stageImgEl = null;
      },
    };
  }

  function filmAction(node) {
    filmIn(node);
  }

  function pushArtifact(meta) {
    const i = queue.findIndex((a) => a.artifact_stem === meta.artifact_stem);
    if (i >= 0) queue[i] = meta;
    else queue.push(meta);
  }

  async function onEvent(ev) {
    if (ev.type === 'job_start') {
      progress = {
        total: ev.total_entries || 0,
        generated: 0,
        skipped: 0,
        resumed: 0,
        done: false,
      };
      inflight = null;
      if (ev.resumable_done) flash(`续跑：${ev.resumable_done} 条已完成会被跳过`);
    } else if (ev.type === 'entry_started') {
      const where = ev.source_line_number != null ? `第 ${ev.source_line_number} 行` : `#${ev.source_prompt_index ?? '?'}`;
      inflight = { where, multichar: !!ev.multichar };
    } else if (ev.type === 'image_saved') {
      progress = { ...progress, generated: progress.generated + 1 };
      inflight = null;
      const stem = ev.image_index ? `${ev.output_stem}_${ev.image_index + 1}` : ev.output_stem;
      try {
        pushArtifact(await api.detail(stem));
      } catch {
        /* metadata not ready yet */
      }
    } else if (ev.type === 'job_complete') {
      progress = { ...progress, done: true };
      inflight = null;
      flash('批量完成');
    } else if (ev.type === 'entry_resumed') {
      progress = { ...progress, resumed: progress.resumed + 1 };
    } else if (ev.type === 'entry_skipped') {
      progress = { ...progress, skipped: progress.skipped + 1 };
      inflight = null;
      const where = ev.source_line_number != null ? `第 ${ev.source_line_number} 行` : `#${ev.source_prompt_index ?? '?'}`;
      const phase = ev.phase === 'agent' ? 'agent' : ev.phase === 'provider' ? 'NAI' : ev.phase || '?';
      const reason = ev.reason || '未知原因';
      flash(`跳过 ${where} · ${phase}/${reason}`);
    } else if (ev.type === 'job_error') {
      flash('批量出错：' + (ev.message || ''));
      inflight = null;
    }
  }

  async function refresh(keepStem) {
    let list;
    try {
      list = await api.listArtifacts();
    } catch (e) {
      flash('刷新失败：' + e.message);
      return;
    }
    queue = list;
    if (keepStem) {
      const i = queue.findIndex((a) => a.output_stem === keepStem || a.artifact_stem === keepStem);
      if (i >= 0) index = i;
    }
    if (index > queue.length - 1) index = Math.max(0, queue.length - 1);
  }

  async function start() {
    const body = {};
    for (const [k, v] of Object.entries(form)) {
      if (v === '' || v == null) continue;
      body[k] = v;
    }
    if (body.prompt_file) delete body.prompt_dir;
    else if (body.prompt_dir) delete body.prompt_file;
    busy = true;
    try {
      await api.run(body);
      started = true;
      await refresh();
      es = connectEvents(onEvent);
    } catch (e) {
      flash('发起失败：' + e.message);
    } finally {
      busy = false;
    }
  }

  function selectIndex(i) {
    index = i;
  }
  function next() {
    if (index < queue.length - 1) index++;
  }
  function prev() {
    if (index > 0) index--;
  }

  async function approveNext() {
    const art = current;
    if (!art || busy) return;
    try {
      await api.review(art.artifact_stem, 'approved');
      art.review_status = 'approved';
    } catch (e) {
      flash('标记失败：' + e.message);
    }
    playApprove(stageImgEl, () => {
      if (index < queue.length - 1) index++;
      else flash('已是最后一张');
    });
  }

  // 按 R：把当前图加入重跑队列（不阻塞，可继续审别的图、再排更多张）。
  function rerun() {
    const art = current;
    if (!art) return;
    const stem = art.artifact_stem;
    if (isStemRerunning(stem)) {
      flash('这张已在重跑队列');
      return;
    }
    rerunQueue = [...rerunQueue, stem];
    dissolveImage(stageImgEl); // 当前看的就是这张，立即溶解
    flash(dirty ? '加入重跑队列 · 新词重生' : '加入重跑队列');
    dirty = false;
    runRerunWorker();
  }

  // 串行消费重跑队列（与后端 NovelAI 单请求锁一致；同一时刻只跑一张）。
  async function runRerunWorker() {
    if (rerunBusy) return;
    rerunBusy = true;
    try {
      while (rerunQueue.length > 0) {
        const stem = rerunQueue[0];
        rerunQueue = rerunQueue.slice(1);
        rerunActive = stem;
        try {
          const res = await api.regenerate(stem, {});
          rerunActive = null; // 先清再对账：重生图带新 sha→新节点按正常进场显影
          await reconcileAfterRerun(stem, res.output_stem);
        } catch (e) {
          rerunActive = null;
          // 失败且用户仍停在这张：取消溶解、恢复显示
          if (current && current.artifact_stem === stem) playEnter(stageImgEl);
          flash('重跑失败：' + e.message);
        }
      }
    } finally {
      rerunBusy = false;
    }
  }

  // 重跑完成后对账列表，但保持用户当前所看的位置不被拉走（区别于 refresh 会跳到 keepStem）。
  async function reconcileAfterRerun(oldStem, newStem) {
    const keepStem = current?.artifact_stem;
    let list;
    try {
      list = await api.listArtifacts();
    } catch (e) {
      flash('刷新失败：' + e.message);
      return;
    }
    queue = list;
    let i = keepStem ? queue.findIndex((a) => a.artifact_stem === keepStem) : -1;
    if (i < 0 && keepStem === oldStem) {
      // 用户正看着刚重生的这张、且改词导致 stem 变了→跟随到新图
      i = queue.findIndex((a) => a.artifact_stem === newStem || a.output_stem === newStem);
    }
    if (i >= 0) index = i;
    else if (index > queue.length - 1) index = Math.max(0, queue.length - 1);
  }

  function del() {
    const art = current;
    if (!art || busy) return;
    if (isStemRerunning(art.artifact_stem)) {
      flash('该图在重跑队列，无法删除');
      return;
    }
    busy = true;
    playDelete(stageImgEl, async () => {
      try {
        await api.remove(art.artifact_stem);
        await refresh();
      } catch (e) {
        flash('删除失败：' + e.message);
      } finally {
        busy = false;
      }
    });
  }

  function startEdit() {
    if (!current) return;
    editText = current.raw_tags || '';
    editing = true;
  }
  function cancelEdit() {
    editing = false;
  }
  async function saveEdit() {
    const art = current;
    if (!art) return;
    busy = true;
    try {
      const res = await api.edit(art.artifact_stem, editText);
      art.raw_tags = res.raw_tags ?? editText;
      dirty = true;
      editing = false;
      flash('已改词 · 按 R 用新词重生');
    } catch (e) {
      flash('改词失败：' + e.message);
    } finally {
      busy = false;
    }
  }

  function onKey(e) {
    if (!started) return;
    if (editing) {
      if (e.key === 'Escape') cancelEdit();
      return;
    }
    if (!current) return;
    if (e.code === 'Space') {
      e.preventDefault();
      approveNext();
    } else if (e.key === 'r' || e.key === 'R') rerun();
    else if (e.key === 'x' || e.key === 'X') del();
    else if (e.key === 'e' || e.key === 'E') {
      e.preventDefault();
      startEdit();
    } else if (e.key === 'ArrowRight') next();
    else if (e.key === 'ArrowLeft') prev();
  }

  onMount(() => {
    const handler = (e) => onKey(e);
    window.addEventListener('keydown', handler);
    return () => {
      window.removeEventListener('keydown', handler);
      es && es.close();
    };
  });
</script>

{#if !started}
  <!-- 最小发起入口（装片台）：填来源 + 必要参数即可开跑 -->
  <div class="panel-wrap darkroom-bg">
    <div class="panel">
      <h1>暗房 · 审片台</h1>
      <p class="sub">装入一卷 prompt，开始边出图边审。</p>

      <label>单文件 .txt（多行=多条）<input bind:value={form.prompt_file} placeholder="绝对路径，留空则用目录" /></label>
      <label>或 目录<input bind:value={form.prompt_dir} placeholder="绝对路径目录" /></label>
      <div class="row">
        <label>切分<select bind:value={form.prompt_split_mode}><option value="nonempty-lines">每行一条</option><option value="single">整文件一条</option></select></label>
        <label>数量<input type="number" min="1" bind:value={form.prompt_limit} /></label>
      </div>
      <label>输出目录<input bind:value={form.output_dir} placeholder="留空自动 output/run_时间戳" /></label>
      <div class="row">
        <label>分辨率<input bind:value={form.resolution_preset} /></label>
        <label>模式<select bind:value={form.mode}><option value="auto-multichar">auto-multichar</option><option value="all-agent">all-agent</option></select></label>
      </div>
      <div class="row">
        <label>agent base_url<input bind:value={form.agent_base_url} placeholder="https://…" /></label>
        <label>agent model<input bind:value={form.agent_model} placeholder="如 c46" /></label>
      </div>
      <div class="row">
        <label>外部 LLM API key<input type="password" autocomplete="off" bind:value={form.agent_api_key} placeholder="留空则读环境变量 AGENT_API_KEY" /></label>
        <label>NovelAI token<input type="password" autocomplete="off" bind:value={form.novelai_token} placeholder="留空则读环境变量 NOVELAI_TOKEN" /></label>
      </div>
      <div class="row">
        <label class="chk"><input type="checkbox" bind:checked={form.dry_run} /> 仅预览(dry-run)</label>
      </div>

      <button class="go" onclick={start} disabled={busy}>{busy ? '装片中…' : '开始审片 ▸'}</button>
      <p class="hint-sm">两个密钥优先读服务端环境变量（NOVELAI_TOKEN / AGENT_API_KEY）；环境变量没有时，用上面填的值。填入的值只发往本机服务端，不回传、不落盘。</p>
    </div>
  </div>
{:else}
  <div class="app darkroom-bg">
    <!-- 中央：当前审查大图 -->
    <section class="stage">
      {#if current}
        {#key current.artifact_stem + (current.sha256 || '')}
          <img class="stage-img" use:enterAction src={imgSrc(current)} alt="" />
        {/key}
      {:else}
        <div class="empty">{progress.done ? '没有可审的图' : '等待出图…'}</div>
      {/if}

      <!-- 重跑浮层：按 artifact 绑定（active=重跑中 / queued=排队中），切走自然消失、切回又在 -->
      {#if currentRerunState}
        <div class="reforge-overlay" class:queued={currentRerunState === 'queued'}>
          <span class="reforge-label">{currentRerunState === 'active' ? '重跑中' : '排队中'}</span>
          <span class="reforge-sub">{currentRerunState === 'active' ? 'Regenerating' : 'Queued'}</span>
        </div>
      {/if}

      <!-- HUD 信息浮层 -->
      {#if current}
        <div class="hud">
          {#if editing}
            <textarea class="prompt-edit" bind:value={editText} rows="4"></textarea>
            <div class="edit-actions">
              <button onclick={saveEdit}>保存 (回写 .txt)</button>
              <button class="ghost" onclick={cancelEdit}>取消 (Esc)</button>
            </div>
          {:else}
            <div class="prompt" bind:this={hudPromptEl}>{current.raw_tags}</div>
            {#if dirty}<div class="dirty">已改词 · 按 R 用新词重生</div>{/if}
          {/if}
          <div class="meta">
            <span class="amber">seed {current.seed ?? '—'}</span>
            <span>{current.width}×{current.height}</span>
            <span>{current.model}</span>
            <span>{baseName(current.source_prompt_file)}{current.source_line_number ? `:${current.source_line_number}` : ''}</span>
          </div>
          {#if current.agent_plan?.char_captions?.length}
            <div class="chars">
              {#each current.agent_plan.char_captions as c, ci}
                <div class="char"><span class="amber">#{ci + 1}</span> {c.prompt ?? c.caption ?? ''}</div>
              {/each}
            </div>
          {/if}
        </div>
      {/if}

      <!-- 进度：出/跳/续/总 -->
      <div class="progress">
        <span class="amber">{progress.generated}</span>{progress.total ? ` 出` : ''}
        {#if progress.skipped}<span class="skip"> · {progress.skipped} 跳</span>{/if}
        {#if progress.resumed}<span class="resumed"> · {progress.resumed} 续</span>{/if}
        {progress.total ? ` / ${progress.total}` : ''}
        {progress.done ? '· 完成' : '· 生成中'}
        {#if inflight && !progress.done}
          <div class="inflight">正在: {inflight.where}{inflight.multichar ? ' · 多人 (agent 规划中)' : ''}</div>
        {/if}
      </div>

      <!-- 重跑队列状态 -->
      {#if rerunCount > 0}
        <div class="rerun-status">⟳ 重跑队列 · 进行 {rerunActive ? 1 : 0}{rerunQueue.length ? ` · 排队 ${rerunQueue.length}` : ''}</div>
      {/if}

      <!-- 键位提示 -->
      <div class="keyhint">
        <kbd>空格</kbd>通过 <kbd>R</kbd>重跑 <kbd>X</kbd>删 <kbd>E</kbd>改词 <kbd>←→</kbd>切换
      </div>
    </section>

    <!-- 底部胶片流 -->
    <footer class="filmstrip">
      {#each queue as art, i (art.artifact_stem)}
        <button
          class="thumb"
          class:active={i === index}
          class:approved={art.review_status === 'approved'}
          class:rerunning={isStemRerunning(art.artifact_stem)}
          use:filmAction
          onclick={() => selectIndex(i)}
          title={art.raw_tags}
        >
          <img src={imgSrc(art)} alt="" />
          <span class="num">{i + 1}</span>
          {#if isStemRerunning(art.artifact_stem)}<span class="thumb-reforge">⟳</span>{/if}
        </button>
      {/each}
      {#if queue.length === 0}
        <div class="film-empty">胶片流为空 · 出图后将在此排队</div>
      {/if}
    </footer>
  </div>
{/if}

{#if toast}
  <div class="toast">{toast}</div>
{/if}

<style>
  .panel-wrap {
    height: 100vh;
    display: grid;
    place-items: center;
  }
  .panel {
    width: min(560px, 92vw);
    padding: 38px 40px;
    background: linear-gradient(180deg, var(--bg-2), var(--bg-1));
    box-shadow: 0 40px 120px var(--shadow), inset 0 1px 0 rgba(255, 255, 255, 0.03);
  }
  .panel h1 {
    margin: 0 0 4px;
    font-weight: 600;
    letter-spacing: 0.12em;
    font-size: 26px;
  }
  .panel .sub {
    margin: 0 0 22px;
    color: var(--ink-dim);
    font-size: 13px;
  }
  .panel label {
    display: block;
    margin: 12px 0;
    font-size: 12px;
    color: var(--ink-dim);
    letter-spacing: 0.04em;
  }
  .panel .row {
    display: flex;
    gap: 16px;
  }
  .panel .row > label {
    flex: 1;
  }
  .panel input,
  .panel select {
    display: block;
    width: 100%;
    margin-top: 5px;
    padding: 8px 2px;
    background: transparent;
    border: none;
    border-bottom: 1px solid var(--line);
    color: var(--ink);
    font-size: 14px;
    outline: none;
    transition: border-color 0.2s;
  }
  .panel input:focus,
  .panel select:focus {
    border-color: var(--amber);
  }
  .panel .chk {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 26px;
  }
  .panel .chk input {
    width: auto;
  }
  .go {
    margin-top: 26px;
    width: 100%;
    padding: 13px;
    background: var(--amber);
    color: #1a1206;
    border: none;
    font-size: 15px;
    font-weight: 600;
    letter-spacing: 0.1em;
    transition: filter 0.2s, transform 0.1s;
  }
  .go:hover {
    filter: brightness(1.1);
  }
  .go:active {
    transform: translateY(1px);
  }
  .go:disabled {
    opacity: 0.6;
    cursor: default;
  }
  .hint-sm {
    margin-top: 14px;
    color: var(--ink-dim);
    font-size: 11px;
  }

  .app {
    height: 100vh;
    display: grid;
    grid-template-rows: 1fr auto;
  }
  .stage {
    position: relative;
    overflow: hidden;
    display: grid;
    place-items: center;
  }
  .stage-img {
    max-height: 72vh;
    max-width: 68vw;
    object-fit: contain;
    filter: drop-shadow(0 30px 80px rgba(0, 0, 0, 0.7));
    will-change: transform, opacity, filter;
  }
  .reforge-overlay {
    position: absolute;
    inset: 0;
    display: grid;
    place-content: center;
    justify-items: center;
    gap: 8px;
    pointer-events: none;
    text-align: center;
    animation: reforge-in 0.4s ease-out both, reforge-blink 3s ease-in-out 0.4s infinite;
  }
  .reforge-label {
    font-size: 26px;
    letter-spacing: 0.5em;
    text-indent: 0.5em;
    color: var(--amber-2);
    text-shadow: 0 0 26px var(--amber-glow), 0 2px 10px rgba(0, 0, 0, 0.85);
  }
  .reforge-sub {
    font-family: var(--mono);
    font-size: 12px;
    letter-spacing: 0.42em;
    text-indent: 0.42em;
    text-transform: uppercase;
    color: var(--ink-dim);
  }
  @keyframes reforge-in {
    from { opacity: 0; }
    to { opacity: 1; }
  }
  @keyframes reforge-blink {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.32; }
  }
  .reforge-overlay.queued {
    animation: reforge-in 0.4s ease-out both, reforge-blink 4.4s ease-in-out 0.4s infinite;
  }
  .reforge-overlay.queued .reforge-label {
    color: var(--ink-2);
    text-shadow: 0 0 18px rgba(0, 0, 0, 0.6), 0 2px 10px rgba(0, 0, 0, 0.85);
  }
  .empty,
  .film-empty {
    color: var(--ink-dim);
    font-size: 14px;
    letter-spacing: 0.1em;
  }

  .hud {
    position: absolute;
    left: 38px;
    top: 34px;
    max-width: 32vw;
    text-shadow: 0 2px 12px rgba(0, 0, 0, 0.9);
    pointer-events: none;
  }
  .hud .prompt {
    font-size: 19px;
    line-height: 1.55;
    color: var(--ink);
    font-weight: 500;
  }
  .hud .dirty {
    margin-top: 8px;
    color: var(--amber-2);
    font-size: 12px;
    letter-spacing: 0.06em;
  }
  .hud .meta {
    margin-top: 14px;
    display: flex;
    flex-wrap: wrap;
    gap: 14px;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--ink-dim);
  }
  .hud .chars {
    margin-top: 12px;
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 12px;
    color: var(--ink-2);
    max-width: 30vw;
  }
  .amber {
    color: var(--amber);
  }
  .prompt-edit {
    width: 32vw;
    background: rgba(0, 0, 0, 0.55);
    border: 1px solid var(--line);
    border-left: 2px solid var(--amber);
    color: var(--ink);
    font-size: 16px;
    line-height: 1.5;
    padding: 10px;
    resize: vertical;
    outline: none;
    pointer-events: auto;
  }
  .edit-actions {
    margin-top: 8px;
    display: flex;
    gap: 10px;
    pointer-events: auto;
  }
  .edit-actions button {
    padding: 7px 14px;
    background: var(--amber);
    color: #1a1206;
    border: none;
    font-size: 13px;
  }
  .edit-actions .ghost {
    background: transparent;
    color: var(--ink-dim);
    border: 1px solid var(--line);
  }

  .progress {
    position: absolute;
    right: 34px;
    top: 30px;
    font-family: var(--mono);
    font-size: 13px;
    color: var(--ink-dim);
    letter-spacing: 0.06em;
  }
  .progress .amber {
    font-size: 18px;
  }
  .progress .skip {
    color: var(--amber-2, #d3925a);
  }
  .progress .resumed {
    color: var(--ink-dim, #888);
  }
  .progress .inflight {
    margin-top: 6px;
    font-size: 12px;
    color: var(--ink-2, #aaa);
    letter-spacing: 0.04em;
    animation: inflight-pulse 1.6s ease-in-out infinite;
  }
  @keyframes inflight-pulse {
    0%, 100% { opacity: 0.55; }
    50% { opacity: 1; }
  }
  .keyhint {
    position: absolute;
    right: 34px;
    bottom: 22px;
    font-size: 11px;
    color: var(--ink-dim);
    letter-spacing: 0.05em;
  }
  .rerun-status {
    position: absolute;
    right: 34px;
    top: 92px;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--amber-2);
    letter-spacing: 0.06em;
  }
  kbd {
    font-family: var(--mono);
    color: var(--ink-2);
    border: 1px solid var(--line);
    padding: 1px 6px;
    margin: 0 2px 0 10px;
  }

  .filmstrip {
    height: 150px;
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 0 20px;
    overflow-x: auto;
    overflow-y: hidden;
    background: linear-gradient(180deg, transparent, var(--bg-0) 60%);
    border-top: 1px solid var(--line);
    box-shadow: inset 0 30px 50px -30px rgba(0, 0, 0, 0.9);
  }
  .thumb {
    position: relative;
    flex: 0 0 auto;
    height: 116px;
    min-width: 64px;
    overflow: hidden;
    padding: 0;
    background: var(--bg-2);
    border: none;
    opacity: 0.5;
    filter: saturate(0.7);
    transition: opacity 0.25s, filter 0.25s, transform 0.25s, box-shadow 0.25s;
  }
  .thumb img {
    height: 100%;
    display: block;
  }
  .thumb:hover {
    opacity: 0.85;
    transform: translateY(-4px);
  }
  .thumb.active {
    opacity: 1;
    filter: saturate(1);
    box-shadow: 0 0 0 2px var(--amber), 0 0 28px var(--amber-glow);
    transform: translateY(-6px);
  }
  .thumb.approved::before {
    content: '✓';
    position: absolute;
    top: 4px;
    left: 6px;
    color: var(--ok);
    font-size: 14px;
    text-shadow: 0 1px 3px #000;
    z-index: 2;
  }
  .thumb .num {
    position: absolute;
    bottom: 3px;
    right: 5px;
    font-family: var(--mono);
    font-size: 10px;
    color: var(--ink-2);
    text-shadow: 0 1px 2px #000;
  }
  .thumb.rerunning {
    box-shadow: inset 0 0 0 2px var(--amber-2), 0 0 18px var(--amber-glow);
  }
  .thumb.rerunning img {
    opacity: 0.5;
    filter: blur(1px) brightness(0.7) saturate(0.6);
  }
  .thumb-reforge {
    position: absolute;
    top: 3px;
    right: 5px;
    font-size: 12px;
    line-height: 1;
    color: var(--amber-2);
    text-shadow: 0 1px 3px #000;
    z-index: 2;
  }

  .toast {
    position: fixed;
    left: 50%;
    bottom: 172px;
    transform: translateX(-50%);
    padding: 9px 18px;
    background: rgba(20, 15, 9, 0.92);
    border: 1px solid var(--line);
    border-left: 2px solid var(--amber);
    color: var(--ink);
    font-size: 13px;
    letter-spacing: 0.04em;
    z-index: 50;
  }
</style>
