"""Единое оформление интерфейсов rdesk на tkinter/ttk."""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

BG = "#f3f5fa"
CARD = "#ffffff"
TEXT = "#172033"
MUTED = "#667085"
BORDER = "#dde2ec"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
ACCENT_LIGHT = "#e8efff"
DARK = "#0f172a"
DARK_CARD = "#17223a"
DARK_MUTED = "#94a3b8"
GREEN = "#16a34a"
GREEN_LIGHT = "#dcfce7"
RED = "#dc2626"
RED_LIGHT = "#fee2e2"
AMBER = "#b45309"
AMBER_LIGHT = "#fef3c7"
WHITE = "#ffffff"
SIDEBAR = "#111c33"


def apply_theme(root: tk.Misc) -> ttk.Style:
    """Настраивает ttk-стили. Тема 'clam' позволяет менять цвета."""
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    for name, size, weight in (
        ("Rdesk.Body", 10, "normal"),
        ("Rdesk.Small", 9, "normal"),
        ("Rdesk.Tiny", 8, "normal"),
        ("Rdesk.Bold", 10, "bold"),
        ("Rdesk.H1", 20, "bold"),
        ("Rdesk.H2", 13, "bold"),
        ("Rdesk.Brand", 16, "bold"),
    ):
        try:
            tkfont.nametofont(name)
        except tk.TclError:
            pass

    default_font = tkfont.Font(root=root, name="RdeskDefault", family="Segoe UI", size=10)
    root.option_add("*Font", f"{{{default_font.actual('family')}}} "
                             f"{default_font.actual('size')}")
    style.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("Dark.TFrame", background=DARK)
    style.configure("Sidebar.TFrame", background=SIDEBAR)

    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Muted.TLabel", background=BG, foreground=MUTED)
    style.configure("Card.TLabel", background=CARD, foreground=TEXT)
    style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED)
    style.configure("CardH2.TLabel", background=CARD, foreground=TEXT,
                    font=("Segoe UI", 13, "bold"))
    style.configure("CardH1.TLabel", background=CARD, foreground=TEXT,
                    font=("Segoe UI", 20, "bold"))
    style.configure("Dark.TLabel", background=DARK, foreground=WHITE)
    style.configure("DarkMuted.TLabel", background=DARK, foreground=DARK_MUTED)
    style.configure("DarkBrand.TLabel", background=DARK, foreground=WHITE,
                    font=("Segoe UI", 16, "bold"))
    style.configure("Sidebar.TLabel", background=SIDEBAR, foreground=WHITE)
    style.configure("SidebarMuted.TLabel", background=SIDEBAR, foreground=DARK_MUTED)
    style.configure("SidebarH.TLabel", background=SIDEBAR, foreground="#cbd5e1",
                    font=("Segoe UI", 9, "bold"))

    style.configure("TButton", padding=(14, 8), borderwidth=0, focusthickness=0,
                    background="#e9edf5", foreground=TEXT, font=("Segoe UI", 10))
    style.map("TButton",
              background=[("active", "#dfe5ef"), ("disabled", "#eef1f6")],
              foreground=[("disabled", "#a0a8b8")])
    style.configure("Accent.TButton", background=ACCENT, foreground=WHITE,
                    font=("Segoe UI", 10, "bold"))
    style.map("Accent.TButton",
              background=[("active", ACCENT_DARK), ("disabled", "#9db8f5")],
              foreground=[("disabled", "#f2f5fb")])
    style.configure("Soft.TButton", background=ACCENT_LIGHT, foreground=ACCENT_DARK)
    style.map("Soft.TButton", background=[("active", "#d8e4ff")])
    style.configure("Dark.TButton", background="#24324c", foreground=WHITE)
    style.map("Dark.TButton", background=[("active", "#2e3f60")])
    style.configure("Danger.TButton", background=RED_LIGHT, foreground=RED)
    style.map("Danger.TButton", background=[("active", "#fecaca")])
    style.configure("Link.TButton", background=CARD, foreground=ACCENT,
                    padding=(2, 2), font=("Segoe UI", 10, "underline"))
    style.map("Link.TButton", background=[("active", CARD)])

    style.configure("TEntry", fieldbackground=WHITE, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, padding=(8, 7),
                    insertcolor=TEXT)
    style.map("TEntry", bordercolor=[("focus", ACCENT)],
              lightcolor=[("focus", ACCENT)], darkcolor=[("focus", ACCENT)])
    style.configure("TCombobox", fieldbackground=WHITE, background=WHITE,
                    bordercolor=BORDER, arrowcolor=MUTED, padding=(6, 5))
    style.map("TCombobox", bordercolor=[("focus", ACCENT)])
    style.configure("TSpinbox", fieldbackground=WHITE, bordercolor=BORDER,
                    arrowcolor=MUTED, padding=(6, 5))

    style.configure("TCheckbutton", background=BG, foreground=TEXT)
    style.configure("Card.TCheckbutton", background=CARD, foreground=TEXT)
    style.map("TCheckbutton", background=[("active", BG)])
    style.map("Card.TCheckbutton", background=[("active", CARD)])

    style.configure("Treeview", background=CARD, fieldbackground=CARD,
                    foreground=TEXT, borderwidth=0, rowheight=32,
                    font=("Segoe UI", 10))
    style.configure("Treeview.Heading", background="#f7f9fd", foreground=MUTED,
                    relief="flat", borderwidth=0, padding=(8, 8),
                    font=("Segoe UI", 9, "bold"))
    style.map("Treeview", background=[("selected", ACCENT_LIGHT)],
              foreground=[("selected", TEXT)])
    style.map("Treeview.Heading", background=[("active", "#eef2f9")])

    style.configure("TSeparator", background=BORDER)
    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure("TNotebook.Tab", background="#e9edf5", foreground=MUTED,
                    padding=(16, 8))
    style.map("TNotebook.Tab", background=[("selected", CARD)],
              foreground=[("selected", TEXT)])
    return style


def center_window(window: tk.Misc, width: int, height: int) -> None:
    window.update_idletasks()
    screen_w = window.winfo_screenwidth()
    screen_h = window.winfo_screenheight()
    x = max(0, (screen_w - width) // 2)
    y = max(0, (screen_h - height) // 3)
    window.geometry(f"{width}x{height}+{x}+{y}")


class CheckBox(tk.Frame):
    """Аккуратный чекбокс без «крестиков» темы clam."""

    def __init__(self, parent: tk.Misc, text: str, variable: tk.BooleanVar,
                 bg: str = CARD, fg: str = TEXT, font: tuple = ("Segoe UI", 9),
                 command=None) -> None:
        super().__init__(parent, bg=bg)
        self.variable = variable
        self.command = command
        self.box = tk.Canvas(self, width=17, height=17, bg=bg, highlightthickness=0,
                             cursor="hand2", takefocus=True)
        self.box.pack(side=tk.LEFT, pady=1)
        self.box.create_rectangle(1, 1, 16, 16, outline=BORDER, width=1,
                                  fill=WHITE, tags="box")
        self.box.create_line(4, 8, 7, 12, 13, 4, fill=WHITE, width=2, tags="tick")
        self.label = tk.Label(self, text=text, bg=bg, fg=fg, font=font,
                              cursor="hand2", anchor="w")
        self.label.pack(side=tk.LEFT, padx=(6, 0))
        for widget in (self.box, self.label):
            widget.bind("<Button-1>", self._click)
        self.box.bind("<space>", self._click)
        self.box.bind("<Return>", self._click)
        self.variable.trace_add("write", lambda *_args: self._refresh())
        self._refresh()

    def _click(self, _event=None) -> str:
        self.variable.set(not bool(self.variable.get()))
        if self.command is not None:
            self.command()
        return "break"

    def _refresh(self) -> None:
        checked = bool(self.variable.get())
        self.box.itemconfigure("box", fill=ACCENT if checked else WHITE,
                               outline=ACCENT if checked else BORDER)
        self.box.itemconfigure("tick", state="normal" if checked else "hidden")


def make_log_text(parent: tk.Misc, height: int = 8) -> tk.Text:
    frame = tk.Frame(parent, bg="#0b1220", highlightthickness=0)
    text = tk.Text(
        frame, height=height, bg="#0b1220", fg="#cbd5e1", insertbackground=WHITE,
        relief="flat", borderwidth=0, padx=12, pady=10, wrap="word",
        font=("Consolas", 9), state="disabled",
    )
    scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=scroll.set)
    text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    scroll.pack(side=tk.RIGHT, fill=tk.Y)
    text.tag_configure("info", foreground="#cbd5e1")
    text.tag_configure("ok", foreground="#86efac")
    text.tag_configure("warn", foreground="#fcd34d")
    text.tag_configure("error", foreground="#fca5a5")
    text.tag_configure("time", foreground="#64748b")
    return text


def log_append(text: tk.Text, message: str, level: str = "info",
               timestamp: str = "") -> None:
    text.configure(state="normal")
    if timestamp:
        text.insert(tk.END, timestamp + "  ", "time")
    text.insert(tk.END, message + "\n", level if level in ("info", "ok", "warn", "error") else "info")
    # держим журнал компактным
    try:
        if int(text.index("end-1c").split(".")[0]) > 1200:
            text.delete("1.0", "300.0")
    except (ValueError, tk.TclError):
        pass
    text.see(tk.END)
    text.configure(state="disabled")
