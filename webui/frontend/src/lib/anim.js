// 电影暗房动画模块（GSAP）。所有动画对 prefers-reduced-motion 退化为极简淡入。
// GSAP 3.13 起 Flip / SplitText / Physics2DPlugin 已并入主 npm 包，免费可用。
import { gsap } from 'gsap';
import { Physics2DPlugin } from 'gsap/Physics2DPlugin';
import { SplitText } from 'gsap/SplitText';

gsap.registerPlugin(Physics2DPlugin, SplitText);

export const reduced = () =>
  typeof window !== 'undefined' &&
  typeof window.matchMedia === 'function' &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches;

// 当前大图进场：从暗处微缩放 + 上浮 + 去模糊淡入。
export function playEnter(el) {
  if (!el) return;
  gsap.killTweensOf(el);
  if (reduced()) {
    gsap.fromTo(el, { opacity: 0 }, { opacity: 1, duration: 0.12 });
    return;
  }
  gsap.fromTo(
    el,
    { opacity: 0, scale: 0.94, y: 18, filter: 'brightness(0.35) blur(8px)' },
    { opacity: 1, scale: 1, y: 0, filter: 'brightness(1) blur(0px)', duration: 0.6, ease: 'power3.out' },
  );
}

// 通过：打光上扬滑出，结束回调切下一张。
export function playApprove(el, done) {
  if (!el || reduced()) {
    gsap.to(el, { opacity: 0, duration: 0.12, onComplete: done });
    return;
  }
  const tl = gsap.timeline({ onComplete: done });
  tl.to(el, { duration: 0.16, filter: 'brightness(1.6)', boxShadow: '0 0 70px var(--amber-glow)' })
    .to(el, { duration: 0.42, y: -46, opacity: 0, scale: 0.98, ease: 'power2.in' });
}

// 删除：抛物线掉落 + 旋转淡出（Physics2D），结束回调。
export function playDelete(el, done) {
  if (!el || reduced()) {
    gsap.to(el, { opacity: 0, duration: 0.12, onComplete: done });
    return;
  }
  gsap.to(el, {
    duration: 0.9,
    physics2D: { velocity: 340, angle: 90, gravity: 1500 },
    rotation: gsap.utils.random(-44, 44),
    opacity: 0,
    ease: 'none',
    onComplete: done,
  });
}

// 重跑「溶解」：把指定大图就地溶解隐去（模糊 + 变暗 + 淡出 + 轻微放大）。
// 只作用于这张 <img>；是否显示溶解态由组件按 artifact 状态驱动（见 App.svelte 的 rerunQueue / currentRerunState），
// 故本函数不持有循环/句柄。instant=true：切回正在重跑的图时直接呈现溶解态，不重播动画。
const DISSOLVED = { opacity: 0, scale: 1.04, filter: 'blur(14px) brightness(0.4) saturate(0.5)' };
export function dissolveImage(el, { instant = false } = {}) {
  if (!el) return;
  gsap.killTweensOf(el);
  if (instant || reduced()) {
    gsap.set(el, DISSOLVED);
    return;
  }
  gsap.to(el, { ...DISSOLVED, duration: 0.55, ease: 'power2.in' });
}

// HUD 提示词逐词浮现。
export function revealText(el) {
  if (!el) return;
  if (reduced()) {
    gsap.fromTo(el, { opacity: 0 }, { opacity: 1, duration: 0.12 });
    return;
  }
  const split = new SplitText(el, { type: 'words' });
  gsap.from(split.words, {
    opacity: 0,
    y: 10,
    filter: 'blur(4px)',
    duration: 0.5,
    stagger: 0.012,
    ease: 'power2.out',
    onComplete: () => split.revert(),
  });
}

// 胶片流新缩略入场。
export function filmIn(el) {
  if (!el || reduced()) return;
  // 只用 transform/opacity，绝不动画 width：缩略图宽度由图片撑开，批量入场时
  // 图常未加载完，动画 width 会被 GSAP 把 inline width 锁成 ~0，导致缩略图叠在一起。
  gsap.from(el, { opacity: 0, scale: 0.72, duration: 0.4, ease: 'back.out(1.6)', clearProps: 'transform,opacity' });
}
