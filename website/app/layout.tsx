import type { Metadata, Viewport } from "next";
import { Cormorant_Garamond, Inter_Tight } from "next/font/google";
import "./globals.css";

const display = Cormorant_Garamond({
  subsets: ["latin", "cyrillic"],
  weight: ["300", "400", "500"],
  style: ["normal", "italic"],
  variable: "--font-cormorant",
  display: "swap",
});

const body = Inter_Tight({
  subsets: ["latin", "cyrillic"],
  weight: ["400", "500"],
  variable: "--font-inter",
  display: "swap",
});

export const metadata: Metadata = {
  title: "ALBION Consult — образовательный консалтинг в Оксфорде",
  description:
    "ALBION Consult — независимый образовательный консалтинг в Оксфорде для международных семей: школы, университеты, подготовка, сопровождение.",
};

export const viewport: Viewport = {
  themeColor: "#f4f1e8",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ru" className={`${display.variable} ${body.variable}`}>
      <body>{children}</body>
    </html>
  );
}
