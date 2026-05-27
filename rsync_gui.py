#!/usr/bin/env python3
"""
RsyncGUI — Copia de carpetas con rsync (local y NAS Synology)
Uso: python3 rsync_gui.py
"""

import json
import os
import re
import shutil
import subprocess
import threading
import tkinter as tk
import pty
import select
from pathlib import Path
from tkinter import filedialog, scrolledtext, ttk

# ── Colores & tipografía ────────────────────────────────────────────────────
BG        = "#1a1a2e"
PANEL     = "#16213e"
CARD      = "#0f3460"
ACCENT    = "#e94560"
ACCENT2   = "#533483"
TEXT      = "#eaeaea"
MUTED     = "#7a8499"
SUCCESS   = "#4ecca3"
WARNING   = "#f5a623"
FONT_MONO = ("Monospace", 11)
FONT_UI   = ("Sans", 12)
FONT_HEAD = ("Sans", 22, "bold")
FONT_LABEL= ("Sans", 11)

RE_PROGRESS = re.compile(r"to-check=(\d+)/(\d+)")


class _AppButton(tk.Frame):
    """
    Botón personalizado que bypasea el tema Aqua de macOS.
    En macOS, tk.Button ignora bg/fg en los estados focus y active;
    Frame+Label nos da control total sobre los colores en todo momento.
    API compatible con tk.Button: configure(state=, bg=).
    """
    _DISABLED_BG = "#3a3a50"
    _DISABLED_FG = "#888899"

    def __init__(self, parent, text, command,
                 bg=CARD, fg=TEXT, active_bg=None,
                 font=None, padx=18, pady=8, **_):
        super().__init__(parent, bg=bg, cursor="hand2")
        self._bg        = bg
        self._fg        = fg
        self._active_bg = active_bg or ACCENT
        self._cmd       = command
        self._enabled   = True

        self._lbl = tk.Label(
            self, text=text, bg=bg, fg=fg,
            font=font or ("Sans", 11, "bold"),
            cursor="hand2", padx=padx, pady=pady)
        self._lbl.pack(fill="both", expand=True)

        for w in (self, self._lbl):
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
        # solo en la label para evitar doble disparo por bubbling
        self._lbl.bind("<Button-1>", self._on_click)

    # ── internos ──────────────────────────────────────────────────────────────
    def _paint(self, bg, fg):
        tk.Frame.configure(self, bg=bg)
        self._lbl.configure(bg=bg, fg=fg)

    def _on_enter(self, _):
        if self._enabled:
            self._paint(self._active_bg, TEXT)

    def _on_leave(self, _):
        if self._enabled:
            self._paint(self._bg, self._fg)

    def _on_click(self, _):
        if self._enabled:
            self._cmd()

    # ── API pública (compatible con tk.Button.configure) ─────────────────────
    def configure(self, **kw):
        state  = kw.pop("state", None)
        new_bg = kw.pop("bg",    None)

        if state == "disabled":
            self._enabled = False
            self._paint(self._DISABLED_BG, self._DISABLED_FG)
            for w in (self, self._lbl):
                tk.Widget.configure(w, cursor="")
        elif state == "normal":
            self._enabled = True
            if new_bg:
                self._bg = new_bg
            self._paint(self._bg, self._fg)
            for w in (self, self._lbl):
                tk.Widget.configure(w, cursor="hand2")
        elif new_bg:
            self._bg = new_bg
            self._paint(new_bg, self._fg)

        if kw:
            tk.Frame.configure(self, **kw)

    config = configure  # alias estándar tkinter

class _RemoteBrowser(tk.Toplevel):
    """
    Explorador de directorios remoto vía SSH para NAS Synology.
    Uso: crear instancia → llamar wait_window(browser) → leer browser.result
    """
    def __init__(self, parent, host, port, user, passwd, initial="/"):
        super().__init__(parent)
        self.title(f"Explorador NAS  ·  {user}@{host}")
        self.configure(bg=BG)
        self.geometry("620x520")
        self.resizable(True, True)
        self.minsize(460, 360)

        self._host    = host
        self._port    = port or "22"
        self._user    = user
        self._passwd  = passwd
        self._path    = initial.rstrip("/") or "/"
        self._loading = False
        self._dirs    = []   # nombres sin prefijo para el índice actual
        self.result   = None

        self._build_ui()
        self._navigate(self._path)

        self.transient(parent)
        self.grab_set()
        self.focus_set()
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        # ── barra de navegación ───────────────────────────────────────────────
        nav = tk.Frame(self, bg=PANEL, padx=10, pady=10)
        nav.pack(fill="x", padx=14, pady=(14, 0))
        nav.configure(highlightbackground=CARD, highlightthickness=1)

        _AppButton(nav, "⬆ Subir", self._go_up,
                   bg=CARD, fg=TEXT, active_bg=ACCENT2,
                   font=FONT_LABEL, padx=10, pady=5).pack(side="left", padx=(0, 8))

        self._path_var = tk.StringVar(value=self._path)
        path_entry = tk.Entry(
            nav, textvariable=self._path_var,
            bg=CARD, fg=TEXT, insertbackground=TEXT,
            relief="flat", bd=0, font=FONT_MONO, highlightthickness=0)
        path_entry.pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 8))
        path_entry.bind("<Return>",
                        lambda _: self._navigate(self._path_var.get().strip()))

        _AppButton(nav, "Ir →",
                   lambda: self._navigate(self._path_var.get().strip()),
                   bg=ACCENT2, fg=TEXT, active_bg=ACCENT,
                   font=FONT_LABEL, padx=10, pady=5).pack(side="left")

        # ── lista de directorios ──────────────────────────────────────────────
        list_outer = tk.Frame(self, bg=BG)
        list_outer.pack(fill="both", expand=True, padx=14, pady=10)

        list_frame = tk.Frame(list_outer, bg="#0d0d1a")
        list_frame.pack(fill="both", expand=True)
        list_frame.configure(highlightbackground=CARD, highlightthickness=1)

        sb = tk.Scrollbar(list_frame, bg=PANEL, troughcolor=BG,
                          relief="flat", bd=0, width=12)
        sb.pack(side="right", fill="y")

        self._lb = tk.Listbox(
            list_frame, bg="#0d0d1a", fg=TEXT,
            selectbackground=ACCENT, selectforeground=TEXT,
            activestyle="none", relief="flat", bd=0,
            font=FONT_MONO, highlightthickness=0,
            yscrollcommand=sb.set)
        self._lb.pack(side="left", fill="both", expand=True)
        sb.config(command=self._lb.yview)

        self._lb.bind("<Double-Button-1>", self._on_double)
        self._lb.bind("<Return>",          self._on_double)

        # ── barra de estado ───────────────────────────────────────────────────
        status_bar = tk.Frame(self, bg=BG)
        status_bar.pack(fill="x", padx=14, pady=(0, 6))
        self._status_var = tk.StringVar(value="Conectando…")
        self._status_lbl = tk.Label(
            status_bar, textvariable=self._status_var,
            bg=BG, fg=MUTED, font=("Sans", 9), anchor="w")
        self._status_lbl.pack(side="left", fill="x", expand=True)

        # ── botones ───────────────────────────────────────────────────────────
        btn_row = tk.Frame(self, bg=BG, pady=0)
        btn_row.pack(fill="x", padx=14, pady=(0, 14))

        _AppButton(btn_row, "✕  Cancelar", self.destroy,
                   bg=CARD, fg=TEXT, active_bg=ACCENT,
                   font=("Sans", 11, "bold"), padx=16, pady=8).pack(
            side="right", padx=(8, 0))
        _AppButton(btn_row, "✓  Seleccionar esta carpeta", self._confirm,
                   bg=ACCENT2, fg=TEXT, active_bg=SUCCESS,
                   font=("Sans", 11, "bold"), padx=16, pady=8).pack(side="right")

    # ── SSH ───────────────────────────────────────────────────────────────────

    def _make_ssh_cmd(self, remote_cmd):
        opts = ["-p", self._port,
                "-o", "StrictHostKeyChecking=no",
                "-o", "ConnectTimeout=8"]
        target = f"{self._user}@{self._host}"
        if self._passwd:
            if not shutil.which("sshpass"):
                return None
            return (["sshpass", f"-p{self._passwd}", "ssh"]
                    + opts + ["-o", "BatchMode=no", target, remote_cmd])
        return (["ssh"] + opts
                + ["-o", "BatchMode=yes",
                   "-o", "PasswordAuthentication=no",
                   target, remote_cmd])

    def _ssh_listdirs(self, path):
        """Devuelve (lista_de_dirs, mensaje_error)."""
        safe = path.replace("'", "'\\''")
        cmd  = self._make_ssh_cmd(f"ls -1ap '{safe}' 2>&1")
        if cmd is None:
            return None, ("sshpass no instalado — necesario para "
                          "autenticación por contraseña")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if r.returncode != 0:
                return None, (r.stdout.strip() or r.stderr.strip()
                              or f"SSH error (código {r.returncode})")
            dirs = sorted(
                line.rstrip("/")
                for line in r.stdout.splitlines()
                if line.endswith("/") and line not in ("./", "../")
            )
            return dirs, None
        except subprocess.TimeoutExpired:
            return None, "Tiempo de conexión agotado (15 s)"
        except FileNotFoundError as e:
            return None, f"Comando no encontrado: {e.filename}"
        except Exception as e:
            return None, str(e)

    # ── Navegación ────────────────────────────────────────────────────────────

    def _navigate(self, path):
        if self._loading:
            return
        self._loading = True
        path = path.rstrip("/") or "/"
        self._path = path
        self._path_var.set(path)
        self._lb.delete(0, "end")
        self._dirs = []
        self._set_status("Cargando…", MUTED)
        threading.Thread(target=self._fetch, args=(path,), daemon=True).start()

    def _fetch(self, path):
        dirs, err = self._ssh_listdirs(path)
        self.after(0, lambda: self._on_loaded(path, dirs, err))

    def _on_loaded(self, path, dirs, err):
        if not self.winfo_exists():
            return
        self._loading = False
        if err:
            self._set_status(f"✗  {err}", ACCENT)
            return
        self._dirs = dirs or []
        self._lb.delete(0, "end")
        if path != "/":
            self._lb.insert("end", "  ↑  ..")
        for d in self._dirs:
            self._lb.insert("end", f"  ▸  {d}")
        n = len(self._dirs)
        self._set_status(f"{n} carpeta{'s' if n != 1 else ''}  ·  {path}", MUTED)

    def _on_double(self, _):
        sel = self._lb.curselection()
        if not sel:
            return
        idx = sel[0]
        has_parent = (self._path != "/")
        if has_parent and idx == 0:
            self._go_up()
        else:
            name = self._dirs[idx - (1 if has_parent else 0)]
            self._navigate(f"{self._path.rstrip('/')}/{name}")

    def _go_up(self):
        if self._path != "/":
            self._navigate(str(Path(self._path).parent))

    def _confirm(self):
        self.result = self._path
        self.destroy()

    def _set_status(self, msg, color=MUTED):
        self._status_var.set(msg)
        self._status_lbl.configure(fg=color)


MODE_LOCAL        = "local"
MODE_LOCAL_TO_NAS = "local_to_nas"
MODE_NAS_TO_LOCAL = "nas_to_local"

CONFIG_FILE       = Path.home() / ".config" / "rsyncgui" / "config.json"
_KEYCHAIN_SERVICE = "rsyncgui"


# ── Keychain helpers (macOS security CLI, sin dependencias extra) ────────────

def _kc_save(account: str, password: str) -> None:
    """Guarda o actualiza una contraseña en el Llavero de macOS."""
    if not password:
        _kc_delete(account)
        return
    subprocess.run(
        ["security", "add-generic-password",
         "-s", _KEYCHAIN_SERVICE, "-a", account, "-w", password,
         "-U"],          # -U: sobreescribir si ya existe
        check=False, capture_output=True,
    )


def _kc_load(account: str) -> str:
    """Lee una contraseña del Llavero. Devuelve '' si no existe."""
    r = subprocess.run(
        ["security", "find-generic-password",
         "-s", _KEYCHAIN_SERVICE, "-a", account, "-w"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else ""


def _kc_delete(account: str) -> None:
    """Elimina una entrada del Llavero (silencioso si no existe)."""
    subprocess.run(
        ["security", "delete-generic-password",
         "-s", _KEYCHAIN_SERVICE, "-a", account],
        check=False, capture_output=True,
    )


class RsyncGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("RsyncGUI")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.geometry("860x820")
        self.minsize(700, 700)

        self._proc    = None
        self._running = False

        self._src          = tk.StringVar()
        self._dst          = tk.StringVar()
        self._progress_val = tk.DoubleVar(value=0.0)
        self._progress_txt = tk.StringVar(value="")
        self._mode         = tk.StringVar(value=MODE_LOCAL)

        # NAS
        self._nas_host = tk.StringVar()
        self._nas_port = tk.StringVar(value="22")
        self._nas_user = tk.StringVar()
        self._nas_pass = tk.StringVar()

        # Opciones rsync
        self._opt_archive  = tk.BooleanVar(value=True)
        self._opt_verbose  = tk.BooleanVar(value=True)
        self._opt_delete   = tk.BooleanVar(value=False)
        self._opt_dryrun   = tk.BooleanVar(value=False)
        self._opt_compress = tk.BooleanVar(value=True)

        self._build_ui()
        self._load_config()   # restaurar última sesión

    # ── Persistencia ────────────────────────────────────────────────────────

    def _load_config(self):
        if not CONFIG_FILE.exists():
            return
        try:
            data = json.loads(CONFIG_FILE.read_text())
        except Exception:
            return

        self._mode.set(data.get("mode", MODE_LOCAL))
        self._nas_host.set(data.get("nas_host", ""))
        self._nas_port.set(data.get("nas_port", "22"))
        self._nas_user.set(data.get("nas_user", ""))
        self._src.set(data.get("src", ""))
        self._dst.set(data.get("dst", ""))
        self._opt_archive.set(data.get("opt_archive", True))
        self._opt_verbose.set(data.get("opt_verbose", True))
        self._opt_delete.set(data.get("opt_delete", False))
        self._opt_dryrun.set(data.get("opt_dryrun", False))
        self._opt_compress.set(data.get("opt_compress", True))

        # ── contraseña: leer del Keychain ─────────────────────────────────────
        host = self._nas_host.get().strip()
        user = self._nas_user.get().strip()
        if host and user:
            account = f"{user}@{host}"
            old_plain = data.get("nas_pass", "")
            if old_plain:
                # Migración: mover contraseña del JSON al Keychain y borrarla del archivo
                _kc_save(account, old_plain)
                data.pop("nas_pass")
                try:
                    CONFIG_FILE.write_text(json.dumps(data, indent=2))
                except Exception:
                    pass
            self._nas_pass.set(_kc_load(account))

        self._on_mode_change()

    def _save_config(self):
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        # nas_pass NO se incluye en el JSON — va al Keychain
        data = {
            "mode":         self._mode.get(),
            "nas_host":     self._nas_host.get(),
            "nas_port":     self._nas_port.get(),
            "nas_user":     self._nas_user.get(),
            "src":          self._src.get(),
            "dst":          self._dst.get(),
            "opt_archive":  self._opt_archive.get(),
            "opt_verbose":  self._opt_verbose.get(),
            "opt_delete":   self._opt_delete.get(),
            "opt_dryrun":   self._opt_dryrun.get(),
            "opt_compress": self._opt_compress.get(),
        }
        CONFIG_FILE.write_text(json.dumps(data, indent=2))

        # Guardar contraseña en el Llavero de macOS
        host = self._nas_host.get().strip()
        user = self._nas_user.get().strip()
        if host and user:
            _kc_save(f"{user}@{host}", self._nas_pass.get())

    def _clear_paths_from_config(self):
        """Borra src/dst del archivo guardado tras una copia exitosa."""
        if not CONFIG_FILE.exists():
            return
        try:
            data = json.loads(CONFIG_FILE.read_text())
            data["src"] = ""
            data["dst"] = ""
            CONFIG_FILE.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    # ── UI ──────────────────────────────────────────────────────────────────

    def _build_ui(self):
        hdr = tk.Frame(self, bg=BG, pady=18)
        hdr.pack(fill="x", padx=30)
        tk.Label(hdr, text="⟳  RsyncGUI", font=FONT_HEAD,
                 bg=BG, fg=TEXT).pack(side="left")
        tk.Label(hdr, text="copia local y NAS Synology",
                 font=FONT_LABEL, bg=BG, fg=MUTED).pack(side="left", padx=14, pady=6)

        tk.Frame(self, bg=ACCENT, height=2).pack(fill="x", padx=30)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=30, pady=20)

        self._build_mode_selector(body)

        self._nas_frame = tk.Frame(body, bg=PANEL)
        self._nas_frame.configure(highlightbackground=CARD, highlightthickness=1)
        self._build_nas_panel(self._nas_frame)

        folder_frame = tk.Frame(body, bg=PANEL, bd=0, relief="flat")
        folder_frame.pack(fill="x", pady=(0, 16))
        folder_frame.configure(highlightbackground=CARD, highlightthickness=1)

        self._folder_frame    = folder_frame
        self._src_label_var   = tk.StringVar(value="  ORIGEN")
        self._dst_label_var   = tk.StringVar(value="  DESTINO")

        self._folder_row(folder_frame, self._src_label_var, self._src, self._pick_src, 0)
        tk.Frame(folder_frame, bg=CARD, height=1).grid(
            row=1, column=0, columnspan=3, sticky="ew", padx=16)
        self._folder_row(folder_frame, self._dst_label_var, self._dst, self._pick_dst, 2)
        folder_frame.columnconfigure(1, weight=1)

        # ── Opciones ──
        opt_outer = tk.Frame(body, bg=BG)
        opt_outer.pack(fill="x", pady=(0, 16))
        tk.Label(opt_outer, text="OPCIONES", font=("Sans", 9, "bold"),
                 bg=BG, fg=MUTED).pack(anchor="w", pady=(0, 6))

        opt_frame = tk.Frame(opt_outer, bg=PANEL)
        opt_frame.pack(fill="x")
        opt_frame.configure(highlightbackground=CARD, highlightthickness=1)

        opts = [
            ("--archive  (-a)", self._opt_archive,
             "Preserva permisos, fechas, enlaces simbólicos y propietario"),
            ("--verbose  (-v)", self._opt_verbose,
             "Lista cada archivo copiado"),
            ("--compress (-z)", self._opt_compress,
             "Comprime datos en tránsito (recomendado para NAS)"),
            ("--delete", self._opt_delete,
             "⚠ Elimina en destino los archivos que no existen en origen"),
            ("--dry-run  (-n)", self._opt_dryrun,
             "Simulación: no copia nada, solo muestra qué haría"),
        ]

        for i, (label, var, tooltip) in enumerate(opts):
            row = i // 2
            col = i %  2
            cell = tk.Frame(opt_frame, bg=PANEL, padx=16, pady=10)
            cell.grid(row=row, column=col, sticky="w")
            cb = tk.Checkbutton(cell, text=label, variable=var,
                                bg=PANEL, fg=TEXT, activebackground=PANEL,
                                activeforeground=ACCENT, selectcolor=CARD,
                                font=FONT_MONO, cursor="hand2",
                                relief="flat", bd=0)
            cb.pack(anchor="w")
            tk.Label(cell, text=tooltip, bg=PANEL, fg=MUTED,
                     font=("Sans", 9)).pack(anchor="w")

        opt_frame.columnconfigure(0, weight=1)
        opt_frame.columnconfigure(1, weight=1)

        # ── Botones ──
        btn_frame = tk.Frame(body, bg=BG)
        btn_frame.pack(fill="x", pady=(0, 10))

        self._btn_start = self._make_btn(
            btn_frame, "▶  INICIAR COPIA", self._start, ACCENT, TEXT)
        self._btn_start.pack(side="left", padx=(0, 10))

        self._btn_stop = self._make_btn(
            btn_frame, "■  DETENER", self._stop, "#6b2737", TEXT)
        self._btn_stop.pack(side="left", padx=(0, 10))

        self._btn_clear = self._make_btn(
            btn_frame, "✕  LIMPIAR LOG", self._clear_log, CARD, TEXT)
        self._btn_clear.pack(side="left")

        self._status_var = tk.StringVar(value="Listo.")
        self._status_lbl = tk.Label(btn_frame, textvariable=self._status_var,
                                    bg=BG, fg=MUTED, font=FONT_LABEL)
        self._status_lbl.pack(side="right")

        # ── Barra de progreso ──
        prog_outer = tk.Frame(body, bg=BG)
        prog_outer.pack(fill="x", pady=(0, 12))

        prog_hdr = tk.Frame(prog_outer, bg=BG)
        prog_hdr.pack(fill="x", pady=(0, 5))
        tk.Label(prog_hdr, text="PROGRESO", font=("Sans", 9, "bold"),
                 bg=BG, fg=MUTED).pack(side="left")
        tk.Label(prog_hdr, textvariable=self._progress_txt,
                 bg=BG, fg=SUCCESS, font=("Monospace", 10, "bold")).pack(side="right")

        style = ttk.Style(self)
        style.theme_use("default")
        style.configure(
            "Rsync.Horizontal.TProgressbar",
            troughcolor=CARD, background=SUCCESS,
            bordercolor=CARD, lightcolor=SUCCESS,
            darkcolor=SUCCESS, thickness=14,
        )
        self._progressbar = ttk.Progressbar(
            prog_outer, style="Rsync.Horizontal.TProgressbar",
            variable=self._progress_val, maximum=100, mode="determinate",
        )
        self._progressbar.pack(fill="x")

        # ── Log ──
        tk.Label(body, text="LOG", font=("Sans", 9, "bold"),
                 bg=BG, fg=MUTED).pack(anchor="w", pady=(0, 4))

        log_frame = tk.Frame(body, bg=CARD)
        log_frame.pack(fill="both", expand=True)
        log_frame.configure(highlightbackground=CARD, highlightthickness=1)

        self._log = scrolledtext.ScrolledText(
            log_frame, bg="#0d0d1a", fg=SUCCESS, font=FONT_MONO,
            relief="flat", bd=0, insertbackground=TEXT,
            wrap="word", state="disabled", padx=12, pady=10, cursor="arrow",
        )
        self._log.pack(fill="both", expand=True)
        self._log.tag_config("warn",  foreground=WARNING)
        self._log.tag_config("error", foreground=ACCENT)
        self._log.tag_config("info",  foreground=MUTED)
        self._log.tag_config("ok",    foreground=SUCCESS)

        self._on_mode_change()

    def _build_mode_selector(self, parent):
        mode_outer = tk.Frame(parent, bg=BG)
        mode_outer.pack(fill="x", pady=(0, 14))
        tk.Label(mode_outer, text="MODO DE COPIA", font=("Sans", 9, "bold"),
                 bg=BG, fg=MUTED).pack(anchor="w", pady=(0, 6))

        mode_frame = tk.Frame(mode_outer, bg=PANEL)
        mode_frame.pack(fill="x")
        mode_frame.configure(highlightbackground=CARD, highlightthickness=1)

        modes = [
            (MODE_LOCAL,         "💻  Local → Local",  "Copia entre carpetas del mismo equipo"),
            (MODE_LOCAL_TO_NAS,  "📤  Local → NAS",    "Copia desde este equipo al NAS Synology"),
            (MODE_NAS_TO_LOCAL,  "📥  NAS → Local",    "Copia desde el NAS Synology a este equipo"),
        ]

        for i, (val, label, tip) in enumerate(modes):
            cell = tk.Frame(mode_frame, bg=PANEL, padx=16, pady=10)
            cell.grid(row=0, column=i, sticky="nsew")
            tk.Radiobutton(
                cell, text=label, variable=self._mode, value=val,
                command=self._on_mode_change,
                bg=PANEL, fg=TEXT, activebackground=PANEL,
                activeforeground=ACCENT, selectcolor=CARD,
                font=("Sans", 11, "bold"), cursor="hand2",
                relief="flat", bd=0,
            ).pack(anchor="w")
            tk.Label(cell, text=tip, bg=PANEL, fg=MUTED,
                     font=("Sans", 9)).pack(anchor="w")

        for col in range(3):
            mode_frame.columnconfigure(col, weight=1)

    def _build_nas_panel(self, parent):
        tk.Label(parent, text="  CONFIGURACIÓN NAS", font=("Sans", 9, "bold"),
                 bg=PANEL, fg=MUTED).grid(
            row=0, column=0, columnspan=4, sticky="w", padx=16, pady=(12, 4))

        fields = [
            (1, 0, "Host / IP",   self._nas_host, False, None),
            (1, 2, "Puerto SSH",  self._nas_port, False, 6),
            (2, 0, "Usuario",     self._nas_user, False, None),
            (2, 2, "Contraseña",  self._nas_pass, True,  None),
        ]
        for row, lcol, ltext, var, secret, width in fields:
            tk.Label(parent, text=ltext, font=FONT_LABEL,
                     bg=PANEL, fg=MUTED).grid(
                row=row, column=lcol, padx=(16 if lcol == 0 else 0, 6),
                pady=6, sticky="w")
            kw = dict(textvariable=var, bg=CARD, fg=TEXT,
                      insertbackground=TEXT, relief="flat", bd=0,
                      font=FONT_UI, highlightthickness=0)
            if secret:
                kw["show"] = "●"
            if width:
                kw["width"] = width
            tk.Entry(parent, **kw).grid(
                row=row, column=lcol + 1, sticky="ew",
                padx=(0, 16), ipady=5)

        tk.Label(
            parent,
            text="  ℹ  Sin contraseña usa clave SSH (recomendado). "
                 "Con contraseña necesitas 'sshpass' instalado (sudo apt install sshpass).",
            font=("Sans", 9), bg=PANEL, fg=MUTED,
            wraplength=700, justify="left",
        ).grid(row=3, column=0, columnspan=4, sticky="w", padx=16, pady=(2, 10))

        parent.columnconfigure(1, weight=1)

    def _on_mode_change(self):
        mode = self._mode.get()
        if mode == MODE_LOCAL:
            self._nas_frame.pack_forget()
            self._src_label_var.set("  ORIGEN")
            self._dst_label_var.set("  DESTINO")
        else:
            self._nas_frame.pack(fill="x", pady=(0, 16), before=self._folder_frame)
            if mode == MODE_LOCAL_TO_NAS:
                self._src_label_var.set("  ORIGEN  (local)")
                self._dst_label_var.set("  DESTINO (NAS)")
            else:
                self._src_label_var.set("  ORIGEN  (NAS)")
                self._dst_label_var.set("  DESTINO (local)")

    def _folder_row(self, parent, label_var, var, cmd, row):
        tk.Label(parent, textvariable=label_var, font=("Sans", 9, "bold"),
                 bg=PANEL, fg=MUTED, width=17, anchor="w").grid(
            row=row, column=0, padx=(16, 8), pady=14, sticky="w")
        tk.Entry(parent, textvariable=var, bg=CARD, fg=TEXT,
                 insertbackground=TEXT, relief="flat", bd=0,
                 font=FONT_UI, highlightthickness=0).grid(
            row=row, column=1, sticky="ew", padx=(0, 8), ipady=6)
        _AppButton(parent, text="Elegir…", command=cmd,
                   bg=ACCENT2, fg=TEXT, active_bg=ACCENT,
                   font=FONT_LABEL, padx=14, pady=6).grid(
            row=row, column=2, padx=(0, 16))

    def _make_btn(self, parent, text, cmd, bg, fg):
        return _AppButton(parent, text=text, command=cmd,
                          bg=bg, fg=fg, active_bg=ACCENT,
                          font=("Sans", 11, "bold"),
                          padx=18, pady=8)

    # ── Selección de carpetas ────────────────────────────────────────────────

    def _pick_src(self):
        mode = self._mode.get()
        if mode == MODE_NAS_TO_LOCAL:
            self._open_remote_browser(self._src)
            return
        d = filedialog.askdirectory(title="Seleccionar carpeta ORIGEN")
        if d:
            self._src.set(d)

    def _pick_dst(self):
        mode = self._mode.get()
        if mode == MODE_LOCAL_TO_NAS:
            self._open_remote_browser(self._dst)
            return
        d = filedialog.askdirectory(title="Seleccionar carpeta DESTINO")
        if d:
            self._dst.set(d)

    def _open_remote_browser(self, target_var):
        host = self._nas_host.get().strip()
        user = self._nas_user.get().strip()
        if not host or not user:
            self._log_write(
                "ℹ  Rellena Host/IP y Usuario del NAS antes de explorar.\n",
                "info")
            return
        port    = self._nas_port.get().strip() or "22"
        passwd  = self._nas_pass.get()
        initial = target_var.get().strip() or "/"
        browser = _RemoteBrowser(self, host, port, user, passwd, initial)
        self.wait_window(browser)
        if browser.result:
            target_var.set(browser.result)

    # ── Construcción del comando ─────────────────────────────────────────────

    def _build_cmd(self):
        if not shutil.which("rsync"):
            return None, "rsync no encontrado. Instálalo con: sudo apt install rsync"

        mode = self._mode.get()
        src  = self._src.get().strip()
        dst  = self._dst.get().strip()

        if not src:
            return None, "Selecciona o escribe la ruta de ORIGEN."
        if not dst:
            return None, "Selecciona o escribe la ruta de DESTINO."

        flags = []
        if self._opt_archive.get():  flags.append("-a")
        if self._opt_verbose.get():  flags.append("-v")
        if self._opt_compress.get(): flags.append("-z")
        flags.append("--progress")
        if self._opt_delete.get():   flags.append("--delete")
        if self._opt_dryrun.get():   flags.append("--dry-run")

        if mode == MODE_LOCAL:
            if not os.path.isdir(src):
                return None, f"El origen no existe:\n{src}"
            if not src.endswith("/"):
                src += "/"
            return ["rsync"] + flags + [src, dst], None

        # ── Modo NAS ──
        host   = self._nas_host.get().strip()
        port   = self._nas_port.get().strip() or "22"
        user   = self._nas_user.get().strip()
        passwd = self._nas_pass.get()

        if not host:
            return None, "Introduce el Host / IP del NAS."
        if not user:
            return None, "Introduce el Usuario del NAS."

        if passwd:
            if not shutil.which("sshpass"):
                return None, (
                    "Para autenticación por contraseña necesitas 'sshpass'.\n"
                    "Instálalo con: sudo apt install sshpass\n"
                    "O configura una clave SSH y deja la contraseña en blanco."
                )
            # BatchMode=no para que sshpass pueda inyectar la contraseña
            ssh_opts = (f"ssh -p {port} "
                        "-o StrictHostKeyChecking=no "
                        "-o BatchMode=no")
            ssh_prefix = ["sshpass", f"-p{passwd}"]
        else:
            # Sin contraseña: BatchMode=yes → falla de inmediato si no hay clave SSH
            # en vez de pedir contraseña en la terminal y bloquear la GUI
            ssh_opts = (f"ssh -p {port} "
                        "-o StrictHostKeyChecking=no "
                        "-o BatchMode=yes "
                        "-o PasswordAuthentication=no")
            ssh_prefix = []

        remote = f"{user}@{host}"

        if mode == MODE_LOCAL_TO_NAS:
            if not os.path.isdir(src):
                return None, f"El origen local no existe:\n{src}"
            if not src.endswith("/"):
                src += "/"
            return ssh_prefix + ["rsync"] + flags + ["-e", ssh_opts, src, f"{remote}:{dst}"], None
        else:  # NAS_TO_LOCAL
            if not src.endswith("/"):
                src += "/"
            return ssh_prefix + ["rsync"] + flags + ["-e", ssh_opts, f"{remote}:{src}", dst], None

    # ── Acciones ────────────────────────────────────────────────────────────

    def _start(self):
        if self._running:
            return

        cmd, err = self._build_cmd()
        if err:
            self._log_write(f"✗  {err}\n", "error")
            self._set_status(f"Error: {err.splitlines()[0]}", ACCENT)
            return

        self._save_config()   # guardar antes de iniciar
        self._running = True
        self._reset_progress()
        self._btn_start.configure(state="disabled", bg=MUTED)
        self._set_status("Copiando…", WARNING)

        # Log con contraseña oculta
        display_cmd, skip = [], False
        for part in cmd:
            if skip:
                display_cmd.append("-p***")
                skip = False
            elif part == "sshpass":
                display_cmd.append(part)
                skip = True
            else:
                display_cmd.append(part)
        self._log_write("$ " + " ".join(display_cmd) + "\n\n", "info")
        self._log_write(
            "⏳ Escaneando archivos y comparando con destino…\n"
            "   En copias grandes esto puede tardar varios minutos sin mostrar nada.\n\n",
            "info")

        threading.Thread(target=self._run_rsync, args=(cmd,), daemon=True).start()

    def _run_rsync(self, cmd):
        master_fd = -1
        try:
            # PTY fuerza a rsync a volcar la salida en tiempo real
            # (sin TTY rsync la acumula en buffer hasta tener KB de datos)
            master_fd, slave_fd = pty.openpty()
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                start_new_session=True,  # evita SIGHUP al terminar el PTY
            )
            os.close(slave_fd)

            buf = ""
            while True:
                try:
                    ready, _, _ = select.select([master_fd], [], [], 0.5)
                except (ValueError, OSError):
                    break
                if ready:
                    try:
                        data = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    buf += data.decode("utf-8", errors="replace")
                    # rsync usa \r para actualizar la línea de progreso
                    parts = re.split(r"[\r\n]", buf)
                    buf = parts[-1]
                    for line in parts[:-1]:
                        m = RE_PROGRESS.search(line)
                        if m:
                            self._update_progress(int(m.group(1)), int(m.group(2)))
                        if line.strip():
                            self._log_write(line + "\n")
                else:
                    if self._proc.poll() is not None:
                        break

            # Volcar lo que quedara en el buffer
            if buf.strip():
                self._log_write(buf + "\n")

            self._proc.wait()
            rc = self._proc.returncode

            if rc == 0:
                self.after(0, lambda: self._progress_val.set(100))
                self.after(0, lambda: self._progress_txt.set("Completado ✓"))
                self._log_write("\n✓  Copia completada correctamente.\n", "ok")
                self.after(0, lambda: self._set_status("✓ Completado", SUCCESS))
                self._clear_paths_from_config()   # éxito → limpiar rutas guardadas
                self.after(0, lambda: (self._src.set(""), self._dst.set("")))
            elif rc == -15:
                self._log_write("\n■  Proceso detenido por el usuario.\n", "warn")
                self.after(0, lambda: self._set_status("Detenido.", MUTED))
            else:
                self._log_write(f"\n✗  rsync terminó con código {rc}.\n", "error")
                if rc == 255:
                    self._log_write(
                        "   Código 255 suele indicar error SSH.\n"
                        "   Comprueba host, usuario y que la clave SSH esté configurada.\n"
                        "   Puedes copiar tu clave con: ssh-copy-id -p PUERTO usuario@host\n",
                        "warn")
                self.after(0, lambda: self._set_status(f"Error (código {rc})", ACCENT))
        except Exception as e:
            self._log_write(f"\n✗  Excepción: {e}\n", "error")
            self.after(0, lambda: self._set_status("Error inesperado.", ACCENT))
        finally:
            if master_fd >= 0:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
            self._running = False
            self._proc    = None
            self.after(0, lambda: self._btn_start.configure(state="normal", bg=ACCENT))

    def _stop(self):
        if self._proc and self._running:
            self._proc.terminate()

    def _clear_log(self):
        self._log.configure(state="normal")
        self._log.delete("1.0", "end")
        self._log.configure(state="disabled")
        self._reset_progress()
        self._set_status("Log limpiado.", MUTED)

    # ── Progreso ─────────────────────────────────────────────────────────────

    def _update_progress(self, remaining, total):
        if total == 0:
            return
        done = total - remaining
        pct  = done / total * 100
        self.after(0, lambda p=pct: self._progress_val.set(p))
        self.after(0, lambda d=done, t=total, p=pct:
                   self._progress_txt.set(f"{d:,} / {t:,} archivos  ({p:.1f}%)"))
        self.after(0, lambda p=pct:
                   self._set_status(f"Copiando… {p:.1f}%", WARNING))

    def _reset_progress(self):
        self._progress_val.set(0)
        self._progress_txt.set("")

    def _log_write(self, text, tag=None):
        def _write():
            self._log.configure(state="normal")
            if tag:
                self._log.insert("end", text, tag)
            else:
                self._log.insert("end", text)
            self._log.see("end")
            self._log.configure(state="disabled")
        self.after(0, _write)

    def _set_status(self, msg, color=MUTED):
        self._status_var.set(msg)
        self._status_lbl.configure(fg=color)


if __name__ == "__main__":
    app = RsyncGUI()
    app.mainloop()
