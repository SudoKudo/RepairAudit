"""GUI for creating RepairAudit participant kits."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable
import tkinter as tk
from tkinter import messagebox, ttk


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.participant_kit import build_participant_kit  # noqa: E402


class KitBuilderGUI(tk.Tk):
    """Tkinter window that creates one or more participant kits."""

    def __init__(self) -> None:
        """Initialize window state and default kit-generation settings."""
        super().__init__()
        self.title("RepairAudit Kit Builder")
        self.geometry("940x700")
        self.minsize(820, 620)

        self._bg = "#eef3f9"
        self._panel = "#ffffff"
        self._panel_alt = "#f7fafd"
        self._panel_soft = "#e6f0fb"
        self._text = "#10233d"
        self._muted = "#5d728a"
        self._accent = "#1f5fae"
        self._accent_hover = "#174f93"
        self._border = "#c6d6ea"
        self._border_strong = "#98b3d5"
        self._log_bg = "#0f1c2d"
        self._log_fg = "#e4efff"

        self.configure(bg=self._bg)
        self.option_add("*Font", "{Segoe UI} 10")

        self.out_root_var = tk.StringVar(value="participant_kits")
        self.metadata_var = tk.StringVar(value="data/metadata/snippet_metadata.csv")
        self.condition_var = tk.StringVar(value="security")
        self.phase_var = tk.StringVar(value="pilot")
        self.study_id_var = tk.StringVar(value="repairaudit-v1")
        self.provider_var = tk.StringVar(value="ollama")
        self.model_var = tk.StringVar(value="qwen2.5-coder:7b-instruct")

        self.base_name_var = tk.StringVar(value="P")
        self.count_var = tk.IntVar(value=1)
        self.start_index_var = tk.IntVar(value=1)
        self.pad_width_var = tk.IntVar(value=3)
        self.overwrite_var = tk.BooleanVar(value=False)

        self._configure_styles()
        self._build_ui()
        self._refresh_preview()

    def _configure_styles(self) -> None:
        """Configure ttk styles shared across the kit builder layout."""
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("Root.TFrame", background=self._bg)
        style.configure("Panel.TFrame", background=self._panel)
        style.configure("Card.TLabelframe", background=self._panel, bordercolor=self._border_strong, borderwidth=1.0, relief="solid")
        style.configure("Card.TLabelframe.Label", background=self._panel, foreground=self._accent, font=("Bahnschrift SemiBold", 11))
        style.configure("HeroTitle.TLabel", background=self._panel, foreground=self._text, font=("Bahnschrift SemiBold", 18))
        style.configure("HeroSub.TLabel", background=self._panel, foreground=self._muted, font=("Segoe UI", 9))
        style.configure("Hint.TLabel", background=self._panel, foreground=self._muted, font=("Segoe UI", 9))
        style.configure("Field.TLabel", background=self._panel, foreground=self._muted, font=("Segoe UI Semibold", 9))
        style.configure("Primary.TButton", font=("Segoe UI Semibold", 9, "bold"), padding=(12, 8), background=self._accent, foreground="#ffffff", borderwidth=0, focusthickness=0)
        style.map("Primary.TButton", background=[("active", self._accent_hover), ("pressed", "#143f77")], foreground=[("disabled", "#dbe7f5")])
        style.configure("Secondary.TButton", font=("Segoe UI Semibold", 9, "bold"), padding=(12, 8), background=self._panel_soft, foreground=self._accent, bordercolor=self._border_strong, borderwidth=1)
        style.map("Secondary.TButton", background=[("active", "#d6e6fb"), ("pressed", "#c6daf7")], foreground=[("disabled", "#8aa3c5")])
        style.configure("TEntry", fieldbackground=self._panel_alt, bordercolor=self._border_strong, lightcolor=self._border_strong, darkcolor=self._border_strong, padding=4)
        style.configure("TCombobox", fieldbackground="#ffffff", background="#ffffff", bordercolor=self._border_strong, lightcolor=self._border_strong, darkcolor=self._border_strong, padding=4, arrowsize=14)
        style.map("TCombobox", fieldbackground=[("readonly", "#ffffff")], background=[("readonly", "#ffffff")], foreground=[("readonly", self._text)], selectbackground=[("readonly", "#ffffff")], selectforeground=[("readonly", self._text)])
        style.configure("TCheckbutton", background=self._panel, foreground=self._text)
        style.map("TCheckbutton", background=[("active", self._panel)], foreground=[("active", self._text)])

    def _build_ui(self) -> None:
        """Create all widgets and layout rules."""
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        root = ttk.Frame(self, style="Root.TFrame", padding=12)
        root.grid(row=0, column=0, sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        header_card = tk.Frame(root, bg=self._panel, highlightbackground=self._border, highlightthickness=1, bd=0)
        header_card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        tk.Frame(header_card, bg=self._accent, height=4).pack(fill="x")

        header_body = ttk.Frame(header_card, style="Panel.TFrame")
        header_body.pack(fill="x", padx=14, pady=(10, 10))
        ttk.Label(header_body, text="RepairAudit Kit Builder", style="HeroTitle.TLabel").pack(anchor="w")
        ttk.Label(header_body, text="Generate participant kits with locked study settings, predictable naming, and duplicate protection.", style="HeroSub.TLabel").pack(anchor="w", pady=(4, 0))

        form = ttk.LabelFrame(root, text="Kit Settings", style="Card.TLabelframe", padding=12)
        form.grid(row=1, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)

        ttk.Label(form, text="Core study settings used for every generated participant kit.", style="Hint.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))
        self._row_entry(form, 1, "Output Folder", self.out_root_var, column_offset=0)
        self._row_combo(form, 1, "Condition", self.condition_var, ["security", "productivity"], column_offset=2)
        self._row_entry(form, 2, "Metadata CSV", self.metadata_var, column_offset=0)
        self._row_combo(form, 2, "Phase", self.phase_var, ["pilot", "main", "self_test"], column_offset=2)
        self._row_entry(form, 3, "Study ID", self.study_id_var, column_offset=0)
        self._row_entry(form, 3, "LLM Provider", self.provider_var, column_offset=2)
        self._row_entry(form, 4, "LLM Model", self.model_var, column_offset=0, columnspan=3)

        naming = ttk.LabelFrame(root, text="Participant Naming", style="Card.TLabelframe", padding=12)
        naming.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        naming.columnconfigure(1, weight=1)
        naming.rowconfigure(7, weight=1)

        ttk.Label(naming, text="Preview the exact participant IDs before creating kit folders.", style="Hint.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        self._row_entry(naming, 1, "Base Name", self.base_name_var, on_change=self._refresh_preview)
        self._row_spin(naming, 2, "How Many Kits", self.count_var, 1, 500, self._refresh_preview)
        self._row_spin(naming, 3, "Start Number", self.start_index_var, 0, 99999, self._refresh_preview)
        self._row_spin(naming, 4, "Zero-Pad Width", self.pad_width_var, 0, 8, self._refresh_preview)
        ttk.Checkbutton(naming, text="Allow overwrite of existing kit folders", variable=self.overwrite_var).grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 8))
        ttk.Label(naming, text="Preview of participant IDs to create", style="Field.TLabel").grid(row=6, column=0, columnspan=2, sticky="w", pady=(0, 6))

        preview_shell = tk.Frame(naming, bg=self._log_bg, highlightbackground=self._border_strong, highlightthickness=1, bd=0)
        preview_shell.grid(row=7, column=0, columnspan=2, sticky="nsew")
        preview_shell.columnconfigure(0, weight=1)
        preview_shell.rowconfigure(0, weight=1)
        self.preview_box = tk.Text(preview_shell, height=6, wrap="word", bg=self._log_bg, fg=self._log_fg, insertbackground=self._log_fg, selectbackground="#1b3555", relief="flat", bd=0, padx=10, pady=10, font=("Consolas", 10))
        self.preview_box.grid(row=0, column=0, sticky="nsew")
        self.preview_box.configure(state="disabled")

        actions = ttk.Frame(root, style="Root.TFrame")
        actions.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        actions.columnconfigure(0, weight=1)

        ttk.Label(actions, text="Refresh the preview before creating kits. Existing folders are blocked unless overwrite is enabled.", style="Hint.TLabel", wraplength=760, justify="left").grid(row=0, column=0, sticky="w")

        button_bar = ttk.Frame(actions, style="Root.TFrame")
        button_bar.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        button_bar.columnconfigure(0, weight=1, uniform="kit_actions")
        button_bar.columnconfigure(1, weight=1, uniform="kit_actions")
        ttk.Button(button_bar, text="Refresh Preview", command=self._refresh_preview, style="Secondary.TButton").grid(row=0, column=0, padx=(0, 6), sticky="ew")
        ttk.Button(button_bar, text="Create Kits", command=self._create_kits, style="Primary.TButton").grid(row=0, column=1, padx=(6, 0), sticky="ew")

    def _row_entry(self, parent: ttk.Frame | ttk.LabelFrame, row: int, label: str, var: tk.StringVar, on_change: Callable[[], None] | None = None, column_offset: int = 0, columnspan: int = 1) -> None:
        """Create one label + entry row."""
        ttk.Label(parent, text=label, style="Field.TLabel").grid(row=row, column=column_offset, sticky="w", pady=5, padx=(0, 12))
        entry = ttk.Entry(parent, textvariable=var)
        entry.grid(row=row, column=column_offset + 1, columnspan=columnspan, sticky="ew", pady=5)
        if on_change is not None:
            entry.bind("<KeyRelease>", lambda _e: on_change())

    def _row_combo(self, parent: ttk.Frame | ttk.LabelFrame, row: int, label: str, var: tk.StringVar, values: list[str], column_offset: int = 0) -> None:
        """Create one label + combobox row."""
        ttk.Label(parent, text=label, style="Field.TLabel").grid(row=row, column=column_offset, sticky="w", pady=5, padx=(0, 12))
        combo = ttk.Combobox(parent, textvariable=var, values=values, state="readonly")
        combo.grid(row=row, column=column_offset + 1, sticky="ew", pady=5)

    def _row_spin(self, parent: ttk.Frame | ttk.LabelFrame, row: int, label: str, var: tk.IntVar, start: int, end: int, on_change: Callable[[], None]) -> None:
        """Create one label + spinbox row and wire value updates."""
        ttk.Label(parent, text=label, style="Field.TLabel").grid(row=row, column=0, sticky="w", pady=5, padx=(0, 12))
        spin = tk.Spinbox(parent, from_=start, to=end, textvariable=var, width=10, command=on_change, bg="#ffffff", relief="solid", bd=1, highlightthickness=0)
        spin.grid(row=row, column=1, sticky="w", pady=5)
        spin.bind("<KeyRelease>", lambda _e: on_change())

    def _build_participant_ids(self) -> list[str]:
        """Build participant IDs from base name + count + index settings."""
        base = self.base_name_var.get().strip()
        if not base:
            raise ValueError("Base Name cannot be empty.")
        count = int(self.count_var.get())
        if count < 1:
            raise ValueError("How Many Kits must be at least 1.")
        start = int(self.start_index_var.get())
        if start < 0:
            raise ValueError("Start Number cannot be negative.")
        pad = int(self.pad_width_var.get())
        if pad < 0:
            raise ValueError("Zero-Pad Width cannot be negative.")
        if count == 1 and start == 0 and pad == 0:
            return [base]
        ids: list[str] = []
        for idx in range(start, start + count):
            suffix = str(idx).zfill(pad) if pad > 0 else str(idx)
            ids.append(f"{base}{suffix}")
        return ids

    def _refresh_preview(self) -> None:
        """Render participant IDs and any immediate validation error."""
        try:
            ids = self._build_participant_ids()
            text = "\n".join(ids)
        except Exception as exc:
            text = f"Preview unavailable: {exc}"
        self.preview_box.configure(state="normal")
        self.preview_box.delete("1.0", tk.END)
        self.preview_box.insert("1.0", text)
        self.preview_box.configure(state="disabled")

    def _validate_before_create(self, participant_ids: list[str]) -> list[str]:
        """Return a list of conflicting IDs that already exist on disk."""
        out_root = Path(self.out_root_var.get().strip())
        conflicts: list[str] = []
        for pid in participant_ids:
            if (out_root / pid).exists():
                conflicts.append(pid)
        return conflicts

    def _create_kits(self) -> None:
        """Create kits for all previewed IDs after strict preflight checks."""
        try:
            participant_ids = self._build_participant_ids()
            out_root = Path(self.out_root_var.get().strip())
            metadata_csv = Path(self.metadata_var.get().strip())
            if not metadata_csv.exists():
                raise FileNotFoundError(f"Metadata CSV not found: {metadata_csv}")
            conflicts = self._validate_before_create(participant_ids)
            if conflicts and not self.overwrite_var.get():
                listed = "\n".join(conflicts)
                raise FileExistsError("The following participant kit IDs already exist.\n\n" f"{listed}\n\n" "Change naming settings or enable overwrite.")
            created: list[str] = []
            for pid in participant_ids:
                args = argparse.Namespace(participant_id=pid, condition=self.condition_var.get().strip(), phase=self.phase_var.get().strip(), metadata_csv=str(metadata_csv), out_root=str(out_root), study_id=self.study_id_var.get().strip(), llm_provider=self.provider_var.get().strip(), llm_model=self.model_var.get().strip(), temperature=0.2, top_p=0.9, top_k=40, num_predict=1200, seed=42, overwrite=bool(self.overwrite_var.get()))
                build_participant_kit(args)
                created.append(pid)
            messagebox.showinfo("Kits Created", "Created participant kits:\n\n" + "\n".join(created))
        except Exception as exc:
            messagebox.showerror("Creation Failed", str(exc))


def main() -> None:
    """Program entry point for local GUI launch."""
    app = KitBuilderGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
