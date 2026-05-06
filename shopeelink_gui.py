#!/usr/bin/env python3
"""Modern desktop GUI for shopeelink.

Tampilan dibangun dengan customtkinter (fallback ke tkinter+ttk bila modul
customtkinter tidak tersedia). Konversi dijalankan di thread terpisah agar UI
tetap responsif.

Aplikasi terdiri dari dua tab:
- **Decode Short Link** — convert short link Shopee ke link produk asli.
- **Generate Affiliate Link** — generate short affiliate link dari long URL +
  tag (sub-id). Sejak v1.3 fitur ini men-drive Chrome Anda via Playwright
  (``channel="chrome"``) supaya request ditandatangani oleh JS SDK Shopee
  sendiri — replay cURL tidak bekerja karena anti-bot signature.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
from typing import List, Tuple

# Pastikan modul lokal (shopeelink, affiliate) bisa diimport baik saat
# dijalankan via `python shopeelink_gui.py` maupun saat dibundle PyInstaller
# (sys._MEIPASS).
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import affiliate  # noqa: E402
import affiliate_browser  # noqa: E402
import shopeelink  # noqa: E402

try:
    import customtkinter as ctk  # type: ignore
    _HAS_CTK = True
except Exception:  # pragma: no cover - fallback only
    _HAS_CTK = False

import tkinter as tk  # noqa: E402
from tkinter import messagebox, ttk  # noqa: E402

APP_TITLE = "Shopee Link Converter"
APP_VERSION = "1.3.0"
DEFAULT_TIMEOUT = 15.0
RESULT_POLL_MS = 80
# How long the "Hubungkan Chrome" worker waits for the user to log in.
LOGIN_TIMEOUT_S = 600


def _split_input(raw: str) -> List[str]:
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _format_decode_results(
    results: List[Tuple[str, str | None, str | None]], show_input: bool
) -> str:
    out_lines: List[str] = []
    for url, out, err in results:
        if err is not None:
            out_lines.append(f"ERROR\t{url}\t{err}")
        elif show_input:
            out_lines.append(f"{url}\t{out}")
        else:
            out_lines.append(out or "")
    return "\n".join(out_lines)


def _format_affiliate_results(
    results: List[affiliate.LinkResult], show_input: bool
) -> str:
    out_lines: List[str] = []
    for r in results:
        if r.error:
            out_lines.append(f"ERROR\t{r.original_link}\t{r.error}")
        elif r.fail_code:
            out_lines.append(
                f"FAIL\t{r.original_link}\tfailCode={r.fail_code}"
            )
        elif show_input:
            out_lines.append(f"{r.original_link}\t{r.short_link}")
        else:
            out_lines.append(r.short_link or "")
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
        self.root.geometry("980x720")
        self.root.minsize(820, 560)

        self._queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._decode_worker: threading.Thread | None = None
        self._affiliate_worker: threading.Thread | None = None
        self._connect_worker: threading.Thread | None = None

        self._build()
        self._poll_queue()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build(self) -> None:
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=1)

        # Header
        header = ctk.CTkFrame(self.root, corner_radius=0, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(20, 4))
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header,
            text=APP_TITLE,
            font=ctk.CTkFont(size=22, weight="bold"),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            header,
            text=(
                "Decode short link Shopee jadi link produk, atau generate "
                "short affiliate link dari long URL + tag."
            ),
            text_color=("gray35", "gray70"),
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))

        appearance = ctk.CTkSegmentedButton(
            header,
            values=["Light", "Dark", "System"],
            command=lambda v: ctk.set_appearance_mode(v),
        )
        appearance.set("System")
        appearance.grid(row=0, column=1, rowspan=2, sticky="e", padx=(12, 0))

        # Tabs
        self.tabs = ctk.CTkTabview(self.root)
        self.tabs.grid(row=1, column=0, sticky="nsew", padx=20, pady=(8, 8))
        self.tab_decode = self.tabs.add("Decode Short Link")
        self.tab_generate = self.tabs.add("Generate Affiliate Link")

        self._build_decode_tab(self.tab_decode)
        self._build_generate_tab(self.tab_generate)

        ctk.CTkLabel(
            self.root,
            text=f"shopeelink v{APP_VERSION} — github.com/pakdosen/shopeelink",
            text_color=("gray45", "gray55"),
        ).grid(row=2, column=0, sticky="e", padx=20, pady=(0, 12))

    def _build_decode_tab(self, parent) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)
        parent.grid_rowconfigure(4, weight=2)

        ctk.CTkLabel(
            parent, text="Input link", font=ctk.CTkFont(weight="bold")
        ).grid(row=0, column=0, sticky="w", padx=4, pady=(8, 4))

        self.dec_input_box = ctk.CTkTextbox(
            parent,
            height=160,
            font=ctk.CTkFont(family="Consolas", size=13),
            wrap="none",
        )
        self.dec_input_box.grid(row=1, column=0, sticky="nsew", padx=2)
        self.dec_input_box.insert("1.0", "https://s.shopee.co.id/1VvkmRGQgz\n")

        actions = ctk.CTkFrame(parent, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="ew", padx=2, pady=12)
        actions.grid_columnconfigure(4, weight=1)

        self.dec_convert_btn = ctk.CTkButton(
            actions,
            text="Convert",
            width=140,
            height=36,
            font=ctk.CTkFont(weight="bold"),
            command=self._on_decode_convert,
        )
        self.dec_convert_btn.grid(row=0, column=0, padx=(0, 8))

        self.dec_clear_btn = ctk.CTkButton(
            actions,
            text="Clear",
            width=100,
            height=36,
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "gray90"),
            border_color=("gray60", "gray40"),
            hover_color=("gray85", "gray25"),
            command=self._on_decode_clear,
        )
        self.dec_clear_btn.grid(row=0, column=1, padx=8)

        self.dec_copy_btn = ctk.CTkButton(
            actions,
            text="Copy results",
            width=140,
            height=36,
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "gray90"),
            border_color=("gray60", "gray40"),
            hover_color=("gray85", "gray25"),
            command=self._on_decode_copy,
        )
        self.dec_copy_btn.grid(row=0, column=2, padx=8)

        self.dec_show_input_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            actions,
            text="Tampilkan input + output (TSV)",
            variable=self.dec_show_input_var,
        ).grid(row=0, column=3, padx=12)

        self.dec_status_lbl = ctk.CTkLabel(
            actions, text="Siap.", text_color=("gray35", "gray70")
        )
        self.dec_status_lbl.grid(row=0, column=4, sticky="e")

        ctk.CTkLabel(
            parent, text="Hasil", font=ctk.CTkFont(weight="bold")
        ).grid(row=3, column=0, sticky="w", padx=4, pady=(0, 4))

        self.dec_output_box = ctk.CTkTextbox(
            parent,
            font=ctk.CTkFont(family="Consolas", size=13),
            wrap="none",
        )
        self.dec_output_box.grid(row=4, column=0, sticky="nsew", padx=2, pady=(0, 8))
        self.dec_output_box.configure(state="disabled")

        self.dec_progress = ctk.CTkProgressBar(parent)
        self.dec_progress.grid(row=5, column=0, sticky="ew", padx=2, pady=(0, 8))
        self.dec_progress.set(0)

    def _build_generate_tab(self, parent) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(3, weight=1)
        parent.grid_rowconfigure(6, weight=2)

        # --- Session card ---
        session_card = ctk.CTkFrame(parent)
        session_card.grid(row=0, column=0, sticky="ew", padx=2, pady=(8, 8))
        session_card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            session_card, text="Profile Chrome", font=ctk.CTkFont(weight="bold")
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 0))

        self.gen_session_lbl = ctk.CTkLabel(
            session_card,
            text=self._session_status_text(),
            text_color=("gray35", "gray70"),
            anchor="w",
            justify="left",
        )
        self.gen_session_lbl.grid(
            row=1, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 8)
        )

        self.gen_connect_btn = ctk.CTkButton(
            session_card,
            text="Hubungkan Chrome…",
            width=180,
            height=32,
            command=self._on_connect_browser,
        )
        self.gen_connect_btn.grid(row=2, column=0, sticky="w", padx=12, pady=(0, 12))

        ctk.CTkButton(
            session_card,
            text="Reset profile",
            width=120,
            height=32,
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "gray90"),
            border_color=("gray60", "gray40"),
            hover_color=("gray85", "gray25"),
            command=self._on_reset_profile,
        ).grid(row=2, column=1, sticky="w", padx=(0, 12), pady=(0, 12))

        ctk.CTkLabel(
            session_card,
            text=(
                "Klik 'Hubungkan Chrome…' — sebuah window Chrome akan terbuka "
                "dengan profile khusus shopeelink. Login ke "
                "affiliate.shopee.co.id sekali; profile-nya tersimpan dan "
                "tidak perlu login ulang. Anti-bot Shopee diatasi karena "
                "request dikirim dari halaman dashboard yang asli."
            ),
            text_color=("gray45", "gray55"),
            wraplength=800,
            justify="left",
        ).grid(row=3, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 10))

        # --- Tag inputs ---
        ctk.CTkLabel(
            parent, text="Tag (Sub-ID)", font=ctk.CTkFont(weight="bold")
        ).grid(row=1, column=0, sticky="w", padx=4, pady=(8, 2))
        ctk.CTkLabel(
            parent,
            text=(
                "Bisa kosong; kalau diisi, akan diterapkan ke SEMUA link yang "
                "Anda generate kali ini (sama seperti dashboard Shopee)."
            ),
            text_color=("gray45", "gray70"),
        ).grid(row=1, column=0, sticky="e", padx=4, pady=(8, 2))

        tag_frame = ctk.CTkFrame(parent, fg_color="transparent")
        tag_frame.grid(row=2, column=0, sticky="ew", padx=2)
        for c in range(5):
            tag_frame.grid_columnconfigure(c, weight=1)

        self.gen_tag_entries: List["ctk.CTkEntry"] = []
        for i in range(5):
            ctk.CTkLabel(tag_frame, text=f"Tag {i + 1}").grid(
                row=0, column=i, sticky="w", padx=(8 if i else 0, 8)
            )
            entry = ctk.CTkEntry(tag_frame, placeholder_text=f"PF{i + 1}")
            entry.grid(row=1, column=i, sticky="ew", padx=(8 if i else 0, 8), pady=(2, 8))
            self.gen_tag_entries.append(entry)

        # --- Long URL input ---
        ctk.CTkLabel(
            parent, text="Long URL (1 per baris)", font=ctk.CTkFont(weight="bold")
        ).grid(row=3, column=0, sticky="nw", padx=4, pady=(4, 4))

        self.gen_input_box = ctk.CTkTextbox(
            parent,
            height=140,
            font=ctk.CTkFont(family="Consolas", size=13),
            wrap="none",
        )
        self.gen_input_box.grid(row=3, column=0, sticky="nsew", padx=2, pady=(28, 0))
        self.gen_input_box.insert(
            "1.0", "https://shopee.co.id/product/274540732/28055610077\n"
        )

        # --- Action bar ---
        gen_actions = ctk.CTkFrame(parent, fg_color="transparent")
        gen_actions.grid(row=4, column=0, sticky="ew", padx=2, pady=12)
        gen_actions.grid_columnconfigure(4, weight=1)

        self.gen_generate_btn = ctk.CTkButton(
            gen_actions,
            text="Generate",
            width=140,
            height=36,
            font=ctk.CTkFont(weight="bold"),
            command=self._on_generate,
        )
        self.gen_generate_btn.grid(row=0, column=0, padx=(0, 8))

        ctk.CTkButton(
            gen_actions,
            text="Clear",
            width=100,
            height=36,
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "gray90"),
            border_color=("gray60", "gray40"),
            hover_color=("gray85", "gray25"),
            command=self._on_generate_clear,
        ).grid(row=0, column=1, padx=8)

        ctk.CTkButton(
            gen_actions,
            text="Copy results",
            width=140,
            height=36,
            fg_color="transparent",
            border_width=1,
            text_color=("gray10", "gray90"),
            border_color=("gray60", "gray40"),
            hover_color=("gray85", "gray25"),
            command=self._on_generate_copy,
        ).grid(row=0, column=2, padx=8)

        self.gen_show_input_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            gen_actions,
            text="Tampilkan input + output (TSV)",
            variable=self.gen_show_input_var,
        ).grid(row=0, column=3, padx=12)

        self.gen_status_lbl = ctk.CTkLabel(
            gen_actions, text="Siap.", text_color=("gray35", "gray70")
        )
        self.gen_status_lbl.grid(row=0, column=4, sticky="e")

        # --- Output ---
        ctk.CTkLabel(
            parent, text="Hasil short link", font=ctk.CTkFont(weight="bold")
        ).grid(row=5, column=0, sticky="w", padx=4, pady=(0, 4))

        self.gen_output_box = ctk.CTkTextbox(
            parent,
            font=ctk.CTkFont(family="Consolas", size=13),
            wrap="none",
        )
        self.gen_output_box.grid(row=6, column=0, sticky="nsew", padx=2, pady=(0, 8))
        self.gen_output_box.configure(state="disabled")

        self.gen_progress = ctk.CTkProgressBar(parent)
        self.gen_progress.grid(row=7, column=0, sticky="ew", padx=2, pady=(0, 8))
        self.gen_progress.set(0)

    # ------------------------------------------------------------------
    # Decode handlers
    # ------------------------------------------------------------------

    def _on_decode_convert(self) -> None:
        if self._decode_worker is not None and self._decode_worker.is_alive():
            return
        urls = _split_input(self.dec_input_box.get("1.0", "end"))
        if not urls:
            self._set_decode_status("Tidak ada link untuk dikonversi.")
            return
        self._set_decode_output("")
        self._set_decode_status(f"Mengonversi {len(urls)} link…")
        self.dec_convert_btn.configure(state="disabled")
        self.dec_progress.configure(mode="indeterminate")
        self.dec_progress.start()

        show_input = self.dec_show_input_var.get()
        self._decode_worker = threading.Thread(
            target=self._run_decode, args=(urls, show_input), daemon=True
        )
        self._decode_worker.start()

    def _run_decode(self, urls: List[str], show_input: bool) -> None:
        try:
            results = shopeelink.convert_many(urls, timeout=DEFAULT_TIMEOUT)
            text = _format_decode_results(results, show_input)
            errors = sum(1 for _, _, err in results if err is not None)
            self._queue.put(("decode_done", (text, len(results), errors)))
        except Exception as e:  # noqa: BLE001
            self._queue.put(("decode_error", str(e)))

    def _on_decode_clear(self) -> None:
        self.dec_input_box.delete("1.0", "end")
        self._set_decode_output("")
        self._set_decode_status("Siap.")

    def _on_decode_copy(self) -> None:
        text = self.dec_output_box.get("1.0", "end").strip()
        if not text:
            self._set_decode_status("Tidak ada hasil untuk disalin.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._set_decode_status("Hasil disalin ke clipboard.")

    def _set_decode_output(self, text: str) -> None:
        self.dec_output_box.configure(state="normal")
        self.dec_output_box.delete("1.0", "end")
        self.dec_output_box.insert("1.0", text)
        self.dec_output_box.configure(state="disabled")

    def _set_decode_status(self, text: str) -> None:
        self.dec_status_lbl.configure(text=text)

    # ------------------------------------------------------------------
    # Generate (affiliate) handlers
    # ------------------------------------------------------------------

    def _session_status_text(self) -> str:
        return affiliate_browser.profile_summary()

    def _refresh_session_label(self) -> None:
        self.gen_session_lbl.configure(text=self._session_status_text())

    def _on_connect_browser(self) -> None:
        if self._connect_worker is not None and self._connect_worker.is_alive():
            return
        ok, msg = affiliate_browser.ensure_browser_available()
        if not ok:
            messagebox.showerror("Playwright belum siap", msg, parent=self.root)
            return
        self.gen_connect_btn.configure(state="disabled")
        self._set_generate_status(
            "Membuka Chrome — silakan login di window yang muncul…"
        )
        self.gen_progress.configure(mode="indeterminate")
        self.gen_progress.start()
        self._connect_worker = threading.Thread(
            target=self._run_connect, daemon=True
        )
        self._connect_worker.start()

    def _run_connect(self) -> None:
        try:
            with affiliate_browser.BrowserSession(headless=False) as bs:
                ok = bs.wait_for_login(timeout_s=LOGIN_TIMEOUT_S)
            if ok:
                self._queue.put(("connect_done", "Login terdeteksi — profile tersimpan."))
            else:
                self._queue.put(
                    (
                        "connect_error",
                        "Tidak terdeteksi login dalam waktu yang ditentukan. "
                        "Coba lagi.",
                    )
                )
        except affiliate.AffiliateError as e:
            self._queue.put(("connect_error", str(e)))
        except Exception as e:  # noqa: BLE001
            self._queue.put(("connect_error", f"Error tak terduga: {e}"))

    def _on_reset_profile(self) -> None:
        if not messagebox.askyesno(
            "Reset profile Chrome",
            "Hapus profile Chrome shopeelink? Anda perlu klik 'Hubungkan "
            "Chrome…' dan login ulang setelah ini.",
            parent=self.root,
        ):
            return
        try:
            affiliate_browser.reset_profile()
        except affiliate.AffiliateError as e:
            messagebox.showerror("Reset gagal", str(e), parent=self.root)
            return
        self._refresh_session_label()
        self._set_generate_status("Profile Chrome di-reset.")

    def _on_generate(self) -> None:
        if self._affiliate_worker is not None and self._affiliate_worker.is_alive():
            return
        ok, msg = affiliate_browser.ensure_browser_available()
        if not ok:
            messagebox.showerror("Playwright belum siap", msg, parent=self.root)
            return
        urls = _split_input(self.gen_input_box.get("1.0", "end"))
        if not urls:
            self._set_generate_status("Tidak ada long URL untuk di-generate.")
            return
        sub_ids = tuple(e.get().strip() for e in self.gen_tag_entries[:5])
        # Pad to 5 elements just in case.
        while len(sub_ids) < 5:
            sub_ids = sub_ids + ("",)
        sub_ids = sub_ids[:5]  # type: ignore[assignment]

        items = [
            affiliate.LinkInput(original_link=u, sub_ids=sub_ids)  # type: ignore[arg-type]
            for u in urls
        ]
        self._set_generate_output("")
        self._set_generate_status(f"Generate {len(items)} link…")
        self.gen_generate_btn.configure(state="disabled")
        self.gen_progress.configure(mode="indeterminate")
        self.gen_progress.start()

        show_input = self.gen_show_input_var.get()
        self._affiliate_worker = threading.Thread(
            target=self._run_generate, args=(items, show_input), daemon=True
        )
        self._affiliate_worker.start()

    def _run_generate(
        self,
        items: List[affiliate.LinkInput],
        show_input: bool,
    ) -> None:
        try:
            with affiliate_browser.BrowserSession(headless=False) as bs:
                results = bs.generate(items)
            text = _format_affiliate_results(results, show_input)
            errors = sum(1 for r in results if not r.ok)
            self._queue.put(("affiliate_done", (text, len(results), errors)))
        except affiliate.AffiliateError as e:
            self._queue.put(("affiliate_error", str(e)))
        except Exception as e:  # noqa: BLE001
            self._queue.put(("affiliate_error", f"Error tak terduga: {e}"))

    def _on_generate_clear(self) -> None:
        self.gen_input_box.delete("1.0", "end")
        for e in self.gen_tag_entries:
            e.delete(0, "end")
        self._set_generate_output("")
        self._set_generate_status("Siap.")

    def _on_generate_copy(self) -> None:
        text = self.gen_output_box.get("1.0", "end").strip()
        if not text:
            self._set_generate_status("Tidak ada hasil untuk disalin.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._set_generate_status("Hasil disalin ke clipboard.")

    def _set_generate_output(self, text: str) -> None:
        self.gen_output_box.configure(state="normal")
        self.gen_output_box.delete("1.0", "end")
        self.gen_output_box.insert("1.0", text)
        self.gen_output_box.configure(state="disabled")

    def _set_generate_status(self, text: str) -> None:
        self.gen_status_lbl.configure(text=text)

    # ------------------------------------------------------------------
    # Queue polling (single dispatcher for both tabs)
    # ------------------------------------------------------------------

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "decode_done":
                    text, total, errors = payload  # type: ignore[misc]
                    self._set_decode_output(text)
                    if errors:
                        self._set_decode_status(
                            f"Selesai: {total - errors}/{total} berhasil, {errors} gagal."
                        )
                    else:
                        self._set_decode_status(f"Selesai: {total}/{total} berhasil.")
                    self.dec_convert_btn.configure(state="normal")
                    self.dec_progress.stop()
                    self.dec_progress.configure(mode="determinate")
                    self.dec_progress.set(0)
                elif kind == "decode_error":
                    self._set_decode_output(f"ERROR: {payload}")
                    self._set_decode_status("Terjadi error.")
                    self.dec_convert_btn.configure(state="normal")
                    self.dec_progress.stop()
                    self.dec_progress.configure(mode="determinate")
                    self.dec_progress.set(0)
                elif kind == "affiliate_done":
                    text, total, errors = payload  # type: ignore[misc]
                    self._set_generate_output(text)
                    if errors:
                        self._set_generate_status(
                            f"Selesai: {total - errors}/{total} berhasil, {errors} gagal."
                        )
                    else:
                        self._set_generate_status(f"Selesai: {total}/{total} berhasil.")
                    self.gen_generate_btn.configure(state="normal")
                    self.gen_progress.stop()
                    self.gen_progress.configure(mode="determinate")
                    self.gen_progress.set(0)
                elif kind == "affiliate_error":
                    self._set_generate_output(f"ERROR: {payload}")
                    self._set_generate_status("Terjadi error.")
                    self.gen_generate_btn.configure(state="normal")
                    self.gen_progress.stop()
                    self.gen_progress.configure(mode="determinate")
                    self.gen_progress.set(0)
                elif kind == "connect_done":
                    self._refresh_session_label()
                    self._set_generate_status(str(payload))
                    self.gen_connect_btn.configure(state="normal")
                    self.gen_progress.stop()
                    self.gen_progress.configure(mode="determinate")
                    self.gen_progress.set(0)
                elif kind == "connect_error":
                    self._refresh_session_label()
                    self._set_generate_output(f"ERROR: {payload}")
                    self._set_generate_status("Connect Chrome gagal.")
                    self.gen_connect_btn.configure(state="normal")
                    self.gen_progress.stop()
                    self.gen_progress.configure(mode="determinate")
                    self.gen_progress.set(0)
        except queue.Empty:
            pass
        self.root.after(RESULT_POLL_MS, self._poll_queue)

    def run(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Fallback UI (vanilla tkinter + ttk) — basic 2-tab parity
# ---------------------------------------------------------------------------


class _TkApp:  # pragma: no cover - GUI
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("960x700")

        self._queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._decode_worker: threading.Thread | None = None
        self._affiliate_worker: threading.Thread | None = None
        self._connect_worker: threading.Thread | None = None

        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")

        self._build()
        self._poll_queue()

    def _build(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        header = ttk.Frame(root, padding=(16, 12))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text=APP_TITLE, font=("Segoe UI", 16, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Decode short link Shopee, atau generate short affiliate link.",
            foreground="#666",
        ).grid(row=1, column=0, sticky="w")

        notebook = ttk.Notebook(root)
        notebook.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))
        self.tab_decode = ttk.Frame(notebook)
        self.tab_generate = ttk.Frame(notebook)
        notebook.add(self.tab_decode, text="Decode Short Link")
        notebook.add(self.tab_generate, text="Generate Affiliate Link")

        self._build_decode_tab(self.tab_decode)
        self._build_generate_tab(self.tab_generate)

    def _build_decode_tab(self, parent) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        parent.rowconfigure(4, weight=2)

        ttk.Label(parent, text="Input link").grid(row=0, column=0, sticky="w", padx=10, pady=(8, 2))
        self.dec_input_box = tk.Text(parent, height=8, wrap="none", font=("Consolas", 11))
        self.dec_input_box.grid(row=1, column=0, sticky="nsew", padx=8)
        self.dec_input_box.insert("1.0", "https://s.shopee.co.id/1VvkmRGQgz\n")

        actions = ttk.Frame(parent, padding=(8, 8))
        actions.grid(row=2, column=0, sticky="ew")
        actions.columnconfigure(4, weight=1)

        self.dec_convert_btn = ttk.Button(actions, text="Convert", command=self._on_decode_convert)
        self.dec_convert_btn.grid(row=0, column=0, padx=(0, 6))
        ttk.Button(actions, text="Clear", command=self._on_decode_clear).grid(row=0, column=1, padx=6)
        ttk.Button(actions, text="Copy results", command=self._on_decode_copy).grid(row=0, column=2, padx=6)
        self.dec_show_input_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(actions, text="Tampilkan input + output", variable=self.dec_show_input_var).grid(row=0, column=3, padx=12)
        self.dec_status_lbl = ttk.Label(actions, text="Siap.", foreground="#666")
        self.dec_status_lbl.grid(row=0, column=4, sticky="e")

        ttk.Label(parent, text="Hasil").grid(row=3, column=0, sticky="w", padx=10)
        self.dec_output_box = tk.Text(parent, wrap="none", font=("Consolas", 11), state="disabled")
        self.dec_output_box.grid(row=4, column=0, sticky="nsew", padx=8, pady=(2, 8))
        self.dec_progress = ttk.Progressbar(parent, mode="determinate")
        self.dec_progress.grid(row=5, column=0, sticky="ew", padx=8, pady=(0, 8))

    def _build_generate_tab(self, parent) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(3, weight=1)
        parent.rowconfigure(6, weight=2)

        session_card = ttk.LabelFrame(parent, text="Profile Chrome", padding=(10, 6))
        session_card.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 6))
        session_card.columnconfigure(0, weight=1)
        self.gen_session_lbl = ttk.Label(session_card, text=self._session_status_text(), foreground="#666")
        self.gen_session_lbl.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.gen_connect_btn = ttk.Button(session_card, text="Hubungkan Chrome…", command=self._on_connect_browser)
        self.gen_connect_btn.grid(row=1, column=0, sticky="w")
        ttk.Button(session_card, text="Reset profile", command=self._on_reset_profile).grid(row=1, column=1, sticky="w", padx=(8, 0))

        tag_frame = ttk.Frame(parent, padding=(8, 6))
        tag_frame.grid(row=2, column=0, sticky="ew")
        for c in range(5):
            tag_frame.columnconfigure(c, weight=1)
        self.gen_tag_entries: List[ttk.Entry] = []
        for i in range(5):
            ttk.Label(tag_frame, text=f"Tag {i + 1}").grid(row=0, column=i, sticky="w", padx=(0 if i == 0 else 6, 6))
            entry = ttk.Entry(tag_frame)
            entry.grid(row=1, column=i, sticky="ew", padx=(0 if i == 0 else 6, 6))
            self.gen_tag_entries.append(entry)

        ttk.Label(parent, text="Long URL (1 per baris)").grid(row=3, column=0, sticky="w", padx=10, pady=(8, 2))
        self.gen_input_box = tk.Text(parent, height=6, wrap="none", font=("Consolas", 11))
        self.gen_input_box.grid(row=3, column=0, sticky="nsew", padx=8, pady=(28, 0))
        self.gen_input_box.insert("1.0", "https://shopee.co.id/product/274540732/28055610077\n")

        actions = ttk.Frame(parent, padding=(8, 8))
        actions.grid(row=4, column=0, sticky="ew")
        actions.columnconfigure(4, weight=1)
        self.gen_generate_btn = ttk.Button(actions, text="Generate", command=self._on_generate)
        self.gen_generate_btn.grid(row=0, column=0, padx=(0, 6))
        ttk.Button(actions, text="Clear", command=self._on_generate_clear).grid(row=0, column=1, padx=6)
        ttk.Button(actions, text="Copy results", command=self._on_generate_copy).grid(row=0, column=2, padx=6)
        self.gen_show_input_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(actions, text="Tampilkan input + output", variable=self.gen_show_input_var).grid(row=0, column=3, padx=12)
        self.gen_status_lbl = ttk.Label(actions, text="Siap.", foreground="#666")
        self.gen_status_lbl.grid(row=0, column=4, sticky="e")

        ttk.Label(parent, text="Hasil short link").grid(row=5, column=0, sticky="w", padx=10)
        self.gen_output_box = tk.Text(parent, wrap="none", font=("Consolas", 11), state="disabled")
        self.gen_output_box.grid(row=6, column=0, sticky="nsew", padx=8, pady=(2, 8))
        self.gen_progress = ttk.Progressbar(parent, mode="determinate")
        self.gen_progress.grid(row=7, column=0, sticky="ew", padx=8, pady=(0, 8))

    # ----- decode handlers -----
    def _on_decode_convert(self) -> None:
        if self._decode_worker is not None and self._decode_worker.is_alive():
            return
        urls = _split_input(self.dec_input_box.get("1.0", "end"))
        if not urls:
            self._set_decode_status("Tidak ada link untuk dikonversi.")
            return
        self._set_decode_output("")
        self._set_decode_status(f"Mengonversi {len(urls)} link…")
        self.dec_convert_btn.configure(state="disabled")
        self.dec_progress.configure(mode="indeterminate")
        self.dec_progress.start(10)
        show_input = self.dec_show_input_var.get()
        self._decode_worker = threading.Thread(
            target=self._run_decode, args=(urls, show_input), daemon=True
        )
        self._decode_worker.start()

    def _run_decode(self, urls: List[str], show_input: bool) -> None:
        try:
            results = shopeelink.convert_many(urls, timeout=DEFAULT_TIMEOUT)
            text = _format_decode_results(results, show_input)
            errors = sum(1 for _, _, err in results if err is not None)
            self._queue.put(("decode_done", (text, len(results), errors)))
        except Exception as e:  # noqa: BLE001
            self._queue.put(("decode_error", str(e)))

    def _on_decode_clear(self) -> None:
        self.dec_input_box.delete("1.0", "end")
        self._set_decode_output("")
        self._set_decode_status("Siap.")

    def _on_decode_copy(self) -> None:
        text = self.dec_output_box.get("1.0", "end").strip()
        if not text:
            self._set_decode_status("Tidak ada hasil untuk disalin.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._set_decode_status("Hasil disalin ke clipboard.")

    def _set_decode_output(self, text: str) -> None:
        self.dec_output_box.configure(state="normal")
        self.dec_output_box.delete("1.0", "end")
        self.dec_output_box.insert("1.0", text)
        self.dec_output_box.configure(state="disabled")

    def _set_decode_status(self, text: str) -> None:
        self.dec_status_lbl.configure(text=text)

    # ----- generate handlers -----
    def _session_status_text(self) -> str:
        return affiliate_browser.profile_summary()

    def _refresh_session_label(self) -> None:
        self.gen_session_lbl.configure(text=self._session_status_text())

    def _on_connect_browser(self) -> None:
        if self._connect_worker is not None and self._connect_worker.is_alive():
            return
        ok, msg = affiliate_browser.ensure_browser_available()
        if not ok:
            messagebox.showerror("Playwright belum siap", msg, parent=self.root)
            return
        self.gen_connect_btn.configure(state="disabled")
        self._set_generate_status("Membuka Chrome — silakan login di window yang muncul…")
        self.gen_progress.configure(mode="indeterminate")
        self.gen_progress.start(10)
        self._connect_worker = threading.Thread(
            target=self._run_connect, daemon=True
        )
        self._connect_worker.start()

    def _run_connect(self) -> None:
        try:
            with affiliate_browser.BrowserSession(headless=False) as bs:
                ok = bs.wait_for_login(timeout_s=LOGIN_TIMEOUT_S)
            if ok:
                self._queue.put(("connect_done", "Login terdeteksi — profile tersimpan."))
            else:
                self._queue.put(("connect_error", "Tidak terdeteksi login dalam waktu yang ditentukan."))
        except affiliate.AffiliateError as e:
            self._queue.put(("connect_error", str(e)))
        except Exception as e:  # noqa: BLE001
            self._queue.put(("connect_error", f"Error tak terduga: {e}"))

    def _on_reset_profile(self) -> None:
        if not messagebox.askyesno("Reset profile Chrome", "Hapus profile Chrome shopeelink?", parent=self.root):
            return
        try:
            affiliate_browser.reset_profile()
        except affiliate.AffiliateError as e:
            messagebox.showerror("Reset gagal", str(e), parent=self.root)
            return
        self._refresh_session_label()
        self._set_generate_status("Profile Chrome di-reset.")

    def _on_generate(self) -> None:
        if self._affiliate_worker is not None and self._affiliate_worker.is_alive():
            return
        ok, msg = affiliate_browser.ensure_browser_available()
        if not ok:
            messagebox.showerror("Playwright belum siap", msg, parent=self.root)
            return
        urls = _split_input(self.gen_input_box.get("1.0", "end"))
        if not urls:
            self._set_generate_status("Tidak ada long URL untuk di-generate.")
            return
        sub_ids = tuple(e.get().strip() for e in self.gen_tag_entries[:5])
        while len(sub_ids) < 5:
            sub_ids = sub_ids + ("",)
        sub_ids = sub_ids[:5]  # type: ignore[assignment]
        items = [
            affiliate.LinkInput(original_link=u, sub_ids=sub_ids)  # type: ignore[arg-type]
            for u in urls
        ]
        self._set_generate_output("")
        self._set_generate_status(f"Generate {len(items)} link…")
        self.gen_generate_btn.configure(state="disabled")
        self.gen_progress.configure(mode="indeterminate")
        self.gen_progress.start(10)
        show_input = self.gen_show_input_var.get()
        self._affiliate_worker = threading.Thread(
            target=self._run_generate, args=(items, show_input), daemon=True
        )
        self._affiliate_worker.start()

    def _run_generate(
        self,
        items: List[affiliate.LinkInput],
        show_input: bool,
    ) -> None:
        try:
            with affiliate_browser.BrowserSession(headless=False) as bs:
                results = bs.generate(items)
            text = _format_affiliate_results(results, show_input)
            errors = sum(1 for r in results if not r.ok)
            self._queue.put(("affiliate_done", (text, len(results), errors)))
        except affiliate.AffiliateError as e:
            self._queue.put(("affiliate_error", str(e)))
        except Exception as e:  # noqa: BLE001
            self._queue.put(("affiliate_error", f"Error tak terduga: {e}"))

    def _on_generate_clear(self) -> None:
        self.gen_input_box.delete("1.0", "end")
        for e in self.gen_tag_entries:
            e.delete(0, "end")
        self._set_generate_output("")
        self._set_generate_status("Siap.")

    def _on_generate_copy(self) -> None:
        text = self.gen_output_box.get("1.0", "end").strip()
        if not text:
            self._set_generate_status("Tidak ada hasil untuk disalin.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._set_generate_status("Hasil disalin ke clipboard.")

    def _set_generate_output(self, text: str) -> None:
        self.gen_output_box.configure(state="normal")
        self.gen_output_box.delete("1.0", "end")
        self.gen_output_box.insert("1.0", text)
        self.gen_output_box.configure(state="disabled")

    def _set_generate_status(self, text: str) -> None:
        self.gen_status_lbl.configure(text=text)

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                if kind == "decode_done":
                    text, total, errors = payload  # type: ignore[misc]
                    self._set_decode_output(text)
                    if errors:
                        self._set_decode_status(f"Selesai: {total - errors}/{total} berhasil, {errors} gagal.")
                    else:
                        self._set_decode_status(f"Selesai: {total}/{total} berhasil.")
                    self.dec_convert_btn.configure(state="normal")
                    self.dec_progress.stop()
                    self.dec_progress.configure(mode="determinate", value=0)
                elif kind == "decode_error":
                    self._set_decode_output(f"ERROR: {payload}")
                    self._set_decode_status("Terjadi error.")
                    self.dec_convert_btn.configure(state="normal")
                    self.dec_progress.stop()
                    self.dec_progress.configure(mode="determinate", value=0)
                elif kind == "affiliate_done":
                    text, total, errors = payload  # type: ignore[misc]
                    self._set_generate_output(text)
                    if errors:
                        self._set_generate_status(f"Selesai: {total - errors}/{total} berhasil, {errors} gagal.")
                    else:
                        self._set_generate_status(f"Selesai: {total}/{total} berhasil.")
                    self.gen_generate_btn.configure(state="normal")
                    self.gen_progress.stop()
                    self.gen_progress.configure(mode="determinate", value=0)
                elif kind == "affiliate_error":
                    self._set_generate_output(f"ERROR: {payload}")
                    self._set_generate_status("Terjadi error.")
                    self.gen_generate_btn.configure(state="normal")
                    self.gen_progress.stop()
                    self.gen_progress.configure(mode="determinate", value=0)
                elif kind == "connect_done":
                    self._refresh_session_label()
                    self._set_generate_status(str(payload))
                    self.gen_connect_btn.configure(state="normal")
                    self.gen_progress.stop()
                    self.gen_progress.configure(mode="determinate", value=0)
                elif kind == "connect_error":
                    self._refresh_session_label()
                    self._set_generate_output(f"ERROR: {payload}")
                    self._set_generate_status("Connect Chrome gagal.")
                    self.gen_connect_btn.configure(state="normal")
                    self.gen_progress.stop()
                    self.gen_progress.configure(mode="determinate", value=0)
        except queue.Empty:
            pass
        self.root.after(RESULT_POLL_MS, self._poll_queue)

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
