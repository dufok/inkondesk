"""XP-Pen Shortcut Remote (ACK05) key map on a Kindle Paperwhite 1.

Reads the XP-Pen driver config (%APPDATA%\\XPPen\\config.xml), follows the
foreground app on Windows like the driver does, renders the remote's layout
with your key labels, pushes it to the Kindle over USB networking (SSH) and
handles taps on the Kindle: an on-screen numpad (0-9, sent to the active PC app),
key group tabs, the frontlight buttons and "calc", which turns the whole screen into
a calculator (with % and a history of past calculations, kept across restarts).
A watchdog on the Kindle shows the BadPrompt screensaver when the PC goes away.

Meant to run permanently (pythonw, started at Windows logon): waits for the
Kindle, takes its screen over again after a Kindle reboot / cable replug, and
logs to xppen_kindle.log next to this file.

Usage:
    pythonw xppen_kindle.py                # run in the background (normal use)
    python xppen_kindle.py                 # run in a console (Ctrl+C to stop)
    python xppen_kindle.py --stop          # stop the running copy, give the Kindle its UI back
    python xppen_kindle.py --preview out   # just render PNGs for every profile
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import functools
import io
import json
import math
import os
import queue
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from decimal import Decimal, DecimalException, localcontext

from PIL import Image, ImageChops, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(os.environ.get("APPDATA", ""), "XPPen", "config.xml")
SSH_KEY = os.path.join(HERE, "kindle-files", "ssh", "kindle_ed25519")
KNOWN_HOSTS = os.path.join(HERE, "kindle-files", "ssh", "known_hosts")
KINDLE_IP = "192.168.15.244"
HOST = "root@" + KINDLE_IP
FONTS = "C:/Windows/Fonts/"
LOG = os.path.join(HERE, "xppen_kindle.log")
STOP_FLAG = os.path.join(HERE, "xppen_kindle.stop")
STATE = os.path.join(HERE, "xppen_kindle_state.json")     # frontlight level, calculator history

# PW1 frontlight: powerd flIntensity 0 (off) .. 24. Uneven steps: the eye notices low levels more.
LIGHT_STEPS = [0, 1, 2, 4, 6, 9, 12, 16, 20, 24]

LW, LH = 1024, 758          # landscape canvas (the Kindle is 758x1024 portrait)
GROUP_TAGS = ["K", "K_1", "K_2", "K_3"]
GROUP_NAMES = ["I", "II", "III", "IV"]

# Mac-style key symbols Segoe UI can't draw -> words
SYMBOLS = {"⌦": "Del", "⌫": "Backspace", "⇥": "Tab", "↖": "Home", "↘": "End",
           "⏎": "Enter", "↩": "Enter", "⎋": "Esc", "⇧": "Shift", "⌘": "Cmd",
           "⌥": "Alt", "⌃": "Ctrl", "⇞": "PgUp", "⇟": "PgDn"}


# --------------------------------------------------------------------------- config

@dataclass
class Key:
    kind: str        # "key" | "mouse" | "func" | "other" | "" (not assigned)
    label: str
    shortcut: str


@dataclass
class Profile:
    name: str
    exe: str         # lower-case full path, "" for the global profile
    groups: list     # 4 x 10 Key
    dial: list       # 4 x (name, ccw, cw) or None


def _symbols(s):
    return "".join(SYMBOLS.get(ch, ch) for ch in s)


def _parse_key(node):
    text = (node.text or "").strip() if node is not None else ""
    if not text:
        return Key("", "", "")
    p = text.split("|")
    kind = {"1": "key", "2": "mouse", "3": "func"}.get(p[0], "other")
    label = p[1].strip() if len(p) > 1 else ""
    raw = p[2].strip() if len(p) > 2 else ""
    if kind == "key":
        shortcut = _symbols(raw)
    elif kind == "mouse":
        shortcut = "mouse"
    elif kind == "func":
        shortcut = "driver function"
    else:
        shortcut = "" if re.fullmatch(r"[\d+]*", raw) else _symbols(raw)
    return Key(kind, _symbols(label) if len(label) <= 2 else label, shortcut)


def _parse_app(node, name, exe):
    groups = []
    for tag in GROUP_TAGS:
        g = node.find(tag)
        groups.append([_parse_key(g.find(f"K{i}") if g is not None else None) for i in range(1, 11)])
    dial = []
    r = node.find("R")
    for i in range(1, 5):
        w = r.find(f"W{i}") if r is not None else None
        p = (w.text or "").split("|") if w is not None and w.text else []
        dial.append((p[1], _symbols(p[2]), _symbols(p[3])) if len(p) >= 4 and p[0] == "4" else None)
    return Profile(name, exe, groups, dial)


def _app_name(exe):
    parent = os.path.basename(os.path.dirname(exe))
    stem = os.path.splitext(os.path.basename(exe))[0]
    if stem.lower() in parent.lower() and parent.lower() != stem.lower():
        return parent.title()          # "blender 5.2" -> "Blender 5.2"
    return stem.title()


def load_profiles(path=CONFIG):
    """Global profile first, then the Windows app profiles (.exe) of the ACK05."""
    root = ET.parse(path).getroot()
    dev = root.find("ACK05")
    if dev is None:
        raise SystemExit("ACK05 (Shortcut Remote) not found in " + path)
    profiles = [_parse_app(dev.find("CommonAPP"), "All apps", "")]
    apps = dev.find("Apps")
    for app in (apps if apps is not None else []):
        exe = (app.findtext("path") or "").strip().lower()
        if exe.endswith(".exe"):
            profiles.append(_parse_app(app, _app_name(exe), exe))
    return profiles


def match_profile(profiles, exe):
    exe = (exe or "").lower()
    for i, p in enumerate(profiles):
        if p.exe and p.exe == exe:
            return i
    base = os.path.basename(exe)
    for i, p in enumerate(profiles):
        if p.exe and os.path.basename(p.exe) == base:
            return i
    return 0


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):          # only here so the union has the right size
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


def send_key(vk):
    """Press and release one key in whatever app is active on the PC (like a real keyboard).
    Note: Windows blocks this for apps running as administrator (UIPI)."""
    user32 = ctypes.windll.user32
    scan = user32.MapVirtualKeyW(vk, 0)                     # MAPVK_VK_TO_VSC
    events = (_INPUT * 2)(
        _INPUT(type=1, u=_INPUTUNION(ki=_KEYBDINPUT(vk, scan, 0, 0, 0))),   # key down
        _INPUT(type=1, u=_INPUTUNION(ki=_KEYBDINPUT(vk, scan, 2, 0, 0))))   # KEYEVENTF_KEYUP
    return user32.SendInput(2, events, ctypes.sizeof(_INPUT)) == 2


def foreground_exe():
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(len(buf))
        ok = kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return buf.value if ok else ""
    finally:
        kernel32.CloseHandle(h)


# --------------------------------------------------------------------------- calculator

CALC_OPS = ("+", "−", "×", "÷")
CALC_MAX_DIGITS = 15
HISTORY_MAX = 50                 # calculations kept in the state file
THIN = " "                  # thousands separator on screen: 1 234 567.5


def calc_show(tok):
    """Number or operator token -> screen text: thin-space thousands groups, a real minus sign."""
    if tok in CALC_OPS:
        return tok
    neg = tok.startswith("-")
    body = tok[1:] if neg else tok
    pct = body.endswith("%")
    body = body.rstrip("%")
    if "e" not in body:
        ip, dot, fp = body.partition(".")
        groups = []
        while len(ip) > 3:
            groups.insert(0, ip[-3:])
            ip = ip[:-3]
        body = THIN.join([ip] + groups) + dot + fp
    return ("−" if neg else "") + body + ("%" if pct else "")


def calc_str(d):
    """Result -> plain string: at most 15 significant digits and 10 decimals ('1437.5', '0.3333333333'),
    exponent form for very big / very small numbers ('1.5e20')."""
    with localcontext() as ctx:
        ctx.prec = 15
        d = +d
    if d == 0:
        return "0"
    if abs(d) >= Decimal("1e15") or abs(d) < Decimal("1e-10"):
        mant, _, exp = f"{d:.9e}".partition("e")
        return mant.rstrip("0").rstrip(".") + "e" + str(int(exp))
    return format(d.quantize(Decimal("1e-10")).normalize(), "f")


def _factor(tok):
    v = Decimal(tok.rstrip("%"))
    return v / 100 if tok.endswith("%") else v


def calc_eval(toks):
    """[number, op, number, ...] -> Decimal. × and ÷ go before + and −. Percent works like on a
    desk calculator: right after + or − it is a percent of what comes before (200 + 10% = 220,
    200 − 10% = 180), anywhere else it is just /100 (50 × 10% = 5, 10% = 0.1)."""
    with localcontext() as ctx:
        ctx.prec = 34
        total, sign, i = None, 1, 0
        while i < len(toks):
            term = [toks[i]]                       # one term: number (× or ÷ number)*
            i += 1
            while i < len(toks) and toks[i] in ("×", "÷"):
                term += toks[i:i + 2]
                i += 2
            if total is not None and len(term) == 1 and term[0].endswith("%"):
                val = total * Decimal(term[0][:-1]) / 100
            else:
                val = _factor(term[0])
                for j in range(1, len(term), 2):
                    v = _factor(term[j + 1])
                    val = val * v if term[j] == "×" else val / v
            total = val if total is None else total + sign * val
            if i < len(toks):
                sign = 1 if toks[i] == "+" else -1
                i += 1
        return total if total is not None else Decimal(0)


class Calc:
    """An ordinary calculator: + − × ÷, %, ±, C, ⌫ and a history of finished calculations.
    Tokens are number strings ("12.5", "-3", "15%") and the operators in CALC_OPS."""

    def __init__(self, history=()):
        self.history = [h for h in history
                        if isinstance(h, dict) and "expr" in h and "result" in h][-HISTORY_MAX:]
        self.tokens = []
        self.done = None          # (expression, result) right after "="
        self.error = ""
        self.rev = 0              # bumped on every change: the screen redraws when it moves

    def _num(self):
        """The number being typed (last token), or None after an operator / at the start."""
        return self.tokens[-1] if self.tokens and self.tokens[-1] not in CALC_OPS else None

    def _complete(self):
        """Tokens without a trailing operator or a lone minus: something calc_eval can take."""
        toks = list(self.tokens)
        while toks and (toks[-1] in CALC_OPS or toks[-1] == "-"):
            toks.pop()
        return toks

    def press(self, k):
        self.rev += 1
        if self.error:                                   # any key clears an error
            self.error, self.tokens = "", []
            if k in ("C", "⌫"):
                return
        if self.done:
            if k == "=":
                return
            result, self.done = self.done[1], None
            # an operator, %, ± or ⌫ goes on with the result; a digit starts a new calculation
            self.tokens = [result] if k in CALC_OPS + ("%", "±", "⌫") else []
        if k == "C":
            self.tokens = []
        elif k == "⌫":
            self._backspace()
        elif k.isdigit() or k == ".":
            self._digit(k)
        elif k in CALC_OPS:
            self._operator(k)
        elif k == "%":
            t = self._num()
            if t and t != "-" and not t.endswith("%"):
                self.tokens[-1] = t.rstrip(".") + "%"
        elif k == "±":
            t = self._num()
            if t is None:
                self.tokens.append("-")
            else:
                t = t[1:] if t.startswith("-") else "-" + t
                if t:
                    self.tokens[-1] = t
                else:
                    self.tokens.pop()
        elif k == "=":
            self._equals()

    def _digit(self, k):
        t = self._num()
        if t is None:
            self.tokens.append("0." if k == "." else k)
            return
        if t.endswith("%") or "e" in t:                  # "15%" / "1.5e20" are finished numbers
            return
        if k == ".":
            if "." not in t:
                self.tokens[-1] = t + ("0." if t in ("", "-") else ".")
            return
        if sum(c.isdigit() for c in t) >= CALC_MAX_DIGITS:
            return
        self.tokens[-1] = t[:-1] + k if t in ("0", "-0") else t + k

    def _operator(self, k):
        if self.tokens and self.tokens[-1] == "-":       # a lone minus is dropped
            self.tokens.pop()
        if not self.tokens:
            self.tokens = ["0"]
        if self.tokens[-1] in CALC_OPS:                  # change your mind: replace the operator
            self.tokens[-1] = k
            return
        if self.tokens[-1].endswith("."):
            self.tokens[-1] = self.tokens[-1][:-1]
        self.tokens.append(k)

    def _backspace(self):
        if not self.tokens:
            return
        t = self.tokens.pop()
        if t in CALC_OPS or "e" in t:                    # operators and 1.5e20 go as a whole
            return
        t = t[:-1]
        if t not in ("", "-"):
            self.tokens.append(t)

    def _equals(self):
        toks = self._complete()
        if not toks or (len(toks) == 1 and not toks[0].endswith("%")):
            self.tokens = toks                           # nothing to calculate
            return
        expr = " ".join(calc_show(t) for t in toks)
        try:
            result = calc_str(calc_eval(toks))
        except (DecimalException, ZeroDivisionError) as e:
            self.tokens = toks
            self.error = "Can't divide by 0" if isinstance(e, ZeroDivisionError) else "Error"
            return
        self.done, self.tokens = (expr, result), []
        self.history.append({"expr": expr, "result": result})
        del self.history[:-HISTORY_MAX]

    def recall(self, i):
        """Tap on a history line: its result becomes the number being typed."""
        self.rev += 1
        result = self.history[i]["result"]
        if self.error or self.done:
            self.error, self.done, self.tokens = "", None, []
        if self._num() is not None:
            self.tokens[-1] = result
        else:
            self.tokens.append(result)

    def clear_history(self):
        self.rev += 1
        self.history = []

    def display(self):
        """(small line: the expression, big line: the number)."""
        if self.error:
            return " ".join(calc_show(t) for t in self.tokens), self.error
        if self.done:
            return self.done[0] + " =", calc_show(self.done[1])
        if not self.tokens:
            return "", "0"
        if self.tokens[-1] in CALC_OPS:                  # after an operator: the running result
            try:
                big = calc_show(calc_str(calc_eval(self._complete())))
            except (DecimalException, ZeroDivisionError):
                big = ""
            return " ".join(calc_show(t) for t in self.tokens), big
        return " ".join(calc_show(t) for t in self.tokens[:-1]), calc_show(self.tokens[-1])


# --------------------------------------------------------------------------- render

@functools.lru_cache(maxsize=None)
def font(bold, size):
    return ImageFont.truetype(FONTS + ("segoeuib.ttf" if bold else "segoeui.ttf"), size)


def _wrap(d, text, f, width):
    lines, line = [], ""
    for word in text.split():
        while d.textlength(word, font=f) > width:          # break over-long words
            cut = len(word)
            while cut > 1 and d.textlength(word[:cut], font=f) > width:
                cut -= 1
            if line:
                lines.append(line)
                line = ""
            lines.append(word[:cut])
            word = word[cut:]
        cand = (line + " " + word).strip()
        if d.textlength(cand, font=f) <= width:
            line = cand
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def fit(d, text, width, height, size_max, size_min, bold):
    # first shrink the font so whole words fit; break words only as a last resort
    for allow_break in (False, True):
        for size in range(size_max, size_min - 1, -2):
            f = font(bold, size)
            if not allow_break and any(d.textlength(w, font=f) > width for w in text.split()):
                continue
            lines = _wrap(d, text, f, width)
            if len(lines) * int(size * 1.2) <= height:
                return f, lines
    f = font(bold, size_min)
    lines = _wrap(d, text, f, width)
    return f, lines[: max(1, height // int(size_min * 1.2))]


def fit_one(d, text, width, size_max, size_min, bold, keep="end"):
    """One line: the biggest font size that fits. If even size_min is too wide, cut the text
    with "…", keeping its end (keep="end") or its start (keep="start")."""
    for size in range(size_max, size_min - 1, -2):
        f = font(bold, size)
        if d.textlength(text, font=f) <= width:
            return f, text
    f = font(bold, size_min)
    while len(text) > 1:
        text = text[1:] if keep == "end" else text[:-1]
        cut = "…" + text if keep == "end" else text + "…"
        if d.textlength(cut, font=f) <= width:
            return f, cut
    return f, text


def draw_lines(d, lines, f, cx, top, fill):
    lh = int(f.size * 1.2)
    for i, ln in enumerate(lines):
        d.text((cx, top + i * lh), ln, font=f, fill=fill, anchor="ma")
    return len(lines) * lh


def draw_key(d, box, kid, key):
    x0, y0, x1, y1 = box
    w = x1 - x0
    if not key.kind:
        d.rounded_rectangle(box, radius=16, outline=180, width=2, fill=238)
        d.text((x0 + 12, y0 + 8), kid, font=font(False, 20), fill=150)
        return
    d.rounded_rectangle(box, radius=16, outline=0, width=3, fill=255)
    d.text((x0 + 12, y0 + 8), kid, font=font(False, 20), fill=110)
    bottom = y1 - 10
    if key.shortcut and key.shortcut != key.label:
        sf, slines = fit(d, key.shortcut, w - 20, 52, 22, 15, False)
        sh = len(slines) * int(sf.size * 1.2)
        draw_lines(d, slines, sf, (x0 + x1) // 2, bottom - sh, 90)
        bottom -= sh + 6
    top = y0 + 34
    lf, llines = fit(d, key.label, w - 20, bottom - top, 40, 16, True)
    lh = len(llines) * int(lf.size * 1.2)
    draw_lines(d, llines, lf, (x0 + x1) // 2, top + max(0, (bottom - top - lh) // 2), 0)


def draw_sun(d, cx, cy, fill=0):
    d.ellipse((cx - 9, cy - 9, cx + 9, cy + 9), outline=fill, width=3)
    for i in range(8):
        a = i * math.pi / 4
        c, s = math.cos(a), math.sin(a)
        d.line((cx + 14 * c, cy + 14 * s, cx + 20 * c, cy + 20 * s), fill=fill, width=3)


def draw_light(d, hits, x, y, light):
    """Frontlight control: sun, [−], level bar, [+], [off/on]. Returns right edge."""
    h = 62
    draw_sun(d, x + 22, y + h // 2)
    x += 50
    for action, label, w in (("down", "−", 80), ("bar", None, 150), ("up", "+", 80)):
        if action == "bar":
            n = len(LIGHT_STEPS) - 1                       # segments for levels 1..max
            seg = (w - (n - 1) * 4) / n
            for i in range(n):
                sx = x + i * (seg + 4)
                on = i < light
                d.rectangle((sx, y + 18, sx + seg, y + h - 18), outline=0, width=2, fill=0 if on else 255)
        else:
            box = (x, y, x + w, y + h)
            d.rounded_rectangle(box, radius=12, outline=0, width=3, fill=255)
            d.text((x + w // 2, y + h // 2 - 2), label, font=font(True, 44), fill=0, anchor="mm")
            hits.append((box, ("light", action)))
        x += w + 12
    box = (x, y, x + 96, y + h)
    off = light == 0
    d.rounded_rectangle(box, radius=12, outline=0, width=3, fill=0 if off else 255)
    d.text((x + 48, y + h // 2), "on" if off else "off", font=font(True, 28),
           fill=255 if off else 0, anchor="mm")
    hits.append((box, ("light", "toggle")))
    return x + 96


def draw_calc_button(d, hits, x, y, active):
    """"calc" next to the frontlight controls: opens the calculator; black while it's open,
    tap again to go back to the key map."""
    box = (x, y, x + 116, y + 62)
    d.rounded_rectangle(box, radius=12, outline=0, width=3, fill=0 if active else 255)
    d.text((x + 58, y + 31), "calc", font=font(True, 30), fill=255 if active else 0, anchor="mm")
    hits.append((box, ("calc",)))


FOOTER_Y = 680          # bottom row: frontlight, calc, key groups
MAP_X1 = 686            # the remote's key map: x 16..686 (left)
PAD_X0 = 702            # our on-screen keys: x 702..1008 (right)

# On-screen keypad: numpad layout, sends the numpad keys (VK_NUMPAD0..9) to the active PC app.
# Numpad keys type digits in text fields and switch views in Blender (1/3/7, 5 = persp/ortho).
NUMPAD = [("7", 0x67), ("8", 0x68), ("9", 0x69),
          ("4", 0x64), ("5", 0x65), ("6", 0x66),
          ("1", 0x61), ("2", 0x62), ("3", 0x63),
          ("0", 0x60)]


def draw_keypad(d, hits, x0, y0, x1, y1, gap=14):
    """Our own on-screen keys (right column)."""
    box = (x0, 12, x1, 84)
    d.rounded_rectangle(box, radius=14, outline=150, width=2, fill=255)
    d.text(((x0 + x1) // 2, 48), "NumPad", font=font(True, 26), fill=90, anchor="mm")
    cols, rows = 3, 4
    cw_ = (x1 - x0 - (cols - 1) * gap) / cols
    rh = (y1 - y0 - (rows - 1) * gap) / rows
    for i, (label, vk) in enumerate(NUMPAD):
        c, r = i % cols, i // cols
        span = cols if i == len(NUMPAD) - 1 else 1          # "0" takes the whole bottom row
        b = (int(x0 + c * (cw_ + gap)), int(y0 + r * (rh + gap)),
             int(x0 + c * (cw_ + gap) + span * cw_ + (span - 1) * gap), int(y0 + r * (rh + gap) + rh))
        d.rounded_rectangle(b, radius=16, outline=0, width=3, fill=255)
        d.text(((b[0] + b[2]) // 2, (b[1] + b[3]) // 2 - 4), label, font=font(True, 60), fill=0, anchor="mm")
        hits.append((b, ("key", vk, label)))


def render(profiles, shown, group, active_exe, light=1):
    """Key map screen. Return (landscape image, hit regions in landscape coords).
    light = index into LIGHT_STEPS (0 = off)."""
    img = Image.new("L", (LW, LH), 255)
    d = ImageDraw.Draw(img)
    hits = []
    prof = profiles[shown]

    # --- right column: our own on-screen keys
    draw_keypad(d, hits, PAD_X0, 100, LW - 16, 660)

    # --- title bar: the program whose profile is shown
    box = (16, 12, MAP_X1, 84)
    d.rounded_rectangle(box, radius=14, outline=0, width=3, fill=255)
    exe = os.path.basename(active_exe) if active_exe else ""
    if exe:
        d.text((MAP_X1 - 18, 48), exe, font=font(False, 20), fill=120, anchor="rm")
    ew = d.textlength(exe, font=font(False, 20)) + 30 if exe else 0
    f, lines = fit(d, prof.name, MAP_X1 - 16 - 40 - ew, 60, 36, 18, True)
    d.text((16 + 22, 48), lines[0], font=f, fill=0, anchor="lm")

    # --- keys (layout of the ACK05)
    gx0, gy0, gx1, gy1, gap = 16, 100, MAP_X1, 660, 14
    cw_ = (gx1 - gx0 - 3 * gap) / 4
    rh = (gy1 - gy0 - 2 * gap) / 3

    def cell(c, r_, cs=1, rs=1):
        return (int(gx0 + c * (cw_ + gap)), int(gy0 + r_ * (rh + gap)),
                int(gx0 + c * (cw_ + gap) + cs * cw_ + (cs - 1) * gap),
                int(gy0 + r_ * (rh + gap) + rs * rh + (rs - 1) * gap))

    layout = {1: cell(0, 0), 2: cell(1, 0), 3: cell(2, 0), 7: cell(3, 0, rs=2),
              4: cell(0, 1), 5: cell(1, 1), 6: cell(2, 1),
              8: cell(0, 2), 9: cell(1, 2, cs=2), 10: cell(3, 2)}
    keys = prof.groups[group]
    for k, box in layout.items():
        draw_key(d, box, f"K{k}", keys[k - 1])

    # --- footer: frontlight and calc on the left, key groups on the right
    # (only groups with something assigned)
    fy = FOOTER_Y
    x = draw_light(d, hits, 16, fy, light)
    draw_calc_button(d, hits, x + 16, fy, False)
    used = [g for g in range(4) if any(k.kind and k.kind != "func" for k in prof.groups[g])] or [0]
    if len(used) > 1:
        w = 96 if len(used) < 4 else 80
        x = LW - 16 - len(used) * (w + 10) + 10
        for g in used:
            box = (x, fy, x + w, fy + 62)
            act = g == group
            d.rounded_rectangle(box, radius=12, outline=0, width=3, fill=0 if act else 255)
            d.text((x + w // 2, fy + 31), GROUP_NAMES[g], font=font(True, 30),
                   fill=255 if act else 0, anchor="mm")
            hits.append((box, ("group", g)))
            x += w + 10

    return img, hits


CALC_X1 = 560           # calculator display + history: x 16..560
CALC_PAD_X0 = 576       # calculator keys: x 576..1008
CALC_KEYS = ["C", "⌫", "%", "÷",
             "7", "8", "9", "×",
             "4", "5", "6", "−",
             "1", "2", "3", "+",
             "±", "0", ".", "="]


def draw_backspace(d, cx, cy, fill=0):
    pts = [(cx - 34, cy), (cx - 14, cy - 22), (cx + 32, cy - 22), (cx + 32, cy + 22), (cx - 14, cy + 22)]
    d.line(pts + pts[:2], fill=fill, width=4, joint="curve")
    d.line((cx - 1, cy - 10, cx + 19, cy + 10), fill=fill, width=4)
    d.line((cx - 1, cy + 10, cx + 19, cy - 10), fill=fill, width=4)


def draw_calc_key(d, box, label):
    eq, op = label == "=", label in CALC_OPS
    d.rounded_rectangle(box, radius=16, outline=0, width=3, fill=0 if eq else 222 if op else 255)
    cx, cy = (box[0] + box[2]) // 2, (box[1] + box[3]) // 2
    if label == "⌫":
        draw_backspace(d, cx, cy)
        return
    size = 76 if label == "." else 60 if (label.isdigit() or op or eq) else 46
    d.text((cx, cy - 4), label, font=font(True, size), fill=255 if eq else 0, anchor="mm")


def render_calc(calc, light=1):
    """Calculator screen. Return (landscape image, hit regions in landscape coords)."""
    img = Image.new("L", (LW, LH), 255)
    d = ImageDraw.Draw(img)
    hits = []
    x0, x1 = 16, CALC_X1

    # --- display: the expression above, the number below, right-aligned like any calculator
    d.rounded_rectangle((x0, 12, x1, 236), radius=14, outline=0, width=3, fill=255)
    top, big = calc.display()
    if top:
        f, t = fit_one(d, top, x1 - x0 - 44, 34, 22, False, keep="end")
        d.text((x1 - 22, 58), t, font=f, fill=100, anchor="rm")
    f, t = fit_one(d, big, x1 - x0 - 44, 100, 36, True, keep="end")
    d.text((x1 - 22, 160), t, font=f, fill=0, anchor="rm")

    # --- history, newest first; tap a line to take its result
    hy0, hy1 = 252, 660
    d.rounded_rectangle((x0, hy0, x1, hy1), radius=14, outline=150, width=2, fill=255)
    d.text((x0 + 20, hy0 + 30), "History", font=font(True, 24), fill=90, anchor="lm")
    if calc.history:
        b = (x1 - 118, hy0 + 8, x1 - 10, hy0 + 52)
        d.rounded_rectangle(b, radius=10, outline=0, width=2, fill=255)
        d.text(((b[0] + b[2]) // 2, (b[1] + b[3]) // 2), "clear", font=font(True, 22), fill=0, anchor="mm")
        hits.append((b, ("calc_clear",)))
    else:
        d.text(((x0 + x1) // 2, (hy0 + 60 + hy1) // 2), "Finished calculations show up here",
               font=font(False, 22), fill=150, anchor="mm")
    row_h, y = 56, hy0 + 62
    rows = (hy1 - 8 - y) // row_h
    for i in range(len(calc.history) - 1, -1, -1)[:rows]:
        h = calc.history[i]
        d.line((x0 + 16, y, x1 - 16, y), fill=200, width=1)
        rf, rt = fit_one(d, "= " + calc_show(h["result"]), (x1 - x0) // 2, 32, 20, True, keep="start")
        d.text((x1 - 22, y + row_h // 2), rt, font=rf, fill=0, anchor="rm")
        room = x1 - x0 - 44 - d.textlength(rt, font=rf) - 20
        ef, et = fit_one(d, h["expr"], room, 24, 18, False, keep="start")
        d.text((x0 + 22, y + row_h // 2), et, font=ef, fill=100, anchor="lm")
        hits.append(((x0 + 8, y, x1 - 8, y + row_h), ("calc_hist", i)))
        y += row_h

    # --- keys
    kx0, ky0, kx1, ky1, gap = CALC_PAD_X0, 12, LW - 16, 660, 12
    cw_ = (kx1 - kx0 - 3 * gap) / 4
    rh = (ky1 - ky0 - 4 * gap) / 5
    for i, label in enumerate(CALC_KEYS):
        c, r = i % 4, i // 4
        b = (int(kx0 + c * (cw_ + gap)), int(ky0 + r * (rh + gap)),
             int(kx0 + c * (cw_ + gap) + cw_), int(ky0 + r * (rh + gap) + rh))
        draw_calc_key(d, b, label)
        hits.append((b, ("calc_key", label)))

    # --- footer: frontlight, calc (black = open; tap to go back to the key map)
    x = draw_light(d, hits, 16, FOOTER_Y, light)
    draw_calc_button(d, hits, x + 16, FOOTER_Y, True)
    return img, hits


SCREENSAVER_SRC = os.path.join(HERE, "assets", "badprompt.webp")
SCREENSAVER_PNG = os.path.join(HERE, "assets", "screensaver.png")


def make_screensaver(src=SCREENSAVER_SRC):
    """Colour logo on a flat background -> black logo on white, glossy highlights kept light.
    Returns the portrait image (as the Kindle shows it)."""
    im = Image.open(src).convert("RGB")
    w, h = im.size
    px = im.load()
    border = ([px[x, 0] for x in range(0, w, 8)] + [px[x, h - 1] for x in range(0, w, 8)]
              + [px[0, y] for y in range(0, h, 8)] + [px[w - 1, y] for y in range(0, h, 8)])
    bg = tuple(sorted(c[i] for c in border)[len(border) // 2] for i in range(3))
    diff = ImageChops.difference(im, Image.new("RGB", im.size, bg)).convert("L")
    mask = diff.point(lambda v: max(0, min(255, (v - 18) * 6)))       # soft edge: bg -> 0, logo -> 255
    logo = im.convert("L").point(lambda v: max(0, min(255, int((v - 35) * 2.2))))
    out = Image.composite(logo, Image.new("L", im.size, 255), mask)
    bbox = mask.point(lambda v: 255 if v > 128 else 0).getbbox() or (0, 0, w, h)
    out = out.crop(bbox)
    margin = 90
    scale = min((LW - 2 * margin) / out.width, (LH - 2 * margin) / out.height)
    out = out.resize((int(out.width * scale), int(out.height * scale)), Image.LANCZOS)
    canvas = Image.new("L", (LW, LH), 255)
    canvas.paste(out, ((LW - out.width) // 2, (LH - out.height) // 2))
    return canvas.rotate(90, expand=True)


def to_landscape(px, py):
    """Kindle touch (portrait) -> landscape canvas coords (canvas rotated 90° CCW)."""
    return LW - 1 - py, px


def hit_test(hits, lx, ly, slop=6):
    """The action of the button under a tap (a few px of slack around each button), or None."""
    for (x0, y0, x1, y1), action in hits:
        if x0 - slop <= lx <= x1 + slop and y0 - slop <= ly <= y1 + slop:
            return action
    return None


# --------------------------------------------------------------------------- kindle

SSH = ["ssh", "-i", SSH_KEY, "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
       "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
       "-o", f"UserKnownHostsFile={KNOWN_HOSTS}", "-o", "LogLevel=ERROR", HOST]
RPATH = "export PATH=$PATH:/sbin:/usr/sbin:/mnt/us/usbnet/bin; "
KD_DIR = "/mnt/us/kindle-dash"                       # our files on the Kindle
WATCHDOG_LOCAL = os.path.join(HERE, "kindle-scripts", "watchdog.sh")
SCREEN_LUA_LOCAL = os.path.join(HERE, "kindle-scripts", "screen.lua")
# start the screensaver watchdog unless it is already running (pidfile + exe check);
# "already running" exits 1 -- that's fine, so never let it fail the whole command
WATCHDOG_START = (f"start-stop-daemon -S -q -b -m -p /tmp/kd_watchdog.pid -x /bin/sh "
                  f"-- {KD_DIR}/watchdog.sh || true; ")
DASH_ON = (RPATH + "lipc-set-prop com.lab126.pillow disableEnablePillow disable; "
           "stop framework >/dev/null 2>&1; sleep 1; killall -STOP awesome; "
           "lipc-set-prop com.lab126.powerd preventScreenSaver 1; "
           "rm -f /tmp/kd_screensaver_on; touch /tmp/kd_heartbeat; " + WATCHDOG_START)
DASH_OFF = (RPATH + "start-stop-daemon -K -q -p /tmp/kd_watchdog.pid; "
            "rm -f /tmp/kd_watchdog.pid /tmp/kd_screensaver_on; "
            "killall -CONT awesome; start framework >/dev/null 2>&1; sleep 5; "
            "lipc-set-prop com.lab126.pillow disableEnablePillow enable")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)    # no console flashes under pythonw


def log(*parts):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(p) for p in parts)
    print(line, flush=True)                                # no-op under pythonw
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def kindle(cmd, data=None, timeout=30):
    try:
        return subprocess.run(SSH + [cmd], input=data, capture_output=True, timeout=timeout,
                              creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 255, b"", b"timeout")


def reachable():
    try:
        socket.create_connection((KINDLE_IP, 22), timeout=2).close()
        return True
    except OSError:
        return False


def push(png, x=0, y=0, flash=True, before=""):
    """One SSH call: run `before` (e.g. set the frontlight), then draw the PNG at x,y (portrait px).
    flash=False: quick partial refresh (no black blink)."""
    draw = (f"cat > /tmp/xpmap.png && fbink -q {'-f ' if flash else ''}-g file=/tmp/xpmap.png,x={x},y={y}"
            if png else "true")
    r = kindle(RPATH + before + draw, data=png)
    if r.returncode != 0:
        log("push failed:", r.stderr.decode(errors="replace").strip())
    return r.returncode == 0


class Screen:
    """Draws landscape pictures on the Kindle through one long-lived SSH session running
    kindle-scripts/screen.lua: no SSH handshake (~0.3 s) per redraw. Only the part of the picture
    that changed is sent and refreshed, unless flash=True (whole screen, flashing refresh).
    Falls back to a one-shot SSH call if the session breaks."""

    def __init__(self):
        self.proc = None
        self.last = None              # landscape picture now on the e-ink screen

    def close(self):
        if self.proc:
            try:
                self.proc.kill()
            except OSError:
                pass
        self.proc = None

    def _send(self, data):
        if self.proc is None or self.proc.poll() is not None:
            self.proc = subprocess.Popen(SSH + [RPATH + f"exec lua {KD_DIR}/screen.lua"],
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
        self.proc.stdin.write(data)
        self.proc.stdin.flush()
        reply = self.proc.stdout.readline()
        if not reply.startswith(b"OK"):
            raise OSError("screen.lua: " + (reply.decode(errors="replace").strip() or "session closed"))

    def show(self, img, flash=False, before=""):
        box = (0, 0, LW, LH)
        if not flash and self.last is not None:
            diff = ImageChops.difference(self.last, img).getbbox()
            box = diff and (max(0, diff[0] - 4), max(0, diff[1] - 4),
                            min(LW, diff[2] + 4), min(LH, diff[3] + 4))
        png, x, y = b"", 0, 0
        if box:
            buf = io.BytesIO()
            img.crop(box).rotate(90, expand=True).save(buf, format="PNG", compress_level=1)
            png, x, y = buf.getvalue(), box[1], LW - box[2]     # landscape box -> portrait corner
        cmd = before.encode()
        try:
            self._send(f"{len(png)} {x} {y} {int(flash)} {len(cmd)}\n".encode() + cmd + png)
        except (OSError, ValueError) as e:
            log(e, "- drawing with a one-shot SSH call instead")
            self.close()
            if not push(png, x, y, flash, before):
                self.last = None
                return False
        self.last = img
        return True


FL_SYSFS = "/sys/devices/system/fl_tps6116x/fl_tps6116x0/fl_intensity"


def light_cmd(idx):
    """powerd keeps its own value in sync; but for 0 it still drives the LEDs at 1,
    so 'off' is also written straight to the frontlight driver."""
    lvl = LIGHT_STEPS[idx]
    cmd = f"lipc-set-prop com.lab126.powerd flIntensity {lvl}; "
    if lvl == 0:
        cmd += f"echo 0 > {FL_SYSFS}; "
    return cmd


def load_state():
    """{"light": index, "light_on": index, "calc_history": [...]} -- {} before the first run."""
    try:
        with open(STATE, encoding="utf-8") as f:
            s = json.load(f)
        return s if isinstance(s, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state):
    try:
        with open(STATE + ".new", "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(STATE + ".new", STATE)
    except OSError as e:
        log("state save failed:", e)


def current_light_idx():
    """Nearest step to the Kindle's current frontlight level (used once, before any state exists)."""
    r = kindle(RPATH + "lipc-get-prop com.lab126.powerd flIntensity", timeout=15)
    try:
        lvl = int(r.stdout.split()[-1])
    except (ValueError, IndexError):
        return 1
    return min(range(len(LIGHT_STEPS)), key=lambda i: abs(LIGHT_STEPS[i] - lvl))


def take_screen():
    """Switch the Kindle to full-screen mode. False = not ready yet, try again later."""
    r = kindle("cat /proc/uptime", timeout=15)
    if r.returncode != 0:
        return False
    uptime = float(r.stdout.split()[0])
    if uptime < 90:                     # just booted: let its UI finish starting, then stop it
        log(f"Kindle booted {uptime:.0f}s ago, waiting for it to settle")
        return False
    install_kindle_files()
    r = kindle(DASH_ON, timeout=40)
    if r.returncode != 0:
        log(f"taking the screen failed (rc {r.returncode}):", r.stderr.decode(errors="replace").strip())
    return r.returncode == 0


def install_kindle_files():
    """Copy the screensaver image, the watchdog and the screen.lua drawing loop to the Kindle
    (atomic replace: a running script keeps reading its old file)."""
    buf = io.BytesIO()
    make_screensaver().save(buf, format="PNG", optimize=True)
    files = [("screensaver.png", buf.getvalue())]
    for path in (WATCHDOG_LOCAL, SCREEN_LUA_LOCAL):
        with open(path, "rb") as f:
            files.append((os.path.basename(path), f.read().replace(b"\r\n", b"\n")))
    for name, data in files:
        r = kindle(f"mkdir -p {KD_DIR} && cat > {KD_DIR}/{name}.new && mv {KD_DIR}/{name}.new {KD_DIR}/{name}",
                   data=data)
        if r.returncode != 0:
            log(f"install {name} failed:", r.stderr.decode(errors="replace").strip())


def heartbeat(light):
    """Every ~20 s while online: tell the watchdog we're alive, keep it running, keep the light
    really off, and report what happened on the Kindle. Returns (ui_back, screensaver_was_on)."""
    cmd = (RPATH + "touch /tmp/kd_heartbeat; "
           "[ -f /tmp/kd_screensaver_on ] && { rm -f /tmp/kd_screensaver_on; echo SS_ON; }; "
           + WATCHDOG_START
           + (f"echo 0 > {FL_SYSFS}; " if light == 0 else "")
           + "status framework")
    r = kindle(cmd, timeout=15)
    return (r.returncode == 0 and b"start/" in r.stdout), b"SS_ON" in r.stdout


class Touch(threading.Thread):
    """Streams raw input events from the Kindle and reports taps (portrait px)."""

    def __init__(self, taps):
        super().__init__(daemon=True)
        self.taps, self.proc, self.stopped = taps, None, False

    def run(self):
        while not self.stopped:
            if not reachable():
                time.sleep(3)
                continue
            self.proc = subprocess.Popen(SSH + ["cat /dev/input/event0"], stdout=subprocess.PIPE,
                                         stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         creationflags=NO_WINDOW)
            x = y = None
            start = None
            while True:
                b = self.proc.stdout.read(16)
                if len(b) < 16:
                    break
                s, us, t, c, v = struct.unpack("<IIHHi", b)
                ts = s + us / 1e6
                if t == 3 and c == 0x35:
                    x = v
                elif t == 3 and c == 0x36:
                    y = v
                elif t == 1 and c == 0x14A:
                    if v == 1 and x is not None and y is not None:
                        start = (x, y, ts)
                    elif v == 0 and start:
                        sx, sy, st = start
                        if ts - st < 0.8 and abs(x - sx) < 60 and abs(y - sy) < 60:
                            self.taps.put((sx, sy))
                        start = None
            if not self.stopped:
                time.sleep(2)

    def stop(self):
        self.stopped = True
        if self.proc:
            self.proc.kill()


def run_kindle():
    # one copy only (the logon autostart and a manual start must not fight over the screen)
    ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\xppen_kindle_map")
    if ctypes.windll.kernel32.GetLastError() == 183:          # ERROR_ALREADY_EXISTS
        log("already running, exiting")
        return
    try:
        if os.path.getsize(LOG) > 1_000_000:
            os.replace(LOG, LOG + ".old")
    except OSError:
        pass
    if os.path.exists(STOP_FLAG):
        os.remove(STOP_FLAG)
    log("started")

    taps = queue.Queue()
    touch = Touch(taps)
    touch.start()
    screen = Screen()
    profiles, mtime = load_profiles(), os.path.getmtime(CONFIG)
    group, shown_key, hits, last_prof = 0, None, [], None
    online, next_check, deliberate = False, 0.0, False
    state = load_state()
    light = state.get("light")              # index into LIGHT_STEPS (None: not known yet)
    light_on = state.get("light_on", 1)     # level "on" goes back to
    calc = Calc(state.get("calc_history", []))
    mode = "map"                            # "map" (XP-Pen keys) or "calc" (calculator)
    pending = ""                            # shell command to run with the next push (frontlight)

    def remember():
        state.update(light=light, light_on=light_on, calc_history=calc.history)
        save_state(state)

    def act(action):
        """Handle one tap. Returns True when the screen layout changes (so the taps still
        queued are handled after the redraw, against the new layout)."""
        nonlocal light, light_on, pending, group, mode
        kind = action[0]
        if kind == "light":
            lt = light if light is not None else 1
            if action[1] == "up":
                lt = min(lt + 1, len(LIGHT_STEPS) - 1)
            elif action[1] == "down":
                lt = max(lt - 1, 0)
            elif action[1] == "toggle":
                lt = 0 if lt else (light_on or 1)
            light = lt
            if lt:
                light_on = lt
            remember()
            pending = light_cmd(light)          # applied with the (partial) redraw
        elif kind == "key":
            if not send_key(action[1]):
                log(f"key {action[2]}: SendInput failed (admin app focused?)")
        elif kind == "group":
            group = action[1]
            return True
        elif kind == "calc":
            mode = "map" if mode == "calc" else "calc"
            return True
        elif kind == "calc_key":
            calc.press(action[1])
            if action[1] == "=" and calc.done:
                remember()                      # the history survives restarts
        elif kind == "calc_hist":
            calc.recall(action[1])
        elif kind == "calc_clear":
            calc.clear_history()
            remember()
        return False

    try:
        while not deliberate:
            try:
                while not os.path.exists(STOP_FLAG):
                    # --- Kindle connection / screen ownership, every 20 s (sooner after a failure)
                    if time.time() >= next_check:
                        next_check = time.time() + 20
                        up = reachable()
                        if up and not online:
                            if take_screen():
                                log("Kindle online, screen taken")
                                online, shown_key = True, None
                                if light is None:              # first run: keep what the Kindle had
                                    light = current_light_idx()
                                    light_on = light or 1
                                    remember()
                                pending = light_cmd(light)     # re-apply after a Kindle reboot
                            else:
                                next_check = time.time() + 10
                        elif up and online:
                            ui_back, ss_was_on = heartbeat(light)
                            if ui_back:
                                log("Kindle UI came back (reboot?), taking the screen again")
                                online = take_screen()
                            if ui_back or ss_was_on:
                                if ss_was_on:
                                    log("screensaver was on (PC was away), redrawing the map")
                                shown_key = None
                                pending = light_cmd(light) if light is not None else ""
                        elif not up and online:
                            log("Kindle offline, waiting for it")
                            online = False

                    # --- driver config edited?
                    try:
                        m = os.path.getmtime(CONFIG)
                        if m != mtime:
                            profiles, mtime, shown_key = load_profiles(), m, None
                            log("config reloaded")
                    except (OSError, ET.ParseError) as e:      # driver may be mid-write
                        log("config read failed:", e)

                    try:
                        exe = foreground_exe()
                    except OSError:
                        exe = ""
                    prof = min(match_profile(profiles, exe), len(profiles) - 1)
                    if prof != last_prof:
                        group, last_prof = 0, prof
                    lt = light if light is not None else 1
                    if mode == "calc":
                        key = ("calc", calc.rev, lt)
                    else:
                        key = ("map", prof, group, lt)
                    # redraw only on a real change, not on every focus change. A new screen
                    # (profile, group, map <-> calc) gets a full flashing refresh; anything else
                    # (a light level, a calculator key) a quick partial one of the changed part only
                    if online and key != shown_key:
                        flash = (shown_key is None or key[0] != shown_key[0]
                                 or (mode == "map" and key[1:3] != shown_key[1:3]))
                        if mode == "calc":
                            img, hits = render_calc(calc, lt)
                        else:
                            img, hits = render(profiles, prof, group, profiles[prof].exe, lt)
                        if screen.show(img, flash=flash, before=pending):
                            shown_key, pending = key, ""
                            if mode == "map":
                                log(f"shown: {profiles[prof].name} / group {GROUP_NAMES[group]} / "
                                    f"light {LIGHT_STEPS[lt]}")
                            elif flash:
                                log(f"shown: calculator / light {LIGHT_STEPS[lt]}")
                        else:
                            online, next_check = False, 0.0      # re-check the link right away

                    try:
                        tap = taps.get(timeout=0.4)
                    except queue.Empty:
                        continue
                    # handle every tap already waiting before redrawing (fast typing on the
                    # calculator), unless one of them changes the layout
                    while tap:
                        action = hit_test(hits, *to_landscape(*tap))
                        if action and act(action):
                            break
                        try:
                            tap = taps.get_nowait()
                        except queue.Empty:
                            tap = None
                deliberate = True                              # stop flag found
            except KeyboardInterrupt:
                deliberate = True
            except Exception as e:                             # keep running no matter what
                log("error:", repr(e))
                shown_key = None
                time.sleep(3)
    finally:
        touch.stop()
        screen.close()
        if os.path.exists(STOP_FLAG):
            os.remove(STOP_FLAG)
        # give the Kindle its normal UI back only on a deliberate stop (--stop / Ctrl+C);
        # if Windows just shuts down, the last map simply stays on the e-ink screen
        if deliberate and reachable():
            log("restoring the normal Kindle UI")
            kindle(DASH_OFF)
        log("stopped")


def stop_running():
    open(STOP_FLAG, "w").close()
    print("Stop requested; the running copy will give the Kindle its UI back within a second or two.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", metavar="DIR", help="render every profile/group to PNGs and exit")
    ap.add_argument("--stop", action="store_true", help="stop the running copy and restore the Kindle UI")
    a = ap.parse_args()
    if a.stop:
        stop_running()
        return
    if a.preview:
        os.makedirs(a.preview, exist_ok=True)
        profiles = load_profiles()
        for i, p in enumerate(profiles):
            for g in range(4):
                if g and not any(k.kind for k in p.groups[g]):
                    continue
                img, _ = render(profiles, i, g, p.exe)
                out = os.path.join(a.preview, f"{i}_{GROUP_NAMES[g]}.png")
                img.save(out)                              # landscape, for easy viewing
                print(out, "-", p.name, GROUP_NAMES[g])
        calc = Calc([{"expr": calc_show("1250") + " + 15%", "result": "1437.5"},
                     {"expr": "2 × 3 + 4", "result": "10"}])
        for k in "1234.5×3+20%":
            calc.press(k)
        img, _ = render_calc(calc)
        out = os.path.join(a.preview, "calc.png")
        img.save(out)
        print(out, "- calculator")
        return
    run_kindle()


if __name__ == "__main__":
    main()
