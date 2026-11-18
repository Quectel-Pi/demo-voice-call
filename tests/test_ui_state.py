#!/usr/bin/env python3
"""Lightweight regression checks for UI state handling without opening a window."""
import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace


SPEC = importlib.util.spec_from_file_location(
    "voipapp", Path(__file__).resolve().parents[1] / "src/ui/voipapp.py")
voipapp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(voipapp)


class Widget:
    def __init__(self):
        self.visible = None
        self.text = ""

    def set_visible(self, value):
        self.visible = value

    def set_text(self, value):
        self.text = value

    def set_label(self, value):
        self.text = value

    def add_css_class(self, _name):
        pass

    def remove_css_class(self, _name):
        pass


class Stack:
    def __init__(self):
        self.visible = "call"

    def get_visible_child_name(self):
        return self.visible

    def set_visible_child_name(self, name):
        self.visible = name


class Buffer:
    def set_text(self, _value):
        pass


class UiStateTest(unittest.TestCase):
    def call_window(self):
        window = SimpleNamespace(
            last_state="idle",
            stack=Stack(),
            ringer=SimpleNamespace(stop=lambda: None),
            dtmf_open=False,
            dtmf_grid=Widget(),
            answer_row=Widget(),
            incall_row=Widget(),
            btn_mute=Widget(),
            btn_speaker=Widget(),
            btn_dtmf=Widget(),
            btn_hangup=Widget(),
            lbl_peer=Widget(),
            lbl_callstate=Widget(),
            lbl_stats=Widget(),
            entry_number=Widget(),
            dial_number="",
            _goto=lambda _name: None,
            _log=lambda *args, **kwargs: None,
            _t=lambda zh, en: zh,
            _toast=lambda _msg: None,
        )
        window._update_call_controls = lambda state: voipapp.VoipWindow._update_call_controls(window, state)
        return window

    def apply_state(self, state):
        window = self.call_window()
        voipapp.VoipWindow._apply_state_ui(
            window, state, {"peer": "1002", "peer_display": "1002"})
        return window

    def test_hangup_remains_available_before_media_is_ready(self):
        for state in ("outgoing", "ringing", "connected"):
            with self.subTest(state=state):
                window = self.apply_state(state)
                self.assertTrue(window.incall_row.visible)
                self.assertTrue(window.btn_hangup.visible)
                self.assertFalse(window.answer_row.visible)
                self.assertFalse(window.btn_mute.visible)
                self.assertFalse(window.btn_dtmf.visible)

    def test_incoming_and_active_call_controls(self):
        incoming = self.apply_state("incoming")
        self.assertTrue(incoming.answer_row.visible)
        self.assertFalse(incoming.btn_hangup.visible)
        for state in ("incall", "paused"):
            with self.subTest(state=state):
                window = self.apply_state(state)
                self.assertTrue(window.btn_hangup.visible)
                self.assertTrue(window.btn_mute.visible)
                self.assertTrue(window.btn_dtmf.visible)

    def test_snapshot_refresh_restores_active_call_controls(self):
        window = self.call_window()
        for widget in (window.incall_row, window.btn_mute, window.btn_speaker,
                       window.btn_dtmf, window.btn_hangup):
            widget.visible = False
        voipapp.VoipWindow._update_call_page(window, {
            "state": "incall", "peer": "1001", "duration": 11,
            "quality": 4.1, "jitter_ms": 0, "loss_pct": 0,
            "up_kbps": 80, "down_kbps": 68, "rtp_rx": 502, "rtp_tx": 540,
            "muted": False, "speaker": False,
        })
        self.assertTrue(window.incall_row.visible)
        self.assertTrue(window.btn_mute.visible)
        self.assertTrue(window.btn_speaker.visible)
        self.assertTrue(window.btn_dtmf.visible)
        self.assertTrue(window.btn_hangup.visible)

    def test_dial_navigation_returns_to_active_call(self):
        for state in ("incoming", "outgoing", "ringing", "connected", "incall", "paused"):
            with self.subTest(state=state):
                window = SimpleNamespace(
                    snap={"state": state}, stack=Stack(), client=None, nav_buttons={})
                voipapp.VoipWindow._goto(window, "chat")
                voipapp.VoipWindow._goto(window, "dial")
                self.assertEqual(window.stack.visible, "call")

    def test_dial_navigation_is_normal_without_active_call(self):
        window = SimpleNamespace(
            snap={"state": "idle"}, stack=Stack(), client=None, nav_buttons={})
        voipapp.VoipWindow._goto(window, "dial")
        self.assertEqual(window.stack.visible, "dial")

    def test_logout_allows_new_account_prefill(self):
        client = SimpleNamespace(
            call=lambda *_args: (True, ""),
            account=lambda: {
                "server": "sip:192.0.2.10:5060",
                "user": "1002",
                "display_name": "New account",
                "registered": False,
            },
        )
        window = SimpleNamespace(
            client=client,
            _account_loaded=True,
            _login_active=True,
            _was_registered=True,
            snap={"account": "1001"},
            lbl_account_info=Widget(),
            chat_target="1001",
            _chat_account=("1001", "sip:old"),
            _chat_list_sig="old-chats",
            _chat_messages_sig="old-messages",
            _last_chat_refresh=123,
            chat_peer=Widget(),
            chat_input=SimpleNamespace(get_buffer=lambda: Buffer()),
            in_user=Widget(),
            in_secret=Widget(),
            in_display=Widget(),
            in_server=Widget(),
            _refresh_chat=lambda: None,
            _set_login_status=lambda *_args: None,
            _goto=lambda _name: None,
            _t=lambda zh, en: zh,
            _log=lambda *args, **kwargs: None,
            _toast=lambda _msg: None,
        )
        voipapp.VoipWindow._logout(window)
        self.assertFalse(window._account_loaded)
        self.assertEqual(window.lbl_account_info.text, "")
        self.assertIsNone(window._chat_list_sig)
        self.assertIsNone(window._chat_messages_sig)
        self.assertEqual(window._last_chat_refresh, 0)
        voipapp.VoipWindow._prefill_login(window)
        self.assertEqual(window.in_user.text, "1002")
        self.assertEqual(window.in_display.text, "New account")
        self.assertEqual(window.in_server.text, "192.0.2.10:5060")


if __name__ == "__main__":
    unittest.main()
