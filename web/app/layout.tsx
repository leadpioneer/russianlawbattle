import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Судебный симулятор — подготовка к спору",
  description:
    "Прения трёх LLM-агентов (юристы сторон и судья) по вашему делу с рекомендациями для выбранной стороны.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="ru" className="h-full antialiased">
      <body className="min-h-full flex flex-col bg-slate-100 text-slate-900">
        {children}
      </body>
    </html>
  );
}
