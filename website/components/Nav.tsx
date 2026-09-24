"use client";

import { useEffect, useRef, useState } from "react";
import styles from "./Nav.module.css";

const MENU: { label: string; items?: { label: string; href: string }[] }[] = [
  {
    label: "О нас",
    items: [
      { label: "Миссия", href: "#route" },
      { label: "Команда", href: "#people" },
      { label: "Цифры и результаты", href: "#proof" },
      { label: "Отзывы", href: "#proof" },
      { label: "Медиа", href: "#journal" },
      { label: "Контакт", href: "#consultation" },
    ],
  },
  {
    label: "Поступление",
    items: [
      { label: "Дневные школы", href: "#" },
      { label: "Школы-пансионы", href: "#" },
      { label: "Университеты UK", href: "#" },
      { label: "Университеты Европы", href: "#" },
      { label: "Университеты США", href: "#" },
      { label: "Оксбридж", href: "#" },
      { label: "Магистратура и MBA", href: "#" },
      { label: "Докторантура", href: "#" },
    ],
  },
  {
    label: "Обучение",
    items: [
      { label: "Тесты в школы — UKiset / ISEB", href: "#" },
      { label: "Тесты в вузы — LNAT / TMUA / UCAT / IELTS", href: "#" },
      { label: "GCSE", href: "#" },
      { label: "A-Level", href: "#" },
      { label: "IB", href: "#" },
      { label: "Домашнее обучение", href: "#" },
      { label: "Интенсивные курсы", href: "#" },
      { label: "Тьюторы", href: "#" },
    ],
  },
  {
    label: "Услуги",
    items: [
      { label: "Опекунство", href: "#" },
      { label: "Академическая оценка", href: "#" },
      { label: "Карьерное ориентирование", href: "#" },
      { label: "Personal Statement", href: "#" },
      { label: "Летние лагеря", href: "#" },
      { label: "Управленческое образование", href: "#" },
    ],
  },
  { label: "Блог", items: [{ label: "Журнал", href: "#journal" }] },
  {
    label: "События",
    items: [
      { label: "Вебинары и встречи", href: "#" },
      { label: "День открытых дверей", href: "#" },
    ],
  },
  {
    label: "Ресурсы",
    items: [
      { label: "Гиды", href: "#" },
      { label: "Прайс", href: "#" },
    ],
  },
];

export default function Nav() {
  const [scrolled, setScrolled] = useState(false);
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState<number | null>(null);
  const rootRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 40);
    onScroll();
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = "";
    };
  }, [open]);

  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("click", onClick);
    return () => document.removeEventListener("click", onClick);
  }, []);

  return (
    <header
      ref={rootRef}
      className={`${styles.nav} ${scrolled ? styles.scrolled : ""}`}
    >
      <a className={styles.brand} href="#top" aria-label="ALBION Consult — на главную">
        <span className={styles.brandName}>ALBION</span>
        <span className={styles.brandSub}>Consult · Oxford</span>
      </a>

      <nav className={styles.desktop} aria-label="Основная навигация">
        <ul>
          {MENU.map((cat) => (
            <li key={cat.label} className={styles.cat}>
              {cat.items && cat.items.length > 1 ? (
                <>
                  <button
                    className={styles.catButton}
                    aria-expanded={undefined}
                    aria-haspopup="true"
                  >
                    {cat.label}
                  </button>
                  <div className={styles.drop}>
                    <ul>
                      {cat.items.map((item) => (
                        <li key={item.label}>
                          <a href={item.href}>{item.label}</a>
                        </li>
                      ))}
                    </ul>
                  </div>
                </>
              ) : (
                <a className={styles.catButton} href={cat.items?.[0]?.href ?? "#"}>
                  {cat.label}
                </a>
              )}
            </li>
          ))}
        </ul>
      </nav>

      <a className={styles.cta} href="#consultation">
        Консультация
      </a>

      <button
        className={styles.burger}
        aria-label={open ? "Закрыть меню" : "Открыть меню"}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        <span />
        <span />
      </button>

      {open && (
        <div className={styles.overlay}>
          <nav aria-label="Мобильная навигация">
            <ul>
              {MENU.map((cat, i) => (
                <li key={cat.label} className={styles.mCat}>
                  {cat.items && cat.items.length > 1 ? (
                    <>
                      <button
                        className={styles.mCatButton}
                        aria-expanded={expanded === i}
                        onClick={() =>
                          setExpanded((v) => (v === i ? null : i))
                        }
                      >
                        <span>{cat.label}</span>
                        <span className={styles.mPlus} aria-hidden="true">
                          {expanded === i ? "—" : "+"}
                        </span>
                      </button>
                      {expanded === i && (
                        <ul className={styles.mList}>
                          {cat.items.map((item) => (
                            <li key={item.label}>
                              <a
                                href={item.href}
                                onClick={() => setOpen(false)}
                              >
                                {item.label}
                              </a>
                            </li>
                          ))}
                        </ul>
                      )}
                    </>
                  ) : (
                    <a
                      className={styles.mCatButton}
                      href={cat.items?.[0]?.href ?? "#"}
                      onClick={() => setOpen(false)}
                    >
                      {cat.label}
                    </a>
                  )}
                </li>
              ))}
            </ul>
          </nav>
          <a
            className={`button ${styles.mCta}`}
            href="#consultation"
            onClick={() => setOpen(false)}
          >
            Book a consultation
          </a>
        </div>
      )}
    </header>
  );
}
