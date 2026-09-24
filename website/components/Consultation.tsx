import styles from "./Consultation.module.css";

export default function Consultation() {
  return (
    <section
      className={styles.consultation}
      id="consultation"
      aria-labelledby="cta-h"
    >
      <div className={styles.bg} aria-hidden="true">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src="/assets/cta-garden.webp" alt="" loading="lazy" />
      </div>
      <div className={styles.inner}>
        <span className="eyebrow">Consultation</span>
        <h2 className={styles.h2} id="cta-h">
          Let&apos;s find
          <br />
          the right route.
        </h2>
        <p className={styles.text}>
          Расскажите о вашей семье и планах — предложим первый шаг и честно
          скажем, чем можем помочь.
        </p>
        <div className={styles.actions}>
          <a className="button" href="mailto:hello@albion.example">
            Book a consultation
          </a>
        </div>
        <p className={styles.small}>
          Oxford · United Kingdom · <span className={styles.tbc}>контакты — TBC</span>
        </p>
      </div>
    </section>
  );
}
