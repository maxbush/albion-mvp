"use client";

import Image from "next/image";
import { useRef } from "react";
import { easeOut, range, useReducedMotion, useScrollScene } from "@/lib/scroll";
import styles from "./Hero.module.css";

function HeroCopy() {
  return (
    <>
      <span className="eyebrow">Oxford · United Kingdom</span>
      <h1 className={styles.title}>
        Education,
        <br />
        <em>with a way forward.</em>
      </h1>
      <p className={styles.lede}>
        ALBION — независимый консалтинг в Оксфорде. Мы сопровождаем
        международные семьи от выбора школы до поступления в университет.
      </p>
      <div className={styles.actions}>
        <a className="button" href="#consultation">
          Book a consultation
        </a>
        <a className="button button--ghost" href="#route">
          Explore ALBION
        </a>
      </div>
    </>
  );
}

export default function Hero() {
  const reduced = useReducedMotion();
  const far = useRef<HTMLDivElement>(null);
  const near = useRef<HTMLDivElement>(null);
  const foliage = useRef<HTMLDivElement>(null);
  const foliageB = useRef<HTMLDivElement>(null);
  const fog = useRef<HTMLDivElement>(null);
  const fogB = useRef<HTMLDivElement>(null);
  const dust = useRef<HTMLDivElement>(null);
  const copy = useRef<HTMLDivElement>(null);
  const transition = useRef<HTMLDivElement>(null);
  const hint = useRef<HTMLDivElement>(null);

  const scene = useScrollScene<HTMLElement>((p) => {
    // Distant courtyard drifts closer — the base "shot".
    if (far.current) {
      far.current.style.transform = `scale(${1 + p * 0.9}) translateY(${
        -2.5 * p
      }%)`;
      far.current.style.opacity = String(1 - range(p, 0.55, 0.85) * 0.9);
    }
    // The facade crossfades in as we arrive.
    if (near.current) {
      near.current.style.opacity = String(range(p, 0.45, 0.75));
      near.current.style.transform = `scale(${1.18 + p * 0.16}) translateY(${
        3 - p * 3
      }%)`;
    }
    // Foliage passes the camera fastest — closest to the viewer.
    if (foliage.current) {
      foliage.current.style.transform = `scale(${1 + p * 2.6}) translateY(${
        -8 * p
      }%)`;
      foliage.current.style.opacity = String(1 - range(p, 0.38, 0.75));
    }
    if (foliageB.current) {
      foliageB.current.style.transform = `scaleX(-1) scale(${
        1.12 + p * 3.1
      }) translateY(${-12 * p}%)`;
      foliageB.current.style.opacity = String(
        0.8 * (1 - range(p, 0.3, 0.68))
      );
    }
    // Fog lifts and drifts aside.
    if (fog.current) {
      fog.current.style.transform = `translateY(${-38 * p}%)`;
      fog.current.style.opacity = String(0.55 * (1 - range(p, 0.1, 0.65)));
    }
    if (fogB.current) {
      fogB.current.style.transform = `translateY(${-20 * p}%) translateX(${
        4 * p
      }%)`;
      fogB.current.style.opacity = String(0.4 * (1 - range(p, 0, 0.5)));
    }
    if (dust.current) {
      dust.current.style.opacity = String(1 - range(p, 0.25, 0.6));
    }
    // Main copy steps aside early, clearing the shot.
    if (copy.current) {
      copy.current.style.opacity = String(1 - range(p, 0.04, 0.26));
      copy.current.style.transform = `translateY(${-56 * range(p, 0, 0.3)}px)`;
    }
    // Arrival statement at the end of the dolly-in.
    if (transition.current) {
      const tIn = easeOut(range(p, 0.72, 0.85));
      const tOut = range(p, 0.94, 1);
      transition.current.style.opacity = String(tIn * (1 - tOut));
      transition.current.style.transform = `translateY(${
        24 * (1 - tIn)
      }px)`;
    }
    if (hint.current) {
      hint.current.style.opacity = String(1 - range(p, 0, 0.12));
    }
  });

  if (reduced) {
    return (
      <section className={styles.static} aria-label="ALBION — Oxford">
        <div className={styles.layer} aria-hidden="true">
          <picture>
            <source media="(max-width: 720px)" srcSet="/assets/hero-mobile.webp" />
            <img src="/assets/hero-far.webp" alt="" />
          </picture>
        </div>
        <div className={styles.foliage} aria-hidden="true">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/assets/foliage-frame.webp" alt="" />
        </div>
        <div className={styles.copy}>
          <HeroCopy />
        </div>
      </section>
    );
  }

  return (
    <section className={styles.scene} ref={scene} aria-label="ALBION — Oxford">
      <div className={styles.sticky}>
        <div className={styles.layer} ref={far} aria-hidden="true">
          <picture>
            <source media="(max-width: 720px)" srcSet="/assets/hero-mobile.webp" />
            <img src="/assets/hero-far.webp" alt="" fetchPriority="high" />
          </picture>
        </div>
        <div className={`${styles.layer} ${styles.near}`} ref={near} aria-hidden="true">
          <Image
            src="/assets/hero-near.webp"
            alt=""
            fill
            sizes="100vw"
            style={{ objectFit: "cover" }}
          />
        </div>
        <div className={styles.fog} ref={fog} aria-hidden="true" />
        <div className={styles.fogB} ref={fogB} aria-hidden="true" />
        <div className={styles.dust} ref={dust} aria-hidden="true">
          {Array.from({ length: 6 }, (_, i) => (
            <span key={i} />
          ))}
        </div>
        <div className={styles.foliage} ref={foliage} aria-hidden="true">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/assets/foliage-frame.webp" alt="" />
        </div>
        <div className={styles.foliageB} ref={foliageB} aria-hidden="true">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src="/assets/foliage-frame.webp" alt="" />
        </div>
        <div className={styles.copy} ref={copy}>
          <HeroCopy />
        </div>
        <div className={styles.transition} ref={transition} aria-hidden="true">
          <p>
            Step inside.
            <br />
            <em>The route begins here.</em>
          </p>
        </div>
        <div className={styles.hint} ref={hint} aria-hidden="true">
          Scroll
        </div>
      </div>
    </section>
  );
}
