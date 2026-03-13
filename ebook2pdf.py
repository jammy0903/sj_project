"""
ebook2pdf.py - NexBook 웹뷰어 교재를 PDF로 변환

사용법:
    python ebook2pdf.py <URL>

예시:
    python ebook2pdf.py http://media.kgs.or.kr:8080/webbook/pro_new/sayong/index.html
"""

import sys
import re
import shutil
import tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from tqdm import tqdm
from pypdf import PdfWriter
from playwright.sync_api import sync_playwright


BATCH_SIZE = 20   # 한 번에 HTML로 합칠 페이지 수
WORKERS    = 8    # 병렬 다운로드 수
RETRY      = 3    # 페이지당 재시도 횟수


# ── 1. URL 파싱 ────────────────────────────────────────────────────────────────

def parse_url(url: str) -> tuple[str, str]:
    """URL → (base_url, book_name)"""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.endswith(".html"):
        path = path.rsplit("/", 1)[0]
    book_name = path.rsplit("/", 1)[-1]
    base_url  = f"{parsed.scheme}://{parsed.netloc}{path}"
    return base_url, book_name


# ── 2. data.js 파싱 ────────────────────────────────────────────────────────────

def fetch_book_info(base_url: str) -> tuple[int, str]:
    """data.js → (총 페이지 수, 파일 확장자)"""
    url = f"{base_url}/assets/data.js"
    r = requests.get(url, timeout=15)
    r.raise_for_status()

    total_match = re.search(r"totalPageNum\s*:\s*(\d+)", r.text)
    ext_match   = re.search(r"pageExt\s*:\s*['\"](\w+)['\"]", r.text)

    if not total_match:
        raise ValueError("data.js에서 totalPageNum을 찾지 못했습니다.")

    total = int(total_match.group(1))
    ext   = ext_match.group(1) if ext_match else "svg"
    return total, ext


# ── 3. 페이지 다운로드 ─────────────────────────────────────────────────────────

def _download_one(base_url: str, page_num: int, ext: str, tmp_dir: Path) -> tuple[int, bool]:
    url      = f"{base_url}/assets/pages/{page_num}.{ext}"
    filepath = tmp_dir / f"{page_num:04d}.{ext}"

    for attempt in range(RETRY):
        try:
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            filepath.write_bytes(r.content)
            return page_num, True
        except Exception as e:
            if attempt == RETRY - 1:
                print(f"\n  경고: {page_num}페이지 실패 - {e}")
    return page_num, False


def download_all(base_url: str, total: int, ext: str, tmp_dir: Path) -> list[int]:
    """모든 페이지 병렬 다운로드. 성공한 페이지 번호 목록 반환."""
    args    = [(base_url, i, ext, tmp_dir) for i in range(1, total + 1)]
    success = []

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = {executor.submit(_download_one, *arg): arg[1] for arg in args}
        with tqdm(total=total, desc="다운로드", unit="p") as bar:
            for future in as_completed(futures):
                page_num, ok = future.result()
                if ok:
                    success.append(page_num)
                bar.update(1)

    return sorted(success)


# ── 4. HTML 빌드 → PDF 변환 ────────────────────────────────────────────────────

def _get_page_size(tmp_dir: Path, ext: str) -> tuple[str, str]:
    """첫 SVG의 width/height를 읽어 PDF 용지 크기로 반환"""
    first = tmp_dir / f"0001.{ext}"
    text  = first.read_text(encoding="utf-8", errors="replace")
    m     = re.search(r'<svg[^>]+width="([^"]+)"[^>]+height="([^"]+)"', text)
    if m:
        return m.group(1), m.group(2)
    return "1401pt", "1908pt"   # 기본값 (KGS 교재 기준)


def _build_html(pages: list[tuple[int, str]]) -> str:
    """(page_num, svg_content) 리스트 → HTML 문자열"""
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


def batch_to_pdf(
    page_nums: list[int],
    ext: str,
    tmp_dir: Path,
    out_path: Path,
    pw_page,
    html_path: Path,
) -> None:
    """페이지 묶음을 읽어 HTML 만들고 Playwright로 PDF 저장"""
    pages = [
        (n, (tmp_dir / f"{n:04d}.{ext}").read_text(encoding="utf-8", errors="replace"))
        for n in page_nums
    ]
    html_path.write_text(_build_html(pages), encoding="utf-8")
    pw_page.goto(f"file:///{html_path.as_posix()}", wait_until="load")
    pw_page.pdf(path=str(out_path), print_background=True, format="A4")


def convert_to_pdf(
    success_pages: list[int],
    ext: str,
    tmp_dir: Path,
    output_path: Path,
) -> None:
    """배치 처리 → 개별 PDF 병합 → 최종 PDF"""
    batches    = [success_pages[i:i+BATCH_SIZE] for i in range(0, len(success_pages), BATCH_SIZE)]
    batch_pdfs = []
    html_path  = tmp_dir / "batch.html"

    with sync_playwright() as p:
        browser = p.chromium.launch()
        pw_page = browser.new_page()

        for idx, batch in enumerate(tqdm(batches, desc="PDF 변환", unit="batch"), 1):
            batch_out = tmp_dir / f"batch_{idx:03d}.pdf"
            batch_to_pdf(batch, ext, tmp_dir, batch_out, pw_page, html_path)
            batch_pdfs.append(batch_out)

        browser.close()

    # 배치 PDF 병합
    print("PDF 병합 중...")
    writer = PdfWriter()
    for pdf in batch_pdfs:
        writer.append(str(pdf))
    with open(output_path, "wb") as f:
        writer.write(f)


# ── 메인 ───────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("사용법: python ebook2pdf.py <URL>")
        sys.exit(1)

    url = sys.argv[1]

    print(f"\n[1/4] URL 분석 중...")
    base_url, book_name = parse_url(url)
    print(f"      base: {base_url}")
    print(f"      이름: {book_name}")

    print(f"\n[2/4] 교재 정보 확인 중...")
    total, ext = fetch_book_info(base_url)
    print(f"      총 {total}페이지 ({ext})")

    output_dir  = Path(__file__).parent / "output"
    output_dir.mkdir(exist_ok=True)
    output_path = output_dir / f"{book_name}.pdf"

    tmp_dir = Path(tempfile.mkdtemp(prefix="ebook2pdf_"))

    try:
        print(f"\n[3/4] 페이지 다운로드 중...")
        success = download_all(base_url, total, ext, tmp_dir)
        failed  = total - len(success)
        if failed:
            print(f"      경고: {failed}페이지 실패")

        batch_count = (len(success) + BATCH_SIZE - 1) // BATCH_SIZE
        print(f"\n[4/4] PDF 변환 중... ({batch_count}개 배치)")
        convert_to_pdf(success, ext, tmp_dir, output_path)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"\n완료! → {output_path}")
    print(f"       총 {len(success)}페이지")


if __name__ == "__main__":
    main()
