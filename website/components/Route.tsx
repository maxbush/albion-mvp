"use client";

import { useEffect, useRef } from "react";
import { clamp01, useReducedMotion } from "@/lib/scroll";
import styles from "./Route.module.css";

const STAGES = [
  {
    title: "Finding the right school",
    label: "ALBION · Dossier",
    desc: "Знакомимся с семьёй и ребёнком, оцениваем академический уровень и составляем длинный список школ — дневных и пансионов.",
    note: "Сначала понять ребёнка — потом открывать каталог.",
    meta: ["Дневные школы", "Пансионы"],
    img: "/assets/dossier-school.webp",
    caption: "School · UK",
    rot: "-1.1deg",
  },
  {
    title: "Preparing for admission",
    label: "ALBION · Dossier",
    desc: "План подготовки: UKiset и ISEB, собеседования, рекомендации и дедлайны — без суеты и пропущенных окон.",
    note: "Дедлайны — это стратегия, а не даты в календаре.",
    meta: ["UKiset · ISEB", "Интервью"],
    img: "/assets/dossier-prepare.webp",
    caption: "Preparation · Desk",
    rot: "0.8deg",
  },
  {
    title: "Building academic foundations",
    label: "ALBION · Dossier",
    desc: "Тьюторы, предметная подготовка, GCSE и A-Level: ребёнок входит в британскую систему уверенно, а не выживает в ней.",
    note: "Фундамент важнее фасада.",
    meta: ["GCSE · A-Level", "Тьюторы"],
    img: "/assets/dossier-foundations.webp",
    caption: "Library · Oxford",
    rot: "-0.6deg",
  },
  {
    title: "Choosing the right university",
    label: "ALBION · Dossier",
    desc: "UK, Европа, США или Оксбридж: выбираем под профиль студента, а не по рейтингу в вакууме.",
    note: "Правильный университет — это тот, который подходит.",
    meta: ["UCAS · Oxbridge", "Европа · США"],
    img: "/assets/dossier-university.webp",
    caption: "Spires · Oxford",
    rot: "1.2deg",
  },
  {
    title: "Preparing for the next step",
    label: "ALBION · Dossier",
    desc: "Первый семестр, адаптация, опекунство и тихая поддержка — маршрут не заканчивается на зачислении.",
    note: "Зачисление — начало маршрута, не его конец.",
    meta: ["Опекунство", "Адаптация"],
    img: "/assets/dossier-nextstep.webp",
    caption: "Doorway · Garden",
    rot: "-0.8deg",
  },
];

export default function Route() {
  const reduced = useReducedMotion();
  const stack = useRef<HTMLDivElement>(null);
  const slotRefs = useRef<(HTMLDivElement | null)[]>([]);
  const cardRefs = useRef<(HTMLElement | null)[]>([]);
  const imgRefs = useRef<(HTMLDivElement | null)[]>([]);
  const veilRefs = useRef<(HTMLDivElement | null)[]>([]);

  useEffect(() => {
    if (reduced) return;
    const el = stack.current;
    if (!el) return;

    let raf = 0;
    let running = false;

    const tick = () => {
      if (!running) return;
      const vh = window.innerHeight;
      for (let i = 0; i < STAGES.length; i++) {
        const card = cardRefs.current[i];
        const img = imgRefs.current[i];
        const veil = veilRefs.current[i];
        const slot = slotRefs.current[i];
        if (!card || !slot) continue;

        // how far the next dossier has slid over this one (0..1)
        const nextSlot = slotRefs.current[i + 1];
        let covered = 0;
        if (nextSlot) {
          const stickTop = vh * 0.09 + i * 18;
          covered = clamp01((vh - nextSlot.getBoundingClientRect().top) / (vh - stickTop));
        }
        const baseRot = parseFloat(STAGES[i].rot);
        const sign = i % 2 === 0 ? -1 : 1;
        card.style.transform = `rotate(${baseRot + covered * sign * 1.6}deg) scale(${1 - covered * 0.055})`;
        if (veil) veil.style.opacity = String(covered * 0.28);

        // subtle parallax inside the image while the card approaches
        if (img) {
          const r = slot.getBoundingClientRect();
          const approach = clamp01(1 - r.top / vh);
          img.style.transform = `translateY(${(approach - 0.5) * -9}%)`;
        }
      }
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
  }, [reduced]);

  return (
    <section className={styles.route} id="route" aria-labelledby="route-h">
      <div className={`${styles.head} wrap`}>
        <span className="eyebrow">The Route</span>
        <h2 className={styles.h2} id="route-h">
          A route,
          <br />
          not a catalogue.
        </h2>
        <p className={styles.intro}>
          ALBION ведёт семью через несколько этапов — от первого разговора до
          первого семестра. Каждый этап — отдельное дело, но один маршрут.
        </p>
      </div>

      <div className={styles.stack} ref={stack}>
        {STAGES.map((s, i) => (
          <div
            className={styles.slot}
            key={s.title}
            ref={(n) => {
              slotRefs.current[i] = n;
            }}
          >
            <article
              className={styles.dossier}
              ref={(n) => {
                cardRefs.current[i] = n;
              }}
              style={
                {
                  "--i": i,
                  "--rot": s.rot,
                } as React.CSSProperties
              }
            >
              <div className={styles.dBody}>
                <div className={styles.dTop}>
                  <span className={styles.dIndex}>
                    {String(i + 1).padStart(2, "0")}
                  </span>
                  <span className={styles.dLabel}>{s.label}</span>
                </div>
                <h3 className={styles.dTitle}>{s.title}</h3>
                <p className={styles.dNote}>{s.note}</p>
                <p className={styles.dDesc}>{s.desc}</p>
                <div className={styles.dMeta}>
                  <span>{s.meta[0]}</span>
                  <span>{s.meta[1]}</span>
                </div>
              </div>
              <figure className={styles.dFigure}>
                <div
                  className={styles.imgWrap}
                  ref={(n) => {
                    imgRefs.current[i] = n;
                  }}
                >
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={s.img} alt="" loading="lazy" />
                </div>
                <figcaption className={styles.dCaption}>{s.caption}</figcaption>
              </figure>
              <div
                className={styles.veil}
                ref={(n) => {
                  veilRefs.current[i] = n;
                }}
                aria-hidden="true"
              />
            </article>
          </div>
        ))}
      </div>
    </section>
  );
}
