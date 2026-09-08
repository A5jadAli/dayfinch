from __future__ import annotations

import time
from typing import Any


def run_desktop(agent: Any) -> None:
    """Run the visible Dayfinch desktop timer.

    Tk is imported lazily so headless/service installs can keep using --no-tray.
    """
    import tkinter as tk
    from tkinter import ttk

    root = tk.Tk()
    root.title("Dayfinch Tracker")
    root.geometry("440x620")
    root.minsize(390, 540)
    root.configure(bg="#111827")

    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TCombobox", padding=9, fieldbackground="#ffffff")

    header = tk.Frame(root, bg="#111827", padx=24, pady=20)
    header.pack(fill="x")
    tk.Label(
        header,
        text="D",
        bg="#6d5dfc",
        fg="white",
        width=3,
        height=1,
        font=("Segoe UI", 16, "bold"),
    ).pack(side="left")
    brand = tk.Frame(header, bg="#111827")
    brand.pack(side="left", padx=12)
    tk.Label(
        brand, text="Dayfinch", bg="#111827", fg="white", font=("Segoe UI", 16, "bold")
    ).pack(anchor="w")
    tk.Label(
        brand,
        text="DESKTOP TRACKER",
        bg="#111827",
        fg="#64748b",
        font=("Segoe UI", 8, "bold"),
    ).pack(anchor="w")

    card = tk.Frame(root, bg="white", padx=24, pady=24)
    card.pack(fill="both", expand=True, padx=16, pady=(0, 16))
    tk.Label(
        card,
        text="CURRENT SESSION",
        bg="white",
        fg="#94a3b8",
        font=("Segoe UI", 8, "bold"),
    ).pack()
    timer = tk.Label(
        card, text="00:00:00", bg="white", fg="#0f172a", font=("Segoe UI", 38, "bold")
    )
    timer.pack(pady=(8, 4))
    status = tk.Label(
        card, text=agent.status, bg="white", fg="#64748b", font=("Segoe UI", 10)
    )
    status.pack(pady=(0, 22))

    try:
        catalog = agent.catalog()
        projects = catalog.get("projects", [])
    except Exception:
        projects = []

    project_by_name = {p["name"]: p for p in projects}
    tk.Label(
        card, text="PROJECT", bg="white", fg="#475569", font=("Segoe UI", 9, "bold")
    ).pack(anchor="w")
    project_box = ttk.Combobox(card, state="readonly", values=list(project_by_name))
    project_box.pack(fill="x", pady=(5, 14))
    tk.Label(
        card, text="TASK", bg="white", fg="#475569", font=("Segoe UI", 9, "bold")
    ).pack(anchor="w")
    task_box = ttk.Combobox(card, state="readonly")
    task_box.pack(fill="x", pady=(5, 14))
    task_by_name: dict[str, dict] = {}

    tk.Label(
        card,
        text="WORK NOTE",
        bg="white",
        fg="#475569",
        font=("Segoe UI", 9, "bold"),
    ).pack(anchor="w")
    note = tk.Entry(
        card,
        bg="#f8fafc",
        fg="#0f172a",
        relief="solid",
        bd=1,
        font=("Segoe UI", 10),
    )
    note.pack(fill="x", ipady=9, pady=(5, 12))
    note.bind("<FocusOut>", lambda _event: agent.set_note(note.get()))
    note.bind("<Return>", lambda _event: agent.set_note(note.get()))

    def project_changed(_event=None) -> None:
        nonlocal task_by_name
        project = project_by_name.get(project_box.get(), {})
        task_by_name = {t["name"]: t for t in project.get("tasks", [])}
        task_box["values"] = ["General project time", *task_by_name]
        task_box.set("General project time")
        agent.select_work(str(project.get("id", "")), "")

    def task_changed(_event=None) -> None:
        project = project_by_name.get(project_box.get(), {})
        task = task_by_name.get(task_box.get(), {})
        agent.select_work(str(project.get("id", "")), str(task.get("id", "")))

    project_box.bind("<<ComboboxSelected>>", project_changed)
    task_box.bind("<<ComboboxSelected>>", task_changed)
    if projects:
        project_box.current(0)
        project_changed()

    started = time.monotonic()
    accumulated = 0.0
    running_since: float | None = started if not agent.paused else None

    action = tk.Button(
        card,
        text="PAUSE TRACKING",
        bg="#6d5dfc",
        fg="white",
        activebackground="#5544cc",
        activeforeground="white",
        relief="flat",
        cursor="hand2",
        font=("Segoe UI", 11, "bold"),
        pady=13,
    )
    action.pack(fill="x", pady=(10, 8))

    def toggle() -> None:
        nonlocal accumulated, running_since
        if running_since is not None:
            accumulated += time.monotonic() - running_since
            running_since = None
        else:
            running_since = time.monotonic()
        agent.toggle_pause()
        action.configure(
            text="START TRACKING" if agent.paused else "PAUSE TRACKING",
            bg="#10b981" if agent.paused else "#6d5dfc",
        )

    action.configure(command=toggle)
    tk.Button(
        card,
        text="Capture screenshot now",
        command=agent.capture_now,
        bg="white",
        fg="#475569",
        relief="flat",
        cursor="hand2",
        font=("Segoe UI", 9),
    ).pack(pady=5)
    privacy = tk.Frame(card, bg="#f8fafc", padx=12, pady=10)
    privacy.pack(fill="x", side="bottom")
    tk.Label(
        privacy,
        text="●  Tracking is visible and controllable",
        bg="#f8fafc",
        fg="#059669",
        font=("Segoe UI", 9, "bold"),
    ).pack(anchor="w")
    tk.Label(
        privacy,
        text="No keystroke content, clipboard, audio, or camera data is collected.",
        bg="#f8fafc",
        fg="#64748b",
        wraplength=340,
        justify="left",
        font=("Segoe UI", 8),
    ).pack(anchor="w", pady=(3, 0))

    def update() -> None:
        nonlocal accumulated, running_since
        if agent.tracking_active and running_since is None:
            running_since = time.monotonic()
        elif not agent.tracking_active and running_since is not None:
            accumulated += time.monotonic() - running_since
            running_since = None
        elapsed = accumulated + (
            (time.monotonic() - running_since) if running_since else 0
        )
        seconds = int(elapsed)
        timer.configure(
            text=f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
        )
        status.configure(text=agent.status)
        if not agent.stop_event.is_set():
            root.after(500, update)
        else:
            root.destroy()

    def close() -> None:
        agent.stop()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", close)
    update()
    root.mainloop()
