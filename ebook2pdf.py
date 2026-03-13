"""
ebook2pdf.py - NexBook 웹뷰어 교재를 PDF로 변환 (core 로직)

CLI 사용법:
    python ebook2pdf.py <URL>
"""

import sys
import re
import shutil
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
from typing import Callable

import requests
from pypdf import PdfWriter
from playwright.sync_api import sync_playwright


BATCH_SIZE = 20
WORKERS    = 8
RETRY      = 3


# ── 1. URL 파싱 ────────────────────────────────────────────────────────────────

def parse_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.endswith(".html"):
        path = path.rsplit("/", 1)[0]
    book_name = path.rsplit("/", 1)[-1]
    base_url  = f"{parsed.scheme}://{parsed.netloc}{path}"
    return base_url, book_name


# ── 2. data.js 파싱 ────────────────────────────────────────────────────────────

def fetch_book_info(base_url: str) -> tuple[int, str]:
    url = f"{base_url}/assets/data.js"
    r = requests.get(url, timeout=15)
    r.raise_for_status()

    total_match = re.search(r"totalPageNum\s*:\s*(\d+)", r.text)
    ext_match   = re.search(r"pageExt\s*:\s*['\"](\w+)['\"]", r.text)

    if not total_match:
        raise ValueError("data.js에서 totalPageNum을 찾지 못했습니다.")

    return int(total_match.group(1)), ext_match.group(1) if ext_match else "svg"


# ── 3. 다운로드 ────────────────────────────────────────────────────────────────

def _download_one(base_url: str, page_num: int, ext: str, tmp_dir: Path) -> tuple[int, bool]:
    url      = f"{base_url}/assets/pages/{page_num}.{ext}"
    filepath = tmp_dir / f"{page_num:04d}.{ext}"
    for attempt in range(RETRY):
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            filepath.write_bytes(r.content)
            return page_num, True
        except Exception:
            if attempt == RETRY - 1:
                return page_num, False
    return page_num, False


def download_all(
    base_url: str,
    total: int,
    ext: str,
    tmp_dir: Path,
    on_progress: Callable[[int, int], None] | None = None,
    stop_flag: Callable[[], bool] | None = None,
) -> list[int]:
    args    = [(base_url, i, ext, tmp_dir) for i in range(1, total + 1)]
    success = []
    done    = 0

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(_download_one, *arg): arg[1] for arg in args}
        for future in as_completed(futures):
            if stop_flag and stop_flag():
                executor.shutdown(wait=False, cancel_futures=True)
                break
            page_num, ok = future.result()
            if ok:
                success.append(page_num)
            done += 1
            if on_progress:
                on_progress(done, total)

    return sorted(success)


# ── 4. HTML 빌드 → PDF 변환 ────────────────────────────────────────────────────

def _build_html(pages: list[tuple[int, str]]) -> str:
    divs = "".join(
        f'<div class="page">{svg}</div>'
        for _, svg in pages
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:white; }}
.page {{ page-break-after:always; display:flex; justify-content:center; align-items:center; }}
.page svg {{ width:100%; height:auto; display:block; }}
</style></head>
<body>{divs}</body></html>"""


def convert_to_pdf(
    success_pages: list[int],
    ext: str,
    tmp_dir: Path,
    output_path: Path,
    on_progress: Callable[[int, int], None] | None = None,
    stop_flag: Callable[[], bool] | None = None,
) -> None:
    batches    = [success_pages[i:i+BATCH_SIZE] for i in range(0, len(success_pages), BATCH_SIZE)]
    total_b    = len(batches)
    batch_pdfs = []
    html_path  = tmp_dir / "batch.html"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        pw_page = browser.new_page()

        for idx, batch in enumerate(batches, 1):
            if stop_flag and stop_flag():
                break

            pages = [
                (n, (tmp_dir / f"{n:04d}.{ext}").read_text(encoding="utf-8", errors="replace"))
                for n in batch
            ]
            html_path.write_text(_build_html(pages), encoding="utf-8")
            pw_page.goto(f"file:///{html_path.as_posix()}", wait_until="load")

            batch_out = tmp_dir / f"batch_{idx:03d}.pdf"
            pw_page.pdf(path=str(batch_out), print_background=True, format="A4")
            batch_pdfs.append(batch_out)

            if on_progress:
                on_progress(idx, total_b)

        browser.close()

    writer = PdfWriter()
    for pdf in batch_pdfs:
        writer.append(str(pdf))
    with open(output_path, "wb") as f:
        writer.write(f)


# ── 5. 전체 실행 ───────────────────────────────────────────────────────────────

def run(
    url: str,
    on_info: Callable[[str], None] | None = None,
    on_download_progress: Callable[[int, int], None] | None = None,
    on_convert_progress: Callable[[int, int], None] | None = None,
    stop_flag: Callable[[], bool] | None = None,
) -> Path:
    def info(msg):
        if on_info:
            on_info(msg)
        else:
            print(msg)

    info("URL 분석 중...")
    base_url, book_name = parse_url(url)

    info("교재 정보 확인 중...")
    total, ext = fetch_book_info(base_url)
    info(f"총 {total}페이지 ({ext})")

    output_dir  = Path.home() / "Desktop" / "ebook_output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / f"{book_name}.pdf"

    tmp_dir = Path(tempfile.mkdtemp(prefix="ebook2pdf_"))
    try:
        info(f"다운로드 시작...")
        success = download_all(base_url, total, ext, tmp_dir, on_download_progress, stop_flag)

        if stop_flag and stop_flag():
            return None

        batch_count = (len(success) + BATCH_SIZE - 1) // BATCH_SIZE
        info(f"PDF 변환 시작... ({batch_count}배치)")
        convert_to_pdf(success, ext, tmp_dir, output_path, on_convert_progress, stop_flag)

        info(f"완료! → {output_path}")
        return output_path
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("사용법: python ebook2pdf.py <URL>")
        sys.exit(1)

    try:
        from tqdm import tqdm
        dl_bar  = tqdm(total=0, desc="다운로드", unit="p", position=0)
        cv_bar  = tqdm(total=0, desc="PDF변환",  unit="batch", position=1)

        def on_dl(cur, total):
            dl_bar.total = total
            dl_bar.n = cur
            dl_bar.refresh()

        def on_cv(cur, total):
            cv_bar.total = total
            cv_bar.n = cur
            cv_bar.refresh()

        run(sys.argv[1], on_download_progress=on_dl, on_convert_progress=on_cv)
        dl_bar.close()
        cv_bar.close()
    except ImportError:
        run(sys.argv[1])
