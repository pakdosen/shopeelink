#!/usr/bin/env python3
"""Modern desktop GUI for shopeelink.

Tampilan dibangun dengan customtkinter (fallback ke tkinter+ttk bila modul
customtkinter tidak tersedia). Konversi dijalankan di thread terpisah agar UI
tetap responsif.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
from typing import List, Tuple

# Pastikan paket lokal `shopeelink` (file shopeelink.py satu folder) bisa
# diimport baik saat dijalankan via `python shopeelink_gui.py` maupun saat
# dibundle PyInstaller (sys._MEIPASS).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import shopeelink  # noqa: E402

try:
    import customtkinter as ctk  # type: ignore
    _HAS_CTK = True
except Exception:  # pragma: no cover - fallback only
    _HAS_CTK = False
    import tkinter as tk
    from tkinter import ttk

APP_TITLE = "Shopee Link Converter"
APP_VERSION = "1.0.0"
DEFAULT_TIMEOUT = 15.0
RESULT_POLL_MS = 80


def _split_input(raw: str) -> List[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _format_results(results: List[Tuple[str, str | None, str | None]], show_input: bool) -> str:
    out_lines: List[str] = []
    for url, out, err in results:
        if err is not None:
            out_lines.append(f"ERROR\t{url}\t{err}")
        elif show_input:
            out_lines.append(f"{url}\t{out}")
        else:
            out_lines.append(out or "")
    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# Modern UI (customtkinter)
# ---------------------------------------------------------------------------


class _CTKApp:  # pragma: no cover - GUI
    def __init__(self) -> None:
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.root = ctk.CTk()
        self.root.title(APP_TITLE)
        self.root.geometry("960x680")
        self.root.minsize(720, 520)

        self._queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._worker: threading.Thread | None = None

        self._build()
        self._poll_queue()

    def _build(self) -> None:
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(2, weight=1)
        self.root.grid_rowconfigure(4, weight=1)

        # Header
        header = ctk.CTkFrame(self.root, corner_radius=0, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(20, 8))
        header.grid_columnconfigure(0, weight=1)

        title = ctk.CTkLabel(
            header,
            text=APP_TITLE,
            font=ctk.CTkFont(size=22, weight="bold"),
        )
        title.grid(row=0, column=0, sticky="w")

        subtitle = ctk.CTkLabel(
            header,
            text=(
                "Tempel satu atau banyak link pendek Shopee "
                "(s.shopee.co.id/...) — satu link per baris."
            ),
            text_color=("gray35", "gray70"),
        )
        subtitle.grid(row=1, column=0, sticky="w", pady=(2, 0))

        appearance = ctk.CTkSegmentedButton(
            header,
            values=["Light", "Dark", "System"],
            command=self._on_appearance_change,
        )
        appearance.set("System")
        appearance.grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))

        # Input label
        input_label = ctk.CTkLabel(
            self.root, text="Input link", font=ctk.CTkFont(weight="bold")
        )
        input_label.grid(row=1, column=0, sticky="w", padx=22, pady=(8, 4))

        # Input textbox
        self.input_box = ctk.CTkTextbox(
            self.root,
            height=180,
            font=ctk.CTkFont(family="Consolas", size=13),
            wrap="none",
        )
        self.input_box.grid(row=2, column=0, sticky="nsew", padx=20)
        self.input_box.insert("1.0", "https://s.shopee.co.id/1VvkmRGQgz\n")

        # Action bar
        actions = ctk.CTkFrame(self.root, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="ew", padx=20, pady=12)
        actions.grid_columnconfigure(4, weight=1)

        self.convert_btn = ctk.CTkButton(
            actions,
            text="Convert",
            width=140,
            height=36,
            font=ctk.CTkFont(weight="bold"),
            command=self._on_convert,
        )
        self.convert_btn.grid(row=0, column=0, padx=(0, 8))

        self.clear_btn = ctk.CTkButton(
            actions,
            text="Clear",
            width=100,
            height=36,
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "gray90"),
            border_color=("gray60", "gray40"),
            hover_color=("gray85", "gray25"),
            command=self._on_clear,
        )
        self.clear_btn.grid(row=0, column=1, padx=8)

        self.copy_btn = ctk.CTkButton(
            actions,
            text="Copy results",
            width=140,
            height=36,
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "gray90"),
            border_color=("gray60", "gray40"),
            hover_color=("gray85", "gray25"),
            command=self._on_copy,
        )
        self.copy_btn.grid(row=0, column=2, padx=8)

        self.show_input_var = ctk.BooleanVar(value=False)
        self.show_input_chk = ctk.CTkCheckBox(
            actions,
            text="Tampilkan input + output (TSV)",
            variable=self.show_input_var,
        )
        self.show_input_chk.grid(row=0, column=3, padx=12)

        self.status_lbl = ctk.CTkLabel(
            actions, text="Siap.", text_color=("gray35", "gray70")
        )
        self.status_lbl.grid(row=0, column=4, sticky="e")

        # Output label
        output_label = ctk.CTkLabel(
            self.root, text="Hasil", font=ctk.CTkFont(weight="bold")
        )
        output_label.grid(row=4, column=0, sticky="w", padx=22, pady=(0, 4))

        self.output_box = ctk.CTkTextbox(
            self.root,
            font=ctk.CTkFont(family="Consolas", size=13),
            wrap="none",
        )
        self.output_box.grid(row=5, column=0, sticky="nsew", padx=20, pady=(0, 16))
        self.output_box.configure(state="disabled")
        self.root.grid_rowconfigure(5, weight=2)

        # Progress bar (indeterminate while working)
        self.progress = ctk.CTkProgressBar(self.root)
        self.progress.grid(row=6, column=0, sticky="ew", padx=20, pady=(0, 16))
        self.progress.set(0)

        footer = ctk.CTkLabel(
            self.root,
            text=f"shopeelink v{APP_VERSION} — github.com/pakdosen/shopeelink",
            text_color=("gray45", "gray55"),
        )
        footer.grid(row=7, column=0, sticky="e", padx=20, pady=(0, 12))

    # ----- handlers -----
    def _on_appearance_change(self, value: str) -> None:
        ctk.set_appearance_mode(value)

    def _on_convert(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        urls = _split_input(self.input_box.get("1.0", "end"))
        if not urls:
            self._set_status("Tidak ada link untuk dikonversi.")
            return

        self._set_output("")
        self._set_status(f"Mengonversi {len(urls)} link…")
        self.convert_btn.configure(state="disabled")
        self.progress.configure(mode="indeterminate")
        self.progress.start()

        show_input = self.show_input_var.get()
        self._worker = threading.Thread(
            target=self._run_conversion, args=(urls, show_input), daemon=True
        )
        self._worker.start()

    def _run_conversion(self, urls: List[str], show_input: bool) -> None:
        try:
            results = shopeelink.convert_many(urls, timeout=DEFAULT_TIMEOUT)
            text = _format_results(results, show_input)
            errors = sum(1 for _, _, err in results if err is not None)
            self._queue.put(("done", (text, len(results), errors)))
        except Exception as e:  # noqa: BLE001
            self._queue.put(("error", str(e)))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "done":
                    text, total, errors = payload  # type: ignore[misc]
                    self._set_output(text)
                    if errors:
                        self._set_status(
                            f"Selesai: {total - errors}/{total} berhasil, {errors} gagal."
                        )
                    else:
                        self._set_status(f"Selesai: {total}/{total} berhasil.")
                    self.convert_btn.configure(state="normal")
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress.set(0)
                elif kind == "error":
                    self._set_output(f"ERROR: {payload}")
                    self._set_status("Terjadi error.")
                    self.convert_btn.configure(state="normal")
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress.set(0)
        except queue.Empty:
            pass
        self.root.after(RESULT_POLL_MS, self._poll_queue)

    def _on_clear(self) -> None:
        self.input_box.delete("1.0", "end")
        self._set_output("")
        self._set_status("Siap.")

    def _on_copy(self) -> None:
        text = self.output_box.get("1.0", "end").strip()
        if not text:
            self._set_status("Tidak ada hasil untuk disalin.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._set_status("Hasil disalin ke clipboard.")

    def _set_output(self, text: str) -> None:
        self.output_box.configure(state="normal")
        self.output_box.delete("1.0", "end")
        self.output_box.insert("1.0", text)
        self.output_box.configure(state="disabled")

    def _set_status(self, text: str) -> None:
        self.status_lbl.configure(text=text)

    def run(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Fallback UI (vanilla tkinter + ttk)
# ---------------------------------------------------------------------------


class _TkApp:  # pragma: no cover - GUI
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("900x640")

        self._queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._worker: threading.Thread | None = None

        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")

        self._build()
        self._poll_queue()

    def _build(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)
        root.rowconfigure(4, weight=2)

        header = ttk.Frame(root, padding=(16, 12))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)

        ttk.Label(header, text=APP_TITLE, font=("Segoe UI", 16, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(
            header,
            text="Tempel satu atau banyak link pendek Shopee, satu per baris.",
            foreground="#666",
        ).grid(row=1, column=0, sticky="w")

        ttk.Label(root, text="Input link").grid(row=1, column=0, sticky="w", padx=18)
        self.input_box = tk.Text(root, height=10, wrap="none", font=("Consolas", 11))
        self.input_box.grid(row=2, column=0, sticky="nsew", padx=16)
        self.input_box.insert("1.0", "https://s.shopee.co.id/1VvkmRGQgz\n")

        actions = ttk.Frame(root, padding=(16, 8))
        actions.grid(row=3, column=0, sticky="ew")
        actions.columnconfigure(4, weight=1)

        self.convert_btn = ttk.Button(actions, text="Convert", command=self._on_convert)
        self.convert_btn.grid(row=0, column=0, padx=(0, 6))
        ttk.Button(actions, text="Clear", command=self._on_clear).grid(row=0, column=1, padx=6)
        ttk.Button(actions, text="Copy results", command=self._on_copy).grid(row=0, column=2, padx=6)
        self.show_input_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            actions, text="Tampilkan input + output (TSV)", variable=self.show_input_var
        ).grid(row=0, column=3, padx=12)
        self.status_lbl = ttk.Label(actions, text="Siap.", foreground="#666")
        self.status_lbl.grid(row=0, column=4, sticky="e")

        ttk.Label(root, text="Hasil").grid(row=4, column=0, sticky="nw", padx=18, pady=(0, 0))

        output_frame = ttk.Frame(root)
        output_frame.grid(row=5, column=0, sticky="nsew", padx=16, pady=(4, 12))
        output_frame.columnconfigure(0, weight=1)
        output_frame.rowconfigure(0, weight=1)
        root.rowconfigure(5, weight=2)

        self.output_box = tk.Text(output_frame, wrap="none", font=("Consolas", 11), state="disabled")
        self.output_box.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(output_frame, orient="vertical", command=self.output_box.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.output_box.configure(yscrollcommand=scroll.set)

        self.progress = ttk.Progressbar(root, mode="determinate")
        self.progress.grid(row=6, column=0, sticky="ew", padx=16, pady=(0, 12))

    def _on_convert(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        urls = _split_input(self.input_box.get("1.0", "end"))
        if not urls:
            self._set_status("Tidak ada link untuk dikonversi.")
            return
        self._set_output("")
        self._set_status(f"Mengonversi {len(urls)} link…")
        self.convert_btn.configure(state="disabled")
        self.progress.configure(mode="indeterminate")
        self.progress.start(10)

        show_input = self.show_input_var.get()
        self._worker = threading.Thread(
            target=self._run_conversion, args=(urls, show_input), daemon=True
        )
        self._worker.start()

    def _run_conversion(self, urls: List[str], show_input: bool) -> None:
        try:
            results = shopeelink.convert_many(urls, timeout=DEFAULT_TIMEOUT)
            text = _format_results(results, show_input)
            errors = sum(1 for _, _, err in results if err is not None)
            self._queue.put(("done", (text, len(results), errors)))
        except Exception as e:  # noqa: BLE001
            self._queue.put(("error", str(e)))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "done":
                    text, total, errors = payload  # type: ignore[misc]
                    self._set_output(text)
                    if errors:
                        self._set_status(
                            f"Selesai: {total - errors}/{total} berhasil, {errors} gagal."
                        )
                    else:
                        self._set_status(f"Selesai: {total}/{total} berhasil.")
                    self.convert_btn.configure(state="normal")
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                elif kind == "error":
                    self._set_output(f"ERROR: {payload}")
                    self._set_status("Terjadi error.")
                    self.convert_btn.configure(state="normal")
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
        except queue.Empty:
            pass
        self.root.after(RESULT_POLL_MS, self._poll_queue)

    def _on_clear(self) -> None:
        self.input_box.delete("1.0", "end")
        self._set_output("")
        self._set_status("Siap.")

    def _on_copy(self) -> None:
        text = self.output_box.get("1.0", "end").strip()
        if not text:
            self._set_status("Tidak ada hasil untuk disalin.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._set_status("Hasil disalin ke clipboard.")

    def _set_output(self, text: str) -> None:
        self.output_box.configure(state="normal")
        self.output_box.delete("1.0", "end")
        self.output_box.insert("1.0", text)
        self.output_box.configure(state="disabled")

    def _set_status(self, text: str) -> None:
        self.status_lbl.configure(text=text)

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:  # pragma: no cover - entry point
    if _HAS_CTK:
        _CTKApp().run()
    else:
        _TkApp().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
