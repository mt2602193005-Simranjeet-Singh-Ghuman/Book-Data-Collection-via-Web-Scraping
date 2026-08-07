# Book Data Collection via Web Scraping

**Programming Lab Assignment 1**  
**Student:** Simranjeet Singh Ghuman  
**Repository:** [Book-Data-Collection-via-Web-Scraping](https://github.com/mt2602193005-Simranjeet-Singh-Ghuman/Book-Data-Collection-via-Web-Scraping)

Local Python CLI that looks up a book by ISBN and collects public details from five sites:

- Amazon  
- Kobo  
- Audible  
- BookBub  
- Goodreads  

There is no website UI, no Flask/Django, and no database. Everything runs in the terminal (VS Code is fine).

---

## What it does

1. Takes **one ISBN**, **first N CSV rows**, an **inclusive CSV range**, or the **entire CSV**.  
2. Checks / converts ISBN-10 → ISBN-13.  
3. Always refreshes existing ISBNs (merge keeps good values; N/A never wipes prior data).  
4. Scrapes Goodreads first (canonical title), then Amazon, then Kobo/Audible/BookBub.  
5. Kobo / Audible / BookBub search by **Goodreads title only**; author is secondary validation. Ambiguous matches are logged as `AMBIGUOUS_TITLE_MATCH` (wrong book is not saved).  
6. Writes JSON, cover images, blurbs, reviews, and a preprocessing CSV log.  
7. Shows progress like `04/20` after each ISBN finishes.  
8. If one site or field fails, it stores `N/A`, logs the issue, and moves on.

---

## Folder structure

```text
Project/
├── input/                      # put CSV files here
├── output/
│   ├── JSON_Master/
│   │   └── master.json         # all ISBNs, all five sites
│   ├── JSON/
│   │   ├── Amazon/amazon metadata.json
│   │   ├── Kobo/kobo metadata.json
│   │   ├── Audible/audible metadata.json
│   │   ├── BookBub/bookbub metadata.json
│   │   └── Goodreads/goodreads metadata.json
│   ├── Cover_Page/
│   │   ├── Amazon_Cover/ … Goodreads_Cover/
│   ├── Blurb/
│   │   ├── Amazon_Blurb/ … Goodreads_Blurb/
│   ├── Reviews/
│   │   ├── Amazon_Reviews/ … Goodreads_Reviews/
│   └── Preprocessing/          # CSV problem log
├── scraper/                    # one module per website + shared base
├── utils/                      # ISBN helpers, JSON I/O, file saving
├── config.py
├── main.py
├── requirements.txt
└── README.md
```

Folders under `output/` are created automatically when you run the program.

---

## File naming

| Type | Pattern | Example |
|------|---------|---------|
| Site JSON | `<source> metadata.json` | `goodreads metadata.json` |
| Cover | `<isbn13>_c_<Source>_<n>.jpg` | `9780143127550_c_Goodreads_1.jpg` |
| Blurb | `<isbn13>_b_<Source>_<n>.txt` | `9780143127550_b_Goodreads_1.txt` |
| Review | `<isbn13>_r_<Source>_<n>.txt` | `9780143127550_r_Goodreads_1.txt` (one file per review) |

Genres stay inside the JSON (comma-separated). There is no separate genres folder.

---

## Setup

### 1. Clone this repository

```bash
git clone https://github.com/mt2602193005-Simranjeet-Singh-Ghuman/Book-Data-Collection-via-Web-Scraping.git
cd Book-Data-Collection-via-Web-Scraping
```

### 2. Python

Use Python 3.10+ (tested on Python 3.14).

### 3. Install packages

```bash
pip install -r requirements.txt
playwright install chromium
```

### 4. CSV (optional)

Place ISBN CSV files in `input/`.

| File | Purpose |
|------|---------|
| `input/2602193005.csv` | Full class CSV (~10k rows, header `Isbn-13`) |

---

## How to run

In the project folder (use the terminal, not the debugger F5 for `input()` prompts):

```bash
python main.py
```

Menu:

1. Single ISBN (manual entry)  
2. First N ISBNs from CSV  
3. Inclusive CSV range (start–end, 1-based data rows)  
4. Entire CSV  
5. Refresh already-scraped ISBNs in `master.json`  
0. Exit  

For options **2–4**, you will be asked for the CSV path. For **2** you choose N; for **3** you enter inclusive start/end. Example for option **2**:

| You type | Meaning |
|----------|---------|
| `20` or `34` | First 20 / first 34 rows from the CSV |
| `all` | Every ISBN in the CSV |
| Enter (blank) | Default = first 20 |

Example manual ISBN:

```text
9780143127550
```

---

## Expected output

For each ISBN you should see terminal progress like:

```text
ISBN
9780143127550

Amazon
Completed
...
Goodreads
Completed

Master JSON Updated
Images / Reviews / Blurb saved when available
Progress: 04/20
```

If that ISBN was scraped before:

```text
ISBN 9780... was already scraped earlier.
  r = scrape again
  s = skip this ISBN
```

Then check:

- `output/JSON_Master/master.json` (blank line between each ISBN block)  
- `output/JSON/<Site>/<site> metadata.json`  
- covers / blurbs / reviews folders  
- `Reviews/<isbn>_r_<source>_all.txt` — each review separated by a blank line  
- `output/Preprocessing/preprocessing_report.csv`

Missing values are kept as `"N/A"`.

---

## Dependencies (why they’re there)

| Package | Use |
|---------|-----|
| `requests` | Normal HTTP downloads |
| `beautifulsoup4` | Parse HTML |
| `lxml` | Faster HTML parser for BeautifulSoup |
| `playwright` | Fallback when a page is JS-heavy or blocked |
| `pandas` | Handy for CSV work / logs |
| `Pillow` | Optional image checks |
| `tqdm` | Optional progress bars |

Built-in modules used as well: `json`, `csv`, `pathlib`, `logging`-style prints, `re`, `time`.

---

## Screenshots

*(Add your own terminal screenshots here after a successful run.)*

1. Menu + ISBN entry  
2. Per-site Completed / Failed lines  
3. Snippet of `master.json`  
4. Files in `Cover_Page/`, `Blurb/`, `Reviews/`

---

## Limitations (why you may see N/A)

- **Kobo** — ebook catalog; paperback ISBN often has a different ebook ISBN. Title fallback helps when Amazon/Goodreads already found the book.  
- **Audible** — audiobook ASINs; print ISBNs rarely match. Title fallback is used after ISBN miss.  
- **BookBub** — `/search` is often 404 / geo-limited outside the US + Cloudflare. Title fallback tries when possible.  
- **Amazon** — captchas / bot blocks; reviews may be fewer than 25.  
- Sites change HTML; selectors may need updates later.  
- Be polite: the code waits about 1–2 seconds between requests.

---

## Possible improvements

- Add more retry / backoff options  
- Cache pages so re-runs are faster  
- Optional export to Excel  
- Unit tests for ISBN conversion and JSON merge

---

## Project layout (code)

| Path | Role |
|------|------|
| `main.py` | Entry point, menu, orchestration |
| `config.py` | Paths, source names, limits |
| `scraper/base.py` | Shared Level-1 / Level-2 engine |
| `scraper/*.py` | One scraper per website |
| `utils/isbn.py` | Validate / convert ISBNs |
| `utils/io_handlers.py` | Read/write JSON + preprocessing CSV |
| `utils/media_saver.py` | Save covers, blurbs, reviews |
| `utils/folder_setup.py` | Create the output tree |

---

## Assignment sources

Based on Programming Lab Assignment 1 (Goodreads, Amazon, BookBub, Kobo, Audible) and the agreed output layout for this project.
