#!/usr/bin/env python3
"""Объединение частей и конвертация в PDF."""

import sys
import os

def convert():
    try:
        from xhtml2pdf import pisa
    except ImportError:
        print("Установите xhtml2pdf: pip install xhtml2pdf --break-system-packages")
        sys.exit(1)

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    part1 = os.path.join(base_dir, "ALBION_GUIDE_part1.html")
    part2 = os.path.join(base_dir, "ALBION_GUIDE_part2.html")
    pdf_path = os.path.join(base_dir, "ALBION_GUIDE.pdf")

    # Read and combine parts
    with open(part1, "r", encoding="utf-8") as f:
        html1 = f.read()
    with open(part2, "r", encoding="utf-8") as f:
        html2 = f.read()

    # Extract body from part2 and insert before closing body of part1
    # Simple approach: remove the closing tags from part1 and the header from part2
    body2_start = html2.find("<body>") + len("<body>")
    body2_end = html2.find("</body>")
    body2_content = html2[body2_start:body2_end]

    # Insert part2 content before </body></html> of part1
    insert_point = html1.rfind("</body>")
    combined_html = html1[:insert_point] + body2_content + html1[insert_point:]

    print(f"Конвертация в PDF...")

    with open(pdf_path, "wb") as pdf_file:
        status = pisa.CreatePDF(combined_html, dest=pdf_file)

    if status.err:
        print(f"Ошибки при конвертации: {status.err}")
        sys.exit(1)

    size_kb = os.path.getsize(pdf_path) / 1024
    print(f"✅ Готово! Размер: {size_kb:.0f} KB")
    print(f"📄 Файл: {pdf_path}")

if __name__ == "__main__":
    convert()
