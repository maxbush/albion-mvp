import styles from "./Journal.module.css";

const FEATURE = {
  title: "UKiset: что это и как готовиться",
  tag: "Разбор тестов",
  note: "Большой материал — в работе",
  img: "/assets/journal-feature.webp",
};

const INDEX = [
  "ISEB Common Pre-Test vs UKiset vs CEM Select",
  "Как выбрать частную школу в Англии",
  "Пансион или дневная школа",
  "A-Level или IB",
  "Собеседование в Оксбридж",
  "Oxford acceptance rate",
  "UCAS deadlines и personal statement",
];

export default function Journal() {
  return (
    <section className={styles.journal} id="journal" aria-labelledby="journal-h">
      <div className="wrap">
        <div className={styles.head}>
          <span className="eyebrow">Journal</span>
          <h2 className={styles.h2} id="journal-h">
            Notes on the way
          </h2>
        </div>

        <div className={styles.layout}>
          <a className={styles.feature} href="#journal" aria-label={FEATURE.title}>
            <figure className={styles.featureFigure}>
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img src={FEATURE.img} alt="" loading="lazy" />
            </figure>
            <div className={styles.featureBody}>
              <span className={styles.featureTag}>{FEATURE.tag}</span>
              <h3 className={styles.featureTitle}>{FEATURE.title}</h3>
              <p className={styles.featureNote}>
                {FEATURE.note} — <span className={styles.tbc}>TBC</span>
              </p>
            </div>
          </a>

          <ol className={styles.index}>
            {INDEX.map((t, i) => (
              <li key={t} className={styles.indexItem}>
                <a className={styles.indexLink} href="#journal">
                  <span className={styles.indexNum}>
                    {String(i + 2).padStart(2, "0")}
                  </span>
                  <span className={styles.indexTitle}>{t}</span>
                  <span className={styles.indexTbc}>скоро</span>
                </a>
              </li>
            ))}
          </ol>
        </div>
      </div>
    </section>
  );
}
