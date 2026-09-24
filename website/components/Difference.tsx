"use client";

import { useRef } from "react";
import { easeInOut, range, useReducedMotion, useScrollScene } from "@/lib/scroll";
import styles from "./Difference.module.css";

const WORDS = [
  {
    word: "Independent",
    text: "Мы не продаём места и не работаем за комиссию от школ. Рекомендуем то, что подходит именно вашей семье.",
  },
  {
    word: "Direct",
    text: "Без посредников и колл-центров: вы говорите напрямую с консультантом в Оксфорде.",
  },
  {
    word: "Personal",
    text: "У каждой семьи — собственный маршрут. Никаких «пакетов», конвейера и одинаковых списков школ.",
  },
  {
    word: "Confidential",
    text: "Информация о семье и ребёнке остаётся между нами. Точка.",
  },
];

export default function Difference() {
  const reduced = useReducedMotion();
  const wordRefs = useRef<(HTMLDivElement | null)[]>([]);
  const markRefs = useRef<(HTMLSpanElement | null)[]>([]);
  const counterRef = useRef<HTMLSpanElement>(null);

  const scene = useScrollScene<HTMLElement>((p) => {
    const n = WORDS.length;
    const active = Math.min(n - 1, Math.floor(p * n));
    for (let i = 0; i < n; i++) {
      const el = wordRefs.current[i];
      if (!el) continue;
      // local progress of word i: it owns [i/n, (i+1)/n]
      const local = range(p, i / n, (i + 1) / n);
      const inT = easeInOut(range(local, 0, 0.22));
      const outT = i === n - 1 ? 0 : easeInOut(range(local, 0.82, 1));
      el.style.opacity = String(inT * (1 - outT));
      el.style.transform = `translateY(${18 * (1 - inT) - 14 * outT}px)`;
    }
    for (let i = 0; i < n; i++) {
      const m = markRefs.current[i];
      if (m) m.style.opacity = i === active ? "1" : "0.28";
    }
    if (counterRef.current) {
      counterRef.current.textContent = `0${active + 1} / 0${n}`;
    }
  });

  if (reduced) {
    return (
      <section className={styles.static} aria-labelledby="diff-h">
        <div className="wrap">
          <span className="eyebrow">The ALBION difference</span>
          <h2 className="visually-hidden" id="diff-h">
            Чем ALBION отличается
          </h2>
          {WORDS.map((w) => (
            <div className={styles.staticRow} key={w.word}>
              <h3 className={styles.staticWord}>{w.word}</h3>
              <p className={styles.staticText}>{w.text}</p>
            </div>
          ))}
        </div>
      </section>
    );
  }

  return (
    <section className={styles.scene} ref={scene} aria-labelledby="diff-h">
      <div className={styles.sticky}>
        <span className="eyebrow">The ALBION difference</span>
        <h2 className="visually-hidden" id="diff-h">
          Чем ALBION отличается
        </h2>
        <div className={styles.words}>
          {WORDS.map((w, i) => (
            <div
              className={styles.word}
              key={w.word}
              ref={(n) => {
                wordRefs.current[i] = n;
              }}
              style={{ opacity: i === 0 ? 1 : 0 }}
              aria-hidden={i !== 0}
            >
              <h3 className={styles.big}>{w.word}</h3>
              <p className={styles.text}>{w.text}</p>
            </div>
          ))}
        </div>
        <div className={styles.marks} aria-hidden="true">
          <span ref={counterRef} className={styles.counter}>
            01 / 04
          </span>
          {WORDS.map((w, i) => (
            <span
              key={w.word}
              className={styles.mark}
              ref={(n) => {
                markRefs.current[i] = n;
              }}
            />
          ))}
        </div>
      </div>
    </section>
  );
}
