"use client";

import Image from "next/image";
import { useRef } from "react";
import { easeInOut, range, useReducedMotion, useScrollScene } from "@/lib/scroll";
import styles from "./WorldScope.module.css";

const STEPS = [
  { name: "Oxford", text: "Всё начинается здесь — с города, где мы живём и работаем." },
  { name: "United Kingdom", text: "Школы и университеты по всей стране." },
  { name: "Europe", text: "Нидерланды, Швейцария, Италия — европейские программы." },
  { name: "USA", text: "Американские кампусы и liberal arts колледжи." },
  { name: "The world", text: "География определяется ребёнком, а не списком." },
];

// approximate marker positions on the watercolor map (%)
const DOTS = [
  { left: "47.5%", top: "33%" }, // UK
  { left: "55%", top: "38%" }, // Europe
  { left: "24%", top: "40%" }, // USA
];

export default function WorldScope() {
  const reduced = useReducedMotion();
  const ox = useRef<HTMLDivElement>(null);
  const map = useRef<HTMLDivElement>(null);
  const stepRefs = useRef<(HTMLDivElement | null)[]>([]);
  const dotRefs = useRef<(HTMLSpanElement | null)[]>([]);

  const scene = useScrollScene<HTMLElement>((p) => {
    // Oxford vignette shrinks into a point on the map (UK position)
    if (ox.current) {
      const t = easeInOut(range(p, 0.08, 0.62));
      ox.current.style.transform = `translate(${-2.3 * t}vw, ${
        -15 * t
      }vh) scale(${1 - t * 0.92})`;
      ox.current.style.opacity = String(1 - range(p, 0.5, 0.66));
    }
    if (map.current) {
      map.current.style.opacity = String(range(p, 0.34, 0.6));
      map.current.style.transform = `scale(${1.16 - 0.16 * easeInOut(p)})`;
    }
    const n = STEPS.length;
    for (let i = 0; i < n; i++) {
      const el = stepRefs.current[i];
      if (!el) continue;
      const inT = range(p, i / n, i / n + 0.08);
      const outT = i === n - 1 ? 0 : range(p, (i + 1) / n - 0.02, (i + 1) / n + 0.06);
      el.style.opacity = String(inT * (1 - outT));
      el.style.transform = `translateY(${14 * (1 - inT) - 10 * outT}px)`;
    }
    dotRefs.current.forEach((d, i) => {
      if (d) d.style.opacity = String(range(p, 0.42 + i * 0.16, 0.52 + i * 0.16));
    });
  });

  if (reduced) {
    return (
      <section className={styles.static} aria-labelledby="scope-h">
        <div className="wrap">
          <span className="eyebrow">Oxford → The world</span>
          <h2 className="visually-hidden" id="scope-h">
            От Оксфорда к миру
          </h2>
          <div className={styles.staticGrid}>
            <div className={styles.staticMap}>
              <Image
                src="/assets/world-map.webp"
                alt="Карта мира в акварели"
                width={1536}
                height={1024}
                style={{ width: "100%", height: "auto" }}
              />
            </div>
            <ol className={styles.staticList}>
              {STEPS.map((s) => (
                <li key={s.name}>
                  <strong>{s.name}</strong>
                  <span>{s.text}</span>
                </li>
              ))}
            </ol>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className={styles.scene} ref={scene} aria-labelledby="scope-h">
      <div className={styles.sticky}>
        <div className={styles.rail}>
          <span className="eyebrow">Oxford → The world</span>
          <h2 className="visually-hidden" id="scope-h">
            От Оксфорда к миру
          </h2>
        </div>

        <div className={styles.stage}>
          <div className={styles.mapWrap} ref={map} aria-hidden="true">
            <Image
              src="/assets/world-map.webp"
              alt=""
              fill
              sizes="90vw"
              style={{ objectFit: "contain" }}
            />
            {DOTS.map((d, i) => (
              <span
                key={i}
                className={styles.dot}
                style={{ left: d.left, top: d.top, opacity: 0 }}
                ref={(n) => {
                  dotRefs.current[i] = n;
                }}
              />
            ))}
          </div>

          <div className={styles.ox} ref={ox} aria-hidden="true">
            <Image
              src="/assets/dossier-university.webp"
              alt=""
              width={1024}
              height={1536}
              style={{ width: "100%", height: "100%", objectFit: "cover" }}
            />
          </div>

          <div className={styles.steps}>
            {STEPS.map((s, i) => (
              <div
                key={s.name}
                className={styles.step}
                style={{ opacity: i === 0 ? 1 : 0 }}
                ref={(n) => {
                  stepRefs.current[i] = n;
                }}
                aria-hidden={i !== 0}
              >
                <h3 className={styles.stepName}>{s.name}</h3>
                <p className={styles.stepText}>{s.text}</p>
              </div>
            ))}
          </div>
        </div>
      </div>
    </section>
  );
}
