#!/usr/bin/env python3
"""Super VoIP Bros. —— 马里奥风格语音通话桌面应用（GTK4 + libadwaita）

只负责界面：所有 SIP/媒体逻辑在 voipd 进程里，通过 D-Bus(org.example.Voip1) 通信。
这样 UI 崩溃不会断通话，重启 UI 不重注册。

页面：
  登录（SIP 账号注册）→ 拨号 / 在线文字聊天 → 通话 / 记录 / 设备信息

运行：
    ./voipapp.py [--bus-name org.example.Voip1] [--fullscreen]
"""
import argparse
from datetime import datetime
import json
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
try:
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    HAS_GST = True
except (ImportError, ValueError):
    HAS_GST = False

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

OBJ_PATH = "/org/example/Voip1"
IFACE = "org.example.Voip1"
HERE = os.path.dirname(os.path.abspath(__file__))
RING_FILE = next((path for path in (
    os.path.join(HERE, "..", "assets", "ring.wav"),        # 安装目录
    os.path.join(HERE, "..", "..", "assets", "ring.wav"),  # 源码目录
) if os.path.isfile(path)), "")
DTMF_KEYS = [
    ("1", ""), ("2", "ABC"), ("3", "DEF"),
    ("4", "GHI"), ("5", "JKL"), ("6", "MNO"),
    ("7", "PQRS"), ("8", "TUV"), ("9", "WXYZ"),
    ("*", ""), ("0", "+"), ("#", ""),
]


# ---------------------------------------------------------------- D-Bus 客户端


class VoipClient:
    """voipd 的薄封装。所有方法都是本地 socket 调用，耗时微秒级。"""

    def __init__(self, bus_name):
        self.bus_name = bus_name
        self.conn = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.proxy = Gio.DBusProxy.new_sync(
            self.conn, Gio.DBusProxyFlags.NONE, None,
            bus_name, OBJ_PATH, IFACE, None)

    def call(self, method, params=None, timeout=3000):
        res = self.proxy.call_sync(
            method, params, Gio.DBusCallFlags.NONE, timeout, None)
        return res.unpack()

    def snapshot(self):
        raw = self.call("GetSnapshot")[0]
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            # 服务端偶尔会发出非法值（如 NaN），不应让 UI 卡死
            raise GLib.Error(f"bad snapshot json: {exc}: {raw[:160]}") from exc

    def history(self):
        return json.loads(self.call("GetHistory")[0])

    def devices(self):
        return json.loads(self.call("GetDevices")[0])

    def account(self):
        return json.loads(self.call("GetAccount")[0])

    def chats(self):
        return json.loads(self.call("GetChats")[0])

    def messages(self, peer):
        return json.loads(self.call("GetMessages", GLib.Variant("(s)", (peer,)))[0])

    def send_text(self, peer, text):
        return self.call("SendText", GLib.Variant("(ss)", (peer, text)))

    def set_account(self, server, user, secret, display_name):
        return self.call(
            "SetAccount",
            GLib.Variant("(ssss)", (server, user, secret, display_name)))

    def connect_signal(self, name, handler):
        def _cb(proxy, sender, signal_name, params):
            if signal_name == name:
                handler(*params.unpack())
        return self.proxy.connect("g-signal", _cb)


# ---------------------------------------------------------------- 铃声


class Ringer:
    def __init__(self):
        self.player = None
        self.active = False
        if not HAS_GST:
            return
        Gst.init(None)
        if not os.path.exists(RING_FILE):
            return
        self.player = Gst.ElementFactory.make("playbin", "ring")
        if not self.player:
            return
        self.player.set_property("uri", GLib.filename_to_uri(RING_FILE, None))
        self.player.set_property("volume", 0.6)
        self.player.connect("about-to-finish", self._loop)

    def _loop(self, player):
        if self.active:
            player.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, 0)

    def start(self):
        if not self.player or self.active:
            return
        self.active = True
        self.player.set_state(Gst.State.PLAYING)

    def stop(self):
        self.active = False
        if self.player:
            self.player.set_state(Gst.State.NULL)


# ---------------------------------------------------------------- 主窗口


class VoipWindow(Adw.ApplicationWindow):
    PAGES = (
        ("login", "登录"),
        ("dial", "拨号"),
        ("chat", "聊天"),
        ("history", "记录"),
        ("info", "设备"),
    )
    PAGES_EN = {
        "login": "Login",
        "dial": "Dial",
        "chat": "Chat",
        "history": "History",
        "info": "Device",
    }

    def __init__(self, app, bus_name, fullscreen=False):
        super().__init__(application=app, title="Super VoIP Bros.")
        self.set_default_size(1000, 700)
        if fullscreen:
            self.fullscreen()

        self.client = None
        self.bus_name = bus_name
        self.snap = {}
        self.dial_number = ""
        self.ringer = Ringer()
        self.dtmf_open = False
        self.last_state = ""
        self.hangup_requested_at = 0
        self.last_hist_sig = None
        self._last_devices_sig = None
        self._chat_account = None
        self._last_chat_refresh = 0
        self._chat_list_sig = None
        self._chat_messages_sig = None
        self._connected = False
        self._was_registered = False
        self._account_loaded = False
        self._login_active = False
        self.log_enabled = os.environ.get("VOIP_LOG", "0").strip().lower() in (
            "1", "true", "yes", "on")
        self._last_registration_log = None
        self.nav_buttons = {}
        self.lang = "en" if os.environ.get("VOIP_LANG", "zh").lower().startswith("en") else "zh"
        self._translated_widgets = []

        self._build_ui()
        self._load_css()
        self._try_connect()

        GLib.timeout_add(400, self._tick)

    def _log(self, event, **fields):
        if not self.log_enabled:
            return
        details = " ".join(f"{key}={value!r}" for key, value in fields.items())
        print(f"[voip-ui] {event}" + (f" {details}" if details else ""), flush=True)

    def _t(self, zh, en):
        return en if self.lang == "en" else zh

    def _translate_widget(self, widget, zh, en, setter="set_label"):
        self._translated_widgets.append((widget, zh, en, setter))
        getattr(widget, setter)(self._t(zh, en))

    def _switch_language(self):
        self.lang = "en" if self.lang == "zh" else "zh"
        for widget, zh, en, setter in self._translated_widgets:
            getattr(widget, setter)(self._t(zh, en))
        self.btn_language.set_label("中文" if self.lang == "en" else "English")
        self.last_hist_sig = None
        self._chat_list_sig = None
        self._chat_messages_sig = None
        self._set_login_status("", "")
        if self.stack.get_visible_child_name() == "chat":
            self._refresh_chat()
        self._tick()

    def _logout(self):
        if not self.client:
            self._toast(self._t("通话服务未连接", "Call service disconnected"))
            return
        try:
            ok, error = self.client.call("Logout")
        except GLib.Error as exc:
            self._toast(self._t("退出失败：", "Logout failed: ") + exc.message)
            return
        if not ok:
            msg = self._t("请先结束通话", "End the call before logging out") if error == "call_active" else self._t("无法保存退出状态", "Unable to save logout state")
            self._toast(msg)
            return
        self._login_active = False
        self._was_registered = False
        self._account_loaded = False
        self.snap = {}
        self.lbl_account_info.set_text("")
        self.chat_target = ""
        self._chat_account = None
        self._chat_list_sig = None
        self._chat_messages_sig = None
        self._last_chat_refresh = 0
        self.chat_peer.set_text("")
        self.chat_input.get_buffer().set_text("")
        self._refresh_chat()
        self.in_user.set_text("")
        self.in_secret.set_text("")
        self.in_display.set_text("")
        self._set_login_status(self._t("已退出当前账号", "Logged out"), "ok")
        self._goto("login")
        self._log("logged out")

    # ------------------------------------------------------------ 构建界面

    def _build_ui(self):
        header = Adw.HeaderBar()
        header.add_css_class("mario-hud")

        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        lbl_title = Gtk.Label(label="SUPER VOIP BROS.")
        lbl_title.add_css_class("hud-title")
        lbl_sub = Gtk.Label(label="— A SIP ADVENTURE —")
        lbl_sub.add_css_class("hud-sub")
        title_box.append(lbl_title)
        title_box.append(lbl_sub)
        header.set_title_widget(title_box)

        nav_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        for name, label in self.PAGES:
            btn = Gtk.Button(label=label)
            self._translate_widget(btn, label, self.PAGES_EN[name])
            btn.add_css_class("nav-btn")
            btn.connect("clicked", lambda _b, n=name: self._goto(n))
            self.nav_buttons[name] = btn
            nav_row.append(btn)
        header.pack_start(nav_row)

        self.btn_language = Gtk.Button(label="中文" if self.lang == "en" else "English")
        self.btn_language.connect("clicked", lambda *_: self._switch_language())
        header.pack_end(self.btn_language)

        self.status_chip = Gtk.Label(label="")
        self.status_chip.add_css_class("status-chip")
        header.pack_end(self.status_chip)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._build_login(), "login")
        self.stack.add_named(self._build_dial(), "dial")
        self.stack.add_named(self._build_chat(), "chat")
        self.stack.add_named(self._build_call(), "call")
        self.stack.add_named(self._build_history(), "history")
        self.stack.add_named(self._build_info(), "info")

        stage = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        stage.add_css_class("stage")
        stage.append(self.stack)

        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(stage)

        # libadwaita 1.1（Ubuntu 22.04）没有 Adw.ToolbarView，
        # 直接把 HeaderBar 和内容叠在一个竖直 Box 里即可。
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(header)
        self.toast_overlay.set_vexpand(True)
        root.append(self.toast_overlay)
        self.set_content(root)

        self._goto("login")

    def _goto(self, name):
        if name == "dial" and self.snap.get("state") in (
                "incoming", "outgoing", "ringing", "connected", "incall", "paused"):
            name = "call"
        self.stack.set_visible_child_name(name)
        if name == "chat" and self.client:
            self._refresh_chat()
        for n, btn in self.nav_buttons.items():
            if n == name:
                btn.add_css_class("nav-active")
            else:
                btn.remove_css_class("nav-active")

    # ---- 登录页：SIP 账号注册 ----

    def _build_login(self):
        wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        wrap.set_valign(Gtk.Align.CENTER)
        wrap.set_halign(Gtk.Align.CENTER)

        title = Gtk.Label(label="WORLD 1-1")
        title.add_css_class("login-title")
        wrap.append(title)
        cap = Gtk.Label(label="输入 SIP 账号开始冒险")
        self._translate_widget(cap, "输入 SIP 账号开始冒险", "Enter your SIP account")
        cap.add_css_class("login-caption")
        wrap.append(cap)

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        panel.add_css_class("brick-panel")
        panel.set_size_request(380, -1)

        def field(label_text, label_en, placeholder, visibility=True):
            lbl = Gtk.Label(label=label_text, xalign=0)
            self._translate_widget(lbl, label_text, label_en)
            lbl.add_css_class("field-label")
            panel.append(lbl)
            entry = Gtk.Entry()
            entry.add_css_class("mario-entry")
            entry.set_placeholder_text(placeholder)
            if not visibility:
                entry.set_visibility(False)
            panel.append(entry)
            return entry

        self.in_server = field("服务器地址 (host:port)", "Server (host:port)", "118.31.23.236:9999")
        self.in_user = field("账号 / 分机号", "Account / extension", "1002")
        self.in_secret = field("密码", "Password", "••••••", visibility=False)
        self.in_display = field("显示名称（可选）", "Display name (optional)", "1P Mario")

        btn_login = Gtk.Button(label="跳起来注册！")
        self._translate_widget(btn_login, "跳起来注册！", "Register")
        btn_login.add_css_class("btn-green")
        btn_login.set_margin_top(6)
        btn_login.connect("clicked", lambda *_: self._do_login())
        panel.append(btn_login)
        self.btn_login = btn_login

        self.lbl_login_status = Gtk.Label(label="", wrap=True)
        self.lbl_login_status.add_css_class("login-status")
        panel.append(self.lbl_login_status)

        wrap.append(panel)
        return wrap

    def _do_login(self):
        if not self.client:
            self._set_login_status(self._t("还没连上 voipd，请稍候再试…", "Connecting to call service…"), "bad")
            return
        server = self.in_server.get_text().strip()
        user = self.in_user.get_text().strip()
        secret = self.in_secret.get_text()
        display = self.in_display.get_text().strip() or user

        if not server or not user:
            self._log("login rejected locally", reason="missing server or user")
            self._set_login_status(self._t("❗ 服务器地址和账号不能为空", "❗ Server and account are required"), "bad")
            return

        self._log("login submitted", server=server, user=user)
        self.btn_login.set_sensitive(False)
        self._set_login_status(self._t("正在敲门（注册中）…", "Registering…"), "")
        try:
            ok, err = self.client.set_account(server, user, secret, display)
        except GLib.Error as e:
            ok, err = False, e.message
        self.btn_login.set_sensitive(True)
        if not ok:
            self._log("login request failed", user=user, error=err or "unknown")
            self._set_login_status(self._t("失败：", "Failed: ") + (err or self._t("未知错误", "Unknown error")), "bad")
            return
        self._log("registration request accepted", user=user, server=server)
        self._set_login_status(self._t("已提交，等待服务器应答…", "Waiting for server…"), "")
        self._login_active = True
        self._login_wait_ticks = 0
        GLib.timeout_add(300, self._poll_login_result)

    def _poll_login_result(self):
        if not self.client or not self._login_active:
            return False
        self._login_wait_ticks = getattr(self, "_login_wait_ticks", 0) + 1
        try:
            acc = self.client.account()
        except GLib.Error:
            return self._login_wait_ticks < 20
        if acc.get("registered"):
            self._log("registration succeeded", user=acc.get("user"), server=acc.get("server"))
            self._set_login_status(self._t("🚩 注册成功！", "🚩 Registration successful!"), "ok")
            self._toast(self._t("已注册为 ", "Registered as ") + str(acc.get('user')))
            GLib.timeout_add(500, self._finish_login)
            return False
        detail = acc.get("reg_detail") or ""
        if detail and detail not in ("", "Registration in progress"):
            self._log("registration failed", user=acc.get("user"), server=acc.get("server"),
                      error=detail)
            self._login_active = False
            self._set_login_status(self._t("注册失败：", "Registration failed: ") + detail, "bad")
            return False
        if self._login_wait_ticks >= 20:
            self._log("registration timed out", user=acc.get("user"), server=acc.get("server"))
            self._login_active = False
            self._set_login_status(self._t("超时未收到服务器应答，请检查网络/服务器地址", "Server timeout; check network and address"), "bad")
            return False
        return True

    def _finish_login(self):
        if self._login_active:
            self._login_active = False
            self._goto("dial")
        return False

    def _set_login_status(self, text, kind):
        self.lbl_login_status.set_text(text)
        self.lbl_login_status.remove_css_class("ok")
        self.lbl_login_status.remove_css_class("bad")
        if kind:
            self.lbl_login_status.add_css_class(kind)

    def _prefill_login(self):
        if not self.client or self._account_loaded:
            return
        try:
            acc = self.client.account()
        except GLib.Error:
            return
        self._account_loaded = True
        if acc.get("server"):
            self.in_server.set_text(acc["server"].replace("sip:", "", 1))
        if acc.get("user"):
            self.in_user.set_text(acc["user"])
        if acc.get("display_name"):
            self.in_display.set_text(acc["display_name"])
        if acc.get("registered"):
            self._set_login_status(self._t("🚩 当前已注册，可直接前往拨号页", "🚩 Registered; ready to call"), "ok")
            self._goto("dial")

    # ---- 拨号页 ----

    def _build_dial(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        outer.set_halign(Gtk.Align.CENTER)
        outer.set_valign(Gtk.Align.CENTER)

        lcd = Gtk.Box()
        lcd.add_css_class("lcd-screen")
        self.entry_number = Gtk.Entry()
        self.entry_number.add_css_class("lcd-entry")
        self.entry_number.set_alignment(0.5)
        self.entry_number.set_editable(False)
        self.entry_number.set_can_focus(False)
        self.entry_number.set_placeholder_text("输入分机号")
        self._translate_widget(self.entry_number, "输入分机号", "Extension", "set_placeholder_text")
        self.entry_number.set_hexpand(True)
        lcd.append(self.entry_number)
        outer.append(lcd)

        grid = Gtk.Grid(row_spacing=8, column_spacing=8)
        grid.add_css_class("keypad")
        grid.set_halign(Gtk.Align.CENTER)
        for i, (key, sub) in enumerate(DTMF_KEYS):
            grid.attach(self._dial_key(key, sub), i % 3, i // 3, 1, 1)
        outer.append(grid)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.add_css_class("dial-actions")
        row.set_halign(Gtk.Align.CENTER)
        btn_back = Gtk.Button(label="退格")
        self._translate_widget(btn_back, "退格", "Backspace")
        btn_back.add_css_class("btn-yellow")
        btn_back.connect("clicked", lambda *_: self._key_back())
        btn_call = Gtk.Button(label="拨打")
        self._translate_widget(btn_call, "拨打", "Call")
        btn_call.add_css_class("btn-green")
        btn_call.connect("clicked", lambda *_: self._dial())
        btn_clear = Gtk.Button(label="清空")
        self._translate_widget(btn_clear, "清空", "Clear")
        btn_clear.add_css_class("btn-red")
        btn_clear.connect("clicked", lambda *_: self._key_clear())
        row.append(btn_back)
        row.append(btn_call)
        row.append(btn_clear)
        outer.append(row)
        return outer

    def _dial_key(self, key, sub):
        btn = Gtk.Button()
        btn.add_css_class("key-btn")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_halign(Gtk.Align.CENTER)
        lbl = Gtk.Label(label=key)
        lbl.add_css_class("key-main")
        box.append(lbl)
        if sub:
            s = Gtk.Label(label=sub)
            s.add_css_class("key-sub")
            box.append(s)
        btn.set_child(box)
        btn.connect("clicked", lambda *_: self._key_press(key))
        return btn

    # ---- 通话页 ----

    def _build_call(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("card")
        card.add_css_class("call-card")
        self.lbl_peer = Gtk.Label(label="")
        self.lbl_peer.add_css_class("call-peer")
        card.append(self.lbl_peer)
        self.lbl_callstate = Gtk.Label(label="")
        self.lbl_callstate.add_css_class("call-state")
        card.append(self.lbl_callstate)
        self.lbl_stats = Gtk.Label(label="")
        self.lbl_stats.add_css_class("call-stats")
        self.lbl_stats.set_wrap(True)
        self.lbl_stats.set_justify(Gtk.Justification.CENTER)
        card.append(self.lbl_stats)
        box.append(card)

        self.answer_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        self.answer_row.set_halign(Gtk.Align.CENTER)
        btn_decline = Gtk.Button(label="拒接")
        self._translate_widget(btn_decline, "拒接", "Decline")
        btn_decline.add_css_class("btn-red")
        btn_decline.connect("clicked", lambda *_: self._cmd("Decline"))
        btn_answer = Gtk.Button(label="接听")
        self._translate_widget(btn_answer, "接听", "Answer")
        btn_answer.add_css_class("btn-green")
        btn_answer.connect("clicked", lambda *_: self._cmd("Answer"))
        self.answer_row.append(btn_decline)
        self.answer_row.append(btn_answer)
        box.append(self.answer_row)

        self.incall_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.incall_row.set_halign(Gtk.Align.CENTER)
        self.btn_mute = Gtk.Button(label="静音")
        self._translate_widget(self.btn_mute, "静音", "Mute")
        self.btn_mute.add_css_class("btn-blue")
        self.btn_mute.connect("clicked", lambda *_: self._toggle_mute())
        self.btn_dtmf = Gtk.Button(label="拨号键")
        self._translate_widget(self.btn_dtmf, "拨号键", "Keypad")
        self.btn_dtmf.add_css_class("btn-blue")
        self.btn_dtmf.connect("clicked", lambda *_: self._toggle_dtmf())
        self.btn_speaker = Gtk.Button(label="免提")
        self._translate_widget(self.btn_speaker, "免提", "Speaker")
        self.btn_speaker.add_css_class("btn-blue")
        self.btn_speaker.connect("clicked", lambda *_: self._toggle_speaker())
        self.btn_hangup = Gtk.Button(label="挂断")
        self._translate_widget(self.btn_hangup, "挂断", "Hang up")
        self.btn_hangup.add_css_class("btn-red")
        self.btn_hangup.connect("clicked", lambda *_: self._cmd("Hangup"))
        for w in (self.btn_mute, self.btn_dtmf, self.btn_speaker, self.btn_hangup):
            self.incall_row.append(w)
        box.append(self.incall_row)

        self.dtmf_grid = Gtk.Grid(row_spacing=6, column_spacing=6)
        self.dtmf_grid.set_halign(Gtk.Align.CENTER)
        for i, (key, _) in enumerate(DTMF_KEYS):
            b = Gtk.Button(label=key)
            b.add_css_class("dtmf-key")
            b.connect("clicked", lambda _b, k=key: self._send_dtmf(k))
            self.dtmf_grid.attach(b, i % 3, i // 3, 1, 1)
        self.dtmf_grid.set_visible(False)
        box.append(self.dtmf_grid)

        return box

    # ---- 在线文字聊天 ----

    def _build_chat(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.add_css_class("card")
        heading = Gtk.Label(label="在线文字聊天", xalign=0)
        self._translate_widget(heading, "在线文字聊天", "Online text chat")
        heading.add_css_class("section-title")
        outer.append(heading)

        tip = Gtk.Label(label="仅支持在线 SIP 分机；本次运行中保留最近 200 条，不保证离线收取。",
                        xalign=0, wrap=True)
        self._translate_widget(tip, "仅支持在线 SIP 分机；本次运行中保留最近 200 条，不保证离线收取。",
                               "Online SIP extensions only; the last 200 messages are shown during this run. No offline delivery guarantee.")
        tip.add_css_class("hint")
        outer.append(tip)

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        content.set_vexpand(True)
        peers = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        peers.add_css_class("chat-peers")
        peers.set_size_request(190, -1)
        peer_label = Gtk.Label(label="会话", xalign=0)
        self._translate_widget(peer_label, "会话", "Conversations")
        peer_label.add_css_class("hist-peer")
        peers.append(peer_label)
        peer_scroll = Gtk.ScrolledWindow()
        peer_scroll.set_vexpand(True)
        peer_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.list_chats = Gtk.ListBox()
        self.list_chats.add_css_class("chat-list")
        self.list_chats.set_selection_mode(Gtk.SelectionMode.NONE)
        peer_scroll.set_child(self.list_chats)
        peers.append(peer_scroll)
        content.append(peers)

        chat = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        chat.set_hexpand(True)
        target_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.chat_peer = Gtk.Entry()
        self.chat_peer.set_hexpand(True)
        self.chat_peer.set_placeholder_text("输入对方分机号（如 1002）")
        self._translate_widget(self.chat_peer, "输入对方分机号（如 1002）", "Peer extension", "set_placeholder_text")
        # 占位文案保持简短，避免窄窗口下被截断成“…”。
        self.chat_peer.set_width_chars(24)
        self.chat_peer.add_css_class("mario-entry")
        self.chat_peer.connect("activate", lambda *_: self._open_chat())
        target_row.append(self.chat_peer)
        btn_open = Gtk.Button(label="打开会话")
        self._translate_widget(btn_open, "打开会话", "Open chat")
        btn_open.add_css_class("btn-blue")
        btn_open.connect("clicked", lambda *_: self._open_chat())
        target_row.append(btn_open)
        chat.append(target_row)

        self.chat_title = Gtk.Label(label="", xalign=0)
        self.chat_title.add_css_class("hist-peer")
        chat.append(self.chat_title)
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_min_content_height(220)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.chat_messages = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.chat_messages.add_css_class("chat-messages")
        scroll.set_child(self.chat_messages)
        chat.append(scroll)

        input_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        text_scroll = Gtk.ScrolledWindow()
        text_scroll.set_hexpand(True)
        text_scroll.set_min_content_height(70)
        self.chat_input = Gtk.TextView()
        self.chat_input.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.chat_input.set_accepts_tab(False)
        self.chat_input.add_css_class("chat-input")
        text_scroll.set_child(self.chat_input)
        input_row.append(text_scroll)
        btn_send = Gtk.Button(label="发送")
        self._translate_widget(btn_send, "发送", "Send")
        btn_send.add_css_class("btn-green")
        btn_send.connect("clicked", lambda *_: self._send_text())
        input_row.append(btn_send)
        chat.append(input_row)
        content.append(chat)
        outer.append(content)
        return outer

    # ---- 记录页 ----

    def _build_history(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        outer.add_css_class("card")
        outer.set_margin_top(4)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        t = Gtk.Label(label="关卡记录 / 通话历史", xalign=0)
        self._translate_widget(t, "关卡记录 / 通话历史", "Call history")
        t.add_css_class("section-title")
        t.set_hexpand(True)
        head.append(t)
        btn_clear_hist = Gtk.Button(label="清空")
        self._translate_widget(btn_clear_hist, "清空", "Clear")
        btn_clear_hist.add_css_class("btn-yellow")
        btn_clear_hist.connect("clicked", lambda *_: self._clear_history())
        head.append(btn_clear_hist)
        outer.append(head)

        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.set_min_content_height(380)
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.list_hist = Gtk.ListBox()
        self.list_hist.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_hist.add_css_class("hist-list")
        sw.set_child(self.list_hist)
        outer.append(sw)
        return outer

    # ---- 设备信息页 ----

    def _build_info(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.add_css_class("card")
        box.set_margin_top(4)

        t0 = Gtk.Label(label="当前账号", xalign=0)
        self._translate_widget(t0, "当前账号", "Current account")
        t0.add_css_class("section-title")
        box.append(t0)
        self.lbl_account_info = Gtk.Label(label="", xalign=0, wrap=True)
        self.lbl_account_info.add_css_class("mono")
        box.append(self.lbl_account_info)

        btn_logout = Gtk.Button(label="退出当前账号")
        self._translate_widget(btn_logout, "退出当前账号", "Log out")
        btn_logout.add_css_class("btn-red")
        btn_logout.connect("clicked", lambda *_: self._logout())
        box.append(btn_logout)

        t = Gtk.Label(label="音频设备（由 voipd 报告）", xalign=0)
        self._translate_widget(t, "音频设备（由 voipd 报告）", "Audio devices")
        t.add_css_class("section-title")
        box.append(t)

        self.lbl_devices_summary = Gtk.Label(label="", xalign=0, wrap=True)
        self.lbl_devices_summary.add_css_class("mono")
        box.append(self.lbl_devices_summary)

        dev_scroll = Gtk.ScrolledWindow()
        dev_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        dev_scroll.set_min_content_height(160)
        self.box_devices = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.box_devices.add_css_class("device-list")
        dev_scroll.set_child(self.box_devices)
        box.append(dev_scroll)

        tip_dev = Gtk.Label(
            label="这里只展示 liblinphone 看到的设备，不能在此切换。"
                  "实际使用的输入/输出设备由 voipd.conf 和系统声音设置决定。",
            xalign=0, wrap=True)
        tip_dev.add_css_class("hint")
        self._translate_widget(tip_dev, "这里只展示 liblinphone 看到的设备，不能在此切换。"
                                       "实际使用的输入/输出设备由 voipd.conf 和系统声音设置决定。",
                               "Read-only: these are the devices liblinphone sees. "
                               "The active input/output is chosen by voipd.conf and the desktop sound settings.")
        box.append(tip_dev)

        tip = Gtk.Label(
            label="想换账号 / 服务器？回到「登录」页重新填写即可，"
                  "不需要手动改配置文件或重启服务。",
            xalign=0, wrap=True)
        tip.add_css_class("hint")
        self._translate_widget(tip, "想换账号 / 服务器？回到「登录」页重新填写即可，不需要手动改配置文件或重启服务。", "Change your account or server on the login page.")
        box.append(tip)
        return box

    def _load_css(self):
        css_path = os.path.join(HERE, "style.css")
        if not os.path.exists(css_path):
            return
        prov = Gtk.CssProvider()
        prov.load_from_path(css_path)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # ------------------------------------------------------------ 连接 voipd

    def _try_connect(self):
        try:
            self.client = VoipClient(self.bus_name)
            self.client.connect_signal("IncomingCall", self._on_incoming)
            self.client.connect_signal("CallStateChanged", self._on_state)
            self.client.connect_signal("RegistrationChanged", self._on_reg)
            self.client.connect_signal("CallEnded", self._on_ended)
            self.client.connect_signal("MessageReceived", self._on_message_received)
            self.client.connect_signal("MessageStateChanged", self._on_message_state)
            self._connected = True
            self._toast(self._t("已连接 voipd", "Connected to call service"))
        except GLib.Error:
            self.client = None
            self._connected = False
            self._set_login_status(self._t("正在连接 voipd…", "Connecting to call service…"), "")
            GLib.timeout_add(1500, self._retry_connect)
        return False

    def _retry_connect(self):
        if self._connected:
            return False
        try:
            self.client = VoipClient(self.bus_name)
            self.client.connect_signal("IncomingCall", self._on_incoming)
            self.client.connect_signal("CallStateChanged", self._on_state)
            self.client.connect_signal("RegistrationChanged", self._on_reg)
            self.client.connect_signal("CallEnded", self._on_ended)
            self.client.connect_signal("MessageReceived", self._on_message_received)
            self.client.connect_signal("MessageStateChanged", self._on_message_state)
            self._connected = True
            self._toast(self._t("已连接 voipd", "Connected to call service"))
            return False
        except GLib.Error:
            return True

    # ------------------------------------------------------------ 信号处理

    def _on_incoming(self, peer, display):
        self.ringer.start()
        self._goto("call")

    def _on_state(self, state, peer, detail):
        if state in ("incoming", "outgoing"):
            self._goto("call")
        if state in ("ended", "idle"):
            self.ringer.stop()
            self.dtmf_open = False
            self.dtmf_grid.set_visible(False)
            GLib.timeout_add(1200, self._back_from_call_if_idle)

    def _back_from_call_if_idle(self):
        if self.snap.get("state") in ("idle", "ended") and \
                self.stack.get_visible_child_name() == "call":
            self._goto("dial")
        return False

    def _on_ended(self, peer, duration, status):
        self.ringer.stop()
        self.last_hist_sig = None  # 使下一次 tick 重新拉取记录
        self._last_devices_sig = None  # 通话可能改变实际使用的设备

    def _on_reg(self, registered, detail):
        pass

    def _on_message_received(self, peer, _id):
        if self.stack.get_visible_child_name() == "chat" and not self.chat_target:
            self._choose_chat(peer)
        else:
            self._refresh_chat()
        if self.stack.get_visible_child_name() != "chat" or not self._chat_matches(peer):
            self._toast(self._t("收到来自 ", "New message from ") + peer)

    def _on_message_state(self, peer, _id, state):
        if self.stack.get_visible_child_name() == "chat" and self._chat_matches(peer):
            self._refresh_chat()
        if state == "failed":
            self._toast(self._t("消息发送失败：", "Message failed: ") + peer)

    # ------------------------------------------------------------ 轮询刷新

    def _connection_text(self, s):
        if s.get("registered"):
            return self._t("已连接", "Connected")
        state = s.get("state", "")
        if state == "registering" or self._login_active:
            return self._t("连接中", "Connecting")
        if state == "registered_failed":
            return self._t("连接失败", "Failed")
        return self._t("未连接", "Disconnected")

    def _tick(self):
        if not self._connected or not self.client:
            return True

        self._prefill_login()

        try:
            self.snap = self.client.snapshot()
        except GLib.Error as exc:
            self._snap_errors = getattr(self, "_snap_errors", 0) + 1
            if self._snap_errors in (1, 10):
                print(f"[voip-ui] snapshot skipped: {exc.message}", flush=True)
            return True
        self._snap_errors = 0

        s = self.snap
        account = (s.get("account", ""), s.get("server", ""))
        if self._chat_account is not None and account != self._chat_account:
            self.chat_target = ""
            self.chat_peer.set_text("")
            self.chat_input.get_buffer().set_text("")
            self._refresh_chat()
        self._chat_account = account
        if self.stack.get_visible_child_name() == "chat" and \
                GLib.get_monotonic_time() - self._last_chat_refresh >= 5_000_000:
            self._refresh_chat()
        state = s.get("state", "idle")
        reg = s.get("registered", False)
        registration_log = (bool(reg), s.get("reg_detail", ""))
        if registration_log != self._last_registration_log:
            self._last_registration_log = registration_log
            self._log("registration state", registered=reg,
                      account=s.get("account", ""), server=s.get("server", ""),
                      detail=s.get("reg_detail", ""))

        dot = "●" if reg else "●"
        klass = "dot-on" if reg else "dot-off"
        # 顶部只展示用户与连接状态，不展示服务器地址和端口。
        account = s.get("account", "")
        if account:
            self.status_chip.set_text(
                f'{dot} {account}  {self._connection_text(s)}')
        else:
            self.status_chip.set_text(f'{dot} {self._t("未登录", "Not signed in")}')
        self.status_chip.remove_css_class("dot-on")
        self.status_chip.remove_css_class("dot-off")
        self.status_chip.add_css_class(klass)

        if reg and not self._was_registered and \
                self.stack.get_visible_child_name() == "login":
            self._goto("dial")
        self._was_registered = reg

        # 呼叫可能在两次轮询之间完成，前后都是 idle；即使状态值没有变化，
        # 也要纠正仍停在通话页的界面。
        call_page_finished = (self.stack.get_visible_child_name() == "call" and
                              state in ("idle", "ended", "registering", "registered", "registered_failed"))
        if state != self.last_state or call_page_finished:
            self._apply_state_ui(state, s)
            self.last_state = state

        if self.hangup_requested_at:
            if state in ("idle", "ended", "registering", "registered", "registered_failed"):
                self.hangup_requested_at = 0
            elif GLib.get_monotonic_time() - self.hangup_requested_at >= 5_000_000:
                self.hangup_requested_at = 0
                self._log("hangup still pending", state=state)
                self._toast(self._t("挂断尚未完成，请重试并查看日志", "Hangup is pending; retry and check the log"))

        if state in ("incall", "connected", "outgoing", "ringing", "incoming", "paused"):
            self._update_call_page(s)

        try:
            hist = self.client.history()
            sig = json.dumps(hist)
            if sig != self.last_hist_sig:
                self.last_hist_sig = sig
                self._render_history(hist)
        except GLib.Error:
            pass

        # 设备列表变化很少，进页时拉一次，内容变了才重建控件，避免每 400ms 抖一遍。
        if self.stack.get_visible_child_name() == "info":
            self._refresh_devices()

        try:
            self.lbl_account_info.set_text(
                f'{self._t("账号", "Account")}: {s.get("account","-")}\n'
                f'{self._t("服务器", "Server")}: {s.get("server","-")}\n'
                f'{self._t("本机 IP", "Local IP")}: {s.get("local_ip","-")}:{s.get("sip_port","-")}\n'
                f'{self._t("注册状态", "Registration")}: {self._t("已注册", "Registered") if reg else self._t("未注册", "Not registered")} ({s.get("reg_detail","-")})')
        except Exception:
            pass

        return True

    def _update_call_controls(self, state):
        incoming = state == "incoming"
        incall = state in ("incall", "paused")
        calling = state in ("outgoing", "ringing", "connected")
        self.answer_row.set_visible(incoming)
        # 静音 / DTMF / 免提只在通话中才出现；拨号、回铃和媒体未就绪阶段只留挂断。
        self.btn_mute.set_visible(incall)
        self.btn_speaker.set_visible(incall)
        self.incall_row.set_visible(incoming or incall or calling)
        self.btn_dtmf.set_visible(incall)
        self.btn_hangup.set_visible(incall or calling)

    def _apply_state_ui(self, state, s):
        previous = self.last_state
        if previous != state:
            self._log("call state", previous=previous, state=state,
                      peer=s.get("peer_display") or s.get("peer") or "",
                      detail=s.get("last_error") or s.get("detail") or "")
        if state in ("incoming", "outgoing", "ringing", "connected", "incall", "paused"):
            self._goto("call")
        if state in ("idle", "ended", "registering", "registered", "registered_failed") and (previous in {
                "incoming", "outgoing", "ringing", "connected", "incall", "paused", "error"}
                or self.stack.get_visible_child_name() == "call"):
            self.ringer.stop()
            self.dtmf_open = False
            self.dtmf_grid.set_visible(False)
            self._goto("dial")
            self.dial_number = ""
            self.entry_number.set_text("")
            error = s.get("last_error") or ""
            if error:
                self._toast(self._t("呼叫结束：", "Call ended: ") + error)
            else:
                self._toast(self._t("通话已结束", "Call ended"))
        self._update_call_controls(state)
        if state not in ("incall", "paused") and self.dtmf_open:
            self.dtmf_open = False
            self.dtmf_grid.set_visible(False)
        if state == "incoming":
            self.lbl_peer.set_text(s.get("peer_display") or s.get("peer") or "")
        elif state in ("outgoing", "ringing"):
            self.ringer.stop()
            self.lbl_peer.set_text(s.get("peer_display") or s.get("peer") or "")
        elif state == "idle":
            self.ringer.stop()

    def _update_call_page(self, s):
        peer = s.get("peer_display") or s.get("peer") or ""
        self.lbl_peer.set_text(peer)
        st = s.get("state")
        self._update_call_controls(st)
        dur = int(s.get("duration", 0))
        names = {
            "incoming": self._t("来电", "Incoming"), "outgoing": self._t("正在呼叫", "Calling"), "ringing": self._t("对方振铃", "Ringing"),
            "connected": self._t("已接通", "Connected"), "incall": self._t("通话中", "In call"), "paused": self._t("已保持", "On hold"),
        }
        txt = names.get(st, st)
        # 回铃阶段还没有真正接通，不计时；只在媒体建立后显示时长。
        if st in ("incall", "paused") and dur > 0:
            txt = f'{self._t("通话中", "In call")}  {dur // 60:02d}:{dur % 60:02d}'
        self.lbl_callstate.set_text(txt)
        q = s.get("quality", -1)
        self.lbl_stats.set_text(
            f'{self._t("质量", "Quality")} {q:.1f}/5   {self._t("抖动", "Jitter")} {s.get("jitter_ms",0):.0f} ms   '
            f'{self._t("丢包", "Loss")} {s.get("loss_pct",0):.1f}%   {self._t("收发", "Up/down")} {s.get("up_kbps",0):.0f}/'
            f'{s.get("down_kbps",0):.0f} kbps   RTP {s.get("rtp_rx",0)}/{s.get("rtp_tx",0)}')
        muted = s.get("muted", False)
        self.btn_mute.set_label(self._t("取消静音", "Unmute") if muted else self._t("静音", "Mute"))
        self.btn_speaker.set_label(self._t("免提开", "Speaker on") if s.get("speaker") else self._t("免提", "Speaker"))

    # ------------------------------------------------------------ 拨号操作

    def _key_press(self, k):
        if self.stack.get_visible_child_name() == "call" and self.dtmf_open:
            self._send_dtmf(k)
            return
        if len(self.dial_number) < 32:
            self.dial_number += k
            self.entry_number.set_text(self.dial_number)

    def _key_back(self):
        self.dial_number = self.dial_number[:-1]
        self.entry_number.set_text(self.dial_number)

    def _key_clear(self):
        self.dial_number = ""
        self.entry_number.set_text("")

    def _self_extension(self):
        """当前登录分机号，用于阻止拨打自己。"""
        return (self.snap.get("account") or "").strip()

    @staticmethod
    def _extension_of(target):
        """从纯号码或 sip:/tel: URI 中取分机号部分。"""
        t = (target or "").strip()
        for prefix in ("sip:", "sips:", "tel:"):
            if t.lower().startswith(prefix):
                t = t[len(prefix):]
                break
        t = t.split("@", 1)[0]
        t = t.split(";", 1)[0]
        return t

    def _dial(self):
        n = self.dial_number.strip()
        if not n or not self.client:
            return
        me = self._self_extension()
        if me and self._extension_of(n) == me:
            self._log("dial rejected locally", destination=n, reason="self")
            self._toast(self._t("不能拨打本机分机号", "You cannot call your own extension"))
            return
        self._log("dial requested", destination=n)
        try:
            ok = self.client.call("Dial", GLib.Variant("(s)", (n,)))[0]
        except GLib.Error as e:
            self._log("dial request failed", destination=n, error=e.message)
            self._toast(self._t("呼叫失败: ", "Call failed: ") + e.message)
            return
        if ok:
            self._log("dial accepted by voipd", destination=n)
            self._goto("call")
        else:
            error = self.snap.get("last_error") or self._t("请检查注册状态", "Check registration")
            self._log("dial rejected by voipd", destination=n, error=error)
            self._goto("dial")
            self._toast(self._t("呼叫失败：", "Call failed: ") + error)

    def _cmd(self, method):
        if not self.client:
            self._log("call command ignored: voipd disconnected", command=method)
            self._toast(self._t("通话服务未连接", "Call service disconnected"))
            return
        self._log("call command", command=method,
                  peer=self.snap.get("peer_display") or self.snap.get("peer") or "")
        try:
            result = self.client.call(method)
            if method == "Hangup" and result and not result[0]:
                self._log("call command rejected", command=method)
                self._toast(self._t("挂断失败，请重试", "Hangup failed; retry"))
                return
            self._log("call command accepted", command=method)
            if method == "Hangup":
                self.hangup_requested_at = GLib.get_monotonic_time()
                self.lbl_callstate.set_text(self._t("正在挂断…", "Hanging up…"))
        except GLib.Error as e:
            self._log("call command failed", command=method, error=e.message)
            self._toast(f'{method}: {self._t("失败", "Failed")}: {e.message}')

    def _toggle_mute(self):
        if not self.client:
            return
        try:
            self.client.call("SetMute", GLib.Variant("(b)", (not self.snap.get("muted"),)))
        except GLib.Error:
            pass

    def _toggle_speaker(self):
        if not self.client:
            return
        try:
            self.client.call("SetSpeaker", GLib.Variant("(b)", (not self.snap.get("speaker"),)))
            self._toast(self._t("音频路由由系统 PipeWire 决定；此处仅记录状态", "Audio routing is controlled by PipeWire"))
        except GLib.Error:
            pass

    def _toggle_dtmf(self):
        self.dtmf_open = not self.dtmf_open
        self.dtmf_grid.set_visible(self.dtmf_open)

    def _send_dtmf(self, k):
        if not self.client:
            return
        try:
            self.client.call("SendDTMF", GLib.Variant("(s)", (k,)))
        except GLib.Error:
            pass

    # ------------------------------------------------------------ 文字聊天

    def _chat_matches(self, peer):
        target = self.chat_target
        if not target:
            return False
        if target == peer:
            return True
        target = target.removeprefix("sip:")
        remote = peer.removeprefix("sip:")
        if "@" in target and "@" in remote:
            user, domain = target.split("@", 1)
            remote_user, remote_domain = remote.split("@", 1)
            return user == remote_user and domain.split(":", 1)[0].lower() == remote_domain.split(":", 1)[0].lower()
        # 手动输入分机号时，后台会补全为当前 SIP 域。
        return "@" not in target and remote.split("@", 1)[0] == target

    def _choose_chat(self, peer):
        self.chat_target = peer
        self.chat_peer.set_text(peer)
        self._chat_messages_sig = None
        self._refresh_chat()

    def _open_chat(self):
        peer = self.chat_peer.get_text().strip()
        if not peer:
            self._toast(self._t("请输入对方分机号", "Enter a peer extension"))
            return
        self._choose_chat(peer)

    def _send_text(self):
        if not self.client:
            self._toast(self._t("通话服务未连接", "Call service disconnected"))
            return
        peer = self.chat_peer.get_text().strip()
        buf = self.chat_input.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        if not peer:
            self._toast(self._t("请输入对方分机号", "Enter a peer extension"))
            return
        if not text.strip() or len(text.encode("utf-8")) > 4096:
            self._toast(self._t("请输入不超过 4096 字节的文字", "Enter text of up to 4096 bytes"))
            return
        try:
            accepted, _id, error = self.client.send_text(peer, text)
        except GLib.Error as exc:
            self._toast(self._t("消息提交失败：", "Message request failed: ") + exc.message)
            return
        if not accepted:
            errors = {
                "not_registered": self._t("请先注册 SIP 账号", "Register a SIP account first"),
                "invalid_peer": self._t("分机号或 SIP 地址无效", "Invalid peer or SIP address"),
                "invalid_text": self._t("消息内容无效", "Invalid message"),
                "chat_unavailable": self._t("无法创建会话", "Cannot open chat"),
                "chat_resources_missing": self._t("缺少 liblinphone 聊天资源：请安装 linphone-common", "Missing liblinphone chat resources: install linphone-common"),
            }
            self._toast(errors.get(error, self._t("发送失败", "Send failed")))
            return
        self.chat_target = peer
        buf.set_text("")
        self._refresh_chat()  # 本地受理后马上显示“发送中”，不冒充送达

    def _refresh_chat(self):
        if not self.client:
            return
        try:
            chats = self.client.chats()
            messages = self.client.messages(self.chat_target) if self.chat_target else []
        except (GLib.Error, ValueError):
            return
        self._last_chat_refresh = GLib.get_monotonic_time()
        chats_sig = json.dumps(chats, ensure_ascii=False)
        if chats_sig != self._chat_list_sig:
            self._chat_list_sig = chats_sig
            self._render_chats(chats)
        messages_sig = (self.chat_target, json.dumps(messages, ensure_ascii=False))
        if messages_sig != self._chat_messages_sig:
            self._chat_messages_sig = messages_sig
            self._render_chat_messages(messages)

    def _render_chats(self, chats):
        child = self.list_chats.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.list_chats.remove(child)
            child = nxt
        for chat in chats:
            peer = chat.get("peer", "")
            if not peer:
                continue
            btn = Gtk.Button(label=peer)
            btn.add_css_class("chat-peer-btn")
            btn.connect("clicked", lambda _b, p=peer: self._choose_chat(p))
            self.list_chats.append(btn)

    def _render_chat_messages(self, messages):
        self.chat_title.set_text(self.chat_target or self._t("选择或输入分机号", "Select or enter a peer"))
        child = self.chat_messages.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.chat_messages.remove(child)
            child = nxt
        if not messages:
            hint = Gtk.Label(label=self._t("暂无本次会话的消息", "No messages in this session"))
            hint.add_css_class("hint")
            self.chat_messages.append(hint)
            return
        for msg in messages:
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            row.set_halign(Gtk.Align.END if msg.get("outgoing") else Gtk.Align.START)
            row.add_css_class("chat-outgoing" if msg.get("outgoing") else "chat-incoming")
            body = Gtk.Label(label=msg.get("text", ""), xalign=0, wrap=True)
            body.set_max_width_chars(55)
            body.set_selectable(True)
            body.add_css_class("chat-body")
            row.append(body)
            states = {
                "sending": self._t("发送中", "Sending"),
                "server_accepted": self._t("服务器已接受", "Server accepted"),
                "delivered": self._t("对端已收到", "Delivered to peer"),
                "displayed": self._t("对端已显示", "Displayed by peer"),
                "failed": self._t("发送失败", "Failed"),
                "received": self._t("收到", "Received"),
            }
            try:
                when = datetime.fromtimestamp(int(msg.get("time", 0))).strftime("%H:%M")
            except (ValueError, OverflowError, TypeError):
                when = "--:--"
            status = Gtk.Label(label=f'{when} · {states.get(msg.get("state"), "")}', xalign=0)
            status.add_css_class("chat-status")
            row.append(status)
            self.chat_messages.append(row)

    def _clear_history(self):
        if not self.client:
            return
        try:
            self.client.call("ClearHistory")
            self.last_hist_sig = None
        except GLib.Error:
            pass

    # ------------------------------------------------------------ 渲染记录

    def _refresh_devices(self):
        try:
            data = self.client.devices()
        except GLib.Error:
            return
        except (ValueError, TypeError):
            return
        sig = json.dumps(data, sort_keys=True)
        if sig == self._last_devices_sig:
            return
        self._last_devices_sig = sig
        self._render_devices(data)

    def _render_devices(self, data):
        devices = data.get("devices") or []
        cur_cap = data.get("capture") or ""
        cur_play = data.get("playback") or ""

        def short(name):
            # liblinphone 设备名形如 "PulseAudio: <描述> [Unknown]"，只留描述部分。
            n = name or ""
            if n.startswith("PulseAudio: "):
                n = n[len("PulseAudio: "):]
            for tail in (" [Unknown]", " [default]"):
                if n.endswith(tail):
                    n = n[: -len(tail)]
            return n or self._t("（系统默认）", "(system default)")

        cap_txt = short(cur_cap) if cur_cap else self._t("（未指定，由系统决定）", "(not set; system decides)")
        play_txt = short(cur_play) if cur_play else self._t("（未指定，由系统决定）", "(not set; system decides)")
        self.lbl_devices_summary.set_text(
            f'{self._t("当前麦克风", "Microphone")}: {cap_txt}\n'
            f'{self._t("当前扬声器", "Speaker")}: {play_txt}')

        child = self.box_devices.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.box_devices.remove(child)
            child = nxt

        if not devices:
            lbl = Gtk.Label(label=self._t("voipd 未报告任何音频设备", "No audio devices reported by voipd"), xalign=0)
            lbl.add_css_class("hint")
            lbl.set_margin_top(4)
            lbl.set_margin_bottom(4)
            self.box_devices.append(lbl)
            return

        for d in devices:
            name = d.get("name", "")
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            role = []
            if d.get("capture"):
                role.append(self._t("录音", "in"))
            if d.get("playback"):
                role.append(self._t("放音", "out"))
            roles = "/".join(role) or self._t("不可用", "unusable")
            active = bool(d.get("active"))
            mark = "●" if active else "○"
            lbl = Gtk.Label(label=f'{mark} {short(name)}', xalign=0)
            lbl.set_hexpand(True)
            lbl.set_ellipsize(3)  # Pango.EllipsizeMode.END
            lbl.set_max_width_chars(40)
            lbl.set_tooltip_text(name)
            lbl.add_css_class("device-active" if active else "device-idle")
            row.append(lbl)
            tag = Gtk.Label(label=roles, xalign=1)
            tag.add_css_class("device-role")
            row.append(tag)
            self.box_devices.append(row)

    def _render_history(self, hist):
        child = self.list_hist.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self.list_hist.remove(child)
            child = nxt
        if not hist:
            lbl = Gtk.Label(label=self._t("暂无记录 —— 快去拨一个吧！", "No calls yet"))
            lbl.add_css_class("hint")
            self.list_hist.append(lbl)
            return
        for h in hist[:50]:
            self.list_hist.append(self._hist_row(h))

    def _hist_row(self, h):
        row = Gtk.ListBoxRow()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(8)
        box.set_margin_end(8)

        arrow = "↘" if h.get("direction") == "incoming" else "↗"
        lbl_a = Gtk.Label(label=arrow)
        lbl_a.add_css_class("hist-arrow")
        box.append(lbl_a)

        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        info.set_hexpand(True)
        peer = Gtk.Label(label=h.get("peer", "?"), xalign=0)
        peer.add_css_class("hist-peer")
        info.append(peer)
        dur = int(h.get("duration", 0))
        status_map = {"answered": self._t("已接通", "Answered"), "missed": self._t("未接", "Missed"), "declined": self._t("已拒接", "Declined"), "failed": self._t("失败", "Failed")}
        sub = Gtk.Label(
            label=f'{status_map.get(h.get("status"), h.get("status"))} · '
                  f'{dur // 60:02d}:{dur % 60:02d}', xalign=0)
        sub.add_css_class("hist-sub")
        info.append(sub)
        box.append(info)

        btn = Gtk.Button(label=self._t("回拨", "Call back"))
        btn.add_css_class("btn-yellow")
        btn.connect("clicked", lambda *_: self._redial(h.get("peer", "")))
        box.append(btn)

        row.set_child(box)
        return row

    def _redial(self, peer):
        if not peer:
            return
        self.dial_number = peer
        self.entry_number.set_text(peer)
        self._goto("dial")

    def _toast(self, msg):
        try:
            self.toast_overlay.add_toast(Adw.Toast(title=msg, timeout=2))
        except Exception:
            pass


# ---------------------------------------------------------------- 应用


class VoipApp(Adw.Application):
    def __init__(self, bus_name, fullscreen):
        # application_id=None：不做单实例 D-Bus 注册。
        # 每台设备只跑一个 UI，且不希望因残留 name 导致启动失败。
        super().__init__(application_id=None,
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.bus_name = bus_name
        self.fullscreen = fullscreen

    def do_activate(self):
        win = self.get_active_window()
        if not win:
            win = VoipWindow(self, self.bus_name, self.fullscreen)
        win.present()
        # 也打到日志里，便于无图形界面时确认窗口已呈现
        print(f"[voip-ui] window presented (bus={self.bus_name}, "
              f"backend={Gdk.Display.get_default().get_name() if Gdk.Display.get_default() else '?'})",
              flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bus-name", default="org.example.Voip1")
    ap.add_argument("--fullscreen", action="store_true")
    args = ap.parse_args()
    app = VoipApp(args.bus_name, args.fullscreen)
    return app.run(sys.argv[:1])


if __name__ == "__main__":
    sys.exit(main())
