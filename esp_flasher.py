#!/usr/bin/env python3
"""
ESP Flasher GUI - a simple Tkinter front-end for esptool (v4/v5).

Requirements:
    pip install esptool pyserial
    sudo apt install python3-tk        # Ubuntu/Debian, if tkinter is missing

Run:
    python esp_flasher.py
"""

import os
import re
import subprocess
import sys
import threading
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from serial.tools import list_ports
except ImportError:
    list_ports = None

CHIPS = ["esp32s3", "esp32", "esp32s2", "esp32c3", "esp32c6", "esp32h2", "esp8266", "auto"]
BAUDS = ["115200", "230400", "460800", "921600", "1500000"]
FLASH_MODES = ["keep", "dio", "qio", "dout", "qout"]
FLASH_FREQS = ["keep", "80m", "40m", "26m", "20m"]
FLASH_SIZES = ["detect", "keep", "2MB", "4MB", "8MB", "16MB", "32MB"]

# Default rows (Arduino / PlatformIO layout for ESP32-S3)
DEFAULT_ROWS = [
    (True,  "0x0",     "bootloader.bin"),
    (True,  "0x8000",  "partitions.bin"),
    (False, "0xe000",  "boot_app0.bin"),
    (True,  "0x10000", "firmware.bin"),
]

PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)\s*%")


class FileRow:
    def __init__(self, parent, row, enabled, addr, fname, base_dir):
        self.enabled = tk.BooleanVar(value=enabled)
        self.addr = tk.StringVar(value=addr)
        path = os.path.join(base_dir, fname)
        self.path = tk.StringVar(value=path if os.path.exists(path) else "")

        ttk.Checkbutton(parent, variable=self.enabled).grid(row=row, column=0, padx=2)
        ttk.Entry(parent, textvariable=self.addr, width=10).grid(row=row, column=1, padx=2, pady=2)
        ttk.Entry(parent, textvariable=self.path).grid(row=row, column=2, sticky="ew", padx=2)
        ttk.Button(parent, text="Browse…", command=self.browse).grid(row=row, column=3, padx=2)

    def browse(self):
        f = filedialog.askopenfilename(
            title="Select binary",
            initialdir=os.path.dirname(self.path.get()) or os.path.expanduser("~/Downloads"),
            filetypes=[("Binary files", "*.bin"), ("All files", "*")],
        )
        if f:
            self.path.set(f)
            self.enabled.set(True)


class FlasherApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("ESP Flasher")
        self.geometry("820x620")
        self.minsize(640, 480)
        self.proc = None
        self.q = queue.Queue()
        self._build_ui()
        self.refresh_ports()
        self.after(50, self._poll_queue)

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 6, "pady": 4}

        # Connection
        conn = ttk.LabelFrame(self, text="Connection")
        conn.pack(fill="x", **pad)

        ttk.Label(conn, text="Port").grid(row=0, column=0, sticky="w", **pad)
        self.port = tk.StringVar()
        self.port_cb = ttk.Combobox(conn, textvariable=self.port, width=22)
        self.port_cb.grid(row=0, column=1, **pad)
        ttk.Button(conn, text="⟳", width=3, command=self.refresh_ports).grid(row=0, column=2)

        ttk.Label(conn, text="Chip").grid(row=0, column=3, sticky="w", **pad)
        self.chip = tk.StringVar(value="esp32s3")
        ttk.Combobox(conn, textvariable=self.chip, values=CHIPS, width=10, state="readonly").grid(row=0, column=4, **pad)

        ttk.Label(conn, text="Baud").grid(row=0, column=5, sticky="w", **pad)
        self.baud = tk.StringVar(value="460800")
        ttk.Combobox(conn, textvariable=self.baud, values=BAUDS, width=9).grid(row=0, column=6, **pad)

        # Flash settings
        fs = ttk.LabelFrame(self, text="Flash settings")
        fs.pack(fill="x", **pad)
        self.mode = tk.StringVar(value="dio")
        self.freq = tk.StringVar(value="80m")
        self.size = tk.StringVar(value="detect")
        for i, (lbl, var, vals) in enumerate([
            ("Mode", self.mode, FLASH_MODES),
            ("Freq", self.freq, FLASH_FREQS),
            ("Size", self.size, FLASH_SIZES),
        ]):
            ttk.Label(fs, text=lbl).grid(row=0, column=i * 2, sticky="w", **pad)
            ttk.Combobox(fs, textvariable=var, values=vals, width=8, state="readonly").grid(row=0, column=i * 2 + 1, **pad)
        self.erase_first = tk.BooleanVar(value=False)
        ttk.Checkbutton(fs, text="Erase all before flashing", variable=self.erase_first).grid(row=0, column=6, **pad)

        # Files
        files = ttk.LabelFrame(self, text="Binaries (tick to include)")
        files.pack(fill="x", **pad)
        files.columnconfigure(2, weight=1)
        for c, h in enumerate(["Use", "Address", "File", ""]):
            ttk.Label(files, text=h, font=("TkDefaultFont", 9, "bold")).grid(row=0, column=c, sticky="w", padx=2)
        base = os.path.expanduser("~/Downloads")
        self.rows = [FileRow(files, i + 1, *r, base_dir=base) for i, r in enumerate(DEFAULT_ROWS)]

        # Buttons
        btns = ttk.Frame(self)
        btns.pack(fill="x", **pad)
        self.flash_btn = ttk.Button(btns, text="⚡ Flash", command=self.flash)
        self.flash_btn.pack(side="left", padx=4)
        self.erase_btn = ttk.Button(btns, text="Erase flash", command=self.erase)
        self.erase_btn.pack(side="left", padx=4)
        self.info_btn = ttk.Button(btns, text="Chip info", command=self.chip_info)
        self.info_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(btns, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(btns, text="Clear log", command=lambda: self.log.delete("1.0", "end")).pack(side="right", padx=4)

        # Progress
        prog = ttk.Frame(self)
        prog.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(prog, maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        self.status = tk.StringVar(value="Idle")
        ttk.Label(prog, textvariable=self.status, width=24).pack(side="left", padx=6)

        # Log
        logf = ttk.Frame(self)
        logf.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logf, bg="#111", fg="#ddd", insertbackground="#ddd",
                           font=("Monospace", 9), wrap="word")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.log.tag_config("err", foreground="#ff6b6b")
        self.log.tag_config("ok", foreground="#6bff8a")
        self.log.tag_config("cmd", foreground="#6bb8ff")

    # ---------- helpers ----------
    def refresh_ports(self):
        ports = []
        if list_ports:
            ports = [p.device for p in list_ports.comports()
                     if "ttyS" not in p.device or p.hwid != "n/a"]
        self.port_cb["values"] = ports
        if ports and self.port.get() not in ports:
            # Prefer USB ports
            usb = [p for p in ports if "USB" in p or "ACM" in p or "COM" in p]
            self.port.set((usb or ports)[0])

    def write_log(self, text, tag=None):
        self.log.insert("end", text, tag)
        self.log.see("end")

    def base_cmd(self):
        port = self.port.get().strip()
        if not port:
            raise ValueError("Select a serial port.")
        cmd = [sys.executable, "-m", "esptool", "-p", port, "-b", self.baud.get()]
        if self.chip.get() != "auto":
            cmd[3:3] = ["--chip", self.chip.get()]
        return cmd

    def set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for b in (self.flash_btn, self.erase_btn, self.info_btn):
            b.config(state=state)
        self.stop_btn.config(state="normal" if busy else "disabled")

    # ---------- actions ----------
    def flash(self):
        try:
            cmd = self.base_cmd()
            pairs = []
            for r in self.rows:
                if not r.enabled.get():
                    continue
                addr, path = r.addr.get().strip(), r.path.get().strip()
                int(addr, 0)  # validate
                if not os.path.isfile(path):
                    raise ValueError(f"File not found for {addr}:\n{path or '(empty)'}")
                pairs += [addr, path]
            if not pairs:
                raise ValueError("Tick at least one binary.")
        except ValueError as e:
            messagebox.showerror("ESP Flasher", str(e))
            return

        cmd += ["--before", "default-reset", "--after", "hard-reset", "write-flash"]
        if self.erase_first.get():
            cmd.append("--erase-all")
        cmd += ["--flash-mode", self.mode.get(), "--flash-freq", self.freq.get(),
                "--flash-size", self.size.get()] + pairs
        self.run(cmd, "Flashing")

    def erase(self):
        if not messagebox.askyesno("ESP Flasher", "Erase the entire flash?"):
            return
        try:
            self.run(self.base_cmd() + ["erase-flash"], "Erasing")
        except ValueError as e:
            messagebox.showerror("ESP Flasher", str(e))

    def chip_info(self):
        try:
            self.run(self.base_cmd() + ["flash-id"], "Reading chip info")
        except ValueError as e:
            messagebox.showerror("ESP Flasher", str(e))

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.write_log("\n[Stopped by user]\n", "err")

    # ---------- process handling ----------
    def run(self, cmd, label):
        self.progress["value"] = 0
        self.status.set(label + "…")
        self.set_busy(True)
        shown = " ".join(f'"{c}"' if " " in c else c for c in cmd[1:])
        self.write_log(f"\n$ python {shown}\n", "cmd")
        threading.Thread(target=self._worker, args=(cmd,), daemon=True).start()

    def _worker(self, cmd):
        env = dict(os.environ, PYTHONUNBUFFERED="1")
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        except Exception as e:
            self.q.put(("line", f"Failed to start esptool: {e}\n"))
            self.q.put(("done", -1))
            return
        buf = b""
        fd = self.proc.stdout.fileno()
        while True:
            chunk = os.read(fd, 1024)
            if not chunk:
                break
            buf += chunk
            # Split on both \n and \r so progress updates stream live
            parts = re.split(rb"(\r\n|\n|\r)", buf)
            buf = parts.pop() if parts else b""
            for i in range(0, len(parts) - 1, 2):
                line, sep = parts[i].decode(errors="replace"), parts[i + 1]
                self.q.put(("progress" if sep == b"\r" else "line", line + ("\n" if sep != b"\r" else "")))
        if buf:
            self.q.put(("line", buf.decode(errors="replace") + "\n"))
        self.q.put(("done", self.proc.wait()))

    def _poll_queue(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind in ("line", "progress"):
                    m = PCT_RE.search(data)
                    if m:
                        self.progress["value"] = min(float(m.group(1)), 100)
                        self.status.set(data.strip()[:24])
                    if kind == "line" and data.strip():
                        low = data.lower()
                        tag = "err" if ("error" in low or "fatal" in low or "failed" in low) else None
                        self.write_log(data, tag)
                elif kind == "done":
                    self.set_busy(False)
                    if data == 0:
                        self.progress["value"] = 100
                        self.status.set("Done ✔")
                        self.write_log("✔ Completed successfully\n", "ok")
                    else:
                        self.status.set(f"Failed (exit {data})")
                        self.write_log(
                            f"✘ esptool exited with code {data}. "
                            "Tip: hold BOOT, tap RESET, release BOOT, then retry.\n", "err")
        except queue.Empty:
            pass
        self.after(50, self._poll_queue)


if __name__ == "__main__":
    FlasherApp().mainloop()