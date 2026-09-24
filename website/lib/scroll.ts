"use client";

import { useEffect, useRef, useState } from "react";

export const clamp01 = (v: number) => Math.min(1, Math.max(0, v));

/** Progress of `p` within the [a, b] window, clamped to 0..1. */
export const range = (p: number, a: number, b: number) =>
  clamp01((p - a) / (b - a));

export const easeInOut = (t: number) => t * t * (3 - 2 * t);
export const easeOut = (t: number) => 1 - Math.pow(1 - t, 3);

export function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);
  return reduced;
}

/**
 * Scroll-driven scene: attach the ref to a tall wrapper (e.g. 240vh) and the
 * sticky viewport lives inside it. onFrame receives a lerped 0..1 progress
 * value every frame while the section intersects the viewport — write DOM
 * transforms inside it; no React re-renders happen.
 *
 * progress = how far the wrapper has been scrolled through, where
 * 0 = wrapper top hits viewport top, 1 = wrapper bottom leaves viewport bottom.
 */
export function useScrollScene<T extends HTMLElement>(
  onFrame: (p: number) => void,
  smooth = 0.14
) {
  const ref = useRef<T | null>(null);
  const frame = useRef(onFrame);
  frame.current = onFrame;

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    let raf = 0;
    let running = false;
    let current = -1;

    const measure = () => {
      const r = el.getBoundingClientRect();
      const vh = window.innerHeight;
      return clamp01(-r.top / Math.max(1, r.height - vh));
    };

    const tick = () => {
      if (!running) return;
      const target = measure();
      if (current < 0) current = target;
      current += (target - current) * smooth;
      if (Math.abs(target - current) < 0.0004) current = target;
      frame.current(current);
      raf = requestAnimationFrame(tick);
    };

    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting && !running) {
          running = true;
          raf = requestAnimationFrame(tick);
        } else if (!entry.isIntersecting && running) {
          running = false;
          cancelAnimationFrame(raf);
        }
      },
      { rootMargin: "5% 0px" }
    );
    io.observe(el);

    return () => {
      running = false;
      cancelAnimationFrame(raf);
      io.disconnect();
    };
  }, [smooth]);

  return ref;
}
