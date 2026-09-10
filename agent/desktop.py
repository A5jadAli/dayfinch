from __future__ import annotations

import queue
import threading
import time
from typing import Any


def run_desktop(agent: Any) -> None:
    """Run the visible Dayfinch desktop timer.

    Tk is imported lazily so headless/service installs can keep using --no-tray.
    """
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("Dayfinch Tracker")
    root.geometry("440x680")
    root.minsize(390, 590)
    root.configure(bg="#111827")
    consent_results: queue.SimpleQueue[tuple[str, str, Exception | None]] = (
        queue.SimpleQueue()
    )
    consent_inflight = ""
    consent_retry_at = 0.0

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
    reminder_banner = tk.Label(
        card,
        text="",
        bg="white",
        fg="#b45309",
        wraplength=340,
        justify="center",
        font=("Segoe UI", 9, "bold"),
    )
    reminder_banner.pack()

    try:
        catalog = agent.catalog()
        projects = catalog.get("projects", [])
    except Exception:
        projects = agent.browser_timer_snapshot().get("projects", [])

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

    accumulated = 0.0
    running_since: float | None = None

    action = tk.Button(
        card,
        text="START TRACKING",
        bg="#10b981",
        fg="white",
        activebackground="#047857",
        activeforeground="white",
        relief="flat",
        cursor="hand2",
        font=("Segoe UI", 11, "bold"),
        pady=13,
    )
    action.pack(fill="x", pady=(10, 8))

    pause = tk.Button(
        card,
        text="PAUSE",
        bg="#eef2ff",
        fg="#4338ca",
        activebackground="#e0e7ff",
        activeforeground="#3730a3",
        relief="flat",
        cursor="hand2",
        font=("Segoe UI", 10, "bold"),
        pady=10,
        state="disabled",
    )
    pause.pack(fill="x", pady=(0, 8))

    def toggle_timer() -> None:
        nonlocal accumulated, running_since
        if agent.timer_state == "stopped":
            try:
                agent.start_tracking()
            except ValueError as exc:
                status.configure(text=str(exc), fg="#dc2626")
                return
            accumulated = 0.0
            running_since = time.monotonic()
        else:
            if running_since is not None:
                accumulated += time.monotonic() - running_since
            agent.stop_tracking()
            accumulated = 0.0
            running_since = None

    def toggle_pause() -> None:
        nonlocal accumulated, running_since
        if agent.timer_state == "active":
            if running_since is not None:
                accumulated += time.monotonic() - running_since
            running_since = None
            agent.pause_tracking()
        elif agent.timer_state == "paused":
            agent.resume_tracking()
            running_since = time.monotonic()

    action.configure(command=toggle_timer)
    pause.configure(command=toggle_pause)
    capture_button = tk.Button(
        card,
        text="Capture screenshot now",
        command=agent.capture_now,
        bg="white",
        fg="#475569",
        relief="flat",
        cursor="hand2",
        font=("Segoe UI", 9),
        state="disabled",
    )
    capture_button.pack(pady=5)
    reminder_button = tk.Button(
        card,
        text="Reminder settings",
        bg="white",
        fg="#475569",
        relief="flat",
        cursor="hand2",
        font=("Segoe UI", 9),
    )
    reminder_button.pack(pady=(0, 5))
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

    def request_automatic_consent(policy: dict) -> None:
        nonlocal consent_inflight
        policy_id = str(policy.get("id", ""))
        consent_inflight = policy_id
        rule = (
            "your published shifts"
            if policy.get("rule_type") == "shifts"
            else "the configured local-time schedule"
        )
        accepted = messagebox.askyesno(
            "Allow automatic tracking?",
            f"Your organization assigned “{policy.get('name', 'Automatic tracking')}”.\n\n"
            f"If you allow it, Dayfinch may visibly start and stop this timer during {rule}. "
            "Screenshots and aggregate activity are collected only while the timer is active. "
            "You can still stop the timer, which suppresses another start for the current window.\n\n"
            "Allow this policy?",
            parent=root,
        )

        def submit() -> None:
            try:
                agent.respond_automatic_policy(policy_id, accepted)
            except Exception as exc:
                consent_results.put((policy_id, "", exc))
            else:
                consent_results.put(
                    (policy_id, "accepted" if accepted else "declined", None)
                )

        threading.Thread(target=submit, name="automatic-consent", daemon=True).start()

    def open_reminder_settings() -> None:
        values = agent.tracking_reminder_settings
        dialog = tk.Toplevel(root)
        dialog.title("Tracking reminders")
        dialog.geometry("420x510")
        dialog.resizable(False, False)
        dialog.configure(bg="white")
        dialog.transient(root)
        dialog.grab_set()

        body = tk.Frame(dialog, bg="white", padx=24, pady=22)
        body.pack(fill="both", expand=True)
        tk.Label(
            body,
            text="Tracking reminders",
            bg="white",
            fg="#0f172a",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w")
        tk.Label(
            body,
            text="Remind me when the timer is stopped during these local hours.",
            bg="white",
            fg="#64748b",
            wraplength=360,
            justify="left",
            font=("Segoe UI", 9),
        ).pack(anchor="w", pady=(4, 14))

        enabled = tk.BooleanVar(value=bool(values["enabled"]))
        tk.Checkbutton(
            body,
            text="Enable reminders",
            variable=enabled,
            bg="white",
            activebackground="white",
            fg="#0f172a",
            font=("Segoe UI", 10, "bold"),
        ).pack(anchor="w")

        times = tk.Frame(body, bg="white")
        times.pack(fill="x", pady=(14, 10))
        start_value = tk.StringVar(value=str(values["start"]))
        end_value = tk.StringVar(value=str(values["end"]))
        for column, (label, variable) in enumerate(
            (("START (HH:MM)", start_value), ("END (HH:MM)", end_value))
        ):
            group = tk.Frame(times, bg="white")
            group.grid(
                row=0,
                column=column,
                sticky="ew",
                padx=(0, 8 if not column else 0),
            )
            tk.Label(
                group,
                text=label,
                bg="white",
                fg="#475569",
                font=("Segoe UI", 8, "bold"),
            ).pack(anchor="w")
            tk.Entry(
                group,
                textvariable=variable,
                bg="#f8fafc",
                fg="#0f172a",
                relief="solid",
                bd=1,
                font=("Segoe UI", 10),
            ).pack(fill="x", ipady=7, pady=(4, 0))
            times.grid_columnconfigure(column, weight=1)

        tk.Label(
            body,
            text="DAYS",
            bg="white",
            fg="#475569",
            font=("Segoe UI", 8, "bold"),
        ).pack(anchor="w")
        day_row = tk.Frame(body, bg="white")
        day_row.pack(fill="x", pady=(5, 12))
        day_names = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
        day_values = {
            day: tk.BooleanVar(value=day in values["days"]) for day in day_names
        }
        for day in day_names:
            tk.Checkbutton(
                day_row,
                text=day.title(),
                variable=day_values[day],
                bg="white",
                activebackground="white",
                padx=2,
                font=("Segoe UI", 8),
            ).pack(side="left")

        options = tk.Frame(body, bg="white")
        options.pack(fill="x", pady=(0, 12))
        tk.Label(
            options,
            text="REPEAT EVERY",
            bg="white",
            fg="#475569",
            font=("Segoe UI", 8, "bold"),
        ).grid(row=0, column=0, sticky="w")
        interval = tk.IntVar(value=int(values["interval_minutes"]))
        tk.Spinbox(
            options,
            from_=5,
            to=240,
            increment=5,
            textvariable=interval,
            width=8,
            font=("Segoe UI", 10),
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        tk.Label(
            options,
            text="minutes",
            bg="white",
            fg="#64748b",
            font=("Segoe UI", 9),
        ).grid(row=1, column=0, sticky="w", padx=(70, 0), pady=(4, 0))

        mode = tk.StringVar(value=str(values["mode"]))
        tk.Radiobutton(
            body,
            text="Notification banner",
            variable=mode,
            value="notification",
            bg="white",
            activebackground="white",
        ).pack(anchor="w")
        tk.Radiobutton(
            body,
            text="Alert dialog",
            variable=mode,
            value="alert",
            bg="white",
            activebackground="white",
        ).pack(anchor="w")

        error = tk.Label(
            body,
            text="",
            bg="white",
            fg="#dc2626",
            wraplength=350,
            justify="left",
            font=("Segoe UI", 8),
        )
        error.pack(anchor="w", pady=(10, 0))

        def save() -> None:
            try:
                agent.configure_tracking_reminders(
                    {
                        "enabled": enabled.get(),
                        "start": start_value.get().strip(),
                        "end": end_value.get().strip(),
                        "days": [day for day in day_names if day_values[day].get()],
                        "interval_minutes": interval.get(),
                        "mode": mode.get(),
                    }
                )
            except (OSError, ValueError) as exc:
                error.configure(text=str(exc))
                return
            dialog.destroy()

        actions = tk.Frame(body, bg="white")
        actions.pack(fill="x", side="bottom")
        tk.Button(
            actions,
            text="Cancel",
            command=dialog.destroy,
            bg="#e2e8f0",
            fg="#334155",
            relief="flat",
            padx=18,
            pady=9,
        ).pack(side="right")
        tk.Button(
            actions,
            text="Save reminders",
            command=save,
            bg="#6d5dfc",
            fg="white",
            activebackground="#5548d9",
            activeforeground="white",
            relief="flat",
            padx=18,
            pady=9,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="right", padx=(0, 8))

    reminder_button.configure(command=open_reminder_settings)

    def update() -> None:
        nonlocal accumulated, consent_inflight, consent_retry_at, running_since
        try:
            policy_id, decision, error = consent_results.get_nowait()
        except queue.Empty:
            pass
        else:
            consent_inflight = ""
            if error:
                consent_retry_at = time.monotonic() + 30
                messagebox.showerror(
                    "Could not save preference",
                    "Dayfinch could not reach the server. The policy remains disabled; retry when online.",
                    parent=root,
                )
            else:
                status.configure(
                    text=f"Automatic tracking {decision}",
                    fg="#059669" if decision == "accepted" else "#64748b",
                )
        pending_policy = agent.pending_automatic_policy
        if (
            pending_policy
            and not consent_inflight
            and time.monotonic() >= consent_retry_at
        ):
            request_automatic_consent(pending_policy)
        reminder = agent.tracking_reminder()
        if reminder:
            if reminder["mode"] == "alert":
                messagebox.showwarning(
                    "Time tracking reminder", reminder["message"], parent=root
                )
            else:
                reminder_banner.configure(text=reminder["message"])
                root.bell()
                root.after(15_000, lambda: reminder_banner.configure(text=""))
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
        status.configure(text=agent.status, fg="#64748b")
        stopped = agent.timer_state == "stopped"
        action.configure(
            text="START TRACKING" if stopped else "STOP TRACKING",
            bg="#10b981" if stopped else "#dc2626",
            activebackground="#047857" if stopped else "#b91c1c",
        )
        pause.configure(
            text="RESUME" if agent.timer_state == "paused" else "PAUSE",
            state="normal" if not stopped else "disabled",
        )
        capture_button.configure(
            state="normal" if agent.tracking_active else "disabled"
        )
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
