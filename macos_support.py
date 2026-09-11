# -*- coding: utf-8 -*-
"""
macOS-Unterbau fuer den Claude Session Browser.

Unter Windows erledigen user32/kernel32, die Registry und Tkinter-Fenster in
eigenen Threads diese Arbeit. Auf dem Mac gehoert AppKit - und damit auch
pywebview - der Hauptthread: jedes Fenster muss dort entstehen und dort
angefasst werden. Deshalb sind Buddy, Reset-Karte, Platzier-Raster und das
Symbol in der Menueleiste hier als native AppKit-Fenster nachgebaut. Die Logik
dahinter bleibt in claude_sessions.py und laeuft wie gehabt in ihrem eigenen
Thread; hierher kommen nur fertige Zeichen-Auftraege.

Koordinaten: alles, was diese Datei annimmt oder zurueckgibt, rechnet wie
Windows - Ursprung oben links am Hauptbildschirm, y waechst nach unten.
Cocoa zaehlt von unten links; umgerechnet wird nur hier.
"""

import atexit
import os
import plistlib
import shlex
import socket
import subprocess
import sys
import tempfile
import threading
import time

import objc
import AppKit
import Foundation
import Quartz
from PyObjCTools import AppHelper

BUNDLE_ID = "io.github.juppeee.claude-session-browser"

_KEEP = {}                       # ObjC-Objekte, die nicht eingesammelt werden duerfen
_STATE = {"main_key": False, "main_visible": True}


# --------------------------------------------------------------------------- #
#  Hauptthread
# --------------------------------------------------------------------------- #
def is_main_thread():
    return bool(Foundation.NSThread.isMainThread())


def on_main(fn, *args):
    """Auf dem Hauptthread ausfuehren, ohne zu warten."""
    if is_main_thread():
        fn(*args)
    else:
        AppHelper.callAfter(fn, *args)


def on_main_sync(fn, *args, timeout=3.0, default=None):
    """Auf dem Hauptthread ausfuehren und das Ergebnis abwarten.

    Vor dem Start der Ereignisschleife bleibt der Auftrag liegen, bis sie
    laeuft - wer so frueh fragt, braucht ein grosszuegiges `timeout`.
    """
    if is_main_thread():
        return fn(*args)
    box = {}
    done = threading.Event()

    def run():
        try:
            box["v"] = fn(*args)
        except Exception:
            pass
        finally:
            done.set()

    AppHelper.callAfter(run)
    if not done.wait(timeout):
        return default
    return box.get("v", default)


# --------------------------------------------------------------------------- #
#  Bildschirme
# --------------------------------------------------------------------------- #
_SCREENS = {"t": 0.0, "v": []}


def _read_screens():
    out = []
    scr = AppKit.NSScreen.screens() or []
    if not scr:
        return out
    ph = scr[0].frame().size.height

    def tl(r):
        left = int(round(r.origin.x))
        top = int(round(ph - (r.origin.y + r.size.height)))
        return (left, top, left + int(round(r.size.width)),
                top + int(round(r.size.height)))

    for i, s in enumerate(scr):
        wl, wt, wr, wb = tl(s.visibleFrame())
        out.append({"left": wl, "top": wt, "right": wr, "bottom": wb,
                    "frame": tl(s.frame()), "primary": i == 0})
    return out


def screens():
    """Alle Bildschirme: Arbeitsbereich (ohne Menueleiste und Dock) als
    left/top/right/bottom, dazu der volle Rahmen. Der erste ist der
    Hauptbildschirm."""
    now = time.time()
    if _SCREENS["v"] and now - _SCREENS["t"] < 2.0:
        return list(_SCREENS["v"])
    v = on_main_sync(_read_screens, default=None)
    if v:
        _SCREENS["t"] = now
        _SCREENS["v"] = v
    return list(_SCREENS["v"])


def _primary_height():
    s = screens()
    if not s:
        return 0
    _l, t, _r, b = s[0]["frame"]
    return b - t


def to_cocoa(x, y, h):
    return x, _primary_height() - y - h


def from_cocoa(x, y, h):
    return x, _primary_height() - y - h


def work_area_at(x, y):
    """Arbeitsbereich des Bildschirms unter (x, y), sonst des naechsten."""
    best, best_d = None, None
    for s in screens():
        l, t, r, b = s["frame"]
        if l <= x < r and t <= y < b:
            return (s["left"], s["top"], s["right"], s["bottom"])
        dx = max(l - x, 0, x - r)
        dy = max(t - y, 0, y - b)
        d = dx * dx + dy * dy
        if best_d is None or d < best_d:
            best, best_d = s, d
    if best:
        return (best["left"], best["top"], best["right"], best["bottom"])
    return None


def primary_size():
    s = screens()
    if not s:
        return 1440, 900
    l, t, r, b = s[0]["frame"]
    return r - l, b - t


def position_is_usable(x, y, w, h):
    """Liegt genug Titelleiste auf irgendeinem Bildschirm, um das Fenster
    greifen zu koennen?"""
    scr = screens()
    if not scr:
        return True
    for s in scr:
        l, t, r, b = s["frame"]
        ox = min(x + w, r) - max(x, l)
        oy = min(y + 28, b) - max(y, t)
        if ox >= 160 and oy >= 20:
            return True
    return False


def pointer_pos():
    p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return int(p.x), int(p.y)


def fit_window(nswin):
    """Holt ein Fenster ganz auf den Arbeitsbereich seines Bildschirms.
    True wenn es verschoben oder verkleinert wurde."""
    def run():
        if nswin is None:
            return False
        scr = nswin.screen() or AppKit.NSScreen.mainScreen()
        if scr is None:
            return False
        vf, f = scr.visibleFrame(), nswin.frame()
        w = min(f.size.width, vf.size.width)
        h = min(f.size.height, vf.size.height)
        x = max(vf.origin.x, min(f.origin.x, vf.origin.x + vf.size.width - w))
        y = max(vf.origin.y, min(f.origin.y, vf.origin.y + vf.size.height - h))
        if (x, y, w, h) == (f.origin.x, f.origin.y, f.size.width, f.size.height):
            return False
        nswin.setFrame_display_(AppKit.NSMakeRect(x, y, w, h), True)
        return True
    return bool(on_main_sync(run, default=False))


# --------------------------------------------------------------------------- #
#  Hauptfenster beobachten
# --------------------------------------------------------------------------- #
class _MainWindowObserver(Foundation.NSObject):
    def becameKey_(self, note):
        _STATE["main_key"] = True

    def resignedKey_(self, note):
        _STATE["main_key"] = False

    def occlusion_(self, note):
        try:
            state = note.object().occlusionState()
            _STATE["main_visible"] = bool(
                state & AppKit.NSWindowOcclusionStateVisible)
        except Exception:
            pass


def track_main_window(nswin):
    """Merkt sich, ob das Hauptfenster vorne bzw. zu sehen ist. Nur auf dem
    Hauptthread aufrufen."""
    if nswin is None or "main_obs" in _KEEP:
        return
    obs = _MainWindowObserver.alloc().init()
    nc = Foundation.NSNotificationCenter.defaultCenter()
    nc.addObserver_selector_name_object_(
        obs, "becameKey:", AppKit.NSWindowDidBecomeKeyNotification, nswin)
    nc.addObserver_selector_name_object_(
        obs, "resignedKey:", AppKit.NSWindowDidResignKeyNotification, nswin)
    nc.addObserver_selector_name_object_(
        obs, "occlusion:", AppKit.NSWindowDidChangeOcclusionStateNotification,
        nswin)
    _STATE["main_key"] = bool(nswin.isKeyWindow())
    _STATE["main_visible"] = bool(
        nswin.occlusionState() & AppKit.NSWindowOcclusionStateVisible)
    _KEEP["main_obs"] = obs


def main_window_visible():
    return bool(_STATE["main_visible"])


# --------------------------------------------------------------------------- #
#  Fenster und Prozesse anderer Programme
# --------------------------------------------------------------------------- #
_OWNERS = {}


def window_list():
    """Sichtbare Fenster anderer Programme, vorderstes zuerst.

    Fenstertitel liefert macOS nur mit der Freigabe "Bildschirmaufnahme".
    Ohne sie bleibt `title` leer - der Programmname kommt trotzdem."""
    opts = (Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements)
    try:
        raw = Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []
    except Exception:
        return []
    me = os.getpid()
    out = []
    for w in raw:
        try:
            if int(w.get(Quartz.kCGWindowLayer, 0) or 0) != 0:
                continue
            pid = int(w.get(Quartz.kCGWindowOwnerPID, 0) or 0)
            if pid == me:
                continue
            item = {"num": int(w.get(Quartz.kCGWindowNumber, 0) or 0),
                    "pid": pid,
                    "owner": str(w.get(Quartz.kCGWindowOwnerName) or ""),
                    "title": str(w.get(Quartz.kCGWindowName) or "")}
        except Exception:
            continue
        out.append(item)
    if len(_OWNERS) > 400:
        _OWNERS.clear()
    for it in out:
        _OWNERS[it["num"]] = it["owner"]
    return out


def window_owner(num):
    return _OWNERS.get(num, "")


def frontmost_title(own_title):
    """Was gerade vorne ist, als "Programm — Fenstertitel".

    Ist es die App selbst, zaehlt nur das Hauptfenster: ein Klick auf den
    Buddy macht die App nicht zum Vordergrund-Programm (er ist ein Panel, das
    nicht aktiviert) - und falls doch, soll er sich davon nicht selbst
    ausblenden."""
    try:
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    except Exception:
        return ""
    if app is None:
        return ""
    pid = int(app.processIdentifier())
    if pid == os.getpid():
        return own_title if _STATE["main_key"] else ""
    name = str(app.localizedName() or "")
    for w in window_list():
        if w["pid"] == pid:
            return f"{name} — {w['title']}" if w["title"] else name
    return name


def claude_cli_pids():
    """PIDs laufender Claude-Code-CLIs.

    Der Programmname taugt nicht zur Erkennung: der native Installer legt die
    CLI als Datei mit der Versionsnummer ab (".../versions/2.1.266"), npm als
    node-Skript. Die Kommandozeile heisst aber in beiden Faellen "claude".
    Die Desktop-App (Claude.app) ist ein Chat-Programm und zaehlt nicht."""
    try:
        out = subprocess.run(["/bin/ps", "-axww", "-o", "pid=,args="],
                             capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    me = os.getpid()
    pids = []
    for line in out.splitlines():
        pid_s, _, args = line.strip().partition(" ")
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        args = args.strip()
        if pid == me or not args or "/Claude.app/" in args:
            continue
        parts = args.split(" ", 2)
        base = os.path.basename(parts[0])
        if base == "claude":
            pids.append(pid)
        elif base in ("node", "bun") and len(parts) > 1 and (
                os.path.basename(parts[1]) == "claude"
                or "@anthropic-ai/claude-code" in parts[1]):
            pids.append(pid)
    return pids


# --------------------------------------------------------------------------- #
#  Kleinkram: Zwischenablage, Benachrichtigung, Ton
# --------------------------------------------------------------------------- #
def copy_text(text):
    env = dict(os.environ, LC_CTYPE="UTF-8")
    try:
        subprocess.run(["/usr/bin/pbcopy"], input=str(text).encode("utf-8"),
                       env=env, timeout=5, check=True)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def notify(message, title="Clawd"):
    """Systembenachrichtigung. Text und Titel gehen als Argumente an das
    Skript, nicht in seinen Quelltext - dann gibt es nichts zu escapen."""
    script = ["-e", "on run argv",
              "-e", "display notification (item 1 of argv) "
                    "with title (item 2 of argv)",
              "-e", "end run"]
    try:
        subprocess.Popen(["/usr/bin/osascript", *script, str(message), str(title)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def play_sound(name="Glass"):
    def run():
        snd = AppKit.NSSound.soundNamed_(name)
        if snd is not None:
            snd.play()
    on_main(run)


# --------------------------------------------------------------------------- #
#  Session im Terminal fortsetzen
# --------------------------------------------------------------------------- #
_ITERM = "com.googlecode.iterm2"


def _app_installed(bundle_id):
    try:
        ws = AppKit.NSWorkspace.sharedWorkspace()
        return ws.URLForApplicationWithBundleIdentifier_(bundle_id) is not None
    except Exception:
        return False


def _app_running(bundle_id):
    try:
        apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(
            bundle_id)
        return bool(apps and len(apps))
    except Exception:
        return False


def resume_script(workdir, argv, env):
    """Das Startskript fuer das neue Terminalfenster.

    Laeuft als .command-Datei: Terminal und iTerm2 fuehren sie ohne
    Automatisierungs-Freigabe aus, anders als ein AppleScript. Zum Schluss
    bleibt eine Shell im Projektordner offen, wie bei `cmd /k` unter Windows.
    """
    envs = " ".join(f"{k}={shlex.quote(str(v))}" for k, v in env.items())
    cmd = " ".join(shlex.quote(str(a)) for a in argv)
    return "\n".join([
        "#!/bin/sh",
        'rm -f "$0"',
        'export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"',
        "cd " + shlex.quote(workdir) + " || exit 1",
        "env " + envs + " " + cmd,
        'exec "${SHELL:-/bin/zsh}" -l',
        "",
    ])


def open_in_terminal(workdir, argv, env, terminal="auto"):
    """Oeffnet ein Terminalfenster mit `argv` in `workdir`.
    Rueckgabe: None bei Erfolg, sonst eine Fehlermeldung."""
    if terminal == "iterm":
        if not _app_installed(_ITERM):
            return "iTerm2 nicht gefunden."
        app = "iTerm"
    elif terminal == "terminal":
        app = "Terminal"
    else:
        app = "iTerm" if _app_running(_ITERM) else "Terminal"
    try:
        fd, path = tempfile.mkstemp(prefix="csb-resume-", suffix=".command")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(resume_script(workdir, argv, env))
        os.chmod(path, 0o700)
    except OSError as e:
        return str(e)
    try:
        r = subprocess.run(["/usr/bin/open", "-a", app, path],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        r = None
        err = str(e)
    else:
        err = (r.stderr or "").strip() if r.returncode != 0 else ""
    if r is None or r.returncode != 0:
        try:
            os.remove(path)
        except OSError:
            pass
        return err or f"open -a {app} fehlgeschlagen"
    return None


# --------------------------------------------------------------------------- #
#  Autostart (LaunchAgent)
# --------------------------------------------------------------------------- #
def _agent_path():
    return os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents",
                        BUNDLE_ID + ".plist")


def app_bundle_path():
    """Pfad der .app, wenn die App als gebautes Bundle laeuft, sonst None."""
    if not getattr(sys, "frozen", False):
        return None
    parts = os.path.abspath(sys.executable).split(os.sep)
    for i in range(len(parts) - 1, 0, -1):
        if parts[i].endswith(".app"):
            return os.sep.join(parts[: i + 1])
    return None


def set_autostart(enable):
    path = _agent_path()
    if not enable:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return True
    app = app_bundle_path()
    if not app:
        return False
    data = {"Label": BUNDLE_ID,
            "ProgramArguments": ["/usr/bin/open", app],
            "RunAtLoad": True,
            "ProcessType": "Interactive"}
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            plistlib.dump(data, fh)
    except OSError:
        return False
    return True


def is_autostart_enabled():
    """Nur dann an, wenn der Eintrag auf genau diese .app zeigt - nach einem
    Umzug der App soll er neu geschrieben werden."""
    app = app_bundle_path()
    try:
        with open(_agent_path(), "rb") as fh:
            data = plistlib.load(fh)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False
    return bool(app) and app in (data.get("ProgramArguments") or [])


# --------------------------------------------------------------------------- #
#  Schluesselbund
# --------------------------------------------------------------------------- #
_KEYCHAIN = {"blocked": False}
# Abgebrochen, verweigert, keine Rueckfrage moeglich
_KEYCHAIN_DENIED = (-128, -25293, -25308)


def read_keychain_password(service):
    """Liest ein Passwort aus dem Anmelde-Schluesselbund.

    Direkt ueber das Security-Framework, nicht ueber /usr/bin/security: wer
    "Immer erlauben" waehlt, gibt den Eintrag so nur dieser App frei - und
    nicht jedem Skript, das das security-Werkzeug aufruft. Nach einer Absage
    wird bis zum Neustart nicht wieder gefragt."""
    if _KEYCHAIN["blocked"]:
        return None
    try:
        import Security
        query = {Security.kSecClass: Security.kSecClassGenericPassword,
                 Security.kSecAttrService: service,
                 Security.kSecReturnData: True,
                 Security.kSecMatchLimit: Security.kSecMatchLimitOne}
        status, data = Security.SecItemCopyMatching(query, None)
    except Exception:
        return None
    if status == 0 and data is not None:
        try:
            return bytes(data).decode("utf-8")
        except (TypeError, ValueError):
            return None
    if status in _KEYCHAIN_DENIED:
        _KEYCHAIN["blocked"] = True
    return None


def keychain_blocked():
    return bool(_KEYCHAIN["blocked"])


# --------------------------------------------------------------------------- #
#  Nur eine Instanz
# --------------------------------------------------------------------------- #
def single_instance(on_show):
    """True, wenn diese Instanz die erste ist. Eine zweite meldet sich bei
    der ersten ("zeig dich") und bekommt False.

    Das gebaute Bundle schuetzt macOS schon selbst; das hier faengt den
    Start aus dem Quelltext und `open -n` ab."""
    path = os.path.join(tempfile.gettempdir(), "ClaudeSessionBrowser.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(path)
    except OSError:
        try:
            c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            c.settimeout(1.5)
            c.connect(path)
            c.sendall(b"show\n")
            c.close()
            srv.close()
            return False
        except OSError:
            # Uebrig von einem Absturz - wegraeumen und selbst uebernehmen.
            try:
                os.unlink(path)
                srv.bind(path)
            except OSError:
                srv.close()
                return True
    srv.listen(4)

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            try:
                conn.settimeout(1.0)
                msg = conn.recv(64)
            except OSError:
                msg = b""
            finally:
                conn.close()
            if msg.startswith(b"show"):
                try:
                    on_show()
                except Exception:
                    pass

    threading.Thread(target=serve, daemon=True, name="csb-instance").start()
    _KEEP["instance_sock"] = srv

    def cleanup():
        try:
            os.unlink(path)
        except OSError:
            pass
    atexit.register(cleanup)
    return True


# --------------------------------------------------------------------------- #
#  Dock, Beenden, Wiederoeffnen
# --------------------------------------------------------------------------- #
def set_dock_visible(visible):
    """Im Hintergrund ohne Dock-Symbol, wie die App unter Windows ohne
    Taskleisten-Eintrag im Tray sitzt."""
    def run():
        app = AppKit.NSApplication.sharedApplication()
        if visible:
            app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
            app.activateIgnoringOtherApps_(True)
        else:
            app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    on_main(run)


def install_app_delegate(on_reopen, on_terminate):
    """Haengt sich in den App-Delegate von pywebview.

    ⌘Q und Abmelden laufen ueber applicationShouldTerminate - ohne diesen
    Haken wuerde das "Im Hintergrund weiterlaufen" beides abfangen, und die
    App liesse sich nur noch ueber die Menueleiste beenden. Ein Klick aufs
    Dock-Symbol soll das versteckte Fenster zurueckholen."""
    if "delegate" in _KEEP:
        return
    from webview.platforms.cocoa import BrowserView

    class CSBAppDelegate(BrowserView.AppDelegate):
        def applicationShouldTerminate_(self, app):
            try:
                on_terminate()
            except Exception:
                pass
            return objc.super(CSBAppDelegate, self).applicationShouldTerminate_(app)

        def applicationShouldHandleReopen_hasVisibleWindows_(self, app, flag):
            try:
                on_reopen()
            except Exception:
                pass
            return True

    delegate = CSBAppDelegate.alloc().init()
    BrowserView._shared_app_delegate = delegate
    _KEEP["delegate"] = delegate


# --------------------------------------------------------------------------- #
#  Zeichnen
# --------------------------------------------------------------------------- #
_COLORS = {}


def _color(hex_color):
    c = _COLORS.get(hex_color)
    if c is None:
        h = hex_color.lstrip("#")
        try:
            r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
        except ValueError:
            r = g = b = 0.0
        c = AppKit.NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0)
        if len(_COLORS) > 256:
            _COLORS.clear()
        _COLORS[hex_color] = c
    return c


def _poly(points, color):
    p = AppKit.NSBezierPath.bezierPath()
    p.moveToPoint_(points[0])
    for pt in points[1:]:
        p.lineToPoint_(pt)
    p.closePath()
    _color(color).set()
    p.fill()


def _line(x1, y1, x2, y2, color, width):
    p = AppKit.NSBezierPath.bezierPath()
    p.moveToPoint_((x1, y1))
    p.lineToPoint_((x2, y2))
    p.setLineWidth_(width)
    _color(color).set()
    p.stroke()


def _oval(x, y, w, h, color):
    _color(color).set()
    AppKit.NSBezierPath.bezierPathWithOvalInRect_(
        AppKit.NSMakeRect(x, y, w, h)).fill()


def _text(s, x, y, font, color, anchor="w"):
    """Text senkrecht mittig auf y; anchor "w" = links buendig ab x,
    "c" = mittig um x. Nur in gespiegelten Views (isFlipped) benutzen."""
    attrs = {AppKit.NSFontAttributeName: font,
             AppKit.NSForegroundColorAttributeName: _color(color)}
    ns = Foundation.NSString.stringWithString_(s)
    size = ns.sizeWithAttributes_(attrs)
    tx = x - size.width / 2 if anchor == "c" else x
    ns.drawAtPoint_withAttributes_((tx, y - size.height / 2), attrs)


def _draw_cam_frame(s):
    """Der "Cam"-Rahmen um den Buddy - dieselbe Zeichnung wie
    _draw_frame_classic() fuer Tk in claude_sessions.py."""
    w, h = s["w"], s["h"]
    pl, pr, pt, pb = s["pad_l"], s["pad_r"], s["pad_t"], s["pad_b"]
    color, darker, dim = s["color"], s["darker"], s["accent_dim"]
    dark, cream = "#14100e", "#F1EBDD"

    cut = max(4, pl // 2)
    _poly([(cut, 0), (w - cut, 0), (w, cut), (w, h - cut),
           (w - cut, h), (cut, h), (0, h - cut), (0, cut)], color)

    cam_l, cam_t, cam_r, cam_b = pl, pt, w - pr, h - pb + 2
    _color(dark).set()
    AppKit.NSRectFill(AppKit.NSMakeRect(cam_l, cam_t, cam_r - cam_l, cam_b - cam_t))

    corner = max(4, pl // 2)
    for cx, cy, dx, dy in ((cam_l, cam_t, 1, 1), (cam_r, cam_t, -1, 1),
                           (cam_l, cam_b, 1, -1), (cam_r, cam_b, -1, -1)):
        _line(cx, cy, cx + dx * corner, cy, color, 2)
        _line(cx, cy, cx, cy + dy * corner, color, 2)

    stripe_w = max(6, w // 8)
    top_y = pt // 2
    for dx in (-stripe_w - 4, 0, stripe_w + 4):
        cx = w // 2 + dx
        _line(cx - 3, top_y, cx + 3, top_y, darker, 2)

    plate_top, plate_bot = h - pb + 3, h - 3
    plate_half = min(w // 2 - 6, max(24, int(w * 0.36)))
    pcx = w // 2
    _poly([(pcx - plate_half + 6, plate_top), (pcx + plate_half - 6, plate_top),
           (pcx + plate_half, plate_bot), (pcx - plate_half, plate_bot)], darker)
    _line(pcx - plate_half + 8, plate_top + 1, pcx + plate_half - 8,
          plate_top + 1, dim, 1)
    font_size = max(6, min(11, (plate_bot - plate_top) - 4))
    _text(s["label"], pcx, (plate_top + plate_bot) / 2,
          AppKit.NSFont.boldSystemFontOfSize_(font_size), cream, anchor="c")

    dot_r = max(2, pt // 3)
    _oval(w - pr - 2 * dot_r - 2, pt // 2 - dot_r, 2 * dot_r, 2 * dot_r, "#ff3a5a")


# --------------------------------------------------------------------------- #
#  Schwebende Fenster
# --------------------------------------------------------------------------- #
_ALL_SPACES = (AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
               | AppKit.NSWindowCollectionBehaviorStationary
               | AppKit.NSWindowCollectionBehaviorIgnoresCycle
               | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary)
_PANEL_MASK = (AppKit.NSWindowStyleMaskBorderless
               | AppKit.NSWindowStyleMaskNonactivatingPanel)


class _Panel(AppKit.NSPanel):
    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


def _floating_panel(rect, level):
    """Rahmenloses, durchsichtiges Panel ueber allen Fenstern und auf allen
    Schreibtischen. Nicht-aktivierend: ein Klick darauf holt die App nicht
    nach vorn - das Terminal behaelt den Fokus."""
    p = _Panel.alloc().initWithContentRect_styleMask_backing_defer_(
        rect, _PANEL_MASK, AppKit.NSBackingStoreBuffered, False)
    p.setOpaque_(False)
    p.setBackgroundColor_(AppKit.NSColor.clearColor())
    p.setHasShadow_(False)
    p.setLevel_(level)
    p.setCollectionBehavior_(_ALL_SPACES)
    # Panels verschwinden sonst, sobald die App nicht mehr vorne ist.
    p.setHidesOnDeactivate_(False)
    p.setReleasedWhenClosed_(False)
    p.setBecomesKeyOnlyIfNeeded_(True)
    p.setWorksWhenModal_(True)
    p.setMovable_(False)
    return p


def _tracking(view):
    opts = (AppKit.NSTrackingMouseEnteredAndExited
            | AppKit.NSTrackingActiveAlways
            | AppKit.NSTrackingInVisibleRect)
    area = AppKit.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
        AppKit.NSZeroRect, opts, view, None)
    view.addTrackingArea_(area)


# ---- Buddy ----------------------------------------------------------------
class _BuddyView(AppKit.NSView):
    def initWithFrame_(self, frame):
        self = objc.super(_BuddyView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.csb_handler = None
        self.csb_pixels = None      # 400 Eintraege: Hex-Farbe oder None
        self.csb_scale = 4
        self.csb_pad = (0, 0)
        self.csb_frame = None       # Zeichen-Auftrag fuer den Cam-Rahmen
        self.csb_bg = None
        self.csb_ring = 0
        self.csb_drag = None
        _tracking(self)
        return self

    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True

    def drawRect_(self, rect):
        if self.csb_frame:
            _draw_cam_frame(self.csb_frame)
        ox, oy = self.csb_pad
        sc = self.csb_scale
        if self.csb_bg:
            _color(self.csb_bg).set()
            AppKit.NSRectFill(AppKit.NSMakeRect(ox, oy, 20 * sc, 20 * sc))
        px = self.csb_pixels
        if px:
            last = None
            for i, c in enumerate(px):
                if c is None:
                    continue
                if c != last:
                    _color(c).set()
                    last = c
                AppKit.NSRectFill(AppKit.NSMakeRect(
                    ox + (i % 20) * sc, oy + (i // 20) * sc, sc, sc))
        if self.csb_ring:
            _color("#ffd66b").set()
            p = AppKit.NSBezierPath.bezierPathWithRect_(
                AppKit.NSInsetRect(self.bounds(), 1.5, 1.5))
            p.setLineWidth_(self.csb_ring)
            p.stroke()

    def mouseDown_(self, event):
        h = self.csb_handler
        if h is None:
            return
        if (event.clickCount() >= 2
                or event.modifierFlags() & AppKit.NSEventModifierFlagControl):
            self.csb_drag = None
            h.hide_toggle()
            return
        loc = AppKit.NSEvent.mouseLocation()
        o = self.window().frame().origin
        self.csb_drag = [loc.x, loc.y, o.x, o.y, False]

    def mouseDragged_(self, event):
        d, h = self.csb_drag, self.csb_handler
        if not d or h is None:
            return
        loc = AppKit.NSEvent.mouseLocation()
        win = self.window()
        fh = win.frame().size.height
        x, y = from_cocoa(d[2] + loc.x - d[0], d[3] + loc.y - d[1], fh)
        sx, sy = h.snap(int(round(x)), int(round(y)))
        cx, cy = to_cocoa(sx, sy, fh)
        win.setFrameOrigin_(AppKit.NSMakePoint(cx, cy))
        d[4] = True

    def mouseUp_(self, event):
        d, h = self.csb_drag, self.csb_handler
        self.csb_drag = None
        if not d or h is None or not d[4]:
            return
        f = self.window().frame()
        x, y = from_cocoa(f.origin.x, f.origin.y, f.size.height)
        h.released(int(round(x)), int(round(y)))

    def rightMouseDown_(self, event):
        if self.csb_handler is not None:
            self.csb_handler.hide_toggle()

    def mouseEntered_(self, event):
        if self.csb_handler is not None:
            self.csb_handler.enter()

    def mouseExited_(self, event):
        if self.csb_handler is not None:
            self.csb_handler.leave()


class BuddyWindow:
    """Das Buddy-Fenster. Alle Methoden nur auf dem Hauptthread aufrufen.

    `handler` bekommt die Maus-Ereignisse: snap(x, y) -> (x, y),
    released(x, y), hide_toggle(), enter(), leave()."""

    def __init__(self, handler):
        rect = AppKit.NSMakeRect(0, 0, 80, 80)
        self.panel = _floating_panel(rect, AppKit.NSStatusWindowLevel)
        self.view = _BuddyView.alloc().initWithFrame_(rect)
        self.view.csb_handler = handler
        self.panel.setContentView_(self.view)
        self.panel.setAlphaValue_(0.0)

    def apply(self, ui):
        p, v = self.panel, self.view
        if "geometry" in ui:
            x, y, w, h = ui["geometry"]
            cx, cy = to_cocoa(x, y, h)
            p.setFrame_display_(AppKit.NSMakeRect(cx, cy, w, h), True)
        if "move" in ui:
            x, y = ui["move"]
            cx, cy = to_cocoa(x, y, p.frame().size.height)
            p.setFrameOrigin_(AppKit.NSMakePoint(cx, cy))
        if "alpha" in ui:
            p.setAlphaValue_(max(0.0, min(1.0, float(ui["alpha"]))))
        redraw = False
        for key in ("csb_pixels", "csb_scale", "csb_pad", "csb_frame",
                    "csb_bg", "csb_ring"):
            if key in ui:
                setattr(v, key, ui[key])
                redraw = True
        if redraw:
            v.setNeedsDisplay_(True)
        if ui.get("visible") is True or ui.get("raise"):
            p.orderFrontRegardless()
        elif ui.get("visible") is False:
            p.orderOut_(None)

    def set_alpha(self, a):
        self.panel.setAlphaValue_(max(0.0, min(1.0, float(a))))

    def close(self):
        self.view.csb_handler = None
        self.panel.orderOut_(None)
        self.panel.close()


# ---- Raster zum Platzieren ------------------------------------------------
class _GridView(AppKit.NSView):
    def isFlipped(self):
        return True

    def drawRect_(self, rect):
        b = self.bounds()
        w, h = int(b.size.width), int(b.size.height)
        _color("#0a0b0d").set()
        AppKit.NSRectFill(b)
        for step, col in ((20, "#3a3d42"), (100, "#5c6068")):
            p = AppKit.NSBezierPath.bezierPath()
            p.setLineWidth_(1.0)
            for x in range(0, w, step):
                p.moveToPoint_((x + 0.5, 0))
                p.lineToPoint_((x + 0.5, h))
            for y in range(0, h, step):
                p.moveToPoint_((0, y + 0.5))
                p.lineToPoint_((w, y + 0.5))
            _color(col).set()
            p.stroke()


class PlaceOverlay:
    """Abgedunkeltes Raster ueber allen Bildschirmen, waehrend der Buddy
    platziert wird. Die Maus geht hindurch - sonst finge es den Griff nach
    dem Buddy ab. ESC bricht ab. Nur auf dem Hauptthread anlegen."""

    def __init__(self, on_escape):
        self.panels = []
        for scr in AppKit.NSScreen.screens() or []:
            f = scr.frame()
            p = _floating_panel(f, AppKit.NSStatusWindowLevel - 2)
            p.setIgnoresMouseEvents_(True)
            p.setAlphaValue_(0.42)
            p.setContentView_(_GridView.alloc().initWithFrame_(
                AppKit.NSMakeRect(0, 0, f.size.width, f.size.height)))
            p.setFrame_display_(f, True)
            p.orderFrontRegardless()
            self.panels.append(p)

        def on_key(event):
            if event.keyCode() == 53:      # ESC
                on_escape()
                return None
            return event

        self.monitor = AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            AppKit.NSEventMaskKeyDown, on_key)

    def close(self):
        if self.monitor is not None:
            AppKit.NSEvent.removeMonitor_(self.monitor)
            self.monitor = None
        for p in self.panels:
            p.orderOut_(None)
            p.close()
        self.panels = []


# ---- Karte "Limit ist zurueck" --------------------------------------------
class _CardView(AppKit.NSView):
    def initWithFrame_(self, frame):
        self = objc.super(_CardView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.csb_owner = None
        self.csb_hover = False
        self.csb_title = ""
        self.csb_sub = ""
        _tracking(self)
        return self

    def isFlipped(self):
        return True

    def acceptsFirstMouse_(self, event):
        return True

    def resetCursorRects(self):
        self.addCursorRect_cursor_(self.bounds(), AppKit.NSCursor.pointingHandCursor())

    def drawRect_(self, rect):
        b = self.bounds()
        w, h = b.size.width, b.size.height
        _color("#efeadf" if self.csb_hover else "#f5f2eb").set()
        AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            b, 12, 12).fill()
        cy = h / 2
        _oval(24 - 9, cy - 9, 18, 18, "#e8a889")
        _oval(24 - 5, cy - 5, 10, 10, "#d97757")
        _text(self.csb_title, 44, 28,
              AppKit.NSFont.systemFontOfSize_weight_(13, AppKit.NSFontWeightSemibold),
              "#1a1815")
        _text(self.csb_sub, 44, 50, AppKit.NSFont.systemFontOfSize_(12), "#6b6660")
        _text("✕", w - 14, 13, AppKit.NSFont.systemFontOfSize_(11),
              "#1a1815" if self.csb_hover else "#8a857f", anchor="c")

    def mouseDown_(self, event):
        if self.csb_owner is not None:
            self.csb_owner.dismiss()

    def mouseEntered_(self, event):
        self.csb_hover = True
        self.setNeedsDisplay_(True)

    def mouseExited_(self, event):
        self.csb_hover = False
        self.setNeedsDisplay_(True)


class ResetCard:
    """Karte oben rechts: das Claude-Limit ist zurueck. Bleibt stehen, bis
    jemand draufklickt - verpassen soll man sie nicht. Gleiche Schnittstelle
    wie LimitResetToast in claude_sessions.py.

    `dodge(x, y, w, h, avoid, screen_bottom)` schiebt sie aus dem Weg des
    Buddys."""

    W, H = 300, 78

    def __init__(self, dodge=None):
        self._alive = False
        self._dodge = dodge
        self._panel = None

    def show(self, title=None, subtitle=None, avoid=None):
        if self._alive:
            return
        self._alive = True
        on_main(self._build, title or "", subtitle or "", avoid)
        play_sound("Glass")

    def _build(self, title, subtitle, avoid):
        scr = screens()
        if not scr:
            self._alive = False
            return
        s0 = scr[0]
        x = s0["right"] - self.W - 16
        y = s0["top"] + 12
        if self._dodge:
            y = self._dodge(x, y, self.W, self.H, avoid, s0["bottom"])
        start_x = s0["frame"][2] + 20
        cx, cy = to_cocoa(start_x, y, self.H)
        rect = AppKit.NSMakeRect(cx, cy, self.W, self.H)
        p = _floating_panel(rect, AppKit.NSStatusWindowLevel)
        p.setHasShadow_(True)
        v = _CardView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, self.W, self.H))
        v.csb_owner = self
        v.csb_title, v.csb_sub = title, subtitle
        p.setContentView_(v)
        p.setAlphaValue_(0.0)
        p.orderFrontRegardless()
        p.invalidateShadow()
        self._panel = p

        tx, ty = to_cocoa(x, y, self.H)
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(0.3)
        p.animator().setFrame_display_(AppKit.NSMakeRect(tx, ty, self.W, self.H), True)
        p.animator().setAlphaValue_(1.0)
        AppKit.NSAnimationContext.endGrouping()

    def dismiss(self):
        p = self._panel
        if p is None:
            return
        self._panel = None
        AppKit.NSAnimationContext.beginGrouping()
        AppKit.NSAnimationContext.currentContext().setDuration_(0.2)
        p.animator().setAlphaValue_(0.0)
        AppKit.NSAnimationContext.endGrouping()

        def gone():
            p.contentView().csb_owner = None
            p.orderOut_(None)
            p.close()
            self._alive = False
        AppHelper.callLater(0.25, gone)


# ---- Symbol in der Menueleiste --------------------------------------------
class _MenuTarget(Foundation.NSObject):
    def fire_(self, sender):
        cb = self.csb_callbacks.get(int(sender.tag()))
        if cb:
            cb()

    def menuNeedsUpdate_(self, menu):
        # Beschriftung beim Aufklappen neu holen: nach einem Sprachwechsel
        # stimmt das Menue sofort.
        for i, (label, _cb) in enumerate(self.csb_items):
            item = menu.itemWithTag_(i)
            if item is not None:
                item.setTitle_(label())


class StatusItemTray:
    """Gegenstueck zum pystray-Icon unter Windows. `items` ist eine Liste aus
    (Beschriftung als Funktion, Rueckruf); die Rueckrufe laufen auf dem
    Hauptthread. `icon_png` wird als Vorlage gezeichnet, also in der Farbe
    der Menueleiste."""

    def __init__(self, icon_png, tooltip, items):
        self._icon_png = icon_png
        self._tooltip = tooltip
        self._items = items
        self._status = None
        self._target = None

    def start(self):
        on_main(self._create)

    def _create(self):
        if self._status is not None:
            return
        st = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
            AppKit.NSVariableStatusItemLength)
        btn = st.button()
        img = None
        if self._icon_png:
            data = Foundation.NSData.dataWithBytes_length_(
                self._icon_png, len(self._icon_png))
            img = AppKit.NSImage.alloc().initWithData_(data)
        if img is not None:
            img.setSize_((18, 18))
            img.setTemplate_(True)
            btn.setImage_(img)
        else:
            btn.setTitle_("Clawd")
        btn.setToolTip_(self._tooltip)

        target = _MenuTarget.alloc().init()
        target.csb_items = self._items
        target.csb_callbacks = {i: cb for i, (_l, cb) in enumerate(self._items)}
        menu = AppKit.NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        for i, (label, _cb) in enumerate(self._items):
            item = AppKit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                label(), "fire:", "")
            item.setTarget_(target)
            item.setTag_(i)
            menu.addItem_(item)
        menu.setDelegate_(target)
        st.setMenu_(menu)
        self._status, self._target = st, target

    def notify(self, message, title="Clawd"):
        notify(message, title)

    def stop(self):
        def run():
            if self._status is not None:
                AppKit.NSStatusBar.systemStatusBar().removeStatusItem_(self._status)
                self._status = None
        on_main(run)


# --------------------------------------------------------------------------- #
#  Bluetooth-Suche (Clawdmeter)
# --------------------------------------------------------------------------- #
def ble_scan(match, timeout=5.0):
    """Sucht BLE-Geraete in Reichweite. `match(name, service_uuids)` waehlt
    aus. Rueckgabe: [{name, address}] - die Adresse ist unter macOS eine
    CoreBluetooth-UUID, keine MAC-Adresse."""
    import asyncio
    from bleak import BleakScanner

    async def run():
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        out = []
        for addr, (dev, adv) in found.items():
            name = (adv.local_name or dev.name or "").strip()
            uuids = [str(u).lower() for u in (adv.service_uuids or [])]
            if match(name, uuids):
                out.append({"name": name or addr, "address": str(addr).upper()})
        return out

    return asyncio.run(run())
