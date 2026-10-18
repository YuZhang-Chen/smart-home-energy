"""Import red cells from an official Taipower PDF, never a holiday library.

Requires the optional [calendar] dependency. Render the source and inspect it
before accepting an updated calendar; this command validates every day exists.
"""
import argparse
from datetime import date
from hashlib import sha256
import json
from pathlib import Path


def extract(pdf, year):
    import pdfplumber
    names = ["一月", "二月", "三月", "四月", "五月", "六月", "七月", "八月", "九月", "十月", "十一月", "十二月"]
    days, red = set(), set()
    with pdfplumber.open(pdf) as document:
        for page in document.pages:
            words = page.dedupe_chars().extract_words()
            headings = [w for w in words if w["text"] in names]
            if len(headings) != 12:
                continue
            for table in page.find_tables():
                x0, y0, x1, y1 = table.bbox
                headers = sorted([w for w in headings if x0 < (w["x0"] + w["x1"]) / 2 < x1], key=lambda w: w["top"])
                for word in words:
                    x, y = (word["x0"] + word["x1"]) / 2, (word["top"] + word["bottom"]) / 2
                    if not word["text"].isdigit() or not (x0 < x < x1 and y0 < y < y1):
                        continue
                    previous = [h for h in headers if h["top"] < word["top"] - 20]
                    if not previous:
                        continue
                    month = names.index(previous[-1]["text"]) + 1
                    value = date(year, month, int(word["text"]))
                    if value in days:
                        raise ValueError("重複日期，請人工檢查 PDF 結構")
                    days.add(value)
                    colors = [r["non_stroking_color"] for r in page.rects if r["x0"] < x < r["x1"] and r["top"] < y < r["bottom"]]
                    is_red = any(isinstance(c, tuple) and len(c) == 3 and c[0] > .8 and c[1] < .2 and c[2] < .2 for c in colors)
                    if is_red:
                        red.add(value.isoformat())
                    if value.weekday() == 6 and not is_red:
                        raise ValueError("週日不是紅色，請檢查頁面年份或 PDF 色彩")
    expected = (date(year + 1, 1, 1) - date(year, 1, 1)).days
    if len(days) != expected:
        raise ValueError(f"只辨識 {len(days)}/{expected} 天，拒絕匯入不完整日曆")
    return sorted(red)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pdf", required=True)
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--source-url", required=True)
    p.add_argument("--output", default="src/smart_home_energy/data/calendar.json")
    a = p.parse_args()
    dates = extract(a.pdf, a.year)
    path = Path(a.output)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"years": [], "offpeak_dates": [], "sources": {}}
    year = str(a.year)
    data["years"] = sorted(set(data["years"]) | {year})
    data["offpeak_dates"] = sorted([d for d in data["offpeak_dates"] if not d.startswith(year)] + dates)
    data["sources"][year] = {"url": a.source_url, "sha256": sha256(Path(a.pdf).read_bytes()).hexdigest(), "method": "PDF red-cell extraction; all calendar days validated"}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Imported {a.year}: {len(dates)} red dates")


if __name__ == "__main__":
    main()
