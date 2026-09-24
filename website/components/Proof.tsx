import styles from "./Proof.module.css";

export default function Proof() {
  return (
    <section className={styles.proof} id="proof" aria-labelledby="proof-h">
      <div className="wrap">
        <div className={styles.lead}>
          <span className="eyebrow">Proof</span>
          <h2 className={styles.h2} id="proof-h">
            Twenty years
            <br />
            <em>of combined experience.</em>
          </h2>
          <p className={styles.intro}>
            Единственная цифра, которую мы пишем сейчас честно: более 20 лет
            совокупного опыта команды. Остальное появится, когда будет подтверждено.
          </p>
        </div>

        <div className={styles.grid}>
          <div className={styles.cell}>
            <span className={styles.cellLabel}>Отзывы семей</span>
            <p className={styles.cellText}>
              Скоро здесь появятся истории семей, с которыми мы прошли этот путь.
            </p>
            <span className={styles.cellTbc}>TBC</span>
          </div>
          <div className={styles.cell}>
            <span className={styles.cellLabel}>Школы и университеты</span>
            <p className={styles.cellText}>
              Список партнёрских и принимающих школ — по запросу и в разделе результатов.
            </p>
            <span className={styles.cellTbc}>TBC</span>
          </div>
          <div className={styles.cell}>
            <span className={styles.cellLabel}>Медиа и публикации</span>
            <p className={styles.cellText}>
              Выступления, статьи и упоминания — добавим по мере публикации.
            </p>
            <span className={styles.cellTbc}>TBC</span>
          </div>
        </div>
      </div>
    </section>
  );
}
