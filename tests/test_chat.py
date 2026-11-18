#!/usr/bin/env python3
"""Local, credential-free SIP MESSAGE smoke test. Run under dbus-run-session.

Example: dbus-run-session -- python3 tests/test_chat.py --binary /tmp/voipd-test
Use --resources-dir /path/to/usr/share with a VOIP_WITH_BELR build of liblinphone 4.4
when linphone-common is not installed system-wide. This stub is NOT an Asterisk test.
"""
import argparse
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from gi.repository import Gio, GLib

IFACE = "org.example.Voip1"
PATH = "/org/example/Voip1"


class SipStub:
    """Minimal UDP registrar/one-to-one MESSAGE relay for controlled local tests."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.contacts = {}
        self.messages = []
        self.running = True
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while self.running:
            try:
                packet, addr = self.sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            data = packet.decode("utf-8", "replace")
            lines = data.split("\r\n")
            headers = {}
            for line in lines[1:]:
                if not line:
                    break
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers.setdefault(key.lower(), value.strip())
            method = lines[0].split(" ", 1)[0]
            status = "200 OK"
            if method == "REGISTER":
                match = re.search(r"sip:([^@;>]+)@", headers.get("from", ""))
                if match:
                    self.contacts[match.group(1)] = addr
            elif method == "MESSAGE":
                match = re.search(r"sip:([^@;>]+)@", lines[0])
                self.messages.append((lines[0], data.split("\r\n\r\n", 1)[-1]))
                if match and match.group(1) in self.contacts:
                    # A real registrar rewrites To to the peer's public Contact.
                    # Use a different loopback address to catch 4.4 replies which
                    # accidentally reuse that NAT address as the sender identity.
                    forwarded = re.sub(r"(?im)^To:[^\r\n]*", f"To: <sip:{match.group(1)}@127.0.0.2>", data)
                    self.sock.sendto(forwarded.encode("utf-8"), self.contacts[match.group(1)])
                else:
                    status = "404 Not Found"
            else:
                continue
            to = headers.get("to", "")
            if "tag=" not in to:
                to += ";tag=localtest"
            response = (
                "SIP/2.0 " + status + "\r\nVia: " + headers.get("via", "") +
                "\r\nFrom: " + headers.get("from", "") +
                "\r\nTo: " + to +
                "\r\nCall-ID: " + headers.get("call-id", "") +
                "\r\nCSeq: " + headers.get("cseq", "") +
                "\r\nContent-Length: 0\r\n\r\n"
            )
            self.sock.sendto(response.encode(), addr)

    def close(self):
        self.running = False
        self.sock.close()
        self.thread.join(timeout=1)


def free_udp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(predicate, desc, timeout=15):
    end = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < end:
        try:
            result = predicate()
            if result:
                return result
        except (GLib.Error, ValueError) as exc:
            last_error = exc
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {desc}: {last_error}")


def run_test(binary, resources, expect_missing_resources=False, peer_binary=""):
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        raise RuntimeError("Run this script under dbus-run-session")
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    def call(user, method, params=None):
        name = f"org.example.VoipChatTest{user}"
        return bus.call_sync(name, PATH, IFACE, method, params, None,
                             Gio.DBusCallFlags.NONE, 3000, None).unpack()

    def messages(user, peer):
        return json.loads(call(user, "GetMessages", GLib.Variant("(s)", (peer,)))[0])

    stub = SipStub()
    processes = []
    with tempfile.TemporaryDirectory(prefix="voip-chat-test-") as temp:
        try:
            for user in ("1001", "1002"):
                home = Path(temp) / user
                (home / ".local/share/linphone").mkdir(parents=True)
                (home / ".config").mkdir()
                cfg = home / "test.conf"
                cfg.write_text(
                    "[account]\n" + f"user={user}\nsecret=\n" +
                    f"server=sip:127.0.0.1:{stub.port}\nsip_port={free_udp_port()}\n" +
                    (f"data_resources_dir={resources}\n" if resources else "") +
                    "[general]\nauto_answer=false\nverbose=false\n"
                )
                log = (home / "daemon.log").open("w+")
                env = dict(os.environ, HOME=str(home))
                executable = peer_binary if user == "1002" and peer_binary else binary
                proc = subprocess.Popen(
                    [executable, "--config", str(cfg), "--data-dir", str(home / "data"),
                     "--bus-name", f"org.example.VoipChatTest{user}"],
                    env=env, stdout=log, stderr=subprocess.STDOUT
                )
                processes.append((proc, log))

            wait_for(lambda: all(
                json.loads(call(u, "GetSnapshot")[0])["registered"] for u in ("1001", "1002")
            ), "both SIP registrations")
            assert json.loads(call("1001", "GetChats")[0]) == []
            if expect_missing_resources:
                assert call("1001", "SendText", GLib.Variant("(ss)", ("1002", "hi")))[2] == "chat_resources_missing"
                if peer_binary:
                    assert call("1002", "SendText", GLib.Variant("(ss)", ("1001", "must be rejected")))[0]
                    wait_for(lambda: len(stub.messages) == 1, "incoming MESSAGE to disabled chat")
                    time.sleep(0.5)
                    assert messages("1001", "1002") == []
                assert all(proc.poll() is None for proc, _ in processes)
                print("PASS: missing linphone 4.4 chat grammar rejects sends/receives without crashing the daemon")
                return
            assert call("1001", "SendText", GLib.Variant("(ss)", ("1002", " ")))[2] == "invalid_text"
            assert call("1001", "SendText", GLib.Variant("(ss)", ("1002", "x" * 4097)))[2] == "invalid_text"
            assert call("1001", "SendText", GLib.Variant("(ss)", ("tel:1002", "hi")))[2] == "invalid_peer"
            text = 'Hello 世界\nline2 <b> & "quoted"'
            accepted, sent_id, error = call("1001", "SendText", GLib.Variant("(ss)", ("1002", text)))
            assert accepted and sent_id and not error
            wait_for(lambda: len(messages("1002", "1001")) == 1, "first received text")
            received = messages("1002", "1001")[0]
            assert received["text"] == text and received["peer"] == "sip:1001@127.0.0.1"
            assert received["state"] == "received" and not received["outgoing"]
            wait_for(lambda: messages("1001", "1002")[0]["state"] == "server_accepted",
                     "server acknowledgement")
            sender = messages("1001", "1002")[0]
            assert sender["outgoing"] and sender["id"] == sent_id
            assert len(stub.messages) == 1 and stub.messages[0][1] == text

            accepted, _, _ = call("1002", "SendText", GLib.Variant("(ss)", ("1001", "reply ✓")))
            assert accepted
            wait_for(lambda: len(messages("1001", "1002")) == 2, "reply")
            assert [chat["peer"] for chat in json.loads(call("1001", "GetChats")[0])] == ["sip:1002@127.0.0.1"]
            accepted, _, _ = call("1001", "SendText", GLib.Variant("(ss)", ("9999", "nobody home")))
            assert accepted  # local acceptance is not remote delivery
            wait_for(lambda: messages("1001", "9999")[0]["state"] == "failed",
                     "SIP 404 delivery failure")
            for i in range(201):
                assert call("1001", "SendText", GLib.Variant("(ss)", ("9999", f"cap-{i}")))[0]
            chats = json.loads(call("1001", "GetChats")[0])
            assert sum(len(messages("1001", chat["peer"])) for chat in chats) == 200
            assert all(item["text"] != text for item in messages("1001", "1002"))

            ok, error = call("1001", "SetAccount", GLib.Variant(
                "(ssss)", (f"127.0.0.1:{stub.port}", "1003", "", "Changed account")))
            assert ok and not error
            assert json.loads(call("1001", "GetChats")[0]) == []
            assert messages("1001", "1002") == []
            ok, error = call("1001", "Logout")
            assert ok and not error
            assert call("1001", "SendText", GLib.Variant("(ss)", ("1002", "hi")))[2] == "not_registered"
            print("PASS: two SIP extensions exchanged UTF-8 text, states, 200-message bound, validation and account isolation")
        except Exception:
            for proc, log in processes:
                log.flush()
                print(f"voipd pid={proc.pid} exit={proc.poll()} log tail:")
                print("\n".join(Path(log.name).read_text().splitlines()[-12:]))
            raise
        finally:
            for proc, log in processes:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
                log.close()
            stub.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", required=True)
    parser.add_argument("--resources-dir", default="", help="extracted linphone-common usr/share, for local 4.4 builds")
    parser.add_argument("--expect-missing-resources", action="store_true", help="verify missing-resource error without a chat send")
    parser.add_argument("--peer-binary", default="", help="optional working sender for incoming missing-resource check")
    args = parser.parse_args()
    if args.resources_dir and not (Path(args.resources_dir) / "belr/grammars/identity_grammar").is_file():
        parser.error("--resources-dir has no belr/grammars/identity_grammar")
    run_test(args.binary, args.resources_dir, args.expect_missing_resources, args.peer_binary)
