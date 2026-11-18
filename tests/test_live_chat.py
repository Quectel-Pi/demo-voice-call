#!/usr/bin/env python3
"""Opt-in real-server chat/call validation using two protected config files.

This registers two SIP accounts, sends test messages and places a brief call.
Close existing instances using those accounts first. Run under dbus-run-session.
Passwords are read from config files, never passed on command lines or printed.
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from gi.repository import Gio, GLib


BUS_PATH = "/org/example/Voip1"
IFACE = "org.example.Voip1"


def udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]


def wait_for(predicate, timeout=25):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            value = predicate()
            if value:
                return value
        except GLib.Error:
            pass
        time.sleep(0.2)
    return False


def validate(args):
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    processes = []
    users = []
    result = {"registered": False, "messages": [], "call": {}, "in_call_chat": {}}

    def call(index, method, params=None):
        return bus.call_sync(
            f"org.example.VoipLiveCheck{index}", BUS_PATH, IFACE, method, params,
            None, Gio.DBusCallFlags.NONE, 4000, None
        ).unpack()

    def snapshot(index):
        return json.loads(call(index, "GetSnapshot")[0])

    def messages(index, peer):
        return json.loads(call(index, "GetMessages", GLib.Variant("(s)", (peer,)))[0])

    def send_and_verify(sender, receiver, label):
        text = f"VoIP {label} {users[sender]} -> {users[receiver]}: 你好，世界!\nplain <b> & \"quote\" {time.time_ns()}"
        accepted, msg_id, error = call(sender, "SendText", GLib.Variant("(ss)", (users[receiver], text)))
        received = bool(wait_for(lambda: any(
            not item["outgoing"] and item["text"] == text
            for item in messages(receiver, users[sender])
        ))) if accepted else False
        state = next((item["state"] for item in messages(sender, users[receiver]) if item["id"] == msg_id), "missing")
        item = {"from": users[sender], "to": users[receiver], "accepted": accepted,
                "received": received, "sender_state": state, "error": error}
        if not received:
            item["receiver_chats"] = json.loads(call(receiver, "GetChats")[0])
        return item

    with tempfile.TemporaryDirectory(prefix="voip-live-check-") as temp:
        try:
            for i, config in enumerate((args.config_a, args.config_b)):
                home = Path(temp) / str(i)
                (home / ".local/share/linphone").mkdir(parents=True)
                (home / ".config").mkdir()
                keyfile = GLib.KeyFile()
                keyfile.load_from_file(config, GLib.KeyFileFlags.NONE)
                user = keyfile.get_string("account", "user")
                if not user or (users and user == users[0]):
                    raise ValueError("Two distinct SIP accounts are required")
                users.append(user)
                keyfile.set_integer("account", "sip_port", udp_port())
                keyfile.set_boolean("general", "auto_answer", False)
                keyfile.set_boolean("general", "verbose", bool(args.diagnostics_dir))
                path = home / "voipd.conf"
                path.write_text(keyfile.to_data()[0])
                path.chmod(0o600)
                log = (home / "daemon.log").open("w")
                process = subprocess.Popen(
                    [args.binary, "--config", str(path), "--data-dir", str(home / "data"),
                     "--bus-name", f"org.example.VoipLiveCheck{i}"],
                    env=dict(os.environ, HOME=str(home)), stdout=log, stderr=subprocess.STDOUT
                )
                processes.append((process, log))

            result["registered"] = bool(wait_for(lambda: all(snapshot(i)["registered"] for i in (0, 1)), 40))
            result["registration_details"] = [snapshot(i)["reg_detail"] for i in (0, 1)]
            if result["registered"]:
                for sender, receiver in ((0, 1), (1, 0)):
                    item = send_and_verify(sender, receiver, "online chat")
                    result["messages"].append(item)
                    print("MESSAGE", json.dumps(item, ensure_ascii=False), flush=True)
                dial = call(0, "Dial", GLib.Variant("(s)", (users[1],)))[0]
                incoming = bool(wait_for(lambda: snapshot(1)["state"] == "incoming", 15))
                result["call"] = {"dial_accepted": dial, "incoming": incoming}
                if incoming:
                    call(1, "Answer")
                    connected = bool(wait_for(lambda: all(snapshot(i)["state"] in ("incall", "connected") for i in (0, 1)), 15))
                    result["call"]["connected"] = connected
                    if connected:
                        result["in_call_chat"] = send_and_verify(0, 1, "in-call chat")
                call(0, "Hangup")
                result["call"]["ended"] = bool(wait_for(lambda: all(snapshot(i)["state"] not in (
                    "incall", "connected", "incoming", "outgoing", "ringing") for i in (0, 1)), 10))
            result["passed"] = bool(
                result["registered"] and len(result["messages"]) == 2 and
                all(item["received"] for item in result["messages"]) and
                result["call"].get("connected") and result["call"].get("ended") and
                result["in_call_chat"].get("received")
            )
            print("RESULT", json.dumps(result, ensure_ascii=False), flush=True)
        finally:
            for i in range(len(processes)):
                try:
                    call(i, "Hangup")
                    call(i, "Logout")
                except GLib.Error:
                    pass
            time.sleep(0.3)
            for process, log in processes:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                log.close()
                if args.diagnostics_dir:
                    directory = Path(args.diagnostics_dir)
                    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                    directory.chmod(0o700)
                    target = directory / f"daemon-{process.pid}.log"
                    shutil.copyfile(log.name, target)
                    target.chmod(0o600)
            if args.result:
                target = Path(args.result)
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
                target.chmod(0o600)
    return result.get("passed", False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True)
    parser.add_argument("--config-a", required=True)
    parser.add_argument("--config-b", required=True)
    parser.add_argument("--result", default="")
    parser.add_argument("--diagnostics-dir", default="", help="opt-in verbose logs (may contain SIP authentication digests); keep private and delete after debugging")
    args = parser.parse_args()
    raise SystemExit(0 if validate(args) else 1)
