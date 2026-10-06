#!/usr/bin/env python
"""
Interactive controller for the Argus SW-0816 PDU (works on Python 2.7 and 3.x,
no third-party packages).

Copyright (c) 2026 Luuk Isbouts

  argus_pdu.py                      interactive menu (default)
  argus_pdu.py status [--json]      print status and exit
  argus_pdu.py on|off|onoff N [N ...] [--yes]   scripted control (N = 0..7)
  argus_pdu.py config               show outlet names and on/off delays
  argus_pdu.py rename N "New name" [--dry-run]  rename outlet N (0..7)

Interactive keys:
  Up/Down (or k/j)  move        Enter  toggle the selected outlet
  F2  rename the selected outlet          F3  connect to another PDU (type its IP)
  F4  turn off ALL outlets (asks first)   F5  refresh now (also reloads names)
  1-8 jump to an outlet
  q / Esc  quit (or select "Quit" + Enter)
  The status refreshes automatically every 10 seconds (--refresh N to change).
  Switching an outlet ON or OFF asks for confirmation (--no-confirm to skip).

PDU address: pass --host (or set PDU_HOST), or just start the script - on the
first run it asks for the IP address, and remembers the last working one in
~/.argus_pdu.json (the address only, never the password).

Login: this unit wants HTTP basic auth. Use --user/--password or
PDU_USER/PDU_PASS, or just start the script: if the PDU won't hand out the
outlet names without a login, a login screen appears before the menu.

Endpoints (from HAR / cURL captures of the web UI):
  GET  /status.xml
  GET  /control_outlet.htm?outletN=1&op=X&submit=Apply   (op 0=ON, 1=OFF, 2=ON/OFF)
  GET  /config_PDU.htm                                   (current names + delays)
  POST /config_PDU.htm   otlt0=..&ondly0=..&ofdly0=..&otlt1=..  (all 8 outlets)
"""
from __future__ import print_function

import argparse
import base64
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET

try:  # Python 3
    from urllib.parse import quote_plus, urlencode
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    from html import unescape
except ImportError:  # Python 2
    from urllib import quote_plus, urlencode
    from urllib2 import Request, urlopen, HTTPError
    from HTMLParser import HTMLParser
    unescape = HTMLParser().unescape

try:
    input = raw_input  # Python 2
except NameError:
    pass

COPYRIGHT = "Copyright (c) 2026 Luuk Isbouts"
DEFAULT_HOST = "10.20.24.200"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".argus_pdu.json")

OPS = {"on": 0, "off": 1, "onoff": 2}
NUM_OUTLETS = 8
DEFAULT_MAX_NAME = 16  # used only if the config page doesn't declare a maxlength


_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?(:\d{1,5})?$")


def normalize_host(text):
    """'http://10.0.0.5/' -> '10.0.0.5'; None if it isn't a plausible IP/hostname."""
    text = re.sub(r"^https?://", "", (text or "").strip(), flags=re.I)
    text = text.split("/")[0].strip()
    return str(text) if _HOST_RE.match(text) else None


def load_saved_host():
    try:
        with open(CONFIG_PATH) as f:
            return normalize_host(json.load(f).get("host"))
    except Exception:
        return None


def save_host(host):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump({"host": host}, f)
    except Exception:
        pass  # a read-only home directory shouldn't stop the tool


class ConfigError(Exception):
    pass


class LoginCancelled(Exception):
    pass


# --------------------------------------------------------------------------
# HTML form parsing (for /config_PDU.htm)
# --------------------------------------------------------------------------
_ATTR = re.compile(r'([\w-]+)\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>"\']+))')


def _attrs(tag):
    out = {}
    for m in _ATTR.finditer(tag):
        for g in m.groups()[1:]:
            if g is not None:
                out[m.group(1).lower()] = g
                break
    return out


def parse_form_fields(html):
    """Return ({field name: current value}, {field name: maxlength})."""
    values, maxlen = {}, {}
    for m in re.finditer(r"<input\b[^>]*>", html, re.I | re.S):
        a = _attrs(m.group(0))
        name = a.get("name")
        if not name or a.get("type", "text").lower() in (
                "submit", "button", "reset", "image", "checkbox", "radio"):
            continue
        values[name] = unescape(a.get("value", ""))
        if a.get("maxlength", "").isdigit():
            maxlen[name] = int(a["maxlength"])
    for m in re.finditer(r"<select\b([^>]*)>(.*?)</select>", html, re.I | re.S):
        name = _attrs(m.group(1)).get("name")
        if not name:
            continue
        chosen = None
        for o in re.finditer(r"<option\b([^>]*)>([^<]*)", m.group(2), re.I | re.S):
            val = _attrs(o.group(1)).get("value", o.group(2).strip())
            if chosen is None or re.search(r"\bselected\b", o.group(1), re.I):
                chosen = val
        if chosen is not None:
            values[name] = unescape(chosen)
    return values, maxlen


def _enc(value):
    """Form-encode a value byte-for-byte (the PDU pages are gb2312/latin-1)."""
    if not isinstance(value, bytes):
        try:
            value = value.encode("latin-1")
        except UnicodeEncodeError:
            raise ConfigError("name contains characters the PDU page can't round-trip")
    return quote_plus(value)


def ascii_safe(text):
    return str(text.encode("ascii", "replace").decode("ascii")).strip()


def validate_name(name, maxlen):
    if not name:
        return "Name can't be empty."
    if any(not 32 <= ord(ch) < 127 for ch in name):
        return "Use plain ASCII characters only."
    if len(name) > maxlen:
        return "Name too long (max %d characters)." % maxlen
    return None


# --------------------------------------------------------------------------
# PDU client
# --------------------------------------------------------------------------
class PDU(object):
    def __init__(self, host, user=None, password=None, timeout=5):
        self.set_host(host)
        self.timeout = timeout
        self.auth = None
        self.user = ""
        if user:
            self.set_credentials(user, password)

    def set_host(self, host):
        self.host = host
        self.base = "http://" + host

    def set_credentials(self, user, password):
        self.user = user
        creds = "{0}:{1}".format(user, password or "").encode("utf-8")
        self.auth = "Basic " + base64.b64encode(creds).decode("ascii")

    def _request(self, path, params=None, body=None):
        url = self.base + path
        if params:
            url += "?" + urlencode(params)  # same parameter order as the web UI
        req = Request(url, data=body.encode("ascii") if body is not None else None)
        if body is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
            req.add_header("Referer", url)
        if self.auth:
            req.add_header("Authorization", self.auth)
        r = urlopen(req, timeout=self.timeout)
        try:
            return r.read()
        finally:
            r.close()

    def _get(self, path, params=None):
        return self._request(path, params)

    def status(self):
        root = ET.fromstring(self._get("/status.xml").strip())

        def g(tag):
            return (root.findtext(tag) or "").strip()

        return {
            "outlets": dict((i, g("outletStat%d" % i) == "on")
                            for i in range(NUM_OUTLETS)),
            "current_a": float(g("curBan") or 0),
            "temperature": float(g("tempBan") or 0),
            "humidity": float(g("humBan") or 0),
            "state": g("statBan"),
        }

    def names(self):
        """Outlet names as configured in the PDU web UI ({} if unavailable)."""
        html = self._get("/control_outlet.htm").decode("latin-1")
        found = {}
        pattern = (r'<td[^>]*>\s*([^<]*?)\s*</td>\s*<td[^>]*>\s*'
                   r'<span\s+id\s*=\s*"outletStat(\d)"')
        for name, idx in re.findall(pattern, html):
            name = ascii_safe(name)
            if name:
                found[int(idx)] = name
        return found

    def set(self, outlets, action):
        if action not in OPS:
            raise ValueError("action must be one of %s" % list(OPS))
        outlets = sorted(set(outlets))
        for o in outlets:
            if not 0 <= o < NUM_OUTLETS:
                raise ValueError("outlet must be 0..%d, got %s" % (NUM_OUTLETS - 1, o))
        params = [("outlet%d" % o, "1") for o in outlets]
        params += [("op", str(OPS[action])), ("submit", "Apply")]
        self._get("/control_outlet.htm", params)

    # -- configuration (names + delays) ------------------------------------
    @staticmethod
    def _field_order():
        order = []
        for i in range(NUM_OUTLETS):
            order += ["otlt%d" % i, "ondly%d" % i, "ofdly%d" % i]
        return order

    def read_config(self):
        """Current config as {'pairs': [(field, value)...], 'maxlen': int}.
        Raises ConfigError if the page doesn't look like we expect."""
        html = self._get("/config_PDU.htm").decode("latin-1")
        values, maxlen = parse_form_fields(html)
        order = self._field_order()
        missing = [f for f in order if f not in values]
        if missing:
            raise ConfigError("config page is missing expected fields (%s...) - "
                              "not changing anything" % ", ".join(missing[:3]))
        name_max = min([maxlen[f] for f in order if f.startswith("otlt") and f in maxlen]
                       or [DEFAULT_MAX_NAME])
        return {"pairs": [(f, values[f]) for f in order], "maxlen": name_max}

    def rename(self, idx, new_name, cfg=None, dry_run=False):
        """Rename outlet idx. Re-sends every other value unchanged, then reads
        the page back to verify. Returns the list of (field, value) sent."""
        if not 0 <= idx < NUM_OUTLETS:
            raise ValueError("outlet must be 0..%d" % (NUM_OUTLETS - 1))
        cfg = cfg or self.read_config()
        problem = validate_name(new_name, cfg["maxlen"])
        if problem:
            raise ConfigError(problem)
        key = "otlt%d" % idx
        sent = [(f, new_name if f == key else v) for f, v in cfg["pairs"]]
        if dry_run:
            return sent
        body = "&".join("%s=%s" % (f, _enc(v)) for f, v in sent)
        self._request("/config_PDU.htm", body=body)

        expected = dict(sent)
        for _ in range(4):  # the device may need a moment to store the change
            time.sleep(0.5)
            actual = dict(self.read_config()["pairs"])
            diff = [f for f in expected if actual.get(f) != expected[f]]
            if not diff:
                return sent
        raise ConfigError("PDU did not store the change as expected (differs: %s). "
                          "Check the web UI." % ", ".join(diff))


def names_from_pairs(pairs):
    d = dict(pairs)
    return dict((i, ascii_safe(d["otlt%d" % i])) for i in range(NUM_OUTLETS)
                if ascii_safe(d["otlt%d" % i]))


# --------------------------------------------------------------------------
# Terminal helpers (colour, clear screen, single key input)
# --------------------------------------------------------------------------
def enable_ansi():
    """True if the terminal understands ANSI colours (turns them on in
    Windows 10+ consoles, where Python 2.7 does not do it by itself)."""
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(k32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


class Term(object):
    def __init__(self):
        self.color = enable_ansi()

    def c(self, text, *codes):
        if not self.color:
            return text
        return "\033[%sm%s\033[0m" % (";".join(codes), text)

    def clear(self):
        if self.color:
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
        else:
            os.system("cls" if os.name == "nt" else "clear")


_KEYMODE = {"active": False}


class keymode(object):
    """Hold the terminal in per-key mode (Unix) for a whole form, so quickly
    typed or pasted characters aren't lost when the mode is switched back and
    forth between key presses. A no-op on Windows."""

    def __enter__(self):
        self.owner = False
        if os.name != "nt" and sys.stdin.isatty() and not _KEYMODE["active"]:
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
            _KEYMODE["active"] = self.owner = True
        return self

    def __exit__(self, *exc):
        if self.owner:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)
            _KEYMODE["active"] = False
        return False


def read_key(timeout=None):
    """Wait for one key; returns 'up', 'down', 'enter', 'esc', 'quit', 'f2',
    'f4', 'f5' or the character typed. Returns None if `timeout` seconds pass
    without a key press."""
    if os.name == "nt":
        import msvcrt
        if timeout is not None:
            end = time.time() + timeout
            while not msvcrt.kbhit():
                if time.time() >= end:
                    return None
                time.sleep(0.05)

        def getch():
            ch = msvcrt.getch()
            if isinstance(ch, bytes) and not isinstance(ch, str):
                ch = ch.decode("latin-1")
            return ch

        ch = getch()
        if ch in ("\x00", "\xe0"):  # special key prefix
            return {"H": "up", "P": "down", "<": "f2", "=": "f3", ">": "f4", "?": "f5"}.get(getch(), "")
        if ch == "\r":
            return "enter"
        if ch == "\x03":
            return "quit"
        if ch == "\x1b":
            return "esc"
        return ch

    import select
    import termios
    import tty
    fd = sys.stdin.fileno()
    held = _KEYMODE["active"]  # a keymode() block already set per-key mode
    old = None if held else termios.tcgetattr(fd)
    try:
        if not held:
            tty.setcbreak(fd)
        if timeout is not None and not select.select([fd], [], [], timeout)[0]:
            return None
        ch = os.read(fd, 1).decode("latin-1")
        if ch == "\x1b":
            def nxt():
                if select.select([fd], [], [], 0.05)[0]:
                    return os.read(fd, 1).decode("latin-1")
                return ""

            c = nxt()
            if not c:
                return "esc"  # a lone Esc key press
            seq = c
            if c == "[":  # CSI sequence: read up to its final byte
                while True:
                    c = nxt()
                    if not c:
                        break
                    seq += c
                    if c.isalpha() or c == "~":
                        break
            elif c == "O":  # SS3 sequence (xterm F1-F4, application cursor keys)
                seq += nxt()
            return {"[A": "up", "OA": "up", "[B": "down", "OB": "down",
                    "OQ": "f2", "[12~": "f2", "[[B": "f2",
                    "OR": "f3", "[13~": "f3", "[[C": "f3",
                    "OS": "f4", "[14~": "f4", "[[D": "f4",
                    "[15~": "f5", "[[E": "f5"}.get(seq, "")
        if ch in ("\r", "\n"):
            return "enter"
        return ch
    finally:
        if not held:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


# --------------------------------------------------------------------------
# Interactive UI
# --------------------------------------------------------------------------
REFRESH_SECONDS = 10
QUIT_ITEM = NUM_OUTLETS         # the menu row after the outlets
NUM_ITEMS = NUM_OUTLETS + 1


def render(t, pdu, names, st, err, sel, msg, updated="", refresh_s=REFRESH_SECONDS):
    t.clear()
    print(t.c(" Argus SW-0816 PDU ", "1", "36") + "  " + t.c(pdu.host, "90")
          + t.c("   updated %s (auto-refresh %ds)" % (updated, refresh_s), "90"))
    print()
    if st:
        state_col = "32" if st["state"] == "normal" else "1;31"
        print("  Current %s   Temp %s   Humidity %s   State %s" % (
            t.c("%.1f A" % st["current_a"], "1"),
            t.c("%.0f" % st["temperature"], "1"),
            t.c("%.0f%%" % st["humidity"], "1"),
            t.c(st["state"] or "?", state_col)))
    else:
        print("  " + t.c("Cannot read PDU status: %s" % err, "1", "31"))
    print()

    for i in range(NUM_OUTLETS):
        label = "%d  %-26s" % (i + 1, names.get(i, "Outlet %d" % (i + 1)))
        if st is None:
            tag = t.c("[ ?  ]", "90")
        elif st["outlets"][i]:
            tag = t.c("[ ON ]", "1", "32")
        else:
            tag = t.c("[ OFF]", "31")
        if i == sel:
            print(t.c("> ", "1", "33") + t.c(label, "1", "33") + " " + tag)
        else:
            print("  " + label + " " + tag)

    print()
    if sel == QUIT_ITEM:
        print(t.c("> Quit", "1", "33"))
    else:
        print("  Quit")
    print()
    print(t.c("  Up/Down move | Enter toggle | F2 rename | F3 change PDU | "
              "F4 turn off all | F5 refresh | 1-8 jump | q quit", "90"))
    if msg:
        print()
        print("  " + t.c(msg[0], *msg[1:]))
    print()
    print("  " + t.c(COPYRIGHT, "90"))
    sys.stdout.flush()


BANNER = [
    r"    _    ____   ____ _   _ ____  ",
    r"   / \  |  _ \ / ___| | | / ___| ",
    r"  / _ \ | |_) | |  _| | | \___ \ ",
    r" / ___ \|  _ <| |_| | |_| |___) |",
    r"/_/   \_\_| \_\\____|\___/|____/ ",
]
BANNER_COLORS = ["38;5;51", "38;5;45", "38;5;39", "38;5;33", "38;5;27"]
FIELD_WIDTH = 24
LABEL_WIDTH = 13
BOX_INNER = 50
BAD_ADDRESS = ("Enter a valid IP address or hostname, e.g. 10.20.24.200", "1", "31")


def _field_box(t, text, active, secret):
    shown = ("*" * len(text) if secret else text)[-(FIELD_WIDTH - 1):]
    pad = FIELD_WIDTH - len(shown)
    if active:
        cursor = t.c(" ", "7") if t.color else "_"
        inner = shown + cursor + " " * (pad - 1)
        return t.c("[", "1", "33") + " " + inner + " " + t.c("]", "1", "33")
    return t.c("[", "90") + " " + shown + " " * pad + " " + t.c("]", "90")


def draw_login(t, pdu, labels, fields, cur, note, ask_host):
    """Draw the sign-in form. `note` is (text, color codes...) or None."""
    t.clear()
    print()
    for line, col in zip(BANNER, BANNER_COLORS):
        print("      " + t.c(line, "1", col))
    print("      " + t.c("Intelligent PDU  -  SW-0816", "1", "37")
          + ("" if ask_host else t.c("   " + pdu.host, "90")))
    print()
    edge = "    " + t.c("+" + "-" * BOX_INNER + "+", "36")
    bar = t.c("|", "36")

    def row(content_colored, plain_len):
        return "    " + bar + content_colored + " " * (BOX_INNER - plain_len) + bar

    print(edge)
    title = "  CONNECT & SIGN IN" if ask_host else "  SIGN IN"
    print(row(t.c(title, "1", "36"), len(title)))
    print(edge)
    print(row("", 0))
    for n, label in enumerate(labels):
        secret = n == len(labels) - 1
        lab = t.c("   " + label.ljust(LABEL_WIDTH), "1" if cur == n else "37")
        plain = 3 + LABEL_WIDTH + (FIELD_WIDTH + 4)
        print(row(lab + _field_box(t, fields[n], cur == n, secret), plain))
        print(row("", 0))
    print(edge)
    print()
    if note:
        print("    " + t.c(note[0], *note[1:]))
        print()
    print("    " + t.c("Enter next / sign in  |  Tab switch field  |  Esc quit", "90"))
    print()
    print("    " + t.c(COPYRIGHT, "90"))
    sys.stdout.flush()


def ask_credentials(t, pdu, note=None, ask_host=False, focus=None):
    """Full-screen form with masked password. Returns (host, user, password)
    - host is None unless ask_host - or None if the user pressed Esc."""
    labels = (["PDU address"] if ask_host else []) + ["Username", "Password"]
    base = 1 if ask_host else 0  # index of the username field
    fields = ([pdu.host or ""] if ask_host else []) + [pdu.user or "", ""]
    cur = focus if focus is not None else (base + 1 if fields[base] else base)
    last = len(fields) - 1
    with keymode():
        while True:
            draw_login(t, pdu, labels, fields, cur, note, ask_host)
            key = read_key()
            if key in ("esc", "quit"):
                return None
            if key == "enter":
                if ask_host and cur == 0:  # check the address as soon as it's entered
                    if normalize_host(fields[0]) is None:
                        note = BAD_ADDRESS
                        continue
                    if note is BAD_ADDRESS:
                        note = None
                if cur < last:
                    cur += 1
                    continue
                host = None
                if ask_host:
                    host = normalize_host(fields[0])
                    if host is None:  # e.g. the address was edited after Tab/Up
                        note, cur = BAD_ADDRESS, 0
                        continue
                return host, fields[base], fields[base + 1]
            elif key in ("\t", "down"):
                cur = (cur + 1) % len(fields)
            elif key == "up":
                cur = (cur - 1) % len(fields)
            elif key in ("\x08", "\x7f"):
                fields[cur] = fields[cur][:-1]
            elif len(key) == 1 and 32 <= ord(key) < 127:
                fields[cur] += key


def _reason(e):
    if isinstance(e, ET.ParseError):
        return "that doesn't look like an Argus PDU"
    return str(getattr(e, "reason", None) or e)


def connect(t, pdu, force_form=False, focus=None):
    """Point `pdu` at a reachable PDU and, if it wants one, a working login.
    Shows the connect form when the address is unknown, unreachable, or the
    login is missing/rejected. Returns the outlet names ({} if the PDU won't
    tell) or None if the user quit the form."""
    show, note = force_form, None
    while True:
        if show:
            res = ask_credentials(t, pdu, note, ask_host=True, focus=focus)
            if res is None:
                return None
            host, user, pwd = res
            pdu.set_host(host)
            if user:
                pdu.set_credentials(user, pwd)
            else:
                pdu.auth, pdu.user = None, ""
        t.clear()
        print("\n  Connecting to %s ..." % pdu.host)
        sys.stdout.flush()
        try:
            pdu.status()
        except Exception as e:
            note = ("Cannot reach %s: %s" % (pdu.host, _reason(e)), "1", "31")
            show, focus = True, 0
            continue
        save_host(pdu.host)  # it answered: remember this address
        try:
            return pdu.names()
        except HTTPError as e:
            if e.code != 401:
                return {}
            if not pdu.auth:
                note = ("This PDU requires a login.", "33")
            elif show:
                note = ("Login failed - check username and password.", "1", "31")
            else:
                note = ("Saved credentials were rejected - please sign in.", "1", "31")
            show, focus = True, (2 if pdu.user else 1)
        except Exception:
            return {}  # reachable but names unavailable: continue without them


def call_with_login(pdu, fn, *args, **kwargs):
    """Run fn; if the PDU answers 401, show the login screen and retry once."""
    try:
        return fn(*args, **kwargs)
    except HTTPError as e:
        if e.code != 401:
            raise
        note = ("Login rejected - please try again.", "1", "31") if pdu.auth \
            else ("This action needs a login.", "33")
        creds = ask_credentials(Term(), pdu, note)
        if creds is None:
            raise LoginCancelled("Login cancelled.")
        pdu.set_credentials(creds[1], creds[2])
        return fn(*args, **kwargs)  # a second 401 propagates to the caller


def wait_for_states(pdu, wanted, timeout=8.0):
    """Wait until every outlet in `wanted` ({index: bool on}) has the wanted
    state. The PDU can take a few seconds (port delay settings).
    Returns (last status, True if everything matched)."""
    end = time.time() + timeout
    st = None
    while time.time() < end:
        time.sleep(0.5)
        st = pdu.status()
        if all(st["outlets"][i] == want for i, want in wanted.items()):
            return st, True
    return st, False


def toggle(t, pdu, names, idx, confirm):
    """Toggle one outlet. Returns (status, message tuple)."""
    st = pdu.status()  # always decide from a fresh reading
    label = names.get(idx, "Outlet %d" % (idx + 1))
    turning_on = not st["outlets"][idx]
    if confirm:
        word = "ON" if turning_on else "OFF"
        print("\n  " + t.c("Turn %s %s?" % (word, label), "1", "32" if turning_on else "33")
              + "  [y/N] ")
        if read_key() not in ("y", "Y"):
            return st, ("Cancelled.", "90")
    print("\n  Applying...")
    call_with_login(pdu, pdu.set, [idx], "on" if turning_on else "off")
    st, ok = wait_for_states(pdu, {idx: turning_on})
    word = "ON" if turning_on else "OFF"
    if ok:
        return st, ("%s turned %s." % (label, word), "1", "32" if turning_on else "33")
    return st, ("Command sent, but %s is not %s yet - press F5 to refresh." % (label, word), "33")


def turn_off_all(t, pdu, names):
    """Switch every outlet that is currently ON to OFF (always asks first).
    Returns (status, message tuple)."""
    st = pdu.status()
    on = [i for i in range(NUM_OUTLETS) if st["outlets"][i]]
    if not on:
        return st, ("All outlets are already OFF.", "90")
    print("\n  " + t.c("Turn OFF ALL %d outlet(s)?" % len(on), "1", "31"))
    for i in on:
        print("    - " + names.get(i, "Outlet %d" % (i + 1)))
    print("  [y/N] ")
    if read_key() not in ("y", "Y"):
        return st, ("Cancelled.", "90")
    print("\n  Applying...")
    call_with_login(pdu, pdu.set, on, "off")
    st, ok = wait_for_states(pdu, dict((i, False) for i in on), timeout=15.0)
    if ok:
        return st, ("All outlets turned OFF.", "1", "33")
    left = [i for i in on if st and st["outlets"][i]]
    return st, ("Command sent, but %d outlet(s) are still ON - press F5 to refresh." % len(left), "33")


def rename_flow(t, pdu, names, idx):
    """Prompt for a new name and save it. Returns a message tuple."""
    cur = names.get(idx, "Outlet %d" % (idx + 1))
    cfg = call_with_login(pdu, pdu.read_config)
    print("\n  Rename " + t.c(cur, "1", "33") + "  (max %d characters, empty = cancel)" % cfg["maxlen"])
    new = input("  New name: ").strip()
    if not new or new == cur:
        return ("Cancelled.", "90")
    problem = validate_name(new, cfg["maxlen"])
    if problem:
        return (problem, "1", "31")
    print("  Saving...")
    call_with_login(pdu, pdu.rename, idx, new, cfg=cfg)
    names[idx] = new
    return ("Renamed %s to %s." % (cur, new), "1", "32")


def _http_msg(e):
    return ("PDU rejected the request: HTTP %d%s" % (
        e.code, " (check username/password)" if e.code == 401 else ""), "1", "31")


def _now():
    return time.strftime("%H:%M:%S")


def interactive(pdu, confirm=True, refresh_s=REFRESH_SECONDS, host_known=True):
    t = Term()
    try:
        # asks for the address first if we don't know one, or for a login if needed
        names = connect(t, pdu, force_form=not host_known, focus=None if host_known else 0)
    except KeyboardInterrupt:
        names = None
    if names is None:
        t.clear()
        print("Bye.")
        return
    st, err, sel, msg, updated = None, None, 0, None, ""
    refresh = True
    try:
        while True:
            if refresh:
                try:
                    st, err = pdu.status(), None
                    updated = _now()
                except Exception as e:
                    st, err = None, str(e)
                if not names:  # names couldn't be read earlier: keep trying
                    try:
                        names = pdu.names()
                    except Exception:
                        pass
                refresh = False
            render(t, pdu, names, st, err, sel, msg, updated, refresh_s)

            key = read_key(timeout=refresh_s)
            if key is None:  # no key pressed: refresh in the background, keep the message
                refresh = True
                continue
            msg = None
            try:
                if key in ("q", "Q", "esc", "quit"):
                    break
                elif key in ("up", "k"):
                    sel = (sel - 1) % NUM_ITEMS
                elif key in ("down", "j"):
                    sel = (sel + 1) % NUM_ITEMS
                elif key in [str(n) for n in range(1, NUM_OUTLETS + 1)]:
                    sel = int(key) - 1
                elif key == "f5":
                    refresh = True
                    try:
                        fresh = pdu.names()
                        if fresh:
                            names = fresh
                    except Exception:
                        pass
                    msg = ("Refreshed.", "90")
                elif key == "f3":
                    cand = PDU(pdu.host, timeout=pdu.timeout)  # don't disturb the live one
                    cand.auth, cand.user = pdu.auth, pdu.user
                    new_names = connect(t, cand, force_form=True, focus=0)
                    if new_names is None:
                        msg = ("Cancelled.", "90")
                    else:
                        pdu, names, sel, refresh = cand, new_names, 0, True
                        msg = ("Connected to %s." % pdu.host, "1", "32")
                elif key == "f2" and sel < NUM_OUTLETS:
                    msg = rename_flow(t, pdu, names, sel)
                elif key == "f4":
                    st, msg = turn_off_all(t, pdu, names)
                    updated = _now()
                elif key == "enter":
                    if sel == QUIT_ITEM:
                        break
                    st, msg = toggle(t, pdu, names, sel, confirm)
                    updated = _now()
                    if not names:  # login may have unlocked the names
                        try:
                            names = pdu.names()
                        except Exception:
                            pass
            except ConfigError as e:
                msg = (str(e), "1", "31")
            except HTTPError as e:
                msg = _http_msg(e)
            except Exception as e:
                msg = ("Error: %s" % e, "1", "31")
    except KeyboardInterrupt:
        pass
    t.clear()
    print("Bye.")


# --------------------------------------------------------------------------
# CLI entry point
# --------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description="Argus SW-0816 PDU control",
                                epilog=COPYRIGHT)
    p.add_argument("--host", default=os.environ.get("PDU_HOST"), metavar="IP",
                   help="PDU address (default: the last working one, else %s)" % DEFAULT_HOST)
    p.add_argument("--user", default=os.environ.get("PDU_USER"))
    p.add_argument("--password", default=os.environ.get("PDU_PASS"))
    p.add_argument("--no-confirm", action="store_true",
                   help="interactive mode: don't ask before switching a single outlet "
                        "ON or OFF (Turn off all with F4 always asks)")
    p.add_argument("--refresh", type=int, default=REFRESH_SECONDS, metavar="SECONDS",
                   help="interactive mode: auto-refresh interval (default %(default)s)")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("menu", help="interactive menu (default)")

    s = sub.add_parser("status", help="print status and exit")
    s.add_argument("--json", action="store_true")

    sub.add_parser("config", help="show outlet names and on/off delays")

    r = sub.add_parser("rename", help="rename an outlet")
    r.add_argument("outlet", type=int, help="outlet index 0-7")
    r.add_argument("name", help="new name")
    r.add_argument("--dry-run", action="store_true",
                   help="show what would be sent, change nothing")

    for name in OPS:
        c = sub.add_parser(name, help="scripted: switch outlet(s) %s" % name.upper())
        c.add_argument("outlets", type=int, nargs="+", help="outlet index 0-7")
        c.add_argument("--yes", action="store_true",
                       help="skip the confirmation for power-cutting actions")

    # Python 2's argparse insists on a subcommand, so default to "menu" here.
    argv = sys.argv[1:]
    if not any(x in ("menu", "status", "config", "rename", "on", "off", "onoff",
                     "-h", "--help") for x in argv):
        argv.append("menu")
    a = p.parse_args(argv)
    if a.host:
        host = normalize_host(a.host)
        if not host:
            p.error("invalid PDU address: %r" % a.host)
    else:
        host = load_saved_host()
    pdu = PDU(host or DEFAULT_HOST, a.user, a.password)

    if a.cmd == "menu":
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            p.error("no command given and not running in an interactive terminal")
        interactive(pdu, confirm=not a.no_confirm, refresh_s=max(1, a.refresh),
                    host_known=bool(host))
        return

    if a.cmd == "status":
        st = pdu.status()
        if a.json:
            print(json.dumps(st))
        else:
            for i in sorted(st["outlets"]):
                print("outlet%d: %s" % (i, "ON" if st["outlets"][i] else "OFF"))
            print("current: %s A  temp: %s  humidity: %s%%  state: %s" % (
                st["current_a"], st["temperature"], st["humidity"], st["state"]))
        return

    if a.cmd == "config":
        cfg = pdu.read_config()
        d = dict(cfg["pairs"])
        for i in range(NUM_OUTLETS):
            print("outlet%d  name: %-20s on-delay: %-4s off-delay: %s" % (
                i, ascii_safe(d["otlt%d" % i]), d["ondly%d" % i], d["ofdly%d" % i]))
        print("max name length: %d" % cfg["maxlen"])
        return

    if a.cmd == "rename":
        try:
            sent = pdu.rename(a.outlet, a.name, dry_run=a.dry_run)
        except ConfigError as e:
            sys.exit("error: %s" % e)
        if a.dry_run:
            print("Would send (POST /config_PDU.htm):")
            print("&".join("%s=%s" % (f, _enc(v)) for f, v in sent))
        else:
            print("outlet%d renamed to %s" % (a.outlet, a.name))
        return

    if a.cmd in ("off", "onoff") and not a.yes:
        ans = input("%s outlet(s) %s? This cuts power. [y/N] " % (a.cmd.upper(), a.outlets))
        if ans.strip().lower() != "y":
            sys.exit("aborted")
    pdu.set(a.outlets, a.cmd)
    st = pdu.status()
    for o in sorted(set(a.outlets)):
        print("outlet%d: %s" % (o, "ON" if st["outlets"][o] else "OFF"))


if __name__ == "__main__":
    main()