import styles from "./Footer.module.css";

const COLS: { title: string; links: string[] }[] = [
  { title: "О нас", links: ["Миссия", "Команда", "Отзывы", "Контакт"] },
  {
    title: "Поступление",
    links: ["Школы", "Университеты UK", "Оксбридж", "Магистратура и MBA"],
  },
  {
    title: "Обучение",
    links: ["UKiset / ISEB", "GCSE · A-Level · IB", "Тьюторы"],
  },
  { title: "Ресурсы", links: ["Журнал", "События", "Гиды", "Прайс"] },
];

export default function Footer() {
  return (
    <footer className={styles.footer}>
      <div className="wrap">
        <div className={styles.top}>
          <div className={styles.brand}>
            <span className={styles.wordmark}>ALBION</span>
            <span className={styles.sub}>Consult — Oxford, United Kingdom</span>
          </div>
          <nav className={styles.cols} aria-label="Карта сайта">
            {COLS.map((c) => (
              <div className={styles.col} key={c.title}>
                <span className={styles.colTitle}>{c.title}</span>
                <ul>
                  {c.links.map((l) => (
                    <li key={l}>
                      <a href="#top">{l}</a>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </nav>
        </div>
        <div className={styles.bottom}>
          <span>© {new Date().getFullYear()} ALBION Consult</span>
          <span className={styles.line}>
            An editorial route into education
          </span>
        </div>
      </div>
    </footer>
  );
}
