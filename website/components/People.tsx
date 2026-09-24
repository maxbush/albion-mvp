"use client";

import { useState } from "react";
import styles from "./People.module.css";

const PEOPLE = [
  { img: "/assets/person-1.webp", name: "Имя — TBC", role: "Роль — TBC" },
  { img: "/assets/person-2.webp", name: "Имя — TBC", role: "Роль — TBC" },
  { img: "/assets/person-3.webp", name: "Имя — TBC", role: "Роль — TBC" },
  { img: "/assets/person-4.webp", name: "Имя — TBC", role: "Роль — TBC" },
];

export default function People() {
  const [active, setActive] = useState<number | null>(null);

  return (
    <section className={styles.people} id="people" aria-labelledby="people-h">
      <div className="wrap">
        <div className={styles.head}>
          <div>
            <span className="eyebrow">People</span>
            <h2 className={styles.h2} id="people-h">
              The people
              <br />
              behind the route
            </h2>
          </div>
          <p className={styles.note}>
            Команда и тьюторы ALBION — реальные люди в Оксфорде. Портреты и
            биографии будут добавлены — <span className={styles.tbc}>TBC</span>.
          </p>
        </div>

        <div className={styles.composition}>
          {PEOPLE.map((p, i) => (
            <figure
              key={i}
              className={`${styles.person} ${styles["p" + i]} ${
                active === i ? styles.active : ""
              } ${active !== null && active !== i ? styles.recede : ""}`}
              tabIndex={0}
              onMouseEnter={() => setActive(i)}
              onMouseLeave={() => setActive(null)}
              onFocus={() => setActive(i)}
              onBlur={() => setActive(null)}
              onClick={() => setActive((v) => (v === i ? null : i))}
            >
              <div className={styles.frame}>
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img src={p.img} alt="" loading="lazy" />
              </div>
              <figcaption className={styles.caption}>
                <span className={styles.pName}>{p.name}</span>
                <span className={styles.pRole}>{p.role}</span>
              </figcaption>
            </figure>
          ))}

          <div
            className={`${styles.panel} ${active !== null ? styles.panelOn : ""}`}
            aria-live="polite"
          >
            <dl>
              <div>
                <dt>Имя</dt>
                <dd>TBC</dd>
              </div>
              <div>
                <dt>Роль</dt>
                <dd>TBC</dd>
              </div>
              <div>
                <dt>Экспертиза</dt>
                <dd>TBC</dd>
              </div>
              <div>
                <dt>Биография</dt>
                <dd>TBC</dd>
              </div>
            </dl>
          </div>
        </div>
      </div>
    </section>
  );
}
