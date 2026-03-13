"""
gui.py - eBook PDF 변환기 GUI
"""

import threading
import subprocess
from pathlib import Path

import customtkinter as ctk
import ebook2pdf


# ── 테마 설정 ──────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("eBook PDF 변환기")
        self.geometry("620x420")
        self.resizable(False, False)

        self._stop_flag  = False
        self._running    = False

        self._build_ui()

    # ── UI 구성 ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # ── 타이틀
        ctk.CTkLabel(
            self, text="eBook → PDF 변환기",
            font=ctk.CTkFont(size=20, weight="bold")
        ).grid(row=0, column=0, pady=(24, 4))

        ctk.CTkLabel(
            self, text="NexBook 웹뷰어 URL을 입력하면 PDF로 자동 변환합니다.",
            font=ctk.CTkFont(size=12), text_color="gray"
        ).grid(row=1, column=0, pady=(0, 16))

        # ── URL 입력 + 버튼
        url_frame = ctk.CTkFrame(self, fg_color="transparent")
        url_frame.grid(row=2, column=0, padx=24, sticky="ew")
        url_frame.grid_columnconfigure(0, weight=1)

        self.url_entry = ctk.CTkEntry(
            url_frame, placeholder_text="http://...", height=40,
            font=ctk.CTkFont(size=13)
        )
        self.url_entry.grid(row=0, column=0, padx=(0, 8), sticky="ew")
        self.url_entry.bind("<Return>", lambda e: self._on_start())

        self.start_btn = ctk.CTkButton(
            url_frame, text="변환", width=80, height=40,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._on_start
        )
        self.start_btn.grid(row=0, column=1)

        # ── 진행 상황
        progress_frame = ctk.CTkFrame(self)
        progress_frame.grid(row=3, column=0, padx=24, pady=16, sticky="nsew")
        progress_frame.grid_columnconfigure(0, weight=1)

        # 다운로드
        ctk.CTkLabel(progress_frame, text="다운로드", font=ctk.CTkFont(size=12)).grid(
            row=0, column=0, padx=16, pady=(14, 2), sticky="w"
        )
        self.dl_bar = ctk.CTkProgressBar(progress_frame, height=14)
        self.dl_bar.grid(row=1, column=0, padx=16, sticky="ew")
        self.dl_bar.set(0)

        self.dl_label = ctk.CTkLabel(progress_frame, text="", font=ctk.CTkFont(size=11), text_color="gray")
        self.dl_label.grid(row=2, column=0, padx=16, sticky="w")

        # PDF 변환
        ctk.CTkLabel(progress_frame, text="PDF 변환", font=ctk.CTkFont(size=12)).grid(
            row=3, column=0, padx=16, pady=(10, 2), sticky="w"
        )
        self.cv_bar = ctk.CTkProgressBar(progress_frame, height=14)
        self.cv_bar.grid(row=4, column=0, padx=16, sticky="ew")
        self.cv_bar.set(0)

        self.cv_label = ctk.CTkLabel(progress_frame, text="", font=ctk.CTkFont(size=11), text_color="gray")
        self.cv_label.grid(row=5, column=0, padx=16, sticky="w")

        # 상태 메시지
        self.status_label = ctk.CTkLabel(
            progress_frame, text="URL을 입력하고 변환 버튼을 눌러주세요.",
            font=ctk.CTkFont(size=12), text_color="gray"
        )
        self.status_label.grid(row=6, column=0, padx=16, pady=(12, 4), sticky="w")

        # ── 하단: 저장 위치 + 폴더 열기
        bottom_frame = ctk.CTkFrame(self, fg_color="transparent")
        bottom_frame.grid(row=4, column=0, padx=24, pady=(0, 20), sticky="ew")
        bottom_frame.grid_columnconfigure(0, weight=1)

        output_dir = Path.home() / "Desktop" / "ebook_output"
        ctk.CTkLabel(
            bottom_frame,
            text=f"저장 위치: {output_dir}",
            font=ctk.CTkFont(size=11), text_color="gray"
        ).grid(row=0, column=0, sticky="w")

        self.open_btn = ctk.CTkButton(
            bottom_frame, text="폴더 열기", width=90, height=28,
            font=ctk.CTkFont(size=12), fg_color="gray30", hover_color="gray40",
            command=self._open_output_folder
        )
        self.open_btn.grid(row=0, column=1, padx=(8, 0))

    # ── 이벤트 핸들러 ─────────────────────────────────────────────────────────

    def _on_start(self):
        if self._running:
            return

        url = self.url_entry.get().strip()
        if not url:
            self._set_status("URL을 입력해주세요.", "orange")
            return

        self._stop_flag = False
        self._running   = True
        self.start_btn.configure(state="disabled", text="변환 중...")
        self.dl_bar.set(0)
        self.cv_bar.set(0)
        self.dl_label.configure(text="")
        self.cv_label.configure(text="")
        self._set_status("시작 중...", "gray")

        thread = threading.Thread(target=self._run_conversion, args=(url,), daemon=True)
        thread.start()

    def _run_conversion(self, url: str):
        try:
            ebook2pdf.run(
                url,
                on_info=self._on_info,
                on_download_progress=self._on_dl_progress,
                on_convert_progress=self._on_cv_progress,
                stop_flag=lambda: self._stop_flag,
            )
        except Exception as e:
            self.after(0, self._set_status, f"오류: {e}", "red")
        finally:
            self.after(0, self._on_done)

    def _on_done(self):
        self._running = False
        self.start_btn.configure(state="normal", text="변환")

    def _on_info(self, msg: str):
        self.after(0, self._set_status, msg, "gray")

    def _on_dl_progress(self, cur: int, total: int):
        def update():
            self.dl_bar.set(cur / total)
            self.dl_label.configure(text=f"{cur} / {total} 페이지")
        self.after(0, update)

    def _on_cv_progress(self, cur: int, total: int):
        def update():
            self.cv_bar.set(cur / total)
            self.cv_label.configure(text=f"{cur} / {total} 배치")
            if cur == total:
                self._set_status("완료!", "#2ecc71")
        self.after(0, update)

    def _set_status(self, msg: str, color: str = "gray"):
        self.status_label.configure(text=msg, text_color=color)

    def _open_output_folder(self):
        output_dir = Path.home() / "Desktop" / "ebook_output"
        output_dir.mkdir(exist_ok=True)
        subprocess.Popen(f'explorer "{output_dir}"')


# ── 진입점 ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()
