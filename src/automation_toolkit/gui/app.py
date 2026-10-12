"""Responsive desktop interface using a worker and a main-thread result queue."""

from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from tkinter import filedialog, messagebox

from automation_toolkit.config import load_config
from automation_toolkit.pipeline import run_pipeline


def main() -> None:
    try:
        import customtkinter as ctk
    except ImportError as exc:
        raise SystemExit('Install the desktop extra: pip install ".[gui]"') from exc
    ctk.set_appearance_mode("light")
    app = ctk.CTk()
    app.title("Python Automation Toolkit")
    app.geometry("700x520")
    app.minsize(650, 480)
    inputs: list[Path] = []
    results: Queue = Queue()
    ctk.CTkLabel(app, text="Sales reporting, automated", font=("Arial", 26, "bold")).pack(pady=25)
    selected = ctk.CTkLabel(app, text="Choose one or more sales files")
    selected.pack(pady=5)

    def choose_files() -> None:
        paths = filedialog.askopenfilenames(filetypes=[("Sales data", "*.csv *.xlsx")])
        if paths:
            inputs[:] = [Path(p) for p in paths]
            selected.configure(text=f"{len(inputs)} file(s) selected")

    ctk.CTkButton(app, text="Select CSV / Excel files", command=choose_files).pack(pady=8)
    output = ctk.CTkEntry(app, width=520, placeholder_text="Output folder")
    output.insert(0, str(Path("reports").resolve()))
    output.pack(pady=10)

    def choose_output() -> None:
        folder = filedialog.askdirectory()
        if folder:
            output.delete(0, "end")
            output.insert(0, folder)

    ctk.CTkButton(app, text="Choose output folder", command=choose_output).pack(pady=4)
    configuration = ctk.CTkEntry(
        app, width=520, placeholder_text="Optional YAML configuration path"
    )
    configuration.pack(pady=10)
    formats = ctk.CTkOptionMenu(app, values=["all", "excel", "pdf", "charts"])
    formats.pack(pady=10)
    status = ctk.CTkLabel(app, text="Ready")
    status.pack(pady=10)

    def generate() -> None:
        if not inputs or not output.get().strip():
            messagebox.showerror("Missing input", "Select sales files and an output folder.")
            return
        # Snapshot widget values on the Tk main thread, never from the worker.
        files, folder, cfg, fmt = (
            inputs.copy(),
            Path(output.get()),
            configuration.get(),
            formats.get(),
        )
        button.configure(state="disabled")
        status.configure(text="Generating reports...")

        def worker() -> None:
            try:
                result = run_pipeline(files, folder, load_config(Path(cfg) if cfg else None), fmt)
                results.put((True, str(result.output_dir.resolve())))
            except Exception as exc:
                results.put((False, str(exc)))

        Thread(target=worker, daemon=True).start()

    def poll() -> None:
        try:
            success, message = results.get_nowait()
            button.configure(state="normal")
            status.configure(text="Reports generated" if success else "Generation failed")
            (messagebox.showinfo if success else messagebox.showerror)("Sales reporting", message)
        except Empty:
            pass
        app.after(100, poll)

    button = ctk.CTkButton(app, text="Generate Report", command=generate, height=40)
    button.pack(pady=5)
    app.after(100, poll)
    app.mainloop()


if __name__ == "__main__":
    main()
