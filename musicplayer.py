#!/usr/bin/env python3
"""Player de música GTK4 / libadwaita / GStreamer.

Recursos: play/pause, stop, anterior/próxima, shuffle (Fisher-Yates com
equilíbrio de frequência), repetir off/todas/uma, filtro, forma de onda,
capas (.covers do kew + embutidas), estado persistente e MPRIS.
"""
import json
import os
import random
import signal
import sys
import threading
import unicodedata

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gst", "1.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gst, Gtk

APP_ID = "br.local.MusicPlayer"
DEFAULT_FOLDER = "/mnt/disco_d/meus_documentos/Musicas/"
EXTENSIONS = {".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav", ".wma", ".aac", ".mkv"}
DATA_DIR = os.path.join(GLib.get_user_data_dir(), "musicplayer")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
FREQ_FILE = os.path.join(DATA_DIR, "frequency.json")
REPEAT_MODES = ["off", "all", "one"]

Gst.init(None)


# ---------------------------------------------------------------- utilitários
def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def norm(text):
    text = unicodedata.normalize("NFD", text.casefold())
    return "".join(c for c in text if not unicodedata.combining(c))


def fisher_yates(items):
    """Embaralhamento de Fisher-Yates (Knuth) com RNG do sistema."""
    a = list(items)
    rnd = random.SystemRandom()
    for i in range(len(a) - 1, 0, -1):
        j = rnd.randint(0, i)
        a[i], a[j] = a[j], a[i]
    return a


def fmt_time(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


class Track:
    def __init__(self, path):
        self.path = path
        base = os.path.splitext(os.path.basename(path))[0]
        if " - " in base:
            self.artist, self.title = (s.strip() for s in base.split(" - ", 1))
        else:
            self.artist, self.title = "", base.strip()
        self.search = norm(f"{self.artist} {self.title}")
        self.sort_key = (norm(self.artist), norm(self.title))

    @property
    def label(self):
        return f"{self.artist} - {self.title}" if self.artist else self.title


def scan_folder(folder):
    found = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in files:
            if os.path.splitext(name)[1].lower() in EXTENSIONS and not name.startswith("."):
                found.append(os.path.join(root, name))
    return found


# ------------------------------------------------- frequência (rotativa)
class FrequencyStore:
    """Conta execuções por música. Ao jogar, o shuffle só sorteia entre as
    menos tocadas. O contador é "rotativo": quando todas as músicas da
    biblioteca já foram tocadas N vezes, subtrai-se N de todas."""

    def __init__(self):
        self.counts = read_json(FREQ_FILE, {})

    def get(self, path):
        return self.counts.get(path, 0)

    def increment(self, path, library_paths):
        self.counts[path] = self.get(path) + 1
        rotated = False
        low = min((self.get(p) for p in library_paths), default=0)
        if low > 0:
            for p in library_paths:
                self.counts[p] = self.get(p) - low
            rotated = True
        self.save()
        return rotated

    def prune(self, library_paths):
        keep = set(library_paths)
        self.counts = {p: c for p, c in self.counts.items() if p in keep}
        self.save()

    def reset(self):
        self.counts = {}
        self.save()

    def save(self):
        try:
            write_json(FREQ_FILE, self.counts)
        except OSError as e:
            print("erro salvando frequência:", e, file=sys.stderr)


# ------------------------------------------------------ análise da onda
class WaveformAnalyzer:
    SAMPLE_RATE = 8000
    BUCKET = 400  # amostras por barra => 0,05 s
    BUCKET_SECONDS = BUCKET / SAMPLE_RATE

    def __init__(self, uri):
        self.peaks = []
        self.max_peak = 0.0
        self._cur_max = 0.0
        self._cur_n = 0
        self.pipeline = Gst.parse_launch(
            "uridecodebin name=src caps=audio/x-raw ! audioconvert ! audioresample ! "
            f"audio/x-raw,format=F32LE,channels=1,rate={self.SAMPLE_RATE} ! "
            "appsink name=sink emit-signals=true sync=false"
        )
        self.pipeline.get_by_name("src").set_property("uri", uri)
        sink = self.pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_sample)
        sink.connect("eos", lambda *_: GLib.idle_add(self.stop))
        self.pipeline.set_state(Gst.State.PLAYING)

    def _on_sample(self, sink):
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            try:
                data = memoryview(info.data).cast("f")
                i, n = 0, len(data)
                while i < n:
                    take = min(self.BUCKET - self._cur_n, n - i)
                    seg = data[i:i + take]
                    self._cur_max = max(self._cur_max, max(seg), -min(seg))
                    self._cur_n += take
                    i += take
                    if self._cur_n >= self.BUCKET:
                        self.peaks.append(self._cur_max)
                        self.max_peak = max(self.max_peak, self._cur_max)
                        self._cur_max, self._cur_n = 0.0, 0
            finally:
                buf.unmap(info)
        return Gst.FlowReturn.OK

    def stop(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        return False


class WaveformView(Gtk.DrawingArea):
    def __init__(self, on_seek):
        super().__init__()
        self.set_content_height(64)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.analyzer = None
        self.duration = 0.0
        self.progress = 0.0
        self.on_seek = on_seek
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._drag_begin)
        drag.connect("drag-update", self._drag_update)
        drag.connect("drag-end", self._drag_end)
        self.add_controller(drag)
        self._drag_x0 = 0.0

    def set_analyzer(self, analyzer):
        if self.analyzer:
            self.analyzer.stop()
        self.analyzer = analyzer
        self.queue_draw()

    def set_progress(self, pos, dur):
        self.duration = dur
        self.progress = pos / dur if dur > 0 else 0.0
        self.queue_draw()

    def _frac(self, x):
        return min(1.0, max(0.0, x / max(1, self.get_width())))

    def _drag_begin(self, g, x, y):
        self._drag_x0 = x
        self.progress = self._frac(x)
        self.queue_draw()

    def _drag_update(self, g, dx, dy):
        self.progress = self._frac(self._drag_x0 + dx)
        self.queue_draw()

    def _drag_end(self, g, dx, dy):
        self.on_seek(self._frac(self._drag_x0 + dx))

    def _draw(self, area, cr, w, h):
        fg = self.get_color()
        accent = Adw.StyleManager.get_default().get_accent_color_rgba()
        mid = h / 2
        step, bar_w = 3, 2
        peaks = self.analyzer.peaks if self.analyzer else []
        scale = (self.analyzer.max_peak if self.analyzer else 0) or 1.0
        count = len(peaks)
        for x in range(0, w - bar_w + 1, step):
            amp = 0.0
            if self.duration > 0 and count:
                i0 = int(x / w * self.duration / WaveformAnalyzer.BUCKET_SECONDS)
                i1 = max(i0 + 1, int((x + step) / w * self.duration / WaveformAnalyzer.BUCKET_SECONDS))
                if i0 < count:
                    amp = (max(peaks[i0:i1]) / scale) ** 0.7
            half = max(1.0, amp * (h / 2 - 2))
            played = (x / w) <= self.progress
            c = accent if played else fg
            cr.set_source_rgba(c.red, c.green, c.blue, 1.0 if played else 0.35)
            cr.rectangle(x, mid - half, bar_w, half * 2)
            cr.fill()


# ---------------------------------------------------------------- MPRIS
MPRIS_XML = """
<node>
 <interface name="org.mpris.MediaPlayer2">
  <method name="Raise"/><method name="Quit"/>
  <property name="CanQuit" type="b" access="read"/>
  <property name="CanRaise" type="b" access="read"/>
  <property name="HasTrackList" type="b" access="read"/>
  <property name="Identity" type="s" access="read"/>
  <property name="SupportedUriSchemes" type="as" access="read"/>
  <property name="SupportedMimeTypes" type="as" access="read"/>
 </interface>
 <interface name="org.mpris.MediaPlayer2.Player">
  <method name="Next"/><method name="Previous"/><method name="Pause"/>
  <method name="PlayPause"/><method name="Stop"/><method name="Play"/>
  <method name="Seek"><arg type="x" direction="in"/></method>
  <method name="SetPosition"><arg type="o" direction="in"/><arg type="x" direction="in"/></method>
  <method name="OpenUri"><arg type="s" direction="in"/></method>
  <property name="PlaybackStatus" type="s" access="read"/>
  <property name="LoopStatus" type="s" access="readwrite"/>
  <property name="Rate" type="d" access="readwrite"/>
  <property name="Shuffle" type="b" access="readwrite"/>
  <property name="Metadata" type="a{sv}" access="read"/>
  <property name="Volume" type="d" access="readwrite"/>
  <property name="Position" type="x" access="read"/>
  <property name="MinimumRate" type="d" access="read"/>
  <property name="MaximumRate" type="d" access="read"/>
  <property name="CanGoNext" type="b" access="read"/>
  <property name="CanGoPrevious" type="b" access="read"/>
  <property name="CanPlay" type="b" access="read"/>
  <property name="CanPause" type="b" access="read"/>
  <property name="CanSeek" type="b" access="read"/>
  <property name="CanControl" type="b" access="read"/>
 </interface>
</node>
"""
LOOP_TO_MPRIS = {"off": "None", "all": "Playlist", "one": "Track"}
MPRIS_TO_LOOP = {v: k for k, v in LOOP_TO_MPRIS.items()}


class Mpris:
    PATH = "/org/mpris/MediaPlayer2"

    def __init__(self, win):
        self.win = win
        self.conn = None
        self.nodes = Gio.DBusNodeInfo.new_for_xml(MPRIS_XML)
        Gio.bus_own_name(
            Gio.BusType.SESSION, "org.mpris.MediaPlayer2.musicplayer",
            Gio.BusNameOwnerFlags.NONE, self._acquired, None, None,
        )

    def _acquired(self, conn, name):
        self.conn = conn
        for iface in self.nodes.interfaces:
            conn.register_object(self.PATH, iface, self._call, self._get, self._set)

    def _props(self, iface):
        w = self.win
        t = w.current
        if iface == "org.mpris.MediaPlayer2":
            return {
                "CanQuit": GLib.Variant("b", True), "CanRaise": GLib.Variant("b", True),
                "HasTrackList": GLib.Variant("b", False), "Identity": GLib.Variant("s", "Music Player"),
                "SupportedUriSchemes": GLib.Variant("as", ["file"]),
                "SupportedMimeTypes": GLib.Variant("as", ["audio/mpeg", "audio/flac", "audio/ogg"]),
            }
        meta = {"mpris:trackid": GLib.Variant("o", "/org/mpris/MediaPlayer2/Track/0")}
        if t:
            meta["xesam:title"] = GLib.Variant("s", t.title)
            meta["xesam:artist"] = GLib.Variant("as", [t.artist] if t.artist else [])
            meta["xesam:url"] = GLib.Variant("s", Gio.File.new_for_path(t.path).get_uri())
            meta["mpris:length"] = GLib.Variant("x", int(w.duration * 1e6))
        status = {"playing": "Playing", "paused": "Paused"}.get(w.status, "Stopped")
        return {
            "PlaybackStatus": GLib.Variant("s", status),
            "LoopStatus": GLib.Variant("s", LOOP_TO_MPRIS[w.repeat]),
            "Rate": GLib.Variant("d", 1.0), "MinimumRate": GLib.Variant("d", 1.0),
            "MaximumRate": GLib.Variant("d", 1.0),
            "Shuffle": GLib.Variant("b", w.shuffle),
            "Metadata": GLib.Variant("a{sv}", meta),
            "Volume": GLib.Variant("d", w.volume),
            "Position": GLib.Variant("x", int(w.position() * 1e6)),
            "CanGoNext": GLib.Variant("b", True), "CanGoPrevious": GLib.Variant("b", True),
            "CanPlay": GLib.Variant("b", True), "CanPause": GLib.Variant("b", True),
            "CanSeek": GLib.Variant("b", True), "CanControl": GLib.Variant("b", True),
        }

    def _get(self, conn, sender, path, iface, prop):
        return self._props(iface).get(prop)

    def _set(self, conn, sender, path, iface, prop, value):
        w = self.win
        if prop == "LoopStatus":
            w.set_repeat(MPRIS_TO_LOOP.get(value.get_string(), "off"))
        elif prop == "Shuffle":
            w.set_shuffle(value.get_boolean())
        elif prop == "Volume":
            w.vol_scale.set_value(max(0.0, min(1.0, value.get_double())))
        else:
            return False
        return True

    def _call(self, conn, sender, path, iface, method, params, inv):
        w = self.win
        actions = {
            "Next": w.next_track, "Previous": w.prev_track, "Pause": w.pause,
            "PlayPause": w.toggle_play, "Stop": w.stop, "Play": w.play,
            "Raise": w.present, "Quit": w.close,
        }
        if method in actions:
            actions[method]()
        elif method == "Seek":
            w.seek_to(w.position() + params.unpack()[0] / 1e6)
        elif method == "SetPosition":
            w.seek_to(params.unpack()[1] / 1e6)
        inv.return_value(None)

    def notify(self):
        if not self.conn:
            return
        props = self._props("org.mpris.MediaPlayer2.Player")
        props.pop("Position")
        self.conn.emit_signal(
            None, self.PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
            GLib.Variant("(sa{sv}as)", ("org.mpris.MediaPlayer2.Player", props, [])),
        )


# ------------------------------------------------------------ janela
class PlayerWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Music Player")
        self.freq = FrequencyStore()
        state = read_json(STATE_FILE, {})
        self.folder = state.get("folder", DEFAULT_FOLDER)
        self.tracks = []
        self.by_path = {}
        self.rows = {}
        self.current = None
        self.status = "stopped"
        self.duration = 0.0
        self.pending_seek = None
        self.filter_text = ""
        self.shuffle = bool(state.get("shuffle", False))
        self.repeat = state.get("repeat", "off") if state.get("repeat") in REPEAT_MODES else "off"
        self.volume_set = bool(state.get("volume_set"))
        self.volume = float(state.get("volume", 1.0)) if self.volume_set else 1.0
        self.shuf_queue = []
        self.cycle_played = set()
        self.history = []
        self.errors = 0
        self.tick_n = 0
        self.set_default_size(*state.get("size", [960, 720]))

        self.pb = Gst.ElementFactory.make("playbin", "player")
        self.pb.set_property("video-sink", Gst.ElementFactory.make("fakesink"))
        bus = self.pb.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus)

        self._build_ui()
        self._build_actions()
        self.vol_scale.set_value(self.volume)
        self.volume_set = bool(state.get("volume_set"))
        self._apply_modes()

        self.connect("close-request", self._on_close)
        GLib.timeout_add(200, self._tick)
        self.mpris = Mpris(self)

        saved = [p for p in state.get("tracks", []) if os.path.exists(p)]
        self.set_library(saved)
        self._rescan()
        cur = self.by_path.get(state.get("current"))
        if cur:
            self.load_track(cur, float(state.get("position", 0)))
        self._autostart()

    def _autostart(self):
        if self.status == "paused":
            self.play()
        elif not self.current and self.tracks:
            self.next_track()

    # ---- interface
    def _build_ui(self):
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        open_btn = Gtk.Button(tooltip_text="Definir a pasta pai das músicas (subpastas incluídas) (Ctrl+O)")
        open_btn.set_child(Adw.ButtonContent(icon_name="folder-open-symbolic", label="Pasta das músicas"))
        open_btn.connect("clicked", lambda *_: self.open_folder_dialog())
        header.pack_start(open_btn)
        menu = Gio.Menu()
        menu.append("Rescanear pasta", "win.rescan")
        menu.append("Zerar contadores de frequência", "win.reset_freq")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu))
        view.add_top_bar(header)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for side in ("start", "end", "bottom", "top"):
            getattr(root, f"set_margin_{side}")(12)

        top = Gtk.Box(spacing=16)
        self.cover_stack = Gtk.Stack()
        self.cover_pic = Gtk.Image(pixel_size=150)
        placeholder = Gtk.Image(icon_name="audio-x-generic-symbolic", pixel_size=64)
        self.cover_stack.add_named(self.cover_pic, "pic")
        self.cover_stack.add_named(placeholder, "ph")
        self.cover_stack.set_size_request(150, 150)
        self.cover_stack.set_valign(Gtk.Align.START)
        self.cover_stack.set_hexpand(False)
        self.cover_stack.set_overflow(Gtk.Overflow.HIDDEN)
        self.cover_stack.add_css_class("card")
        top.append(self.cover_stack)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        self.title_lbl = Gtk.Label(label="Nada tocando", xalign=0, ellipsize=3)
        self.title_lbl.add_css_class("title-2")
        self.artist_lbl = Gtk.Label(label="", xalign=0, ellipsize=3)
        self.artist_lbl.add_css_class("dim-label")
        info.append(self.title_lbl)
        info.append(self.artist_lbl)
        self.wave = WaveformView(self.seek_fraction)
        info.append(self.wave)
        times = Gtk.Box()
        self.pos_lbl = Gtk.Label(label="0:00")
        self.dur_lbl = Gtk.Label(label="0:00", hexpand=True, xalign=1)
        for lb in (self.pos_lbl, self.dur_lbl):
            lb.add_css_class("numeric")
            lb.add_css_class("dim-label")
        times.append(self.pos_lbl)
        times.append(self.dur_lbl)
        info.append(times)

        controls = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        self.prev_btn = Gtk.Button(icon_name="media-skip-backward-symbolic", tooltip_text="Anterior (Ctrl+←)")
        self.play_btn = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Play/Pause (Ctrl+Espaço)")
        self.stop_btn = Gtk.Button(icon_name="media-playback-stop-symbolic", tooltip_text="Stop")
        self.next_btn = Gtk.Button(icon_name="media-skip-forward-symbolic", tooltip_text="Próxima (Ctrl+→)")
        self.shuffle_btn = Gtk.Button(icon_name="media-playlist-shuffle-symbolic", tooltip_text="Aleatório (Ctrl+S)")
        self.repeat_btn = Gtk.Button(icon_name="media-playlist-repeat-symbolic")
        self.play_btn.add_css_class("suggested-action")
        self.play_btn.add_css_class("circular")
        self.prev_btn.connect("clicked", lambda *_: self.prev_track())
        self.play_btn.connect("clicked", lambda *_: self.toggle_play())
        self.stop_btn.connect("clicked", lambda *_: self.stop())
        self.next_btn.connect("clicked", lambda *_: self.next_track())
        self.shuffle_btn.connect("clicked", lambda *_: self.set_shuffle(not self.shuffle))
        self.repeat_btn.connect("clicked", lambda *_: self.cycle_repeat())
        for b in (self.shuffle_btn, self.prev_btn, self.play_btn, self.stop_btn, self.next_btn, self.repeat_btn):
            controls.append(b)
        self.vol_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 1, 0.01)
        self.vol_scale.set_size_request(110, -1)
        self.vol_scale.connect("value-changed", self._on_volume)
        controls.append(Gtk.Image(icon_name="audio-volume-high-symbolic", margin_start=12))
        controls.append(self.vol_scale)
        info.append(controls)
        top.append(info)
        root.append(top)

        self.search = Gtk.SearchEntry(placeholder_text="Filtrar músicas… (Ctrl+F / F3)")
        self.search.connect("search-changed", self._on_filter)
        root.append(self.search)

        self.listbox = Gtk.ListBox(activate_on_single_click=False, selection_mode=Gtk.SelectionMode.SINGLE)
        self.listbox.add_css_class("rich-list")
        self.listbox.set_filter_func(self._row_visible)
        self.listbox.connect("row-activated", lambda lb, row: self.play_track(row.track))
        self.scroller = Gtk.ScrolledWindow(vexpand=True, child=self.listbox)
        self.scroller.add_css_class("card")
        root.append(self.scroller)
        self.count_lbl = Gtk.Label(xalign=0)
        self.count_lbl.add_css_class("dim-label")
        root.append(self.count_lbl)

        view.set_content(root)
        self.set_content(view)

    def _build_actions(self):
        app = self.get_application()
        specs = {
            "play_pause": (self.toggle_play, "<Ctrl>space"),
            "next": (self.next_track, "<Ctrl>Right"),
            "prev": (self.prev_track, "<Ctrl>Left"),
            "shuffle": (lambda: self.set_shuffle(not self.shuffle), "<Ctrl>s"),
            "repeat": (self.cycle_repeat, "<Ctrl>r"),
            "open": (self.open_folder_dialog, "<Ctrl>o"),
            "focus_filter": (lambda: self.search.grab_focus(), ["<Ctrl>f", "F3"]),
            "rescan": (self._rescan, None),
            "reset_freq": (self._reset_freq, None),
        }
        for name, (fn, accel) in specs.items():
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", lambda a, p, f=fn: f())
            self.add_action(act)
            if accel:
                app.set_accels_for_action(f"win.{name}", [accel] if isinstance(accel, str) else accel)

    # ---- biblioteca
    def set_library(self, paths):
        self.tracks = sorted((Track(p) for p in paths), key=lambda t: t.sort_key)
        self.by_path = {t.path: t for t in self.tracks}
        self.rows = {}
        self.listbox.remove_all()
        for t in self.tracks:
            row = self._make_row(t)
            self.rows[t.path] = row
            self.listbox.append(row)
        self._update_count()
        if self.current:
            self._mark_current()

    def _make_row(self, t):
        row = Gtk.ListBoxRow()
        row.track = t
        box = Gtk.Box(spacing=10)
        row.icon = Gtk.Image(icon_name="media-playback-start-symbolic", opacity=0)
        lab = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        esc = GLib.markup_escape_text
        lab.set_markup(f"<b>{esc(t.title)}</b>  <span alpha='60%'>{esc(t.artist)}</span>")
        row.cnt = Gtk.Label(label=str(self.freq.get(t.path)), tooltip_text="Vezes tocada")
        row.cnt.add_css_class("dim-label")
        row.cnt.add_css_class("numeric")
        for w in (row.icon, lab, row.cnt):
            box.append(w)
        row.set_child(box)
        click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        click.connect("pressed", lambda g, n, x, y, r=row: self._show_row_menu(r, x, y))
        row.add_controller(click)
        return row

    def _show_row_menu(self, row, x, y):
        pop = Gtk.Popover(has_arrow=False, halign=Gtk.Align.START)
        pop.set_parent(row)
        pop.set_pointing_to(Gdk.Rectangle(int(x), int(y), 1, 1))
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        actions = [
            ("Copiar path", "edit-copy-symbolic", self._copy_path),
            ("Abrir pasta", "folder-open-symbolic", self._open_containing_folder),
            ("Deletar música", "user-trash-symbolic", self._confirm_delete),
        ]
        for label, icon, fn in actions:
            btn = Gtk.Button(has_frame=False)
            content = Gtk.Box(spacing=8)
            content.append(Gtk.Image(icon_name=icon))
            content.append(Gtk.Label(label=label, xalign=0))
            btn.set_child(content)
            btn.connect("clicked", lambda b, f=fn, p=pop: (p.popdown(), f(row.track)))
            box.append(btn)
        pop.set_child(box)
        self.listbox.select_row(row)
        pop.popup()

    def _copy_path(self, t):
        self.get_clipboard().set(t.path)

    def _open_containing_folder(self, t):
        # FileManager1.ShowItems abre a pasta já com o arquivo selecionado
        file_uri = Gio.File.new_for_path(t.path).get_uri()
        dir_uri = Gio.File.new_for_path(os.path.dirname(t.path)).get_uri()

        def fallback():
            try:
                Gio.AppInfo.launch_default_for_uri(dir_uri, None)
            except GLib.Error as e:
                print("erro abrindo pasta:", e.message, file=sys.stderr)

        def done(conn, res):
            try:
                conn.call_finish(res)
            except GLib.Error:
                fallback()

        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            fallback()
            return
        bus.call(
            "org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
            "org.freedesktop.FileManager1", "ShowItems",
            GLib.Variant("(ass)", ([file_uri], "")), None,
            Gio.DBusCallFlags.NONE, 5000, None, done,
        )

    def _confirm_delete(self, t):
        dlg = Adw.AlertDialog(
            heading="Deletar música?",
            body=f"“{t.label}” será enviada para a lixeira e removida da lista.",
        )
        dlg.add_response("cancel", "Cancelar")
        dlg.add_response("delete", "Enviar para a lixeira")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.connect("response", lambda d, r: self._delete_track(t) if r == "delete" else None)
        dlg.present(self)

    def _delete_track(self, t):
        try:
            Gio.File.new_for_path(t.path).trash(None)
        except GLib.Error as e:
            err = Adw.AlertDialog(heading="Não foi possível enviar para a lixeira", body=e.message)
            err.add_response("ok", "OK")
            err.present(self)
            return
        if self.current is t:
            nxt = self._pick_next(False) if len(self.tracks) > 1 else None
            if nxt is t:
                nxt = None
            was_playing = self.status == "playing"
            if nxt and was_playing:
                self.play_track(nxt, record=False)
            else:
                self.stop()
                self.current = None
                self.title_lbl.set_label("Nada tocando")
                self.artist_lbl.set_label("")
                self.set_title("Music Player")
                self._show_cover(None)
                self.wave.set_analyzer(None)
        self.tracks.remove(t)
        self.by_path.pop(t.path, None)
        self.listbox.remove(self.rows.pop(t.path))
        self.freq.counts.pop(t.path, None)
        self.freq.save()
        self.history = [p for p in self.history if p != t.path]
        self.shuf_queue = [p for p in self.shuf_queue if p != t.path]
        self.cycle_played.discard(t.path)
        self._update_count()
        self.save_state()

    def _rescan(self):
        folder = self.folder

        def work():
            paths = scan_folder(folder) if os.path.isdir(folder) else []
            GLib.idle_add(self._scan_done, folder, paths)

        threading.Thread(target=work, daemon=True).start()

    def _scan_done(self, folder, paths):
        if folder != self.folder or not paths:
            return False
        if set(paths) != set(self.by_path):
            self.set_library(paths)
            self.freq.prune(paths)
        if self.status == "stopped" and not self.current:
            self._autostart()
        return False

    def open_folder_dialog(self):
        dlg = Gtk.FileDialog(title="Escolha a pasta pai das músicas (subpastas incluídas)")
        if os.path.isdir(self.folder):
            dlg.set_initial_folder(Gio.File.new_for_path(self.folder))

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            self.folder = f.get_path()
            self.set_library([])
            self._rescan()

        dlg.select_folder(self, None, done)

    def _reset_freq(self):
        self.freq.reset()
        for row in self.rows.values():
            row.cnt.set_label("0")

    # ---- filtro
    def _matches(self, t):
        return all(w in t.search for w in self.filter_text.split())

    def _row_visible(self, row):
        return self._matches(row.track)

    def visible_tracks(self):
        return [t for t in self.tracks if self._matches(t)]

    def _on_filter(self, entry):
        self.filter_text = norm(entry.get_text())
        self.listbox.invalidate_filter()
        self.shuf_queue = []
        self._update_count()

    def _update_count(self):
        self.count_lbl.set_label(f"{len(self.visible_tracks())} de {len(self.tracks)} músicas")

    # ---- navegação
    def _seq_step(self, direction, auto):
        n = len(self.tracks)
        if not n:
            return None
        vis = {t.path for t in self.visible_tracks()}
        idx = self.tracks.index(self.current) if self.current in self.tracks else (-1 if direction > 0 else n)
        for s in range(1, n + 1):
            i = idx + direction * s
            if auto and self.repeat != "all" and not 0 <= i < n:
                return None
            t = self.tracks[i % n]
            if t.path in vis:
                return t
        return None

    def _shuffle_next(self, auto):
        vis = self.visible_tracks()
        if not vis:
            return None
        cur = self.current.path if self.current else None
        paths = {t.path for t in vis}
        if paths <= (self.cycle_played | {cur}):
            if auto and self.repeat != "all":
                return None
            self.cycle_played = {cur} if cur else set()
        cand = [t.path for t in vis if t.path != cur] or [t.path for t in vis]
        low = min(self.freq.get(p) for p in cand)
        cset = set(cand)
        queue = [p for p in self.shuf_queue if p in cset and self.freq.get(p) == low]
        if not queue:
            queue = fisher_yates(p for p in cand if self.freq.get(p) == low)
        self.shuf_queue = queue[1:]
        return self.by_path[queue[0]]

    def _pick_next(self, auto):
        return self._shuffle_next(auto) if self.shuffle else self._seq_step(1, auto)

    def next_track(self):
        t = self._pick_next(False)
        if t:
            self.play_track(t)

    def prev_track(self):
        if not self.current:
            return
        if self.shuffle:
            while self.history:
                t = self.by_path.get(self.history.pop())
                if t:
                    self.play_track(t, count=False, record=False)
                    return
            t = self._shuffle_next(False)
            if t:
                self.play_track(t)
            return
        t = self._seq_step(-1, False)
        if t:
            self.play_track(t, count=False, record=False)

    # ---- reprodução
    def _set_uri(self, t):
        self.pb.set_state(Gst.State.NULL)
        self.pb.set_property("uri", Gio.File.new_for_path(t.path).get_uri())
        self.current = t
        self.duration = 0.0
        self.wave.set_analyzer(WaveformAnalyzer(Gio.File.new_for_path(t.path).get_uri()))
        self.title_lbl.set_label(t.title)
        self.artist_lbl.set_label(t.artist)
        self.set_title(t.label)
        self._load_cover_file(t)
        self._mark_current()

    def play_track(self, t, count=True, record=True):
        if record and self.current and self.current is not t:
            self.history.append(self.current.path)
            del self.history[:-200]
        self._set_uri(t)
        self.pending_seek = None
        self.cycle_played.add(t.path)
        if count:
            rotated = self.freq.increment(t.path, [x.path for x in self.tracks])
            for p in (self.rows if rotated else [t.path]):
                self.rows[p].cnt.set_label(str(self.freq.get(p)))
        self.pb.set_state(Gst.State.PLAYING)
        self._set_status("playing")

    def load_track(self, t, position):
        self._set_uri(t)
        self.pending_seek = position
        self.pb.set_state(Gst.State.PAUSED)
        self._set_status("paused")

    def play(self):
        if self.status == "paused":
            self.pb.set_state(Gst.State.PLAYING)
            self._set_status("playing")
        elif self.status == "stopped":
            if self.current:
                self.play_track(self.current, count=False, record=False)
            else:
                self.next_track()

    def pause(self):
        if self.status == "playing":
            self.pb.set_state(Gst.State.PAUSED)
            self._set_status("paused")

    def toggle_play(self):
        self.pause() if self.status == "playing" else self.play()

    def stop(self):
        self.pb.set_state(Gst.State.NULL)
        self.pending_seek = None
        self.wave.set_progress(0, self.duration)
        self.pos_lbl.set_label("0:00")
        self._set_status("stopped")

    def position(self):
        if self.status == "stopped":
            return 0.0
        ok, pos = self.pb.query_position(Gst.Format.TIME)
        return pos / Gst.SECOND if ok else (self.pending_seek or 0.0)

    def seek_to(self, sec):
        if self.status == "stopped":
            return
        sec = max(0.0, sec)
        self.pb.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE, int(sec * Gst.SECOND))

    def seek_fraction(self, frac):
        if self.status == "stopped" and self.current:
            self.play_track(self.current, count=False, record=False)
            self.pending_seek = frac * self.duration
            return
        self.seek_to(frac * self.duration)

    def _set_status(self, status):
        self.status = status
        self.play_btn.set_icon_name(
            "media-playback-pause-symbolic" if status == "playing" else "media-playback-start-symbolic"
        )
        self.mpris.notify()

    def set_shuffle(self, on):
        self.shuffle = on
        self.shuf_queue = []
        self.cycle_played = {self.current.path} if self.current else set()
        self._apply_modes()

    def set_repeat(self, mode):
        self.repeat = mode
        self._apply_modes()

    def cycle_repeat(self):
        self.set_repeat(REPEAT_MODES[(REPEAT_MODES.index(self.repeat) + 1) % 3])

    def _apply_modes(self):
        def style(btn, active):
            (btn.add_css_class if active else btn.remove_css_class)("suggested-action")
            btn.set_opacity(1.0 if active else 0.6)

        style(self.shuffle_btn, self.shuffle)
        self.shuffle_btn.set_tooltip_text(f"Aleatório: {'ligado' if self.shuffle else 'desligado'} (Ctrl+S)")
        style(self.repeat_btn, self.repeat != "off")
        self.repeat_btn.set_icon_name(
            "media-playlist-repeat-song-symbolic" if self.repeat == "one" else "media-playlist-repeat-symbolic"
        )
        names = {"off": "desligado", "all": "todas", "one": "uma"}
        self.repeat_btn.set_tooltip_text(f"Repetir: {names[self.repeat]} (Ctrl+R)")
        if hasattr(self, "mpris"):
            self.mpris.notify()

    def _on_volume(self, scale):
        self.volume = scale.get_value()
        self.volume_set = True
        self.pb.set_property("volume", self.volume ** 3)

    # ---- eventos do GStreamer
    def _on_bus(self, bus, msg):
        t = msg.type
        if t == Gst.MessageType.EOS:
            if self.repeat == "one" and self.current:
                self.play_track(self.current, record=False)
                return
            nxt = self._pick_next(True)
            self.play_track(nxt) if nxt else self.stop()
        elif t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            print("erro:", err.message, file=sys.stderr)
            self.errors += 1
            nxt = self._pick_next(True) if self.errors < 5 else None
            self.play_track(nxt) if nxt else self.stop()
        elif t == Gst.MessageType.ASYNC_DONE and self.pending_seek is not None:
            pos, self.pending_seek = self.pending_seek, None
            if pos > 0:
                self.seek_to(pos)
        elif t == Gst.MessageType.TAG:
            self._cover_from_tags(msg.parse_tag())
        elif t == Gst.MessageType.STATE_CHANGED and msg.src == self.pb:
            if msg.parse_state_changed().newstate == Gst.State.PLAYING:
                self.errors = 0

    def _cover_from_tags(self, tags):
        for key in ("image", "preview-image"):
            ok, sample = tags.get_sample(key)
            if ok:
                buf = sample.get_buffer()
                ok, info = buf.map(Gst.MapFlags.READ)
                if ok:
                    try:
                        tex = Gdk.Texture.new_from_bytes(GLib.Bytes.new(bytes(info.data)))
                        self._show_cover(tex)
                    except GLib.Error:
                        pass
                    finally:
                        buf.unmap(info)
                return

    # ---- capas (mesmo esquema do kew: .covers/<Artista>.jpg)
    def _load_cover_file(self, t):
        self._show_cover(None)
        root = os.path.abspath(self.folder)
        d = os.path.dirname(t.path)
        dirs = []
        while True:
            dirs.append(d)
            if d == root or len(d) <= 1 or not d.startswith(root):
                break
            d = os.path.dirname(d)
        if root not in dirs:
            dirs.append(root)
        names = [f"{t.artist}.jpg", f"{t.artist}.png"] if t.artist else []
        for d in dirs:
            cands = [os.path.join(d, ".covers", n) for n in names]
            cands += [os.path.join(d, n) for n in ("cover.jpg", "folder.jpg", "cover.png", "folder.png")]
            for c in cands:
                if os.path.isfile(c):
                    try:
                        self._show_cover(Gdk.Texture.new_from_filename(c))
                        return
                    except GLib.Error:
                        pass

    def _show_cover(self, tex):
        self.cover_pic.set_from_paintable(tex)
        self.cover_stack.set_visible_child_name("pic" if tex else "ph")

    # ---- UI periódica
    def _mark_current(self):
        for p, row in self.rows.items():
            row.icon.set_opacity(1 if self.current and p == self.current.path else 0)
        row = self.rows.get(self.current.path) if self.current else None
        if row:
            self.listbox.select_row(row)
            GLib.idle_add(self._scroll_to, row)

    def _scroll_to(self, row):
        ok, pt = row.compute_point(self.listbox, Graphene.Point().init(0, 0))
        if ok:
            adj = self.scroller.get_vadjustment()
            adj.set_value(pt.y - adj.get_page_size() / 2 + row.get_height() / 2)
        return False

    def _tick(self):
        self.tick_n += 1
        if self.status != "stopped":
            if self.duration <= 0:
                ok, d = self.pb.query_duration(Gst.Format.TIME)
                if ok and d > 0:
                    self.duration = d / Gst.SECOND
                    self.dur_lbl.set_label(fmt_time(self.duration))
            pos = self.position()
            self.pos_lbl.set_label(fmt_time(pos))
            self.wave.set_progress(pos, self.duration)
            if self.tick_n % 50 == 0:
                self.save_state()
        elif self.wave.analyzer and self.wave.analyzer.pipeline:
            self.wave.queue_draw()
        return True

    # ---- estado
    def save_state(self):
        w, h = self.get_default_size()
        try:
            write_json(STATE_FILE, {
                "folder": self.folder,
                "tracks": [t.path for t in self.tracks],
                "current": self.current.path if self.current else None,
                "position": self.position(),
                "volume": self.volume,
                "volume_set": self.volume_set,
                "shuffle": self.shuffle,
                "repeat": self.repeat,
                "size": [w, h],
            })
        except OSError as e:
            print("erro salvando estado:", e, file=sys.stderr)

    def _on_close(self, *_):
        self.save_state()
        self.wave.set_analyzer(None)
        self.pb.set_state(Gst.State.NULL)
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        # SIGTERM (ex.: enviado pelo iniciar.sh) fecha a janela salvando o estado
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self._on_sigterm)

    def _on_sigterm(self):
        win = self.props.active_window
        if win:
            win.close()
        self.quit()
        return GLib.SOURCE_REMOVE

    def do_activate(self):
        win = self.props.active_window or PlayerWindow(self)
        win.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
