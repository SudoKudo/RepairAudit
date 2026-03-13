
"""GUI for the RepairAudit researcher workflow."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import threading
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

# Allow relative project imports when launching the GUI directly.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.privacy_check import Finding, run_prepublish_check

PIPELINE_STEPS: list[str] = [
    "Run Analyzed",
    "Interaction Merged",
    "Pilot Aggregated",
    "Stats Generated",
    "Report Built",
]

# User-facing strategy labels mapped to internal strategy keys.
STRATEGY_LABELS: list[tuple[str, str]] = [
    ("CoT", "cot"),
    ("Zero-shot", "zero_shot"),
    ("Few-shot", "few_shot"),
    ("Self-consistency", "self_consistency"),
]


def _resolve_run_dir(run_dir: Path) -> Path:
    """Resolve one extra nested extracted run directory when present."""
    run_dir = Path(run_dir)
    if not run_dir.exists():
        return run_dir

    top_level_markers = ["edits", "logs", "analysis", "condition.txt", "start_end_times.json"]
    if any((run_dir / marker).exists() for marker in top_level_markers):
        return run_dir

    nested_candidates = [
        p for p in run_dir.iterdir() if p.is_dir() and (p / "edits").exists() and (p / "logs").exists()
    ]
    if len(nested_candidates) == 1:
        return nested_candidates[0]
    return run_dir


@dataclass
class CommandStep:
    """One item in the workflow queue."""

    label: str
    command: Optional[list[str]] = None
    env: Optional[dict[str, str]] = None
    action: Optional[Callable[[], None]] = None


class StudyGUI(tk.Tk):
    """Main desktop window."""

    def __init__(self) -> None:
        """Initialize window state, controls, and persisted session behavior."""
        super().__init__()

        self.title("RepairAudit Research Console")
        self.geometry("1460x980")
        self.minsize(1220, 820)
        self._right_panes_initialized = False

        # Visual palette for the desktop layout.
        self._bg = "#eef3f9"
        self._panel = "#ffffff"
        self._panel_alt = "#f7fafd"
        self._panel_soft = "#e6f0fb"
        self._text = "#10233d"
        self._muted = "#5d728a"
        self._accent = "#1f5fae"
        self._accent_hover = "#174f93"
        self._accent_soft = "#d9e8fb"
        self._border = "#c6d6ea"
        self._border_strong = "#98b3d5"
        self._success = "#0f8a6b"
        self._warning = "#b7791f"
        self._danger = "#b63a3a"
        self._log_bg = "#0f1c2d"
        self._log_fg = "#e4efff"
        self._log_muted = "#97b0d1"
        self.configure(bg=self._bg)
        # Tk expects font families with spaces to be wrapped in braces.
        # Without braces, "Segoe UI" is parsed as two separate tokens and can
        # trigger "expected integer but got UI" during widget creation.
        self.option_add("*Font", "{Segoe UI} 10")

        # Process tracking for stop/close behavior.
        self._active_procs: set[subprocess.Popen[str]] = set()
        self._active_procs_lock = threading.Lock()

        # Workflow control flags.
        self._workflow_thread: Optional[threading.Thread] = None
        self._workflow_pause_event = threading.Event()
        self._workflow_pause_event.set()
        self._workflow_stop_requested = False
        self._workflow_lock = threading.Lock()
        self._last_privacy_scan: Optional[tuple[bool, list[Finding], str]] = None

        # Live workflow feedback shown in the Actions panel.
        self.workflow_state_var = tk.StringVar(value="Idle")
        self.workflow_detail_var = tk.StringVar(value="No workflow running.")
        self.workflow_count_var = tk.StringVar(value="0 / 0")
        self.workflow_progress_var = tk.DoubleVar(value=0.0)

        # GUI session state is local operator cache, not study data.
        self._session_state_dir = REPO_ROOT / "gui" / ".cache"
        self._session_state_path = self._session_state_dir / "gui_session_state.json"
        self._legacy_session_state_paths = [
            REPO_ROOT / "runs" / "_gui_session_state.json",
            REPO_ROOT / ".repairaudit" / "gui_session_state.json",
        ]
        self._session_state_lock = threading.Lock()
        self._migrate_legacy_session_state()

        self._shutting_down = False

        self._configure_styles()
        self._build_ui()
        self._restore_state_into_form()
        self.refresh_participants()
        self.refresh_pipeline_progress()
        self.refresh_participant_badges()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(250, self._offer_resume_if_needed)

    # ------------------------------------------------------------------
    # Style and layout
    # ------------------------------------------------------------------

    def _configure_styles(self) -> None:
        """Configure ttk style primitives."""
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure("Root.TFrame", background=self._bg)
        style.configure("Panel.TFrame", background=self._panel)
        style.configure("Soft.TFrame", background=self._panel_soft)
        style.configure(
            "Card.TLabelframe",
            background=self._panel,
            bordercolor=self._border_strong,
            borderwidth=1.0,
            relief="solid",
        )
        style.configure(
            "Card.TLabelframe.Label",
            background=self._panel,
            foreground=self._accent,
            font=("Bahnschrift SemiBold", 11),
        )
        style.configure(
            "HeroTitle.TLabel",
            background=self._panel,
            foreground=self._text,
            font=("Bahnschrift SemiBold", 17),
        )
        style.configure(
            "HeroSub.TLabel",
            background=self._panel,
            foreground=self._muted,
            font=("Segoe UI", 8),
        )
        style.configure("Title.TLabel", background=self._bg, foreground=self._text, font=("Bahnschrift SemiBold", 21))
        style.configure("SubTitle.TLabel", background=self._bg, foreground=self._muted, font=("Segoe UI", 9))
        style.configure(
            "Hint.TLabel",
            background=self._panel,
            foreground=self._muted,
            font=("Segoe UI", 8),
        )
        style.configure(
            "SoftHint.TLabel",
            background=self._panel_soft,
            foreground=self._muted,
            font=("Segoe UI", 8),
        )
        style.configure(
            "SoftValue.TLabel",
            background=self._panel_soft,
            foreground=self._text,
            font=("Segoe UI Semibold", 8),
        )
        style.configure(
            "Field.TLabel",
            background=self._panel,
            foreground=self._muted,
            font=("Segoe UI Semibold", 8),
        )
        style.configure(
            "Badge.TLabel",
            background=self._accent_soft,
            foreground=self._accent,
            font=("Segoe UI Semibold", 8, "bold"),
            padding=(6, 3),
        )
        style.configure(
            "Primary.TButton",
            font=("Segoe UI Semibold", 8, "bold"),
            padding=(10, 6),
            background=self._accent,
            foreground="#ffffff",
            borderwidth=0,
            focusthickness=0,
        )
        style.map(
            "Primary.TButton",
            background=[("active", self._accent_hover), ("pressed", "#143f77")],
            foreground=[("disabled", "#dbe7f5")],
        )
        style.configure(
            "Secondary.TButton",
            font=("Segoe UI Semibold", 8, "bold"),
            padding=(10, 6),
            background=self._panel_soft,
            foreground=self._accent,
            bordercolor=self._border_strong,
            borderwidth=1,
        )
        style.map(
            "Secondary.TButton",
            background=[("active", "#d6e6fb"), ("pressed", "#c6daf7")],
            foreground=[("disabled", "#8aa3c5")],
        )
        style.configure(
            "Success.TButton",
            font=("Segoe UI Semibold", 8, "bold"),
            padding=(10, 6),
            background="#e7f7f1",
            foreground=self._success,
            bordercolor="#9ad7c4",
            borderwidth=1,
        )
        style.map(
            "Success.TButton",
            background=[("active", "#d7f0e6"), ("pressed", "#c7eadb")],
            foreground=[("disabled", "#7fae9f")],
        )
        style.configure(
            "Danger.TButton",
            font=("Segoe UI Semibold", 8, "bold"),
            padding=(10, 6),
            background="#fdecec",
            foreground=self._danger,
            bordercolor="#e6b4b9",
            borderwidth=1,
        )
        style.map(
            "Danger.TButton",
            background=[("active", "#fbdfe1"), ("pressed", "#f7d0d5")],
            foreground=[("disabled", "#c59da2")],
        )
        style.configure(
            "Toolbar.TButton",
            font=("Segoe UI Semibold", 8),
            padding=(6, 3),
            background=self._panel_alt,
            foreground=self._accent,
            bordercolor=self._border,
            borderwidth=1,
        )
        style.map("Toolbar.TButton", background=[("active", self._panel_soft), ("pressed", self._accent_soft)])
        style.configure(
            "Treeview",
            rowheight=18,
            font=("Segoe UI", 8),
            fieldbackground=self._panel,
            background=self._panel,
            foreground=self._text,
            bordercolor=self._border,
            borderwidth=0,
        )
        style.map(
            "Treeview",
            background=[("selected", self._accent_soft)],
            foreground=[("selected", self._text)],
        )
        style.configure(
            "Treeview.Heading",
            background=self._panel_alt,
            foreground=self._text,
            font=("Segoe UI Semibold", 8, "bold"),
            padding=(4, 3),
            relief="flat",
        )
        style.map("Treeview.Heading", background=[("active", self._panel_soft)])
        style.configure(
            "TEntry",
            fieldbackground=self._panel_alt,
            bordercolor=self._border_strong,
            lightcolor=self._border_strong,
            darkcolor=self._border_strong,
            padding=3,
        )
        style.configure(
            "TCombobox",
            fieldbackground="#ffffff",
            background="#ffffff",
            bordercolor=self._border_strong,
            lightcolor=self._border_strong,
            darkcolor=self._border_strong,
            padding=3,
            arrowsize=14,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", "#ffffff")],
            background=[("readonly", "#ffffff")],
            foreground=[("readonly", self._text)],
            selectbackground=[("readonly", "#ffffff")],
            selectforeground=[("readonly", self._text)],
        )
        style.configure(
            "Light.TCombobox",
            fieldbackground="#ffffff",
            background="#ffffff",
            foreground=self._text,
            arrowcolor=self._accent,
            bordercolor=self._border_strong,
            lightcolor=self._border_strong,
            darkcolor=self._border_strong,
            padding=3,
            arrowsize=14,
        )
        style.map(
            "Light.TCombobox",
            fieldbackground=[("readonly", "#ffffff"), ("disabled", "#f2f5f9")],
            background=[("readonly", "#ffffff"), ("disabled", "#f2f5f9")],
            foreground=[("readonly", self._text), ("disabled", self._muted)],
            selectbackground=[("readonly", "#ffffff")],
            selectforeground=[("readonly", self._text)],
            arrowcolor=[("readonly", self._accent), ("active", self._accent_hover)],
            bordercolor=[("focus", self._accent), ("readonly", self._border_strong)],
        )
        style.configure("TCheckbutton", background=self._panel, foreground=self._text)
        style.map("TCheckbutton", background=[("active", self._panel)], foreground=[("active", self._text)])
        style.configure(
            "Study.Horizontal.TProgressbar",
            troughcolor="#d8e3f2",
            background=self._accent,
            bordercolor="#d8e3f2",
            lightcolor=self._accent,
            darkcolor=self._accent,
            thickness=7,
        )
        style.configure(
            "Accent.Vertical.TScrollbar",
            background=self._accent_soft,
            troughcolor=self._panel_soft,
            bordercolor=self._border,
            arrowcolor=self._accent,
            lightcolor=self._accent_soft,
            darkcolor=self._accent_soft,
            relief="flat",
            gripcount=0,
        )
        style.map(
            "Accent.Vertical.TScrollbar",
            background=[("active", "#c8dcfb"), ("pressed", "#b7cff6")],
            arrowcolor=[("active", self._accent_hover)],
        )

    def _build_ui(self) -> None:
        """Build responsive two-column layout using grid."""
        root = ttk.Frame(self, style="Root.TFrame")
        root.pack(fill="both", expand=True, padx=12, pady=8)
        root.columnconfigure(0, weight=6)
        root.columnconfigure(1, weight=8)
        root.rowconfigure(1, weight=1)

        header_card = tk.Frame(root, bg=self._panel, highlightbackground=self._border, highlightthickness=1, bd=0)
        header_card.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        tk.Frame(header_card, bg=self._accent, height=4).pack(fill="x")

        header_body = ttk.Frame(header_card, style="Panel.TFrame")
        header_body.pack(fill="x", padx=14, pady=(9, 8))

        ttk.Label(header_body, text="RepairAudit Research Console", style="HeroTitle.TLabel").pack(anchor="w")
        ttk.Label(
            header_body,
            text="Run participant analysis, inspect pipeline status, and build offline study outputs from one workspace.",
            style="HeroSub.TLabel",
        ).pack(anchor="w", pady=(4, 0))



        left_shell = ttk.Frame(root, style="Root.TFrame")
        right = ttk.Frame(root, style="Root.TFrame")
        left_shell.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        right.grid(row=1, column=1, sticky="nsew", padx=(10, 0))

        left_shell.columnconfigure(0, weight=1)
        left_shell.rowconfigure(2, weight=1)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        self._build_run_setup(left_shell)
        self._build_judge_controls(left_shell)
        self._build_buttons(left_shell)

        self._right_panes = tk.PanedWindow(
            right,
            orient=tk.VERTICAL,
            sashwidth=8,
            showhandle=False,
            opaqueresize=True,
            bd=0,
            relief="flat",
            bg=self._bg,
        )
        self._right_panes.grid(row=0, column=0, sticky="nsew")

        status_host = ttk.Frame(self._right_panes, style="Root.TFrame")
        status_host.columnconfigure(0, weight=1)
        status_host.rowconfigure(0, weight=1)
        log_host = ttk.Frame(self._right_panes, style="Root.TFrame")
        log_host.columnconfigure(0, weight=1)
        log_host.rowconfigure(0, weight=1)

        self._right_panes.add(status_host, minsize=220, stretch="always")
        self._right_panes.add(log_host, minsize=360, stretch="always")

        self._build_status_panel(status_host)
        self._build_log(log_host)
        self._right_panes.bind("<Configure>", self._on_right_panes_configure, add="+")
        self.after_idle(self._initialize_pane_layout)


    def _initialize_pane_layout(self) -> None:
        """Apply initial pane sizing once the window has real geometry."""
        self._set_default_right_pane_layout()

    def _on_right_panes_configure(self, _event: tk.Event) -> None:
        """Initialize right-side pane sizes once after geometry settles."""
        if not self._right_panes_initialized:
            self.after_idle(self._set_default_right_pane_layout)

    def _set_default_right_pane_layout(self) -> None:
        """Set usable default heights for the right-side panes."""
        if not hasattr(self, "_right_panes"):
            return

        self.update_idletasks()
        total_height = self._right_panes.winfo_height()
        if total_height <= 1:
            return

        min_log = 360
        status_height = max(220, int(total_height * 0.32))
        first = min(status_height, total_height - min_log)

        try:
            self._right_panes.sash_place(0, 1, first)
            self._right_panes_initialized = True
        except tk.TclError:
            pass

    def _build_run_setup(self, parent: ttk.Frame) -> None:
        """Build phase/path settings panel."""
        panel = ttk.LabelFrame(parent, text="Run Setup", style="Card.TLabelframe", padding=(6, 5))
        panel.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        panel.columnconfigure(1, weight=1)

        self.phase_var = tk.StringVar(value="pilot")
        self.metadata_var = tk.StringVar(value=str(Path("data") / "metadata" / "snippet_metadata.csv"))
        self.stats_csv_var = tk.StringVar(value=str(Path("data") / "aggregated" / "pilot_summary.csv"))

        ttk.Label(panel, text="Core phase and analysis file paths.", style="Hint.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )

        ttk.Label(panel, text="Phase", style="Field.TLabel").grid(row=1, column=0, pady=5, sticky="w")
        phase_combo = ttk.Combobox(
            panel,
            textvariable=self.phase_var,
            values=["self_test", "pilot", "main"],
            width=12,
            state="readonly",
            style="Light.TCombobox",
        )
        phase_combo.grid(row=1, column=1, pady=5, sticky="w")
        phase_combo.bind("<<ComboboxSelected>>", lambda _: self._on_phase_changed())

        ttk.Label(panel, text="Metadata CSV", style="Field.TLabel").grid(row=2, column=0, pady=5, sticky="w")
        ttk.Entry(panel, textvariable=self.metadata_var).grid(row=2, column=1, pady=5, sticky="ew")

        ttk.Label(panel, text="Stats CSV", style="Field.TLabel").grid(row=3, column=0, pady=5, sticky="w")
        ttk.Entry(panel, textvariable=self.stats_csv_var).grid(row=3, column=1, pady=5, sticky="ew")

    def _build_judge_controls(self, parent: ttk.Frame) -> None:
        """Build prompt strategy + generation controls used by the LLM judge."""
        panel = ttk.LabelFrame(parent, text="Judge Strategy Controls", style="Card.TLabelframe", padding=(6, 5))
        panel.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        panel.columnconfigure(0, weight=1)
        panel.columnconfigure(1, weight=1)
        panel.columnconfigure(2, weight=1)
        panel.columnconfigure(3, weight=1)

        ttk.Label(
            panel,
            text="Prompt strategy toggles and judge generation settings.",
            style="Hint.TLabel",
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))

        self.strategy_vars: dict[str, tk.BooleanVar] = {}
        ttk.Label(panel, text="Enabled Prompt Strategies", style="Field.TLabel").grid(
            row=1, column=0, columnspan=4, sticky="w"
        )

        strategy_frame = ttk.Frame(panel, style="Panel.TFrame")
        strategy_frame.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(3, 8))
        for col in range(4):
            strategy_frame.columnconfigure(col, weight=1)
        for idx, (label, key) in enumerate(STRATEGY_LABELS):
            var = tk.BooleanVar(value=True)
            self.strategy_vars[key] = var
            ttk.Checkbutton(strategy_frame, text=label, variable=var).grid(row=0, column=idx, padx=(0, 8), pady=1, sticky="w")

        self.self_consistency_samples_var = tk.StringVar(value="5")
        ttk.Label(panel, text="Self-Consistency Samples", style="Field.TLabel").grid(row=3, column=0, pady=3, sticky="w")
        ttk.Entry(panel, textvariable=self.self_consistency_samples_var, width=8).grid(row=3, column=1, pady=3, sticky="w")

        self.gen_vars: dict[str, tk.StringVar] = {
            "temperature": tk.StringVar(value="0.2"),
            "top_p": tk.StringVar(value="0.9"),
            "top_k": tk.StringVar(value="40"),
            "num_predict": tk.StringVar(value="250"),
            "repeat_penalty": tk.StringVar(value="1.0"),
            "presence_penalty": tk.StringVar(value="0.0"),
            "frequency_penalty": tk.StringVar(value="0.0"),
            "seed": tk.StringVar(value="42"),
            "num_ctx": tk.StringVar(value="4096"),
        }

        gen = ttk.LabelFrame(panel, text="Generation Options", style="Card.TLabelframe", padding=(6, 5))
        gen.grid(row=4, column=0, columnspan=4, pady=(4, 0), sticky="ew")
        for i in range(6):
            gen.columnconfigure(i, weight=1)

        for idx, (name, var) in enumerate(self.gen_vars.items()):
            r = idx // 3
            c = (idx % 3) * 2
            ttk.Label(gen, text=name, style="Field.TLabel").grid(row=r, column=c, padx=5, pady=3, sticky="w")
            ttk.Entry(gen, textvariable=var, width=10).grid(row=r, column=c + 1, padx=5, pady=3, sticky="ew")

    def _build_buttons(self, parent: ttk.Frame) -> None:
        """Build the primary workflow action buttons."""
        panel = ttk.LabelFrame(parent, text="Actions", style="Card.TLabelframe", padding=(6, 5))
        panel.grid(row=2, column=0, sticky="nsew")
        panel.columnconfigure(0, weight=1)
        panel.columnconfigure(1, weight=1)
        panel.columnconfigure(2, weight=1)

        ttk.Label(panel, text="Run, control, and inspect the pipeline from one place.", style="Hint.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 8)
        )

        status_card = ttk.Frame(panel, style="Soft.TFrame", padding=(6, 5))
        status_card.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        status_card.columnconfigure(0, weight=1)
        status_card.columnconfigure(1, weight=0)

        ttk.Label(status_card, text="Workflow Status", style="SoftHint.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(status_card, textvariable=self.workflow_state_var, style="SoftValue.TLabel").grid(row=0, column=1, sticky="e")
        ttk.Label(
            status_card,
            textvariable=self.workflow_detail_var,
            style="SoftHint.TLabel",
            wraplength=340,
            justify="left",
        ).grid(row=1, column=0, sticky="ew", pady=(4, 3))
        ttk.Label(status_card, textvariable=self.workflow_count_var, style="SoftValue.TLabel").grid(
            row=1, column=1, sticky="e", padx=(12, 0)
        )
        ttk.Progressbar(
            status_card,
            variable=self.workflow_progress_var,
            maximum=100.0,
            mode="determinate",
            style="Study.Horizontal.TProgressbar",
        ).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        ttk.Button(panel, text="Start Analysis", command=self.start_analysis, style="Success.TButton").grid(
            row=2, column=0, padx=(0, 6), pady=(0, 6), sticky="ew"
        )
        ttk.Button(panel, text="Pause / Resume", command=self.pause_or_resume_workflow, style="Secondary.TButton").grid(
            row=2, column=1, padx=6, pady=(0, 6), sticky="ew"
        )
        ttk.Button(panel, text="Stop", command=self.stop_workflow, style="Danger.TButton").grid(
            row=2, column=2, padx=(6, 0), pady=(0, 6), sticky="ew"
        )
        ttk.Button(panel, text="Open HTML Report", command=self.open_html_report, style="Secondary.TButton").grid(
            row=3, column=0, columnspan=3, pady=(0, 6), sticky="ew"
        )
        ttk.Button(panel, text="Pre-Publish Repo Scan", command=self.run_privacy_check, style="Toolbar.TButton").grid(
            row=4, column=0, columnspan=3, sticky="w"
        )
        ttk.Label(
            panel,
            text="Checks runs, kits, and outputs for blocked data before publishing or sharing.",
            style="Hint.TLabel",
            wraplength=340,
            justify="left",
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))


    def _build_status_panel(self, parent: ttk.Frame) -> None:
        """Build one combined participant and pipeline status table."""
        panel = ttk.LabelFrame(parent, text="Participants and Pipeline Status", style="Card.TLabelframe", padding=(6, 5))
        panel.grid(row=0, column=0, sticky="nsew")
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(2, weight=1)

        ttk.Label(
            panel,
            text="Select participants and inspect current pipeline status in one table.",
            style="Hint.TLabel",
            wraplength=500,
            justify="left",
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))

        toolbar = ttk.Frame(panel, style="Panel.TFrame")
        toolbar.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ttk.Button(toolbar, text="Refresh", command=self.refresh_participants, style="Toolbar.TButton").pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="Select All", command=self._select_all_participants, style="Toolbar.TButton").pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="Clear", command=self._clear_participants, style="Toolbar.TButton").pack(side="left")

        table_shell = ttk.Frame(panel, style="Panel.TFrame")
        table_shell.grid(row=2, column=0, sticky="nsew")
        table_shell.columnconfigure(0, weight=1)
        table_shell.rowconfigure(0, weight=1)

        columns = ("participant", "analyze", "merge", "aggregate", "stats", "report")
        self.participant_status_tree = ttk.Treeview(
            table_shell,
            columns=columns,
            show="headings",
            selectmode="extended",
            height=8,
        )
        self.participant_status_tree.heading("participant", text="Participant")
        self.participant_status_tree.heading("analyze", text="Analyze")
        self.participant_status_tree.heading("merge", text="Merge")
        self.participant_status_tree.heading("aggregate", text="Aggregate")
        self.participant_status_tree.heading("stats", text="Stats")
        self.participant_status_tree.heading("report", text="Report")
        self.participant_status_tree.column("participant", width=120, minwidth=105, anchor="w", stretch=True)
        self.participant_status_tree.column("analyze", width=78, minwidth=70, anchor="center", stretch=False)
        self.participant_status_tree.column("merge", width=72, minwidth=66, anchor="center", stretch=False)
        self.participant_status_tree.column("aggregate", width=84, minwidth=76, anchor="center", stretch=False)
        self.participant_status_tree.column("stats", width=62, minwidth=56, anchor="center", stretch=False)
        self.participant_status_tree.column("report", width=68, minwidth=60, anchor="center", stretch=False)
        self.participant_status_tree.grid(row=0, column=0, sticky="nsew")

        status_scroll = ttk.Scrollbar(
            table_shell,
            orient="vertical",
            command=self.participant_status_tree.yview,
            style="Accent.Vertical.TScrollbar",
        )
        status_scroll.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        self.participant_status_tree.configure(yscrollcommand=status_scroll.set)
        self.participant_status_tree.bind("<<TreeviewSelect>>", lambda _: self._on_participant_selection_changed())
        self.participant_status_tree.tag_configure("ok", foreground=self._success, background="#eefaf4")
        self.participant_status_tree.tag_configure("pending", foreground=self._warning, background="#fff8ec")
        self.participant_status_tree.tag_configure("mixed", foreground="#516275", background="#f5f8fc")

    def _build_log(self, parent: ttk.Frame) -> None:
        """Execution log inside a bordered panel with full resize behavior."""
        panel = ttk.LabelFrame(parent, text="Execution Log", style="Card.TLabelframe", padding=(6, 5))
        panel.grid(row=0, column=0, sticky="nsew")
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(1, weight=1)

        ttk.Label(panel, text="Live command output, stderr, and workflow decisions appear here.", style="Hint.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )

        log_shell = tk.Frame(panel, bg=self._log_bg, highlightbackground=self._border_strong, highlightthickness=1, bd=0)
        log_shell.grid(row=1, column=0, sticky="nsew")
        log_shell.columnconfigure(0, weight=1)
        log_shell.rowconfigure(1, weight=1)

        log_header = tk.Frame(log_shell, bg="#12253c", height=30)
        log_header.grid(row=0, column=0, sticky="ew")
        tk.Label(
            log_header,
            text="Execution stream",
            bg="#12253c",
            fg=self._log_muted,
            font=("Segoe UI Semibold", 8),
            padx=10,
            pady=6,
        ).pack(side="left")

        self.log_widget = ScrolledText(
            log_shell,
            wrap="word",
            font=("Consolas", 10),
            bg=self._log_bg,
            fg=self._log_fg,
            insertbackground="#ffffff",
            selectbackground=self._accent,
            selectforeground="#ffffff",
            relief="flat",
            bd=0,
            padx=12,
            pady=12,
            spacing1=2,
            spacing3=2,
        )
        self.log_widget.grid(row=1, column=0, sticky="nsew")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _run_dir(self, participant_id: str) -> Path:
        """Return relative run folder path for one participant in the active phase."""
        return Path("runs") / self.phase_var.get().strip() / participant_id

    def _phase_root(self) -> Path:
        """Return absolute phase root under runs/."""
        return REPO_ROOT / "runs" / self.phase_var.get().strip()

    def _python_cmd(self, *args: str) -> list[str]:
        """Build a python command list using the currently running interpreter."""
        return [sys.executable, *args]

    def _append_log(self, text: str) -> None:
        """Append one line to the execution log in a thread-safe way."""
        # Always update Tk widgets from the Tk main thread.
        if threading.current_thread() is threading.main_thread():
            self.log_widget.insert("end", text + "\n")
            self.log_widget.see("end")
            return
        self.after(0, lambda: self._append_log(text))

    def _on_ui_thread(self, callback: Callable[[], None]) -> None:
        """Run one callback on the Tk main thread when the window is alive."""
        if self._shutting_down:
            return
        if threading.current_thread() is threading.main_thread():
            callback()
            return
        self.after(0, callback)

    def _set_workflow_feedback(
        self,
        *,
        state: str,
        detail: str,
        completed: Optional[int] = None,
        total: Optional[int] = None,
    ) -> None:
        """Update the Actions-panel workflow feedback widgets."""

        def apply() -> None:
            self.workflow_state_var.set(state)
            self.workflow_detail_var.set(detail)
            if completed is not None and total is not None:
                if total <= 0:
                    self.workflow_count_var.set("0 / 0")
                    self.workflow_progress_var.set(0.0)
                else:
                    self.workflow_count_var.set(f"{completed} / {total}")
                    self.workflow_progress_var.set(max(0.0, min(100.0, (completed / total) * 100.0)))

        self._on_ui_thread(apply)

    def _show_message(self, kind: str, title: str, message: str) -> None:
        """Show a modal message box from any thread."""

        def apply() -> None:
            if kind == "error":
                messagebox.showerror(title, message)
            elif kind == "warning":
                messagebox.showwarning(title, message)
            else:
                messagebox.showinfo(title, message)

        self._on_ui_thread(apply)

    def _refresh_status_views(self) -> None:
        """Refresh participant and pipeline views on the Tk main thread."""

        def apply() -> None:
            self.refresh_participants()
            self.refresh_pipeline_progress()
            self.refresh_participant_badges()

        self._on_ui_thread(apply)

    def _is_busy(self) -> bool:
        """Return True when any tracked subprocess is still running."""
        with self._active_procs_lock:
            return any(proc.poll() is None for proc in self._active_procs)

    def _snapshot_form(self) -> dict[str, Any]:
        """Capture current form values for session-state persistence."""
        return {
            "phase": self.phase_var.get().strip(),
            "metadata_csv": self.metadata_var.get().strip(),
            "stats_csv": self.stats_csv_var.get().strip(),
            "judge": self._snapshot_judge_settings(),
        }

    def _snapshot_judge_settings(self) -> dict[str, Any]:
        """Capture judge strategy and generation options from UI controls."""
        return {
            "strategies": [k for k, v in self.strategy_vars.items() if v.get()],
            "self_consistency_samples": self.self_consistency_samples_var.get().strip(),
            "generation": {k: v.get().strip() for k, v in self.gen_vars.items()},
        }

    def _migrate_legacy_session_state(self) -> None:
        """Move legacy session cache out of blocked study-data paths."""
        current = self._session_state_path
        try:
            self._session_state_dir.mkdir(parents=True, exist_ok=True)
            for legacy in self._legacy_session_state_paths:
                if not legacy.exists():
                    continue
                if not current.exists():
                    current.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
                legacy.unlink(missing_ok=True)
            old_dir = REPO_ROOT / ".repairaudit"
            if old_dir.exists() and not any(old_dir.iterdir()):
                old_dir.rmdir()
        except Exception:
            # Ignore cache migration issues; they should never block the GUI.
            pass

    def _write_session_state(self, *, status: str, message: str, workflow_name: str = "", steps: Optional[list[dict[str, Any]]] = None, next_index: int = 0) -> None:
        """Persist workflow/session status JSON used for resume support."""
        payload: dict[str, Any] = {
            "updated_utc": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "message": message,
            "workflow_name": workflow_name,
            "steps": steps or [],
            "next_index": next_index,
            "form": self._snapshot_form(),
        }
        self._session_state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._session_state_lock:
            self._session_state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _read_session_state(self) -> dict[str, Any]:
        """Read workflow/session state JSON from disk."""
        target = self._session_state_path
        if not target.exists():
            for legacy in self._legacy_session_state_paths:
                if legacy.exists():
                    target = legacy
                    break
        if not target.exists():
            return {}
        try:
            with self._session_state_lock:
                data = json.loads(target.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _restore_state_into_form(self) -> None:
        """Restore previously saved form values at GUI startup."""
        state = self._read_session_state()
        form_obj = state.get("form")
        form = form_obj if isinstance(form_obj, dict) else {}
        if not form:
            return

        self.phase_var.set(str(form.get("phase", self.phase_var.get())))
        self.metadata_var.set(str(form.get("metadata_csv", self.metadata_var.get())))
        self.stats_csv_var.set(str(form.get("stats_csv", self.stats_csv_var.get())))

        judge_obj = form.get("judge")
        judge = judge_obj if isinstance(judge_obj, dict) else {}
        if judge:
            selected_obj = judge.get("strategies")
            selected = set(selected_obj) if isinstance(selected_obj, list) else set()
            if selected:
                for key, var in self.strategy_vars.items():
                    var.set(key in selected)

            self.self_consistency_samples_var.set(str(judge.get("self_consistency_samples", self.self_consistency_samples_var.get())))

            gen_obj = judge.get("generation")
            gen = gen_obj if isinstance(gen_obj, dict) else {}
            for key, var in self.gen_vars.items():
                if key in gen:
                    var.set(str(gen[key]))

    # ------------------------------------------------------------------
    # Participant + progress views
    # ------------------------------------------------------------------

    def _on_phase_changed(self) -> None:
        """Refresh participant and progress views after phase changes."""
        self.refresh_participants()
        self.refresh_pipeline_progress()
        self.refresh_participant_badges()

    def refresh_participants(self) -> None:
        """Reload participants and current pipeline status into the unified table."""
        if not hasattr(self, "participant_status_tree"):
            return

        previous_selection = set(self._extract_selected_participant_ids())
        for item in self.participant_status_tree.get_children():
            self.participant_status_tree.delete(item)

        phase_root = self._phase_root()
        if not phase_root.exists():
            return

        run_dirs = sorted([p for p in phase_root.iterdir() if p.is_dir()], key=lambda p: p.name)
        selected_items: list[str] = []
        for run_dir in run_dirs:
            st = self._step_status_for_run(REPO_ROOT / self._run_dir(run_dir.name))
            values = (
                run_dir.name,
                "OK" if st.get("Run Analyzed", False) else "...",
                "OK" if st.get("Interaction Merged", False) else "...",
                "OK" if st.get("Pilot Aggregated", False) else "...",
                "OK" if st.get("Stats Generated", False) else "...",
                "OK" if st.get("Report Built", False) else "...",
            )
            completed = sum(1 for flag in st.values() if flag)
            tag = "ok" if completed == len(PIPELINE_STEPS) else "pending" if completed == 0 else "mixed"
            item_id = self.participant_status_tree.insert("", "end", values=values, tags=(tag,))
            if run_dir.name in previous_selection:
                selected_items.append(item_id)

        if selected_items:
            self.participant_status_tree.selection_set(selected_items)

    def _extract_selected_participant_ids(self) -> list[str]:
        """Return participant IDs currently selected in the unified status table."""
        if not hasattr(self, "participant_status_tree"):
            return []
        selected: list[str] = []
        for item_id in self.participant_status_tree.selection():
            values = self.participant_status_tree.item(item_id, "values")
            if values:
                selected.append(str(values[0]))
        return selected

    def _select_all_participants(self) -> None:
        """Select all participants in the unified status table."""
        if hasattr(self, "participant_status_tree"):
            self.participant_status_tree.selection_set(self.participant_status_tree.get_children())

    def _clear_participants(self) -> None:
        """Clear participant selection in the unified status table."""
        if hasattr(self, "participant_status_tree"):
            self.participant_status_tree.selection_remove(self.participant_status_tree.selection())

    def _on_participant_selection_changed(self) -> None:
        """Handle participant selection changes."""
        return

    def _aggregated_run_ids(self, summary_csv: Path) -> set[str]:
        """Return participant/run IDs present in the aggregated pilot summary."""
        if not summary_csv.exists():
            return set()
        try:
            with summary_csv.open("r", newline="", encoding="utf-8") as handle:
                return {
                    (row.get("run_id") or "").strip()
                    for row in csv.DictReader(handle)
                    if (row.get("run_id") or "").strip()
                }
        except Exception:
            return set()

    def _report_contains_run(self, report_html: Path, run_id: str) -> bool:
        """Return True when the built HTML report visibly includes one run ID."""
        if not report_html.exists() or not run_id:
            return False
        try:
            text = report_html.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return False
        return run_id in text

    def _step_status_for_run(self, run_dir: Path) -> dict[str, bool]:
        """Compute pipeline completion booleans for one run folder."""
        run_dir = _resolve_run_dir(run_dir)
        run_id = run_dir.parent.name if run_dir.name.startswith("run_") else run_dir.name
        out = {step: False for step in PIPELINE_STEPS}
        out["Run Analyzed"] = (run_dir / "analysis" / "results.csv").exists()

        summary_path = run_dir / "analysis" / "summary.json"
        if summary_path.exists():
            try:
                payload = json.loads(summary_path.read_text(encoding="utf-8"))
                out["Interaction Merged"] = isinstance(payload.get("interaction"), dict)
            except Exception:
                out["Interaction Merged"] = False

        aggregated_dir = REPO_ROOT / "data" / "aggregated"
        pilot_summary_path = aggregated_dir / "pilot_summary.csv"
        pilot_stats_path = aggregated_dir / "pilot_stats.txt"
        report_path = aggregated_dir / "report.html"

        aggregated_run_ids = self._aggregated_run_ids(pilot_summary_path)
        out["Pilot Aggregated"] = run_id in aggregated_run_ids
        out["Stats Generated"] = (
            out["Pilot Aggregated"]
            and pilot_stats_path.exists()
            and pilot_summary_path.exists()
            and pilot_stats_path.stat().st_mtime >= pilot_summary_path.stat().st_mtime
        )
        out["Report Built"] = (
            out["Pilot Aggregated"]
            and report_path.exists()
            and pilot_summary_path.exists()
            and report_path.stat().st_mtime >= pilot_summary_path.stat().st_mtime
            and self._report_contains_run(report_path, run_id)
        )
        return out

    def refresh_pipeline_progress(self) -> None:
        """Compatibility wrapper for legacy refresh callers."""
        self.refresh_participants()

    def refresh_participant_badges(self) -> None:
        """Compatibility wrapper for legacy refresh callers."""
        self.refresh_participants()

    # ------------------------------------------------------------------
    # Judge settings -> environment variables
    # ------------------------------------------------------------------

    def _collect_judge_env(self) -> dict[str, str]:
        """Convert judge UI controls into environment overrides for CLI commands."""
        selected = [name for name, var in self.strategy_vars.items() if var.get()]
        if not selected:
            raise ValueError("Enable at least one prompt strategy.")

        sc_samples = int(self.self_consistency_samples_var.get().strip())
        if sc_samples < 1:
            raise ValueError("Self-consistency samples must be >= 1.")

        options = {
            "temperature": float(self.gen_vars["temperature"].get().strip()),
            "top_p": float(self.gen_vars["top_p"].get().strip()),
            "top_k": int(self.gen_vars["top_k"].get().strip()),
            "num_predict": int(self.gen_vars["num_predict"].get().strip()),
            "repeat_penalty": float(self.gen_vars["repeat_penalty"].get().strip()),
            "presence_penalty": float(self.gen_vars["presence_penalty"].get().strip()),
            "frequency_penalty": float(self.gen_vars["frequency_penalty"].get().strip()),
            "seed": int(self.gen_vars["seed"].get().strip()),
            "num_ctx": int(self.gen_vars["num_ctx"].get().strip()),
        }

        env = {
            "GLACIER_JUDGE_STRATEGY_MODE": "ensemble" if len(selected) > 1 else "single",
            "GLACIER_JUDGE_PRIMARY_STRATEGY": selected[0],
            "GLACIER_JUDGE_SELECTED_STRATEGIES": ",".join(selected),
            "GLACIER_JUDGE_VOTE_RULE": "majority",
            "GLACIER_JUDGE_MIN_CONFIDENCE": "0.0",
            "GLACIER_JUDGE_OPTIONS_JSON": json.dumps(options),
            "GLACIER_JUDGE_SELF_CONSISTENCY_SAMPLES": str(sc_samples),
        }
        return env

    # ------------------------------------------------------------------
    # Workflow engine (start/pause/stop/resume)
    # ------------------------------------------------------------------

    def _serialize_steps(self, steps: list[CommandStep]) -> list[dict[str, Any]]:
        """Serialize command steps to plain dictionaries for session-state JSON."""
        return [{"label": s.label, "command": s.command or [], "env": s.env or {}} for s in steps]

    def _wait_if_paused_or_stopped(self) -> bool:
        """Block while paused and exit early when stop is requested."""
        while True:
            with self._workflow_lock:
                stop = self._workflow_stop_requested
            if stop:
                return False
            if self._workflow_pause_event.is_set():
                return True
            threading.Event().wait(0.1)

    def _spawn_and_capture(self, cmd: list[str], env_overrides: Optional[dict[str, str]]) -> tuple[int, str, str]:
        """Execute one subprocess command, streaming output into the log as it arrives."""
        proc: Optional[subprocess.Popen[str]] = None
        out_lines: list[str] = []
        err_lines: list[str] = []

        def _pump(stream: Optional[Any], sink: list[str], *, is_stderr: bool = False) -> None:
            """Forward one process stream line-by-line into the execution log."""
            if stream is None:
                return
            try:
                for raw in iter(stream.readline, ""):
                    line = raw.rstrip()
                    sink.append(line)
                    if not line:
                        continue
                    display = f"[stderr] {line}" if is_stderr else line
                    self._append_log(display)
                    if line.startswith("[analyze]") or line.startswith("[judge]"):
                        self._set_workflow_feedback(state="Running", detail=line)
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        stdout_thread: Optional[threading.Thread] = None
        stderr_thread: Optional[threading.Thread] = None
        try:
            env = os.environ.copy()
            if env_overrides:
                env.update(env_overrides)

            proc = subprocess.Popen(
                cmd,
                cwd=REPO_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
            with self._active_procs_lock:
                self._active_procs.add(proc)

            stdout_thread = threading.Thread(target=_pump, args=(proc.stdout, out_lines), daemon=True)
            stderr_thread = threading.Thread(
                target=_pump,
                args=(proc.stderr, err_lines),
                kwargs={"is_stderr": True},
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()

            rc = proc.wait()
            stdout_thread.join()
            stderr_thread.join()
            return rc, "\n".join(out_lines), "\n".join(err_lines)
        finally:
            if proc is not None:
                with self._active_procs_lock:
                    self._active_procs.discard(proc)

    def _start_workflow(
        self,
        workflow_name: str,
        steps: list[CommandStep],
        start_index: int = 0,
        is_resume: bool = False,
        on_complete: Optional[Callable[[], None]] = None,
        on_failure: Optional[Callable[[str], None]] = None,
        on_stopped: Optional[Callable[[], None]] = None,
    ) -> None:
        """Start queued workflow execution in a background worker thread."""
        if self._shutting_down:
            return
        if self._workflow_thread and self._workflow_thread.is_alive():
            messagebox.showwarning("Workflow running", "Another workflow is already running.")
            return

        with self._workflow_lock:
            self._workflow_stop_requested = False
        self._workflow_pause_event.set()

        serialized = self._serialize_steps(steps)
        total_steps = len(steps)
        self._set_workflow_feedback(
            state="Running",
            detail=f"Starting workflow: {workflow_name}",
            completed=start_index,
            total=total_steps,
        )
        self._write_session_state(status="running", message=f"Workflow started: {workflow_name}", workflow_name=workflow_name, steps=serialized, next_index=start_index)

        def worker() -> None:
            """Run queued workflow steps with pause/stop/error handling."""
            if is_resume:
                self._append_log(f"[{workflow_name}] Resuming from step {start_index + 1}")

            for idx in range(start_index, total_steps):
                if not self._wait_if_paused_or_stopped():
                    self._append_log(f"[{workflow_name}] STOPPED")
                    self._write_session_state(status="interrupted", message="Workflow stopped", workflow_name=workflow_name, steps=serialized, next_index=idx)
                    self._set_workflow_feedback(
                        state="Stopped",
                        detail="Workflow stopped before completion.",
                        completed=idx,
                        total=total_steps,
                    )
                    self._refresh_status_views()
                    if on_stopped is not None:
                        self._on_ui_thread(on_stopped)
                    return

                step = steps[idx]
                self._set_workflow_feedback(
                    state="Running",
                    detail=f"Running step {idx + 1}/{total_steps}: {step.label}",
                    completed=idx,
                    total=total_steps,
                )
                self._append_log(f"\n[{workflow_name}] ({idx + 1}/{total_steps}) {step.label}")

                streamed_step = False
                if step.action is not None:
                    try:
                        step.action()
                        rc, out, err = 0, "", ""
                    except Exception as exc:
                        rc, out, err = 1, "", str(exc)
                elif step.command is not None:
                    streamed_step = True
                    self._append_log(f"[{workflow_name}] {' '.join(step.command)}")
                    rc, out, err = self._spawn_and_capture(step.command, step.env)
                else:
                    rc, out, err = 1, "", "Invalid step"

                if not streamed_step and out.strip():
                    self._append_log(out.strip())
                if not streamed_step and err.strip():
                    self._append_log(err.strip())

                if rc != 0:
                    self._append_log(f"[{workflow_name}] FAILED at step: {step.label}")
                    self._write_session_state(status="failed", message=f"Failed at: {step.label}", workflow_name=workflow_name, steps=serialized, next_index=idx)
                    self._set_workflow_feedback(
                        state="Failed",
                        detail=f"Stopped at step: {step.label}",
                        completed=idx,
                        total=total_steps,
                    )
                    self._refresh_status_views()
                    if on_failure is not None:
                        failure_callback = on_failure
                        self._on_ui_thread(lambda step_label=step.label, callback=failure_callback: callback(step_label))
                    return

                self._write_session_state(status="running", message=f"Progress {idx + 1}/{total_steps}", workflow_name=workflow_name, steps=serialized, next_index=idx + 1)
                self._set_workflow_feedback(
                    state="Running",
                    detail=f"Completed step {idx + 1}/{total_steps}: {step.label}",
                    completed=idx + 1,
                    total=total_steps,
                )
                self._refresh_status_views()

            self._append_log(f"[{workflow_name}] COMPLETE")
            self._write_session_state(status="completed", message=f"Completed: {workflow_name}", workflow_name=workflow_name, steps=serialized, next_index=total_steps)
            self._set_workflow_feedback(
                state="Complete",
                detail=f"Workflow finished: {workflow_name}",
                completed=total_steps,
                total=total_steps,
            )
            self._refresh_status_views()
            if on_complete is not None:
                self._on_ui_thread(on_complete)

        self._workflow_thread = threading.Thread(target=worker, daemon=True)
        self._workflow_thread.start()

    def pause_or_resume_workflow(self) -> None:
        """Toggle pause state for the active workflow thread."""
        if not (self._workflow_thread and self._workflow_thread.is_alive()):
            messagebox.showinfo("No running workflow", "No workflow is currently running.")
            return

        if self._workflow_pause_event.is_set():
            self._workflow_pause_event.clear()
            self._append_log("[workflow] PAUSED")
            self._set_workflow_feedback(state="Paused", detail="Workflow paused. Use Pause / Resume to continue.")
        else:
            self._workflow_pause_event.set()
            self._append_log("[workflow] RESUMED")
            self._set_workflow_feedback(state="Running", detail="Workflow resumed.")

    def stop_workflow(self) -> None:
        """Request workflow stop and terminate tracked subprocesses."""
        with self._workflow_lock:
            self._workflow_stop_requested = True
        self._stop_active_processes()
        if self._workflow_thread and self._workflow_thread.is_alive():
            self._append_log("[workflow] STOP REQUESTED")
            self._set_workflow_feedback(state="Stopping", detail="Stop requested. Waiting for the active step to exit.")

    def _offer_resume_if_needed(self) -> None:
        """Prompt to resume an interrupted workflow when state exists."""
        state = self._read_session_state()
        status = str(state.get("status", "")).strip().lower()
        if status not in {"running", "interrupted", "failed"}:
            return

        steps_obj = state.get("steps")
        steps_data = steps_obj if isinstance(steps_obj, list) else []
        if not steps_data:
            return

        wf_name = str(state.get("workflow_name", "workflow"))
        next_index_obj = state.get("next_index", 0)
        next_index = next_index_obj if isinstance(next_index_obj, int) else 0

        if not messagebox.askyesno("Resume workflow?", f"Found unfinished workflow: {wf_name}\nNext step: {next_index + 1}/{len(steps_data)}\n\nResume now?"):
            return

        steps: list[CommandStep] = []
        for row in steps_data:
            if not isinstance(row, dict):
                continue
            cmd_obj = row.get("command")
            env_obj = row.get("env")
            cmd = [str(x) for x in cmd_obj] if isinstance(cmd_obj, list) else []
            env = {str(k): str(v) for k, v in env_obj.items()} if isinstance(env_obj, dict) else {}
            steps.append(CommandStep(label=str(row.get("label", "step")), command=cmd, env=env))

        if steps:
            self._start_workflow(wf_name, steps, start_index=next_index, is_resume=True)

    # ------------------------------------------------------------------
    # Process termination and close handling
    # ------------------------------------------------------------------

    def _terminate_proc_tree(self, proc: subprocess.Popen[str]) -> None:
        """Terminate a subprocess and its child processes safely."""
        if proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            else:
                proc.terminate()
        except Exception:
            pass

    def _stop_active_processes(self) -> None:
        """Terminate and reap all currently tracked subprocesses."""
        with self._active_procs_lock:
            procs = list(self._active_procs)
        for proc in procs:
            self._terminate_proc_tree(proc)
        for proc in procs:
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    if proc.poll() is None:
                        proc.kill()
                except Exception:
                    pass

    def _on_close(self) -> None:
        """Handle window close by stopping work and saving session state."""
        self._shutting_down = True
        self.stop_workflow()
        status = "interrupted" if self._is_busy() else "idle"
        self._write_session_state(status=status, message="GUI closed")
        self.destroy()

    def _privacy_scope_label(self, rel_path: str) -> str:
        """Convert a finding path into a short scope label."""
        parts = Path(rel_path).parts
        if len(parts) >= 2 and parts[0] == "participant_kits":
            return "/".join(parts[:2])
        if len(parts) >= 3 and parts[0] == "runs":
            return "/".join(parts[:3])
        if len(parts) >= 2 and parts[0] == "data":
            return "/".join(parts[:2])
        if len(parts) >= 2:
            return "/".join(parts[:2])
        return rel_path

    def _log_privacy_scan_result(self, ok: bool, findings: list[Finding], mode: str) -> None:
        """Write one structured privacy-scan summary into the execution log."""
        self._append_log(f"[privacy_check] Scan mode: {mode}")
        if not findings:
            self._append_log("[privacy_check] No findings.")
            self._append_log("[privacy_check] PASS: Repository is clear for publish review.")
            return

        high = sum(1 for finding in findings if finding.severity == "HIGH")
        medium = sum(1 for finding in findings if finding.severity == "MEDIUM")
        self._append_log(f"[privacy_check] Findings: HIGH={high} MEDIUM={medium}")
        for finding in findings:
            self._append_log(
                f"[privacy_check] {finding.severity} [{finding.category}] {finding.path} :: {finding.detail}"
            )
        if ok:
            self._append_log("[privacy_check] PASS WITH WARNINGS: No HIGH findings.")
        else:
            self._append_log("[privacy_check] FAIL: Resolve HIGH findings before publish.")

    def _run_privacy_check_step(self) -> None:
        """Run the repository pre-publish scan and raise on HIGH findings."""
        ok, findings, mode = run_prepublish_check(REPO_ROOT)
        self._last_privacy_scan = (ok, findings, mode)
        self._log_privacy_scan_result(ok, findings, mode)
        if not ok:
            raise RuntimeError("Pre-publish scan found HIGH issues.")

    def _show_privacy_scan_popup(self) -> None:
        """Summarize the last pre-publish scan in a concise popup."""
        if self._last_privacy_scan is None:
            self._show_message("info", "Pre-Publish Repo Scan", "No scan results are available yet.")
            return

        ok, findings, mode = self._last_privacy_scan
        if not findings:
            self._show_message(
                "info",
                "Pre-Publish Repo Scan",
                f"No findings.\n\nScan mode: {mode}\nRepository is clear for publish review.",
            )
            return

        grouped: dict[tuple[str, str], int] = {}
        for finding in findings:
            key = (finding.severity, self._privacy_scope_label(finding.path))
            grouped[key] = grouped.get(key, 0) + 1

        ordered = sorted(grouped.items(), key=lambda item: (0 if item[0][0] == "HIGH" else 1, item[0][1]))
        lines = [f"- {severity}: {scope} ({count})" for (severity, scope), count in ordered[:8]]
        if len(ordered) > 8:
            lines.append(f"- ... and {len(ordered) - 8} more scope(s)")

        if ok:
            title = "Pre-Publish Scan Warnings"
            kind = "warning"
            header = f"No HIGH findings.\n\nScan mode: {mode}\nReview these warning scopes:"
        else:
            title = "Pre-Publish Scan Failed"
            kind = "error"
            header = f"Resolve HIGH findings before publishing.\n\nScan mode: {mode}\nAffected scopes:"

        self._show_message(kind, title, header + "\n" + "\n".join(lines) + "\n\nSee the Execution Log for full details.")

    # ------------------------------------------------------------------
    # User actions
    # ------------------------------------------------------------------

    def start_analysis(self) -> None:
        """Run full pipeline for selected participants.

        Pipeline:
        1) Analyze each selected participant
        2) Merge interaction log when available
        3) Aggregate pilot summary
        4) Compute stats
        5) Build HTML report
        """
        participants = self._extract_selected_participant_ids()
        if not participants:
            messagebox.showinfo("No participants", "Select one or more participants in the status table.")
            return

        try:
            judge_env = self._collect_judge_env()
        except Exception as exc:
            messagebox.showerror("Invalid judge settings", str(exc))
            return

        steps: list[CommandStep] = []
        for pid in participants:
            analyze_cmd = self._python_cmd(
                "-m",
                "scripts.study_cli",
                "analyze-run",
                "--participant_id",
                pid,
                "--phase",
                self.phase_var.get().strip(),
                "--metadata_csv",
                self.metadata_var.get().strip(),
            )
            steps.append(CommandStep(label=f"Analyze {pid}", command=analyze_cmd, env=judge_env))

            run_rel = self._run_dir(pid).as_posix()
            resolved_run_dir = _resolve_run_dir(REPO_ROOT / run_rel)
            log_csv = (resolved_run_dir / "logs" / "snippet_log.csv").resolve()
            if log_csv.exists():
                merge_cmd = self._python_cmd("-m", "scripts.study_cli", "merge-interaction", "--run_dir", run_rel)
                steps.append(CommandStep(label=f"Merge Interaction {pid}", command=merge_cmd))
            else:
                def _skip_merge(local_pid: str, local_log: Path) -> Callable[[], None]:
                    """Build a small action that logs why merge was skipped."""
                    def skip() -> None:
                        """Log a skip message when no snippet interaction file exists."""
                        self._append_log(f"[start_analysis] Skip merge for {local_pid}: missing {local_log}")
                    return skip

                steps.append(CommandStep(label=f"Skip Merge {pid}", action=_skip_merge(pid, log_csv)))

        steps.append(CommandStep(label="Aggregate Pilot", command=self._python_cmd("-m", "scripts.study_cli", "aggregate-pilot")))
        steps.append(
            CommandStep(
                label="Compute Stats",
                command=self._python_cmd("-m", "scripts.study_cli", "compute-stats", "--in_csv", self.stats_csv_var.get().strip()),
            )
        )
        steps.append(
            CommandStep(
                label="Build Report",
                command=self._python_cmd("-m", "scripts.study_cli", "build-report", "--phase", self.phase_var.get().strip()),
            )
        )

        self._start_workflow(
            "start_analysis_pipeline",
            steps,
            on_complete=lambda count=len(participants): self._show_message(
                "info",
                "Analysis Complete",
                f"Completed the analysis pipeline for {count} participant(s).",
            ),
            on_failure=lambda step_label: self._show_message(
                "error",
                "Analysis Failed",
                f"The analysis pipeline stopped at '{step_label}'. Review the Execution Log for details.",
            ),
            on_stopped=lambda: self._show_message(
                "warning",
                "Analysis Stopped",
                "The analysis pipeline was stopped before completion.",
            ),
        )

    def run_privacy_check(self) -> None:
        """Run the pre-publish privacy gate and show actionable results."""
        self._last_privacy_scan = None
        self._append_log("[privacy_check] Running repository-wide pre-publish scan.")
        self._append_log("[privacy_check] This is separate from participant analysis and only checks whether the repo is safe to publish/share.")
        step = CommandStep(label="Privacy Check", action=self._run_privacy_check_step)
        self._start_workflow(
            "privacy_check",
            [step],
            on_complete=self._show_privacy_scan_popup,
            on_failure=lambda _step_label: self._show_privacy_scan_popup(),
            on_stopped=lambda: self._show_message(
                "warning",
                "Pre-Publish Repo Scan",
                "The pre-publish scan was stopped before completion.",
            ),
        )

    def open_html_report(self) -> None:
        """Open the generated HTML report in the default browser."""
        report_path = (REPO_ROOT / "data" / "aggregated" / "report.html").resolve()
        if not report_path.exists():
            messagebox.showwarning(
                "Report missing",
                "Report not found yet. Run Start Analysis first to generate data/aggregated/report.html.",
            )
            return

        try:
            webbrowser.open(report_path.as_uri(), new=2)
            self._append_log(f"[open_report] Opened: {report_path}")
        except Exception as exc:
            messagebox.showerror("Open report failed", str(exc))


def main() -> None:
    """Launch the Tk application loop."""
    app = StudyGUI()
    app.mainloop()


if __name__ == "__main__":
    main()















































