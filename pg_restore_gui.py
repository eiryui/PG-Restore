#!/usr/bin/env python3
"""
PG Restore - a small desktop app to restore a PostgreSQL database
from a .sql / .sql.gz / .dump (custom, tar or directory) backup.

Standalone: the PostgreSQL client tools (psql, pg_restore + DLLs) are expected in
a 'pgsql/bin' folder next to this script / the built exe. Falls back to PATH.
"""

import gzip
import ipaddress
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
from urllib.parse import unquote, urlsplit, urlunsplit

IS_WIN = os.name == "nt"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if IS_WIN else 0

ACCENT = "#2563eb"
ACCENT_HOVER = "#1d4ed8"
BG = "#f5f6f8"
URL_PLACEHOLDER = "postgresql://user:password@host:5432/dbname"

# ----------------------------------------------------------------- helpers


def mask(text: str) -> str:
    """Hide passwords inside postgres:// URLs."""
    return re.sub(r"(://[^:/@\s]+:)[^@\s]+@", r"\1****@", text)


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


ERR_RE = re.compile(r"\b(ERROR|FATAL):|pg_restore: error")


def hint_for(err: str) -> str:
    """Plain-language explanation for the most common restore errors."""
    low = err.lower()
    if "already exists" in low:
        return (
            "The target database already contains some of these objects. Restore into an empty "
            "database, or tick 'Ignore errors and continue'. For .dump files you can also tick "
            "'Drop existing objects'."
        )
    if "transaction_timeout" in low or "unrecognized configuration parameter" in low:
        return (
            "The backup uses a setting this server doesn't recognise (usually because the backup "
            "was made by a newer PostgreSQL than the target server). This is normally harmless: "
            "tick 'Ignore errors and continue'."
        )
    if "role" in low and "does not exist" in low:
        return (
            "The backup refers to a database user (role) that doesn't exist on the target. Tick "
            "'Ignore errors and continue' to skip the owner/permission statements, or create the role first."
        )
    if "extension" in low and ("not available" in low or "permission denied" in low):
        return "The backup needs an extension that the target server doesn't provide or that you aren't allowed to create."
    if "permission denied" in low:
        return "The database user in the URL doesn't have enough rights for this restore."
    if "syntax error" in low:
        return (
            "A statement in the file couldn't be parsed. Check that it is a complete plain SQL "
            "backup (not cut off, not another format)."
        )
    if "invalid command" in low:
        return "The file contains a psql command that this psql version doesn't understand."
    return ""


# ------------------------------------------------- remote / cloud database support


def is_remote(host: str) -> bool:
    """True for public hosts; False for localhost, LAN addresses and docker service names."""
    h = (host or "").strip().lower()
    if not h or ("." not in h and ":" not in h):
        return False
    try:
        ip = ipaddress.ip_address(h)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local)
    except ValueError:
        return not h.endswith((".local", ".internal", ".lan", ".localdomain"))


def prepare_url(url: str):
    """Return (url_for_the_tools, extra_env, notes).

    - the password is moved into PGPASSWORD, so it isn't visible in the process list
      (this also copes with an unencoded '@' in the password)
    - sslmode=require is added for remote hosts when the URL doesn't set an SSL mode
    """
    notes = []
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        port = p.port
    except ValueError:
        return url, {}, notes  # unusual URL: leave it to libpq
    if "," in p.netloc.rpartition("@")[2]:
        return url, {}, notes  # multi-host URL: leave it alone
    env = {}
    if p.password is not None:
        env["PGPASSWORD"] = unquote(p.password)
    hostpart = f"[{host}]" if ":" in host else host
    if port:
        hostpart += f":{port}"
    netloc = (p.username + "@" if p.username else "") + hostpart
    query = p.query
    keys = {kv.split("=", 1)[0].lower() for kv in query.split("&") if kv}
    if is_remote(host) and "sslmode" not in keys:
        query += ("&" if query else "") + "sslmode=require"
        notes.append("Remote host detected: SSL is required (sslmode=require added).")
    return urlunsplit((p.scheme, netloc, p.path, query, p.fragment)), env, notes


def provider_info(url: str):
    """Recognise popular hosted PostgreSQL providers. Returns (name or None, [warnings])."""
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        port = p.port
    except ValueError:
        return None, []
    name, notes = None, []
    if host.endswith("neon.tech"):
        name = "Neon"
        if "-pooler" in host:
            notes.append(
                "This is Neon's pooled connection. For restores use the direct connection "
                "(the same host without '-pooler')."
            )
    elif "supabase" in host:
        name = "Supabase"
        if port == 6543:
            notes.append(
                "Port 6543 is Supabase's transaction pooler, which is unreliable for restores. "
                "Use the Session pooler (port 5432) or the direct connection."
            )
        if host.startswith("db.") and host.endswith(".supabase.co"):
            notes.append(
                "Supabase's direct host is IPv6-only unless you have the IPv4 add-on. If it won't "
                "connect, use the Session pooler connection string instead."
            )
    elif host.endswith("aivencloud.com"):
        name = "Aiven"
    elif host.endswith("prisma.io") or "prisma-data.net" in host:
        name = "Prisma Postgres"
    elif host.endswith("render.com"):
        name = "Render"
    elif host.endswith(("rlwy.net", "railway.app")):
        name = "Railway"
    elif host.endswith("rds.amazonaws.com"):
        name = "AWS RDS"
    elif host.endswith("postgres.database.azure.com"):
        name = "Azure Database for PostgreSQL"
    elif host.endswith("ondigitalocean.com"):
        name = "DigitalOcean"
    return name, notes


def connection_hint(err: str) -> str:
    low = err.lower()
    if "password authentication failed" in low or "authentication failed" in low:
        return (
            "Wrong user name or password. Copy the connection string again from your provider's "
            "dashboard (some providers show the password only once)."
        )
    if "could not translate host name" in low or "name or service not known" in low or "no such host" in low:
        return (
            "The host name couldn't be found. Check the URL. Supabase's direct host "
            "(db.<project>.supabase.co) is IPv6-only: on an IPv4 network use the Session pooler string."
        )
    if "no pg_hba.conf entry" in low:
        return "The server refused this connection (SSL setting or IP allow-list). Check the provider's network settings."
    if "timeout expired" in low or "timed out" in low or "connection refused" in low:
        return "Couldn't reach the server. Check host and port, your internet/VPN/firewall, and the provider's IP allow-list."
    if "database" in low and "does not exist" in low:
        return "That database name doesn't exist on the server. Check the last part of the URL."
    if "ssl" in low and ("not support" in low or "required" in low or "certificate" in low):
        return (
            "SSL problem. For a server that has no SSL add ?sslmode=disable to the URL; "
            "for certificate errors try sslmode=require."
        )
    return ""


LIST_SKIP_RE = re.compile(r"^\d+;\s+\d+\s+\d+\s+(COMMENT - EXTENSION |COMMENT - SCHEMA public |SCHEMA - public )")


def make_restore_list(pg_restore: str, path: str, env: dict):
    """Cloud-friendly mode for dump files: write a restore list that leaves out the items
    managed databases don't let you touch (extension comments, the built-in public schema).
    Returns (list_file_path or None, number_of_items_skipped)."""
    res = subprocess.run([pg_restore, "-l", path], capture_output=True, creationflags=NO_WINDOW, env=env)
    if res.returncode != 0:
        return None, 0
    lines, skipped = [], 0
    for line in res.stdout.decode("utf-8", errors="replace").splitlines():
        if LIST_SKIP_RE.match(line):
            skipped += 1
            lines.append(";" + line)  # a leading ';' comments the entry out
        else:
            lines.append(line)
    fd, list_path = tempfile.mkstemp(suffix=".list", prefix="pgrestore_")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
    return list_path, skipped


_SKIP_SQL = re.compile(
    rb"^(ALTER\s[^;]*\sOWNER\s+TO\s|GRANT\s|REVOKE\s|ALTER\s+DEFAULT\s+PRIVILEGES\s|"
    rb"SET\s+SESSION\s+AUTHORIZATION\s|COMMENT\s+ON\s+EXTENSION\s)",
    re.IGNORECASE,
)
_COPY_FROM_STDIN = re.compile(rb"^COPY\s.+\sFROM\s+stdin", re.IGNORECASE)
_CREATE_SCHEMA = re.compile(rb"^(CREATE\s+SCHEMA\s+)(?!IF\s+NOT\s+EXISTS)", re.IGNORECASE)


class FilteredSql:
    """File-like wrapper that drops owner/permission statements from a plain SQL dump.

    Skipped statements are replaced by empty lines, so line numbers in error messages
    still match the original file. COPY data blocks are never touched.
    """

    def __init__(self, raw):
        self.raw = raw
        self.in_copy = False
        self.skipping = False
        self.skipped = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.raw.close()

    def _process(self, line: bytes) -> bytes:
        if self.in_copy:
            if line.rstrip(b"\r\n") == b"\\.":
                self.in_copy = False
            return line
        if self.skipping:  # continuation of a multi-line statement being skipped
            if b";" in line:
                self.skipping = False
            return b"\n"
        if _COPY_FROM_STDIN.match(line):
            self.in_copy = True
            return line
        if _SKIP_SQL.match(line):
            self.skipped += 1
            self.skipping = b";" not in line
            return b"\n"
        return _CREATE_SCHEMA.sub(rb"\1IF NOT EXISTS ", line, count=1)

    def read(self, size: int = 1 << 20) -> bytes:
        out, n = [], 0
        while n < size:
            line = self.raw.readline()
            if not line:
                break
            res = self._process(line)
            out.append(res)
            n += len(res)
        return b"".join(out)


def resource_path(*parts: str) -> str:
    """Path to a bundled resource (works from source and from the PyInstaller build)."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)


def tool_dirs() -> list:
    """Folders (next to the app) where bundled PostgreSQL tools are looked up."""
    base = app_dir()
    return [
        os.path.join(base, "pgsql", "bin"),
        os.path.join(base, "pgsql"),
        os.path.join(base, "bin"),
        base,
    ]


def locate_tool(name: str):
    """Return (path, source) where source is 'bundled' or 'system'; (None, None) if missing."""
    for d in tool_dirs():
        exe = shutil.which(name, path=d)
        if exe:
            return exe, "bundled"
    exe = shutil.which(name)
    if exe:
        return exe, "system"
    return None, None


def find_tool(name: str) -> str:
    exe, _ = locate_tool(name)
    if not exe:
        raise RuntimeError(
            f"'{name}' was not found.\n\n"
            "Copy the PostgreSQL client tools into the 'pgsql\\bin' folder next to this app "
            "(psql.exe, pg_restore.exe and all the .dll files from the same bin folder)."
        )
    return exe


def tool_env(extra: dict = None) -> dict:
    """Environment for child processes: bundled tool folders first on PATH."""
    env = os.environ.copy()
    dirs = [d for d in tool_dirs() if os.path.isdir(d)]
    env["PATH"] = os.pathsep.join(dirs + [env.get("PATH", "")])
    env.setdefault("PGCONNECT_TIMEOUT", "30")  # cloud databases can take a while to wake up
    if extra:
        env.update(extra)
    return env


def tools_status() -> str:
    found = {n: locate_tool(n) for n in ("psql", "pg_restore")}
    missing = [n for n, (p, _) in found.items() if not p]
    if missing:
        return "⚠ PostgreSQL tools missing (" + ", ".join(missing) + ") - put them in the 'pgsql\\bin' folder next to this app."
    sources = {s for _, s in found.values()}
    if sources == {"bundled"}:
        return "✔ Using the bundled PostgreSQL tools (pgsql\\bin)."
    if sources == {"system"}:
        return "ℹ Using PostgreSQL tools found on the system PATH."
    return "ℹ Using a mix of bundled and system PostgreSQL tools."


def detect_format(path: str) -> str:
    """directory | custom | tar | gzip-sql | sql"""
    if os.path.isdir(path):
        return "directory"
    with open(path, "rb") as f:
        head = f.read(512)
    if head.startswith(b"PGDMP"):
        return "custom"
    if head[:2] == b"\x1f\x8b":
        return "gzip-sql"
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar"
    return "sql"


# --------------------------------------------------------------------- app


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PG Restore — by AziF")
        self.geometry("780x640")
        self.minsize(660, 540)
        self.configure(bg=BG)
        self._set_window_icon()

        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.proc = None
        self.cancelled = False
        self.busy = False
        self.err_lines = []
        self.conn_env = {}
        self._filter = None
        self._cloud_touched = False

        self.path_var = tk.StringVar()
        self.url_var = tk.StringVar()
        self.clean_var = tk.BooleanVar(value=False)
        self.keep_var = tk.BooleanVar(value=False)
        self.cloud_var = tk.BooleanVar(value=False)
        self.show_var = tk.BooleanVar(value=False)

        self._style()
        self._build()
        self.after(100, self._poll)

    def _set_window_icon(self):
        try:
            if IS_WIN:
                self.iconbitmap(resource_path("assets", "app.ico"))
            else:
                self._icon_img = tk.PhotoImage(file=resource_path("assets", "icon.png"))
                self.iconphoto(True, self._icon_img)
        except tk.TclError:
            pass  # icon is cosmetic; never block startup

    # ------------------------------------------------------------- styling
    def _style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("vista" if IS_WIN else "clam")
        except tk.TclError:
            pass
        st.configure("TFrame", background=BG)
        st.configure("Card.TLabelframe", background=BG)
        st.configure("Card.TLabelframe.Label", background=BG, font=("Segoe UI" if IS_WIN else "TkDefaultFont", 10, "bold"))
        st.configure("TLabel", background=BG)
        st.configure("TCheckbutton", background=BG)
        st.configure("Title.TLabel", font=("Segoe UI" if IS_WIN else "TkDefaultFont", 18, "bold"))
        st.configure("Sub.TLabel", foreground="#555")
        st.configure("Warn.TLabel", foreground="#b45309")

    # -------------------------------------------------------------- layout
    def _build(self):
        root = ttk.Frame(self, padding=18)
        root.pack(fill="both", expand=True)

        head = ttk.Frame(root)
        head.pack(fill="x", pady=(0, 12))
        try:
            self.logo_img = tk.PhotoImage(file=resource_path("assets", "logo_header.png"))
            ttk.Label(head, image=self.logo_img).pack(side="left", padx=(0, 14))
        except tk.TclError:
            pass  # logo is cosmetic
        titles = ttk.Frame(head)
        titles.pack(side="left")
        trow = ttk.Frame(titles)
        trow.pack(anchor="w")
        ttk.Label(trow, text="PG Restore", style="Title.TLabel").pack(side="left")
        ttk.Label(trow, text="by AziF", style="Sub.TLabel").pack(side="left", padx=(10, 0), pady=(10, 0))
        ttk.Label(
            titles,
            text="Restore a PostgreSQL database from a .sql or .dump backup.",
            style="Sub.TLabel",
        ).pack(anchor="w")

        # 1. Backup file
        f1 = ttk.LabelFrame(root, text=" 1 · Backup file ", style="Card.TLabelframe", padding=10)
        f1.pack(fill="x", pady=(0, 10))
        f1.columnconfigure(0, weight=1)
        ttk.Entry(f1, textvariable=self.path_var).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(f1, text="Browse file…", command=self._browse_file).grid(row=0, column=1, padx=(0, 6))
        ttk.Button(f1, text="Folder…", command=self._browse_dir).grid(row=0, column=2)
        self.fmt_label = ttk.Label(f1, text="Supported: .sql, .sql.gz, .dump / .backup (custom, tar, directory)", style="Sub.TLabel")
        self.fmt_label.grid(row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.path_var.trace_add("write", lambda *_: self._update_format())

        # 2. Database
        f2 = ttk.LabelFrame(root, text=" 2 · Target database ", style="Card.TLabelframe", padding=10)
        f2.pack(fill="x", pady=(0, 10))
        f2.columnconfigure(0, weight=1)
        self.url_entry = ttk.Entry(f2, textvariable=self.url_var, show="•")
        self.url_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Checkbutton(f2, text="Show", variable=self.show_var, command=self._toggle_show).grid(row=0, column=1)
        self.url_hint = ttk.Label(f2, text=URL_PLACEHOLDER, style="Sub.TLabel", wraplength=640, justify="left")
        self.url_hint.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.url_var.trace_add("write", lambda *_: self._update_url_hint())

        # Options
        f3 = ttk.Frame(root)
        f3.pack(fill="x", pady=(0, 10))
        ttk.Checkbutton(
            f3,
            text="Drop existing objects before restoring (dump files only)",
            variable=self.clean_var,
        ).pack(anchor="w")
        ttk.Checkbutton(
            f3,
            text="Cloud-friendly restore: skip owners, permissions & extension comments (Supabase, Neon, Aiven…)",
            variable=self.cloud_var,
            command=lambda: setattr(self, "_cloud_touched", True),
        ).pack(anchor="w", pady=(4, 0))
        ttk.Checkbutton(
            f3,
            text="Ignore errors and continue (not all-or-nothing; use when owners/roles don't match)",
            variable=self.keep_var,
        ).pack(anchor="w", pady=(4, 0))

        # Buttons
        bar = ttk.Frame(root)
        bar.pack(fill="x", pady=(0, 8))
        self.test_btn = ttk.Button(bar, text="Test connection", command=self._on_test)
        self.test_btn.pack(side="left")
        self.copy_btn = ttk.Button(bar, text="Copy log", command=self._copy_log)
        self.copy_btn.pack(side="left", padx=(8, 0))
        self.cancel_btn = ttk.Button(bar, text="Cancel", command=self._on_cancel, state="disabled")
        self.cancel_btn.pack(side="right")
        self.restore_btn = tk.Button(
            bar,
            text="Restore database",
            command=self._on_restore,
            bg=ACCENT,
            fg="white",
            activebackground=ACCENT_HOVER,
            activeforeground="white",
            relief="flat",
            bd=0,
            padx=18,
            pady=6,
            cursor="hand2",
            font=("Segoe UI" if IS_WIN else "TkDefaultFont", 10, "bold"),
        )
        self.restore_btn.pack(side="right", padx=8)

        # Progress + status
        self.progress = ttk.Progressbar(root, mode="indeterminate")
        self.progress.pack(fill="x")
        self.status = ttk.Label(root, text="Ready.", style="Sub.TLabel")
        self.status.pack(anchor="w", pady=(4, 8))

        # Log
        self.log = scrolledtext.ScrolledText(
            root, height=12, state="disabled", wrap="word",
            font=("Consolas" if IS_WIN else "TkFixedFont", 9),
            bg="#111827", fg="#e5e7eb", relief="flat", padx=8, pady=8,
        )
        self.log.pack(fill="both", expand=True)
        self.log.tag_config("err", foreground="#f87171")
        self.log.tag_config("ok", foreground="#4ade80")
        self.log.tag_config("info", foreground="#93c5fd")

        self.tools_label = ttk.Label(root, text=tools_status(), style="Sub.TLabel")
        self.tools_label.pack(anchor="w", pady=(6, 0))

    # ------------------------------------------------------------ UI utils
    def _toggle_show(self):
        self.url_entry.config(show="" if self.show_var.get() else "•")

    def _update_url_hint(self):
        url = self.url_var.get().strip()
        host = ""
        try:
            host = urlsplit(url).hostname or ""
        except ValueError:
            pass
        remote = bool(url) and is_remote(host)
        name, notes = provider_info(url) if url else (None, [])
        parts = []
        if name:
            parts.append(f"Detected: {name}")
        if remote:
            parts.append("SSL will be required")
        text = " · ".join(parts)
        if notes:
            text = (text + "\n" if text else "") + "\n".join(notes)
        self.url_hint.config(text=text or URL_PLACEHOLDER, style="Warn.TLabel" if notes else "Sub.TLabel")
        if not self._cloud_touched:  # pre-tick cloud mode for remote hosts until the user decides
            self.cloud_var.set(remote)

    def _copy_log(self):
        text = self.log.get("1.0", "end").strip()
        if not text:
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.config(text="Log copied to the clipboard.")

    def _browse_file(self):
        p = filedialog.askopenfilename(
            title="Select backup file",
            filetypes=[
                ("Backups", "*.sql *.gz *.dump *.backup *.tar *.bak"),
                ("All files", "*.*"),
            ],
        )
        if p:
            self.path_var.set(os.path.normpath(p))

    def _browse_dir(self):
        p = filedialog.askdirectory(title="Select directory-format dump folder")
        if p:
            self.path_var.set(os.path.normpath(p))

    def _update_format(self):
        p = self.path_var.get().strip().strip('"')
        if p and os.path.exists(p):
            try:
                self.fmt_label.config(text=f"Detected format: {detect_format(p)}")
                return
            except OSError:
                pass
        self.fmt_label.config(text="Supported: .sql, .sql.gz, .dump / .backup (custom, tar, directory)")

    def _write_log(self, text: str, tag: str = None):
        self.log.config(state="normal")
        self.log.insert("end", text.rstrip("\n") + "\n", tag or ())
        self.log.see("end")
        self.log.config(state="disabled")

    def _set_busy(self, busy: bool, status: str = None):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.test_btn.config(state=state)
        self.restore_btn.config(state=state, bg="#9ca3af" if busy else ACCENT)
        self.cancel_btn.config(state="normal" if busy else "disabled")
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()
        if status is not None:
            self.status.config(text=status)

    # ------------------------------------------------------- queue polling
    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    text = item[1]
                    low = text.lower()
                    tag = "err" if ("error" in low or "fatal" in low) else None
                    self._write_log(mask(text), tag)
                elif kind == "info":
                    self._write_log(item[1], "info")
                elif kind == "status":
                    self.status.config(text=item[1])
                elif kind == "done":
                    ok, title, msg = item[1], item[2], item[3]
                    self._set_busy(False, msg.splitlines()[0])
                    self._write_log(msg, "ok" if ok else "err")
                    (messagebox.showinfo if ok else messagebox.showerror)(title, msg, parent=self)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ------------------------------------------------------------- actions
    def _validate(self, need_file: bool):
        path = self.path_var.get().strip().strip('"').strip("'")
        url = self.url_var.get().strip()
        if need_file:
            if not path or not os.path.exists(path):
                messagebox.showwarning("Backup file", "Please choose an existing backup file or folder.", parent=self)
                return None
        if not url.startswith(("postgresql://", "postgres://")):
            messagebox.showwarning("Database URL", "The URL must start with postgresql:// or postgres://", parent=self)
            return None
        return os.path.expanduser(path), url

    def _on_test(self):
        v = self._validate(need_file=False)
        if not v:
            return
        _, url = v
        self.cancelled = False
        self._set_busy(True, "Testing connection…")
        threading.Thread(target=self._test_worker, args=(url,), daemon=True).start()

    def _on_restore(self):
        v = self._validate(need_file=True)
        if not v:
            return
        path, url = v
        clean = self.clean_var.get()
        keep_going = self.keep_var.get()
        cloud = self.cloud_var.get()
        msg = f"Restore\n\n{path}\n\ninto\n\n{mask(url)}\n"
        if clean:
            msg += "\nExisting objects will be DROPPED first.\n"
        if keep_going:
            msg += "\nErrors will be ignored and the restore will continue (changes are NOT rolled back).\n"
        msg += "\nContinue?"
        if not messagebox.askyesno("Confirm restore", msg, icon="warning", parent=self):
            return
        self.cancelled = False
        self._set_busy(True, "Starting…")
        threading.Thread(target=self._restore_worker, args=(path, url, clean, keep_going, cloud), daemon=True).start()

    def _on_cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass
        self.status.config(text="Cancelling…")

    # ------------------------------------------------------------- workers
    def _run(self, cmd, feed_factory=None) -> int:
        """Run a command, streaming its output to the log. Returns exit code."""
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if feed_factory else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW,
            env=tool_env(self.conn_env),
        )
        proc = self.proc

        if feed_factory:
            def feeder():
                try:
                    with feed_factory() as src:
                        while True:
                            chunk = src.read(1024 * 1024)
                            if not chunk:
                                break
                            proc.stdin.write(chunk)
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass

            threading.Thread(target=feeder, daemon=True).start()

        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                if ERR_RE.search(line):
                    self.err_lines.append(line)
                self.q.put(("log", line))
        return proc.wait()

    def _prepare(self, url: str) -> str:
        """Prepare the URL for the command-line tools (SSL, password via environment)."""
        cli_url, extra, notes = prepare_url(url)
        self.conn_env = extra
        for n in notes:
            self.q.put(("info", n))
        return cli_url

    def _connect_test(self, psql: str, url: str, attempts: int = 3) -> bool:
        self.q.put(("status", "Testing connection…"))
        err = ""
        for i in range(attempts):
            res = subprocess.run(
                [psql, url, "-X", "-w", "-Atc",
                 "SELECT current_database() || ' @ ' || split_part(version(), ',', 1)"],
                capture_output=True,
                creationflags=NO_WINDOW,
                env=tool_env(self.conn_env),
            )
            out = res.stdout.decode("utf-8", "replace").strip()
            err = res.stderr.decode("utf-8", "replace").strip()
            if res.returncode == 0:
                self.q.put(("info", f"Connected: {out}"))
                return True
            low = err.lower()
            transient = any(s in low for s in (
                "timeout", "timed out", "could not connect", "connection refused",
                "terminating connection", "starting up", "server closed the connection",
            ))
            if transient and i < attempts - 1 and not self.cancelled:
                self.q.put(("info", "Couldn't connect yet - retrying in 4 s (a cloud database may be waking up)…"))
                time.sleep(4)
                continue
            break
        self.q.put(("log", f"Connection failed: {err}"))
        hint = connection_hint(err)
        if hint:
            self.q.put(("info", hint))
        return False

    def _test_worker(self, url: str):
        try:
            psql = find_tool("psql")
            ok = self._connect_test(psql, self._prepare(url))
            if ok:
                self.q.put(("done", True, "Connection OK", "Connection OK\nThe database is reachable."))
            else:
                self.q.put(("done", False, "Connection failed", "Connection failed\nSee the log for details."))
        except Exception as e:  # noqa: BLE001
            self.q.put(("done", False, "Error", f"Error\n{e}"))

    def _restore_worker(self, path: str, url: str, clean: bool, keep_going: bool = False, cloud: bool = False):
        self.err_lines = []
        tmp_list = None
        try:
            fmt = detect_format(path)
            self.q.put(("info", f"File: {path}"))
            self.q.put(("info", f"Format: {fmt}"))
            self.q.put(("info", f"Database: {mask(url)}"))

            psql = find_tool("psql")
            cli_url = self._prepare(url)
            is_archive = fmt in ("custom", "tar", "directory")
            pg_restore = find_tool("pg_restore") if is_archive else None

            if not self._connect_test(psql, cli_url):
                self.q.put(("done", False, "Connection failed", "Connection failed\nSee the log for details."))
                return

            self.q.put(("status", "Restoring…"))
            if is_archive:
                cmd = [pg_restore, "--dbname", cli_url, "--no-owner", "--no-privileges", "--no-password", "--verbose"]
                if not keep_going:
                    cmd.append("--single-transaction")
                if clean:
                    cmd += ["--clean", "--if-exists"]
                if cloud:
                    tmp_list, skipped = make_restore_list(pg_restore, path, tool_env(self.conn_env))
                    if tmp_list:
                        cmd += ["--use-list", tmp_list]
                        if skipped:
                            self.q.put(("info", f"Cloud-friendly mode left out {skipped} item(s) managed databases don't allow (extension comments / built-in schema)."))
                cmd.append(path)
                code = self._run(cmd)
            else:
                self._filter = None

                def opener():
                    raw = gzip.open(path, "rb") if fmt == "gzip-sql" else open(path, "rb")
                    if cloud:
                        self._filter = FilteredSql(raw)
                        return self._filter
                    return raw

                if clean:
                    self.q.put(("info", "Note: 'Drop existing objects' only works with .dump files, so it is ignored for a plain .sql file."))
                cmd = [psql, cli_url, "-X", "-w"]
                if not keep_going:
                    cmd += ["-v", "ON_ERROR_STOP=1", "--single-transaction"]
                cmd += ["-f", "-"]
                code = self._run(cmd, feed_factory=opener)
                if self._filter and self._filter.skipped:
                    self.q.put(("info", f"Cloud-friendly mode skipped {self._filter.skipped} owner/permission statement(s)."))

            n_err = len(self.err_lines)
            first_err = mask(self.err_lines[0]).strip()[:500] if self.err_lines else ""
            detail = ""
            if first_err:
                detail += f"\n\nFirst error:\n{first_err}"
                hint = hint_for(first_err)
                if hint:
                    detail += f"\n\nWhat it usually means:\n{hint}"

            if self.cancelled:
                if keep_going:
                    note = "Restore cancelled\nSome changes may already have been applied."
                else:
                    note = "Restore cancelled\nNothing was committed (single transaction)."
                self.q.put(("done", False, "Cancelled", note))
            elif code == 0 and not (keep_going and n_err):
                self.q.put(("done", True, "Done", "Restore completed successfully\nYour database has been restored."))
            elif keep_going and n_err and code in (0, 1):
                self.q.put((
                    "done", True, "Finished with errors",
                    f"Restore finished with {n_err} error(s)\n"
                    "The rest of the backup was loaded. Many errors (owners, permissions, objects "
                    f"that already exist) are harmless - check the log.{detail}",
                ))
            else:
                if keep_going:
                    effect = "The restore stopped early, so some changes may have been applied."
                else:
                    effect = "Nothing was changed because the restore ran as a single transaction."
                self.q.put((
                    "done", False, "Restore failed",
                    f"Restore failed (exit code {code})\n{effect}{detail}",
                ))
        except Exception as e:  # noqa: BLE001
            self.q.put(("done", False, "Error", f"Error\n{e}"))
        finally:
            if tmp_list:
                try:
                    os.remove(tmp_list)
                except OSError:
                    pass


def main():
    if IS_WIN:
        try:  # crisp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    App().mainloop()


if __name__ == "__main__":
    main()
