from __future__ import annotations

from pathlib import Path

import fitz


def create_report_fixture(path: Path) -> None:
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 60), "Quarterly Analysis Report", fontsize=20, fontname="helv")
    page.insert_text((72, 100), "Left column line 1", fontsize=11, fontname="helv")
    page.insert_text((72, 124), "Left column line 2", fontsize=11, fontname="helv")
    page.insert_text((320, 100), "Right column line 1", fontsize=11, fontname="helv")
    page.insert_text((320, 124), "Right column line 2", fontsize=11, fontname="helv")
    pdf.save(path)
    pdf.close()


def create_text_table_fixture(path: Path) -> None:
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((72, 60), "Exam Lesson Overview", fontsize=18, fontname="helv")
    page.insert_text((72, 90), "Table 2 Topic Coverage", fontsize=12, fontname="helv")
    page.insert_text((72, 120), "Topic", fontsize=11, fontname="helv")
    page.insert_text((220, 120), "Count", fontsize=11, fontname="helv")
    page.insert_text((72, 144), "Grammar", fontsize=11, fontname="helv")
    page.insert_text((220, 144), "6", fontsize=11, fontname="helv")
    page.insert_text((72, 168), "Reading", fontsize=11, fontname="helv")
    page.insert_text((220, 168), "4", fontsize=11, fontname="helv")
    pdf.save(path)
    pdf.close()


def create_continued_table_fixture(path: Path) -> None:
    pdf = fitz.open()
    for page_index in range(2):
        page = pdf.new_page()
        page.insert_text((72, 60), "Quarterly Report", fontsize=18, fontname="helv")
        page.insert_text((72, 90), "Table 1 Revenue Breakdown", fontsize=12, fontname="helv")
        x0, y0, width, height = 72, 120, 240, 90
        for row in range(4):
            y = y0 + row * (height / 3)
            page.draw_line((x0, y), (x0 + width, y))
        for col in range(3):
            x = x0 + col * (width / 2)
            page.draw_line((x, y0), (x, y0 + height))
        page.insert_text((80, 144), "Region", fontsize=10, fontname="helv")
        page.insert_text((200, 144), "Value", fontsize=10, fontname="helv")
        if page_index == 0:
            page.insert_text((80, 174), "North", fontsize=10, fontname="helv")
            page.insert_text((200, 174), "120", fontsize=10, fontname="helv")
            page.insert_text((80, 204), "South", fontsize=10, fontname="helv")
            page.insert_text((200, 204), "95", fontsize=10, fontname="helv")
        else:
            page.insert_text((80, 174), "East", fontsize=10, fontname="helv")
            page.insert_text((200, 174), "88", fontsize=10, fontname="helv")
            page.insert_text((80, 204), "West", fontsize=10, fontname="helv")
            page.insert_text((200, 204), "102", fontsize=10, fontname="helv")
    pdf.save(path)
    pdf.close()
