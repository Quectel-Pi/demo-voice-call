// voipd —— 语音通话守护进程
//
// 职责：SIP 账号注册、呼叫状态机、媒体控制、通话记录；
//       通过 D-Bus(org.example.Voip1) 对外提供接口，UI 进程无需接触 SIP。
//
// 构建见 Makefile。运行：voipd [--config FILE] [--bus-name NAME] [--auto-answer]
#include <linphone/core.h>

#include <arpa/inet.h>
#include <gio/gio.h>
#include <glib.h>
#include <ifaddrs.h>
#include <unistd.h>
#include <netinet/in.h>
#include <net/if.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <strings.h>
#include <ctime>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

// liblinphone 4.4 在库加载时就把 belr 语法目录写死成 /usr/share，
// 非系统安装（sysroot / 自定义 prefix）需要自己把路径补进 belr 的查找列表。
// 只有这种场景才需要 belr 头文件与 -lbelr，因此做成显式编译开关 -DVOIP_WITH_BELR，
// 正常系统安装（liblinphone 5.x，资源在 /usr/share）不会引入该依赖。
#ifdef VOIP_WITH_BELR
#  include <belr/grammarbuilder.h>
#endif

// ---------------------------------------------------------------- 版本兼容
//
// liblinphone 5.x 引入 LinphoneAccount / LinphoneAccountParams；
// 4.4.x（如 Ubuntu 22.04）只有 LinphoneProxyConfig。两者功能等价，这里做编译期分流，
// 同一份源码可在 Debian 13(5.3) 与 Ubuntu 22.04(4.4) 上构建。
#if defined(__has_include)
#  if __has_include(<linphone/api/c-account-params.h>)
#    define VOIP_HAS_ACCOUNT_API 1
#  else
#    define VOIP_HAS_ACCOUNT_API 0
#  endif
#else
#  define VOIP_HAS_ACCOUNT_API 0
#endif

// ---------------------------------------------------------------- 常量

static const char *kBusName = "org.example.Voip1";
static const char *kObjectPath = "/org/example/Voip1";
static const char *kIface = "org.example.Voip1";

static const char *kIntrospection = R"XML(
<node>
  <interface name="org.example.Voip1">
    <method name="Dial">
      <arg type="s" name="uri" direction="in"/>
      <arg type="b" name="ok" direction="out"/>
    </method>
    <method name="Answer"/>
    <method name="Decline"/>
    <method name="Hangup">
      <arg type="b" name="ok" direction="out"/>
    </method>
    <method name="SetMute">
      <arg type="b" name="muted" direction="in"/>
    </method>
    <method name="SetSpeaker">
      <arg type="b" name="on" direction="in"/>
    </method>
    <method name="SendDTMF">
      <arg type="s" name="digits" direction="in"/>
    </method>
    <method name="GetSnapshot">
      <arg type="s" name="json" direction="out"/>
    </method>
    <method name="GetHistory">
      <arg type="s" name="json" direction="out"/>
    </method>
    <method name="SendText">
      <arg type="s" name="peer" direction="in"/>
      <arg type="s" name="text" direction="in"/>
      <arg type="b" name="accepted" direction="out"/>
      <arg type="s" name="id" direction="out"/>
      <arg type="s" name="error" direction="out"/>
    </method>
    <method name="GetChats">
      <arg type="s" name="json" direction="out"/>
    </method>
    <method name="GetMessages">
      <arg type="s" name="peer" direction="in"/>
      <arg type="s" name="json" direction="out"/>
    </method>
    <method name="ClearHistory"/>
    <method name="GetDevices">
      <arg type="s" name="json" direction="out"/>
    </method>
    <method name="GetAccount">
      <arg type="s" name="json" direction="out"/>
    </method>
    <method name="Logout">
      <arg type="b" name="ok" direction="out"/>
      <arg type="s" name="error" direction="out"/>
    </method>
    <method name="SetAccount">
      <arg type="s" name="server" direction="in"/>
      <arg type="s" name="user" direction="in"/>
      <arg type="s" name="secret" direction="in"/>
      <arg type="s" name="display_name" direction="in"/>
      <arg type="b" name="ok" direction="out"/>
      <arg type="s" name="error" direction="out"/>
    </method>
    <signal name="IncomingCall">
      <arg type="s" name="peer"/>
      <arg type="s" name="display"/>
    </signal>
    <signal name="CallStateChanged">
      <arg type="s" name="state"/>
      <arg type="s" name="peer"/>
      <arg type="s" name="detail"/>
    </signal>
    <signal name="CallEnded">
      <arg type="s" name="peer"/>
      <arg type="i" name="duration"/>
      <arg type="s" name="status"/>
    </signal>
    <signal name="RegistrationChanged">
      <arg type="b" name="registered"/>
      <arg type="s" name="detail"/>
    </signal>
    <signal name="MessageReceived">
      <arg type="s" name="peer"/>
      <arg type="s" name="id"/>
    </signal>
    <signal name="MessageStateChanged">
      <arg type="s" name="peer"/>
      <arg type="s" name="id"/>
      <arg type="s" name="state"/>
    </signal>
  </interface>
</node>
)XML";

// ---------------------------------------------------------------- 配置

struct Config {
    std::string user = "1001";
    std::string secret;
    std::string server = "sip:127.0.0.1:5060";
    std::string display_name;
    // 空 = 自动挑选真实硬件设备（会排除回声消除/监视/空设备等虚拟节点）
    std::string capture_device;
    std::string playback_device;
    int sip_port = 5060;
    bool auto_answer = false;
    bool verbose = false;
    int no_answer_timeout = 60;   // 秒：去电无应答自动挂断，防止僵尸通话锁死音频
    float playback_gain_db = 0.0f;
    float mic_gain_db = 0.0f;
    std::string data_dir;  // 空 = $XDG_DATA_HOME/voipd
    // 非系统安装时，liblinphone 的资源目录（belr 语法、铃声、滤镜）需要显式指定。
    // 例如 sysroot 安装：/path/to/sysroot/usr/share 。空 = 用编译期默认（/usr/share）。
    std::string data_resources_dir;

    std::string realm;  // 可选，默认取 server 的 host
};

static bool chat_resources_present(const Config &cfg) {
#if VOIP_HAS_ACCOUNT_API
    (void)cfg;
    return true; // liblinphone 5.x uses its own resource loading strategy
#else
    std::string grammar = "/usr/share/belr/grammars/identity_grammar";
#ifdef VOIP_WITH_BELR
    if (!cfg.data_resources_dir.empty())
        grammar = cfg.data_resources_dir + "/belr/grammars/identity_grammar";
#else
    (void)cfg;
#endif
    return g_file_test(grammar.c_str(), G_FILE_TEST_IS_REGULAR);
#endif
}

static std::string server_host(const std::string &server) {
    std::string s = server;
    auto p = s.find("sip:");
    if (p == 0) s = s.substr(4);
    auto at = s.rfind('@');
    if (at != std::string::npos) s = s.substr(at + 1);
    auto colon = s.find(':');
    if (colon != std::string::npos) s = s.substr(0, colon);
    return s;
}

// ---------------------------------------------------------------- 工具

static std::string json_escape(const std::string &in) {
    std::string out;
    for (char c : in) {
        switch (c) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if ((unsigned char)c < 0x20) {
                    char buf[8];
                    snprintf(buf, sizeof(buf), "\\u%04x", c);
                    out += buf;
                } else out += c;
        }
    }
    return out;
}

static std::string local_ipv4() {
    struct ifaddrs *ifs = nullptr;
    std::string best;
    if (getifaddrs(&ifs) == 0) {
        for (struct ifaddrs *i = ifs; i; i = i->ifa_next) {
            if (!i->ifa_addr || i->ifa_addr->sa_family != AF_INET) continue;
            if (!(i->ifa_flags & IFF_UP) || (i->ifa_flags & IFF_LOOPBACK)) continue;
            char buf[INET_ADDRSTRLEN] = {0};
            auto *sin = (struct sockaddr_in *)i->ifa_addr;
            inet_ntop(AF_INET, &sin->sin_addr, buf, sizeof(buf));
            std::string name = i->ifa_name ? i->ifa_name : "";
            // eth*/wlan* 优先
            if (name.rfind("eth", 0) == 0 || name.rfind("wlan", 0) == 0) { best = buf; break; }
            if (best.empty()) best = buf;
        }
        freeifaddrs(ifs);
    }
    return best;
}

// JSON 数字必须是有限值；liblinphone 在搭建媒体流阶段会返回 NaN/Inf，
// 直接 printf 出来是 "nan"/"inf"，会让客户端 json 解析失败（UI 曾因此卡住不刷新）。
static double jnum(double v, double dflt) { return std::isfinite(v) ? v : dflt; }

// 判断设备名是否为"虚拟/非硬件"节点。
// 板子上的 mic-ns / mic-ns-sink（PipeWire 回声消除模块）实测拿不到有效音频
// （采集电平约 -76 dBFS），PC 上的空输出同样是哑的。必须排除。
static bool is_virtual_device(const std::string &name) {
    static const char *kBads[] = {
        "echo-cancel", "echo cancel", "mic-ns", "monitor", "null", "dummy",
        "空输出", "空输入", "loopback", "环回", "回环",
        // 数字输出一般没接音箱，除非没有别的可选
        "spdif", "hdmi", "digital", "数字",
    };
    std::string low;
    low.reserve(name.size());
    for (char c : name) low += (char)tolower((unsigned char)c);
    for (const char *b : kBads) {
        std::string bl(b);
        std::string lowb;
        for (char c : bl) lowb += (char)tolower((unsigned char)c);
        if (low.find(lowb) != std::string::npos) return true;
    }
    return false;
}

// 通过 pactl 查询系统默认设备，并映射成 liblinphone 的设备名。
//
// 为什么不用关键字猜：liblinphone 无接口查询系统默认设备，它默认取列表第一个，
// 常常是错的（PC 上选了未接线的 S/PDIF，板子上选了 mic-ns 虚拟节点）。
// pactl --format=json 输出稳定且不受语言影响（Ubuntu 15.99 / PipeWire 17 都支持）。
static bool pactl_default(const char *kind, std::string &name, std::string &desc) {
    // kind = "sinks" / "sources"
    // 注意：g_spawn_command_line_sync 不经过 shell，不能带 "2>/dev/null"
    std::string cmd = std::string("pactl get-default-") +
                      (strcmp(kind, "sinks") == 0 ? "sink" : "source");
    gchar *out = nullptr;
    int st = 0;
    GError *err = nullptr;
    if (!g_spawn_command_line_sync(cmd.c_str(), &out, nullptr, &st, &err)) {
        if (err) g_error_free(err);
        return false;
    }
    std::string def = out ? out : "";
    g_free(out);
    while (!def.empty() && (def.back() == '\n' || def.back() == ' ')) def.pop_back();
    if (def.empty() || st != 0) return false;

    // 取该设备的 description
    std::string cmd2 = std::string("pactl --format=json list ") + kind;
    gchar *js = nullptr;
    if (!g_spawn_command_line_sync(cmd2.c_str(), &js, nullptr, nullptr, nullptr)) return false;
    std::string all = js ? js : "";
    g_free(js);

    // 极简 JSON 扫描：找 {"index":..,"state":..,"name":"<def>",..,"description":"<desc>"
    std::string key = "\"name\":\"" + def + "\"";
    auto pos = all.find(key);
    if (pos == std::string::npos) return false;
    auto dpos = all.find("\"description\":\"", pos);
    if (dpos == std::string::npos) return false;
    auto b = dpos + strlen("\"description\":\"");
    auto e = all.find('"', b);
    if (e == std::string::npos) return false;
    name = def;
    desc = all.substr(b, e - b);
    return !desc.empty();
}

// 在 liblinphone 的设备名列表里挑一个"真实硬件"设备。
// prefer 中的关键字用于在多个候选里排序（如播放优先 Speaker/扬声器）。
static std::string pick_device(const char **names, bool for_playback) {
    if (!names) return "";
    static const char *kPlayPref[] = {"speaker", "扬声器", "analog", "内置", "headphone", "耳机"};
    static const char *kCapPref[] = {"mic", "麦克风", "analog", "内置", "headset", "耳麦"};

    std::string best;
    int best_score = -1;
    for (int i = 0; names[i]; ++i) {
        std::string n = names[i];
        if (is_virtual_device(n)) continue;
        std::string low;
        for (char c : n) low += (char)tolower((unsigned char)c);
        int score = 0;
        const char **pref = for_playback ? kPlayPref : kCapPref;
        int pref_n = 6;
        for (int k = 0; k < pref_n; ++k) {
            std::string pk(pref[k]);
            std::string lowp;
            for (char c : pk) lowp += (char)tolower((unsigned char)c);
            if (low.find(lowp) != std::string::npos) score += (pref_n - k);
        }
        if (score > best_score) { best_score = score; best = n; }
    }
    return best;
}

static const char *state_name(LinphoneCallState s) {
    switch (s) {
        case LinphoneCallIdle: return "idle";
        case LinphoneCallIncomingReceived: return "incoming";
        case LinphoneCallOutgoingInit: return "outgoing";
        case LinphoneCallOutgoingProgress: return "outgoing";
        case LinphoneCallOutgoingRinging: return "ringing";
        case LinphoneCallOutgoingEarlyMedia: return "ringing";
        case LinphoneCallConnected: return "connected";
        case LinphoneCallStreamsRunning: return "incall";
        case LinphoneCallPausing: return "incall";
        case LinphoneCallPaused: return "paused";
        case LinphoneCallResuming: return "incall";
        case LinphoneCallEnd: return "ended";
        case LinphoneCallError: return "error";
        default: return "incall";
    }
}

// ---------------------------------------------------------------- 应用状态

struct HistoryEntry {
    std::string peer;
    std::string direction; // incoming / outgoing
    long long start = 0;
    int duration = 0;
    std::string status; // answered / missed / declined / failed
};

struct ChatEntry {
    std::string id;
    std::string peer;   // canonical sip:user@host, without an optional port
    std::string text;
    std::string state;  // received / sending / server_accepted / delivered / displayed / failed
    long long timestamp = 0;
    bool outgoing = false;
};

struct App {
    Config cfg;
    LinphoneCore *lc = nullptr;
    GMainLoop *loop = nullptr;
    GDBusConnection *bus = nullptr;
    guint reg_id = 0;
    std::string bus_name;

    // 状态
    std::string state = "idle";     // 对外状态
    std::string peer;               // 当前/最近对端
    std::string peer_display;
    std::string detail;             // 最近一次状态说明
    std::string direction;          // incoming / outgoing
    long long call_start = 0;   // 通话真正开始（StreamsRunning）的时间
    long long call_ts = 0;      // 呼叫发起/来电到达时间，用于无应答超时
    bool media_ready = false;   // StreamsRunning 且已经过对端应答，才算真正通话中
    int stale_ticks = 0;        // 僵尸通话计数（见 tick）
    bool registered = false;
    bool muted = false;
    bool speaker = false;
    std::string reg_detail;
    std::string last_error;

    // 注意：不保存 LinphoneCall 裸指针，统一用 active_call() 查询，避免悬垂指针
    bool answered = false;

    std::vector<HistoryEntry> history;
    // Chat is intentionally an in-memory, bounded session view; liblinphone may also
    // maintain its own database under the isolated HOME set by run-app.sh.
    std::vector<ChatEntry> messages;
    // Retain outbound messages until final delivery state; key is never dereferenced.
    std::unordered_map<LinphoneChatMessage *, std::string> pending_messages;
    std::string history_path;
    std::string cfg_path;   // 登录页 SetAccount 需要把新账号写回配置文件
};

static App g_app;
static std::atomic<bool> g_stopping{false};

// ---------------------------------------------------------------- 历史记录

static void history_load(App &a) {
    gchar *data = nullptr;
    if (!g_file_get_contents(a.history_path.c_str(), &data, nullptr, nullptr)) return;
    for (char *line = data; line && *line;) {
        char *nl = strchr(line, '\n');
        if (nl) *nl = '\0';
        if (*line) {
            // 极简解析，格式固定，避免引入 json 依赖
            HistoryEntry e;
            std::string s = line;
            auto grab = [&](const char *key) -> std::string {
                std::string k = std::string("\"") + key + "\":";
                auto p = s.find(k);
                if (p == std::string::npos) return "";
                p += k.size();
                if (p < s.size() && s[p] == '"') {
                    auto q = s.find('"', p + 1);
                    if (q == std::string::npos) return "";
                    return s.substr(p + 1, q - p - 1);
                }
                auto q = s.find_first_of(",}", p);
                return s.substr(p, q == std::string::npos ? std::string::npos : q - p);
            };
            e.peer = grab("peer");
            e.direction = grab("direction");
            e.status = grab("status");
            e.start = atoll(grab("start").c_str());
            e.duration = atoi(grab("duration").c_str());
            if (!e.peer.empty()) a.history.push_back(e);
        }
        line = nl ? nl + 1 : nullptr;
    }
    g_free(data);
    // 只保留最近 200 条
    if (a.history.size() > 200) a.history.erase(a.history.begin(), a.history.end() - 200);
}

static void history_append(App &a, const HistoryEntry &e) {
    a.history.push_back(e);
    if (a.history.size() > 200) a.history.erase(a.history.begin());
    char buf[1024];
    snprintf(buf, sizeof(buf),
             "{\"peer\":\"%s\",\"direction\":\"%s\",\"start\":%lld,\"duration\":%d,\"status\":\"%s\"}\n",
             json_escape(e.peer).c_str(), e.direction.c_str(), e.start, e.duration, e.status.c_str());
    // 追加写
    FILE *f = fopen(a.history_path.c_str(), "a");
    if (f) { fputs(buf, f); fclose(f); }
}

// ---------------------------------------------------------------- D-Bus 发送

static void emit_signal(const char *name, GVariant *params) {
    if (!g_app.bus) { if (params) g_variant_unref(params); return; }
    GError *err = nullptr;
    g_dbus_connection_emit_signal(g_app.bus, nullptr, kObjectPath, kIface, name, params, &err);
    if (err) {
        g_printerr("emit %s failed: %s\n", name, err->message);
        g_error_free(err);
    }
}

static void set_state(const std::string &s, const std::string &why) {
    g_app.state = s;
    g_app.detail = why;
    emit_signal("CallStateChanged",
                g_variant_new("(sss)", s.c_str(), g_app.peer.c_str(), why.c_str()));
    g_message("[state] %s (%s) peer=%s", s.c_str(), why.c_str(), g_app.peer.c_str());
}

static LinphoneCall *active_call();
static bool save_config(App &a);

// ---------------------------------------------------------------- 快照 JSON

static std::string snapshot_json() {
    App &a = g_app;
    LinphoneCall *c = active_call();
    int duration = 0;
    float quality = -1, jitter = 0, loss = 0, up = 0, down = 0;
    unsigned long long rx = 0, tx = 0;
    std::string codec;

    if (c) {
        // 只用 call_start 计时：它由 StreamsRunning（真正建立媒体）设置。
        // 早期媒体阶段 call_start 为 0，此时保持 0，不能回退到
        // linphone_call_get_duration()，否则通话时长会从拨出瞬间就开始走。
        duration = a.call_start ? (int)(time(nullptr) - a.call_start) : 0;
        quality = linphone_call_get_average_quality(c);
        const LinphoneCallStats *st = linphone_call_get_audio_stats(c);
        if (st) {
            jitter = linphone_call_stats_get_jitter_buffer_size_ms(st);
            loss = linphone_call_stats_get_receiver_loss_rate(st);
            up = linphone_call_stats_get_upload_bandwidth(st);
            down = linphone_call_stats_get_download_bandwidth(st);
            const rtp_stats_t *r = linphone_call_stats_get_rtp_stats(st);
            if (r) { rx = r->packet_recv; tx = r->packet_sent; }
        }
    }

    char buf[2048];
    snprintf(buf, sizeof(buf),
        "{"
        "\"state\":\"%s\",\"peer\":\"%s\",\"peer_display\":\"%s\",\"direction\":\"%s\","
        "\"detail\":\"%s\",\"duration\":%d,\"quality\":%.1f,\"jitter_ms\":%.0f,"
        "\"loss_pct\":%.1f,\"up_kbps\":%.1f,\"down_kbps\":%.1f,"
        "\"rtp_rx\":%llu,\"rtp_tx\":%llu,\"muted\":%s,\"speaker\":%s,"
        "\"registered\":%s,\"reg_detail\":\"%s\",\"account\":\"%s\",\"server\":\"%s\","
        "\"local_ip\":\"%s\",\"sip_port\":%d,\"last_error\":\"%s\",\"codec\":\"%s\""
        "}",
        a.state.c_str(), json_escape(a.peer).c_str(), json_escape(a.peer_display).c_str(),
        a.direction.c_str(), json_escape(a.detail).c_str(), duration,
        jnum(quality, -1.0), jnum(jitter, 0.0), jnum(loss, 0.0),
        jnum(up, 0.0), jnum(down, 0.0), rx, tx, a.muted ? "true" : "false", a.speaker ? "true" : "false",
        a.registered ? "true" : "false", json_escape(a.reg_detail).c_str(),
        a.cfg.user.c_str(), a.cfg.server.c_str(), local_ipv4().c_str(), a.cfg.sip_port,
        json_escape(a.last_error).c_str(), codec.c_str());
    return buf;
}

static std::string history_json() {
    std::string out = "[";
    bool first = true;
    // 倒序（最新在前）
    for (auto it = g_app.history.rbegin(); it != g_app.history.rend(); ++it) {
        if (!first) out += ",";
        first = false;
        char buf[512];
        snprintf(buf, sizeof(buf),
                 "{\"peer\":\"%s\",\"direction\":\"%s\",\"start\":%lld,\"duration\":%d,\"status\":\"%s\"}",
                 json_escape(it->peer).c_str(), it->direction.c_str(), it->start,
                 it->duration, it->status.c_str());
        out += buf;
    }
    out += "]";
    return out;
}

// ---------------------------------------------------------------- 在线文字消息（本次运行中最多 200 条）

static constexpr size_t kMaxChatMessages = 200;
static constexpr size_t kMaxTextBytes = 4096;

// 与拨号使用同一个 SIP 域；完整 URI 可指定端口，但会话键忽略端口。
static bool normalize_peer(const std::string &input, std::string &peer, std::string &uri) {
    if (input.empty() || input.size() > 256 || !g_utf8_validate(input.c_str(), -1, nullptr))
        return false;
    std::string target = input;
    if (target.rfind("sip:", 0) != 0) {
        if (target.find(':') != std::string::npos) return false; // 不支持 tel:/sips:/其他协议
        target = "sip:" + target;
    }
    if (target.find('@') == std::string::npos)
        target += "@" + server_host(g_app.cfg.server);
    LinphoneAddress *addr = linphone_address_new(target.c_str());
    if (!addr) return false;
    const char *user = linphone_address_get_username(addr);
    const char *host = linphone_address_get_domain(addr);
    bool ok = user && *user && host && *host && strlen(user) <= 64 && strlen(host) <= 128;
    if (ok) {
        for (const unsigned char *p = (const unsigned char *)user; *p; ++p) {
            if (!g_ascii_isalnum(*p) && *p != '.' && *p != '_' && *p != '+' && *p != '-') {
                ok = false;
                break;
            }
        }
    }
    if (ok) {
        std::string domain = host;
        std::transform(domain.begin(), domain.end(), domain.begin(),
                       [](unsigned char c) { return (char)g_ascii_tolower(c); });
        peer = "sip:" + std::string(user) + "@" + domain;
        uri = peer;
        int port = linphone_address_get_port(addr);
        if (port <= 0 && g_ascii_strcasecmp(domain.c_str(), server_host(g_app.cfg.server).c_str()) == 0) {
            LinphoneAddress *proxy = linphone_address_new(g_app.cfg.server.c_str());
            if (proxy) {
                port = linphone_address_get_port(proxy);
                linphone_address_unref(proxy);
            }
        }
        if (port > 0) uri += ":" + std::to_string(port);
    }
    linphone_address_unref(addr);
    return ok;
}

static std::string chat_peer_of(const LinphoneAddress *addr) {
    if (!addr) return "";
    const char *user = linphone_address_get_username(addr);
    const char *domain = linphone_address_get_domain(addr);
    if (!user || !*user || !domain || !*domain) return "";
    std::string peer, uri;
    return normalize_peer("sip:" + std::string(user) + "@" + domain, peer, uri) ? peer : "";
}

static void clear_chat(App &a) {
    // Detach before unref: an unref must not let an old delivery callback touch new account state.
    auto old_pending = std::move(a.pending_messages);
    a.pending_messages.clear();
    a.messages.clear();
    for (auto &item : old_pending) linphone_chat_message_unref(item.first);
}

static void append_chat(ChatEntry entry) {
    App &a = g_app;
    if (a.messages.size() >= kMaxChatMessages) {
        const std::string old_id = a.messages.front().id;
        a.messages.erase(a.messages.begin());
        for (auto it = a.pending_messages.begin(); it != a.pending_messages.end();) {
            if (it->second == old_id) {
                LinphoneChatMessage *msg = it->first;
                it = a.pending_messages.erase(it);
                linphone_chat_message_unref(msg);
            } else ++it;
        }
    }
    a.messages.push_back(std::move(entry));
}

static std::string chats_json() {
    std::string out = "[";
    std::vector<std::string> seen;
    for (auto it = g_app.messages.rbegin(); it != g_app.messages.rend(); ++it) {
        if (std::find(seen.begin(), seen.end(), it->peer) != seen.end()) continue;
        seen.push_back(it->peer);
        if (seen.size() > 1) out += ",";
        out += "{\"peer\":\"" + json_escape(it->peer) + "\",\"time\":" +
               std::to_string(it->timestamp) + "}";
    }
    return out + "]";
}

static std::string messages_json(const std::string &peer) {
    std::string out = "[";
    bool first = true;
    for (const auto &m : g_app.messages) {
        if (m.peer != peer) continue;
        if (!first) out += ",";
        first = false;
        out += "{\"id\":\"" + json_escape(m.id) + "\",\"peer\":\"" +
               json_escape(m.peer) + "\",\"text\":\"" + json_escape(m.text) +
               "\",\"state\":\"" + m.state + "\",\"time\":" +
               std::to_string(m.timestamp) + ",\"outgoing\":" +
               (m.outgoing ? "true" : "false") + "}";
    }
    return out + "]";
}

static void on_message_state(LinphoneChatMessage *msg, LinphoneChatMessageState state) {
    auto it = g_app.pending_messages.find(msg);
    if (it == g_app.pending_messages.end()) return; // 账号已切换或消息超出内存上限
    const std::string id = it->second;
    const char *status = nullptr;
    switch (state) {
        case LinphoneChatMessageStateInProgress: status = "sending"; break;
        case LinphoneChatMessageStateDelivered: status = "server_accepted"; break;
        case LinphoneChatMessageStateDeliveredToUser: status = "delivered"; break;
        case LinphoneChatMessageStateDisplayed: status = "displayed"; break;
        case LinphoneChatMessageStateNotDelivered: {
            status = "failed";
            const LinphoneErrorInfo *error = linphone_chat_message_get_error_info(msg);
            const char *phrase = error ? linphone_error_info_get_phrase(error) : nullptr;
            g_message("text delivery failed: id=%s sip_code=%d reason=%s", id.c_str(),
                      error ? linphone_error_info_get_protocol_code(error) : 0,
                      phrase ? phrase : "unknown");
            break;
        }
        default: break;
    }
    if (status) {
        for (auto &m : g_app.messages) {
            if (m.id == id) {
                m.state = status;
                emit_signal("MessageStateChanged",
                            g_variant_new("(sss)", m.peer.c_str(), id.c_str(), status));
                break;
            }
        }
    }
    if (state == LinphoneChatMessageStateNotDelivered ||
        state == LinphoneChatMessageStateDisplayed) {
        g_app.pending_messages.erase(it);
        linphone_chat_message_unref(msg); // release our retained reference
    }
}

static void on_message_received(LinphoneCore *, LinphoneChatRoom *, LinphoneChatMessage *msg) {
    if (g_app.cfg.user.empty() || !linphone_chat_message_is_text(msg)) return;
    const LinphoneAddress *to = linphone_chat_message_get_to_address(msg);
    const char *to_user = to ? linphone_address_get_username(to) : nullptr;
    if (!to_user || g_app.cfg.user != to_user) return; // ignore a late message for an old account
    const char *text = linphone_chat_message_get_text(msg);
    if (!text || !*text || strlen(text) > kMaxTextBytes ||
        !g_utf8_validate(text, -1, nullptr)) return;
    std::string peer = chat_peer_of(linphone_chat_message_get_from_address(msg));
    if (peer.empty()) return;
    const char *remote_id = linphone_chat_message_get_message_id(msg);
    std::string id = remote_id && *remote_id ? remote_id : "";
    if (!id.empty()) {
        for (const auto &m : g_app.messages)
            if (!m.outgoing && m.peer == peer && m.id == id) return;
    } else {
        gchar *uuid = g_uuid_string_random();
        id = uuid;
        g_free(uuid);
    }
    append_chat({id, peer, text, "received", (long long)time(nullptr), false});
    emit_signal("MessageReceived", g_variant_new("(ss)", peer.c_str(), id.c_str()));
}

// ---------------------------------------------------------------- liblinphone 回调

static std::string peer_of(LinphoneCall *call) {
    const LinphoneAddress *ra = linphone_call_get_remote_address(call);
    if (!ra) return "";
    char *s = linphone_address_as_string_uri_only(ra);
    std::string out = s ? s : "";
    if (s) ms_free(s);
    return out;
}

static std::string display_of(LinphoneCall *call) {
    const LinphoneAddress *ra = linphone_call_get_remote_address(call);
    if (!ra) return "";
    const char *u = linphone_address_get_username(ra);
    return u ? u : "";
}

static void finish_history(const char *status) {
    App &a = g_app;
    if (a.peer.empty()) return;
    HistoryEntry e;
    e.peer = a.peer_display.empty() ? a.peer : a.peer_display;
    e.direction = a.direction;
    e.start = a.call_start ? a.call_start : time(nullptr);
    e.duration = a.call_start ? (int)(time(nullptr) - a.call_start) : 0;
    e.status = status;
    history_append(a, e);
    emit_signal("CallEnded", g_variant_new("(sis)", e.peer.c_str(), e.duration, status));
}

static void on_call_state(LinphoneCore *lc, LinphoneCall *call, LinphoneCallState cstate,
                          const char *message) {
    App &a = g_app;
    a.detail = message ? message : "";

    // Released 之后 LinphoneCall 可能已被释放，且不代表业务状态变化
    if (cstate == LinphoneCallReleased) {
        // Error 回调之后 core 会释放该 call。此前只忽略 Released，令业务状态永久
        // 停在 error，UI 也就无法离开通话页。这里完成错误呼叫的最终清理。
        if (a.state == "error") {
            if (a.last_error.empty()) a.last_error = a.detail.empty() ? "呼叫失败" : a.detail;
            a.call_start = 0;
            a.call_ts = 0;
            a.answered = false;
            a.media_ready = false;
            a.muted = false;
            a.stale_ticks = 0;
            g_message("call released after error; returning to idle (%s)", a.last_error.c_str());
            set_state("idle", a.last_error);
        }
        return;
    }
    if (cstate == LinphoneCallIncomingReceived || cstate == LinphoneCallOutgoingInit)
        a.last_error.clear();

    switch (cstate) {
        case LinphoneCallIncomingReceived:
            a.peer = peer_of(call);
            a.peer_display = display_of(call);
            a.direction = "incoming";
            a.answered = false;
            a.media_ready = false;
            a.call_start = 0;
            a.call_ts = time(nullptr);
            set_state("incoming", message ? message : "来电");
            emit_signal("IncomingCall",
                        g_variant_new("(ss)", a.peer.c_str(), a.peer_display.c_str()));
            if (a.cfg.auto_answer) {
                g_message("auto-answering (test mode)");
                linphone_call_accept(call);
            }
            break;

        case LinphoneCallOutgoingInit:
        case LinphoneCallOutgoingProgress:
            if (!a.call_ts) a.call_ts = time(nullptr);
            if (a.peer.empty()) {
                a.peer = peer_of(call);
                a.peer_display = display_of(call);
                a.direction = "outgoing";
            }
            set_state("outgoing", message ? message : "正在呼叫");
            break;

        case LinphoneCallOutgoingRinging:
            set_state("ringing", message ? message : "对方振铃");
            break;

        case LinphoneCallConnected:
            a.peer = peer_of(call);
            if (a.peer_display.empty()) a.peer_display = display_of(call);
            // Connected 表示收到 200 OK，对端已应答。但媒体可能尚未建立，
            // 要等 StreamsRunning 才能真正收发声音。
            a.answered = true;
            set_state("connected", "已接通");
            break;

        case LinphoneCallStreamsRunning:
            // 出局呼叫在早期媒体（183 / 回铃音）时也会进入 StreamsRunning，
            // 此时对端尚未接听。只有经历过 Connected（对端应答）后才算通话中，
            // 否则静音/DTMF/免提会提前亮起，通话时长也从拨出瞬间开始计。
            if (!a.answered) {
                g_message("early media before answer; staying in %s", a.state.c_str());
                break;
            }
            a.media_ready = true;
            if (!a.call_start) a.call_start = time(nullptr);
            set_state("incall", "通话中");
            break;

        case LinphoneCallPaused:
            set_state("paused", "已保持");
            break;

        case LinphoneCallEnd:
            set_state("ended", message ? message : "通话结束");
            finish_history(a.answered ? "answered" : (a.direction == "incoming" ? "missed" : "failed"));
            a.call_start = 0;
            a.call_ts = 0;
            a.answered = false;
            a.media_ready = false;
            a.muted = false;
            set_state("idle", "空闲");
            break;

        case LinphoneCallError:
            a.last_error = message ? message : "呼叫失败";
            g_message("call failed: peer=%s reason=%s", a.peer.c_str(), a.last_error.c_str());
            set_state("error", a.last_error);
            finish_history(a.direction == "incoming" ? "missed" : "failed");
            a.call_start = 0;
            a.call_ts = 0;
            a.answered = false;
            a.media_ready = false;
            break;

        default:
            // 早期媒体等中间状态不映射成业务状态，避免界面出现陌生状态名；
            // 保留当前状态直到收到明确的应答或结束事件。
            if (cstate == LinphoneCallOutgoingEarlyMedia ||
                cstate == LinphoneCallIncomingEarlyMedia) {
                if (a.state.empty() || a.state == "idle")
                    set_state(a.direction == "incoming" ? "incoming" : "outgoing",
                              message ? message : "早期媒体");
                break;
            }
            set_state(state_name(cstate), message ? message : "");
            break;
    }
}

static void on_registration(LinphoneCore *lc, LinphoneProxyConfig *cfg,
                            LinphoneRegistrationState cstate, const char *message) {
    (void)lc; (void)cfg;
    App &a = g_app;
    if (a.cfg.user.empty()) {
        a.registered = false;
        a.reg_detail = "Not configured";
        return;
    }
    a.reg_detail = message ? message : "";
    // 注册刷新与通话可以同时发生；注册回调不能覆盖当前通话状态。
    const bool in_call = active_call() != nullptr;
    switch (cstate) {
        case LinphoneRegistrationOk:
            a.registered = true;
            if (a.state == "idle" || a.state == "registering") a.state = "registered";
            break;
        case LinphoneRegistrationProgress:
            a.registered = false;
            if (!in_call) a.state = "registering";
            break;
        case LinphoneRegistrationFailed:
            a.registered = false;
            if (!in_call) a.state = "registered_failed";
            break;
        case LinphoneRegistrationCleared:
            a.registered = false;
            if (!in_call) a.state = "idle";
            break;
        default:
            break;
    }
    g_message("[reg] %d %s", (int)cstate, a.reg_detail.c_str());
    emit_signal("RegistrationChanged",
                g_variant_new("(bs)", a.registered, a.reg_detail.c_str()));
}

// ---------------------------------------------------------------- liblinphone 初始化

// 清理 core/linphonerc 中的旧账号。首次未配置启动时也必须清理，否则上一次
// 持久化的账号可能在登录页提交前自行注册。
static void clear_accounts(App &a) {
    const bctbx_list_t *infos = linphone_core_get_auth_info_list(a.lc);
    std::vector<LinphoneAuthInfo *> old_auth;
    for (const bctbx_list_t *it = infos; it; it = it->next)
        old_auth.push_back((LinphoneAuthInfo *)it->data);
    for (auto *o : old_auth) linphone_core_remove_auth_info(a.lc, o);

#if VOIP_HAS_ACCOUNT_API
    std::vector<LinphoneAccount *> old;
    for (const bctbx_list_t *it = linphone_core_get_account_list(a.lc); it; it = it->next)
        old.push_back((LinphoneAccount *)it->data);
    for (auto *o : old) linphone_core_remove_account(a.lc, o);
#else
    std::vector<LinphoneProxyConfig *> old;
    for (const bctbx_list_t *it = linphone_core_get_proxy_config_list(a.lc); it; it = it->next)
        old.push_back((LinphoneProxyConfig *)it->data);
    for (auto *o : old) linphone_core_remove_proxy_config(a.lc, o);
#endif
}

// 重新应用 SIP 账号（用户名/密码/服务器）：清空旧账号，用新 Config 重新注册。
// core_init() 首次启动和登录页 SetAccount 都复用这个函数，无需重启整个 LinphoneCore。
static bool apply_account(App &a) {
    std::string dom = server_host(a.cfg.server);
    std::string port_part;
    {
        auto c = a.cfg.server.rfind(':');
        if (c != std::string::npos && c > 4) port_part = a.cfg.server.substr(c);  // ":5060"
    }
    std::string aor = "sip:" + a.cfg.user + "@" + dom + port_part;
    LinphoneAddress *server = linphone_address_new(a.cfg.server.c_str());
    LinphoneAddress *identity = linphone_address_new(aor.c_str());
    if (!server || !identity) {
        g_printerr("invalid server/identity: %s / %s\n", a.cfg.server.c_str(), aor.c_str());
        if (server) linphone_address_unref(server);
        if (identity) linphone_address_unref(identity);
        return false;
    }

    // 清空旧账号和认证信息，避免旧密码/realm 残留导致下次挑战失败。
    clear_accounts(a);
    if (!a.cfg.secret.empty()) {
        LinphoneAuthInfo *ai = linphone_auth_info_new(
            a.cfg.user.c_str(), nullptr, a.cfg.secret.c_str(), nullptr,
            a.cfg.realm.empty() ? nullptr : a.cfg.realm.c_str(), nullptr);
        linphone_core_add_auth_info(a.lc, ai);
        linphone_auth_info_unref(ai);
    }

#if VOIP_HAS_ACCOUNT_API
    // ---- liblinphone 5.x：LinphoneAccount ----
    // linphonerc 会持久化上次的账号；配置改了 server/user 后旧账号会残留并覆盖新身份，
    // 导致 From 域与服务器 realm 不一致（表现为 401 循环）。这里先清空再建。
    {
        LinphoneAccountParams *ap = linphone_account_params_new(a.lc);
        linphone_account_params_set_server_address(ap, server);
        linphone_account_params_set_identity_address(ap, identity);
        linphone_account_params_enable_register(ap, TRUE);
        linphone_account_params_set_expires(ap, 300);
        LinphoneAccount *acc = linphone_core_create_account(a.lc, ap);
        linphone_core_add_account(a.lc, acc);
        linphone_core_set_default_account(a.lc, acc);
        linphone_account_params_unref(ap);
    }
#else
    // ---- liblinphone 4.4.x：LinphoneProxyConfig（等价能力）----
    {
        LinphoneProxyConfig *pc = linphone_core_create_proxy_config(a.lc);
        linphone_proxy_config_set_identity_address(pc, identity);
        linphone_proxy_config_set_server_addr(pc, a.cfg.server.c_str());
        linphone_proxy_config_enable_register(pc, TRUE);
        linphone_proxy_config_set_expires(pc, 300);
        linphone_core_add_proxy_config(a.lc, pc);
        linphone_core_set_default_proxy_config(a.lc, pc);
        linphone_proxy_config_unref(pc);
    }
#endif

    linphone_address_unref(server);
    linphone_address_unref(identity);

    // 归位状态
    a.state = "registering";
    a.registered = false;
    a.reg_detail = "";
    g_message("account=%s server=%s port=%d", aor.c_str(), a.cfg.server.c_str(), a.cfg.sip_port);
    return true;
}

static bool core_init(App &a) {
    linphone_factory_get();
    if (!a.cfg.data_resources_dir.empty()) {
        linphone_factory_set_data_resources_dir(linphone_factory_get(),
                                                a.cfg.data_resources_dir.c_str());
        g_message("data resources dir -> %s", a.cfg.data_resources_dir.c_str());
#ifdef VOIP_WITH_BELR
        // 4.4 的 belr 语法目录在库加载时固定为 /usr/share，这里追加自定义路径
        belr::GrammarLoader::get().addPath(a.cfg.data_resources_dir + "/belr/grammars");
        g_message("belr grammar path += %s/belr/grammars", a.cfg.data_resources_dir.c_str());
#endif
    }

    // 独立 data 目录，避免与系统 linphone 的用户数据库冲突
    std::string data_dir = a.cfg.data_dir.empty()
                               ? std::string(g_get_user_data_dir()) + "/voipd"
                               : a.cfg.data_dir;
    g_mkdir_with_parents(data_dir.c_str(), 0700);
    std::string rc = data_dir + "/linphonerc";
    a.history_path = data_dir + "/history.jsonl";

    linphone_core_set_log_level(a.cfg.verbose ? ORTP_DEBUG : ORTP_WARNING);

    LinphoneCoreCbs *cbs = linphone_factory_create_core_cbs(linphone_factory_get());
    linphone_core_cbs_set_call_state_changed(cbs, on_call_state);
    linphone_core_cbs_set_registration_state_changed(cbs, on_registration);
    linphone_core_cbs_set_message_received(cbs, on_message_received);

    a.lc = linphone_factory_create_core_3(linphone_factory_get(), rc.c_str(), nullptr, nullptr);
    if (!a.lc) { g_printerr("failed to create LinphoneCore\n"); return false; }
    if (!chat_resources_present(a.cfg)) {
        // 4.4 aborts while parsing a chat room without the grammar, including on incoming MESSAGE.
        linphone_core_disable_chat(a.lc, LinphoneReasonNotAcceptable);
        g_warning("chat disabled: missing linphone-common identity_grammar");
    }
    linphone_core_add_callbacks(a.lc, cbs);
    linphone_core_cbs_unref(cbs);

    // 纯语音
    linphone_core_enable_video_capture(a.lc, FALSE);
    linphone_core_enable_video_display(a.lc, FALSE);
    LinphoneVideoPolicy vpol;
    memset(&vpol, 0, sizeof(vpol));  // 4.4/5.x 结构体成员不同，用清零避免初始化差异
    vpol.automatically_initiate = FALSE;
    vpol.automatically_accept = FALSE;
    linphone_core_set_video_policy(a.lc, &vpol);

    // SIP 传输：只开 UDP，端口可配
    LinphoneTransports *tr = linphone_core_get_transports(a.lc);
    linphone_transports_set_udp_port(tr, a.cfg.sip_port);
    linphone_transports_set_tcp_port(tr, LC_SIP_TRANSPORT_DISABLED);
    linphone_transports_set_tls_port(tr, LC_SIP_TRANSPORT_DISABLED);
    linphone_transports_set_dtls_port(tr, LC_SIP_TRANSPORT_DISABLED);
    linphone_core_set_transports(a.lc, tr);
    linphone_transports_unref(tr);

    // 音频设备：显式指定 > 自动挑选真实硬件 > 系统默认
    // 注意：绝不能用系统默认。默认可能是回声消除虚拟设备（实测采集/播放无声），
    // 或者被 PulseAudio module-stream-restore 记忆到未接线的数字输出（PC 上踩过）。
    const char **dev_names = linphone_core_get_sound_devices(a.lc);

    // 把 pactl 的系统默认设备描述，映射成 liblinphone 列表里的那一项
    auto map_default = [&](const char *kind, bool for_playback) -> std::string {
        std::string nm, desc;
        if (!pactl_default(kind, nm, desc)) return "";
        if (is_virtual_device(desc) || is_virtual_device(nm)) {
            g_message("system default %s '%s' is virtual, will pick hardware instead",
                      kind, desc.c_str());
            return "";
        }
        for (int i = 0; dev_names && dev_names[i]; ++i) {
            std::string d = dev_names[i];
            if (is_virtual_device(d)) continue;
            if (d.find(desc) != std::string::npos) return d;
        }
        // 描述匹配不上时，退化为按关键字挑
        (void)for_playback;
        return "";
    };

    {
        std::string cap = a.cfg.capture_device;
        if (cap.empty()) cap = map_default("sources", false);
        if (cap.empty()) cap = pick_device(dev_names, false);
        if (!cap.empty()) {
            if (linphone_core_set_capture_device(a.lc, cap.c_str()) != 0)
                g_warning("set capture device failed: %s", cap.c_str());
            else
                g_message("capture device -> %s", cap.c_str());
        } else {
            g_warning("no hardware capture device found, using system default");
        }

        std::string play = a.cfg.playback_device;
        if (play.empty()) play = map_default("sinks", true);
        if (play.empty()) play = pick_device(dev_names, true);
        if (!play.empty()) {
            if (linphone_core_set_playback_device(a.lc, play.c_str()) != 0)
                g_warning("set playback device failed: %s", play.c_str());
            else
                g_message("playback device -> %s", play.c_str());
        } else {
            g_warning("no hardware playback device found, using system default");
        }
    }

    if (a.cfg.playback_gain_db != 0.0f) {
        linphone_core_set_playback_gain_db(a.lc, a.cfg.playback_gain_db);
        g_message("playback gain %.1f dB", a.cfg.playback_gain_db);
    }
    if (a.cfg.mic_gain_db != 0.0f) {
        linphone_core_set_mic_gain_db(a.lc, a.cfg.mic_gain_db);
        g_message("mic gain %.1f dB", a.cfg.mic_gain_db);
    }

    linphone_core_start(a.lc);  // 5.x 必须显式启动

    if (a.cfg.user.empty() || a.cfg.server.empty()) {
        clear_accounts(a);
        a.state = "idle";
        a.registered = false;
        a.reg_detail = "Not configured";
        g_message("SIP account is not configured; waiting for UI login");
        return true;
    }
    return apply_account(a);
}

// ---------------------------------------------------------------- D-Bus 方法

// 不保存通话裸指针；current_call 在某些通话阶段可能为空，回退到 core 通话列表。
static LinphoneCall *active_call() {
    if (!g_app.lc) return nullptr;
    auto usable = [](LinphoneCall *c) {
        if (!c) return false;
        switch (linphone_call_get_state(c)) {
            case LinphoneCallEnd:
            case LinphoneCallError:
            case LinphoneCallReleased:
            case LinphoneCallIdle:
                return false;
            default:
                return true;
        }
    };
    LinphoneCall *c = linphone_core_get_current_call(g_app.lc);
    if (usable(c)) return c;
    for (const bctbx_list_t *it = linphone_core_get_calls(g_app.lc); it; it = it->next) {
        c = static_cast<LinphoneCall *>(bctbx_list_get_data(it));
        if (usable(c)) return c;
    }
    return nullptr;
}

static void handle_method(GDBusConnection *, const gchar *, const gchar *, const gchar *,
                          const gchar *method, GVariant *params,
                          GDBusMethodInvocation *inv, gpointer) {
    App &a = g_app;
    std::string m = method;

    if (m == "Dial") {
        const char *uri = nullptr;
        g_variant_get(params, "(&s)", &uri);
        std::string target = uri ? uri : "";
        if (target.empty()) {
            g_dbus_method_invocation_return_value(inv, g_variant_new("(b)", FALSE));
            return;
        }
        if (target.find("sip:") != 0 && target.find("tel:") != 0) {
            // 纯号码/用户名 -> 补全为 sip:user@server
            if (target.find('@') == std::string::npos)
                target = "sip:" + target + "@" + server_host(a.cfg.server);
            else
                target = "sip:" + target;
        }
        // 禁止拨打本机分机：把目标规整成分机号再与当前账号比对，
        // 无论传 "1002"、"sip:1002@host" 还是带参数都能拦住。
        {
            std::string t = target;
            for (const char *pre : {"sips:", "sip:", "tel:"}) {
                const size_t n = strlen(pre);
                if (t.size() >= n && strncasecmp(t.c_str(), pre, n) == 0) { t = t.substr(n); break; }
            }
            const size_t at = t.find('@');
            if (at != std::string::npos) t = t.substr(0, at);
            const size_t semi = t.find(';');
            if (semi != std::string::npos) t = t.substr(0, semi);
            if (!a.cfg.user.empty() && t == a.cfg.user) {
                g_message("dial rejected: target equals own extension (%s)", t.c_str());
                a.last_error = "不能拨打本机分机号";
                g_dbus_method_invocation_return_value(inv, g_variant_new("(b)", FALSE));
                return;
            }
        }
        // 已有通话（可能是对端异常消失后残留的通话）先结束，否则 liblinphone 会因
        // "sound are locked by another call" 直接拒绝新呼叫。
        if (LinphoneCall *cur = active_call()) {
            g_message("dial while busy: terminating previous call first");
            linphone_core_terminate_call(a.lc, cur);
            for (int i = 0; i < 25 && active_call(); ++i) { linphone_core_iterate(a.lc); g_usleep(20000); }
        }
        a.call_ts = 0;
        a.answered = false;
        a.media_ready = false;
        a.call_start = 0;
        g_message("dial %s", target.c_str());
        a.peer = target;
        a.direction = "outgoing";
        a.peer_display = target;
        {
            auto p = target.find(':');
            auto at = target.find('@');
            if (at != std::string::npos)
                a.peer_display = target.substr(p + 1, at - p - 1);
        }
        LinphoneCall *c = linphone_core_invite(a.lc, target.c_str());
        if (!c) {
            a.last_error = "invite failed";
            g_message("call invite rejected locally: target=%s", target.c_str());
            set_state("error", a.last_error);
            a.call_start = 0;
            a.call_ts = 0;
            a.answered = false;
            a.media_ready = false;
            set_state("idle", a.last_error);
            g_dbus_method_invocation_return_value(inv, g_variant_new("(b)", FALSE));
            return;
        }
        g_dbus_method_invocation_return_value(inv, g_variant_new("(b)", TRUE));
        return;
    }

    if (m == "Answer") {
        LinphoneCall *c = active_call();
        if (c) linphone_call_accept(c);
        g_dbus_method_invocation_return_value(inv, nullptr);
        return;
    }

    if (m == "Decline") {
        LinphoneCall *c = active_call();
        if (c) linphone_core_decline_call(a.lc, c, LinphoneReasonDeclined);
        g_dbus_method_invocation_return_value(inv, nullptr);
        return;
    }

    if (m == "Hangup") {
        const int count = a.lc ? linphone_core_get_calls_nb(a.lc) : 0;
        g_message("hangup requested: peer=%s state=%s core_calls=%d",
                  a.peer.c_str(), a.state.c_str(), count);
        if (count == 0) {
            // UI 可能还显示上一次通话；没有真实通话时直接让它退出通话页。
            if (a.state == "incoming" || a.state == "outgoing" ||
                a.state == "ringing" || a.state == "connected" ||
                a.state == "incall" || a.state == "paused" ||
                a.state == "error") {
                a.call_start = 0;
                a.call_ts = 0;
                a.answered = false;
            a.media_ready = false;
                a.muted = false;
                a.stale_ticks = 0;
                set_state("idle", "没有进行中的通话");
            }
            g_dbus_method_invocation_return_value(inv, g_variant_new("(b)", TRUE));
            return;
        }
        // 这是单通话应用；遍历 core 内所有通话，避免 current_call 为空时静默无效。
        const LinphoneStatus status = linphone_core_terminate_all_calls(a.lc);
        g_message("hangup result: status=%d remaining_calls=%d", status,
                  linphone_core_get_calls_nb(a.lc));
        g_dbus_method_invocation_return_value(inv, g_variant_new("(b)", status == 0));
        return;
    }

    if (m == "SetMute") {
        gboolean muted = FALSE;
        g_variant_get(params, "(b)", &muted);
        LinphoneCall *c = active_call();
        if (c) linphone_call_set_microphone_muted(c, muted ? TRUE : FALSE);
        a.muted = muted;
        g_dbus_method_invocation_return_value(inv, nullptr);
        return;
    }

    if (m == "SetSpeaker") {
        gboolean on = FALSE;
        g_variant_get(params, "(b)", &on);
        a.speaker = on;
        // 免提：语音场景下由系统默认 sink 播放；此处保留状态供 UI 展示，
        // 实际路由由 PipeWire/WirePlumber 决定（见 README“音频路由”）。
        g_dbus_method_invocation_return_value(inv, nullptr);
        return;
    }

    if (m == "SendDTMF") {
        const char *digits = nullptr;
        g_variant_get(params, "(&s)", &digits);
        LinphoneCall *c = active_call();
        if (c && digits) {
            for (const char *p = digits; *p; ++p) linphone_call_send_dtmf(c, *p);
        }
        g_dbus_method_invocation_return_value(inv, nullptr);
        return;
    }

    if (m == "GetSnapshot") {
        std::string j = snapshot_json();
        g_dbus_method_invocation_return_value(inv, g_variant_new("(s)", j.c_str()));
        return;
    }

    if (m == "GetHistory") {
        std::string j = history_json();
        g_dbus_method_invocation_return_value(inv, g_variant_new("(s)", j.c_str()));
        return;
    }

    if (m == "SendText") {
        const char *target = nullptr, *text = nullptr;
        g_variant_get(params, "(&s&s)", &target, &text);
        auto reject = [&](const char *why) {
            g_dbus_method_invocation_return_value(inv, g_variant_new("(bss)", FALSE, "", why));
        };
        if (!a.lc || !a.registered || a.cfg.user.empty()) { reject("not_registered"); return; }
        if (!chat_resources_present(a.cfg)) { reject("chat_resources_missing"); return; }
        if (!text || !*text || strlen(text) > kMaxTextBytes ||
            !g_utf8_validate(text, -1, nullptr)) { reject("invalid_text"); return; }
        bool non_blank = false;
        for (const char *p = text; *p;) {
            gunichar c = g_utf8_get_char(p);
            if (!g_unichar_isspace(c)) non_blank = true;
            p = g_utf8_next_char(p);
        }
        if (!non_blank) { reject("invalid_text"); return; }
        std::string peer, uri;
        if (!target || !normalize_peer(target, peer, uri)) { reject("invalid_peer"); return; }
        LinphoneAddress *addr = linphone_address_new(uri.c_str());
        // 4.4 can reuse an incoming room whose local To is a NAT Contact address.
        // Without an explicit account identity, replies then use that public IP
        // as their From domain and split the peer's conversation into two rooms.
        LinphoneAddress *local = linphone_address_new(a.cfg.server.c_str());
        if (local) linphone_address_set_username(local, a.cfg.user.c_str());
        LinphoneChatRoom *room = addr && local ? linphone_core_get_chat_room_2(a.lc, addr, local) : nullptr;
        if (local) linphone_address_unref(local);
        if (addr) linphone_address_unref(addr);
        if (!room) { reject("chat_unavailable"); return; }
        LinphoneChatMessage *msg = linphone_chat_room_create_message(room, text);
        if (!msg) { reject("chat_unavailable"); return; }
        LinphoneChatMessageCbs *cbs = linphone_chat_message_get_callbacks(msg);
        linphone_chat_message_cbs_set_msg_state_changed(cbs, on_message_state);
        gchar *uuid = g_uuid_string_random();
        std::string id = uuid;
        g_free(uuid);
        append_chat({id, peer, text, "sending", (long long)time(nullptr), true});
        a.pending_messages.emplace(msg, id);
        linphone_chat_message_ref(msg); // held until final state, account switch or eviction
        linphone_chat_message_send(msg); // asynchronous; accepted != delivered
        linphone_chat_message_unref(msg); // release create_message's reference
        g_dbus_method_invocation_return_value(inv, g_variant_new("(bss)", TRUE, id.c_str(), ""));
        return;
    }

    if (m == "GetChats") {
        std::string j = chats_json();
        g_dbus_method_invocation_return_value(inv, g_variant_new("(s)", j.c_str()));
        return;
    }

    if (m == "GetMessages") {
        const char *target = nullptr;
        g_variant_get(params, "(&s)", &target);
        std::string peer, uri;
        std::string j = target && normalize_peer(target, peer, uri) ? messages_json(peer) : "[]";
        g_dbus_method_invocation_return_value(inv, g_variant_new("(s)", j.c_str()));
        return;
    }

    if (m == "ClearHistory") {
        a.history.clear();
        if (!a.history_path.empty()) unlink(a.history_path.c_str());
        g_dbus_method_invocation_return_value(inv, nullptr);
        return;
    }

    if (m == "GetDevices") {
        // 返回完整的声音设备快照：每个设备的名称、能否录音/放音、是否正在使用。
        // 只做展示，不提供切换接口；实际选设备仍由配置文件与系统音频设置决定。
        const char *cur_cap = linphone_core_get_capture_device(a.lc);
        const char *cur_play = linphone_core_get_playback_device(a.lc);
        std::string j = "{\"devices\":[";
        const char **devs = linphone_core_get_sound_devices(a.lc);
        bool first = true;
        for (int i = 0; devs && devs[i]; ++i) {
            const char *name = devs[i];
            const bool can_cap = linphone_core_sound_device_can_capture(a.lc, name);
            const bool can_play = linphone_core_sound_device_can_playback(a.lc, name);
            const bool active = (cur_cap && strcmp(cur_cap, name) == 0) ||
                                (cur_play && strcmp(cur_play, name) == 0);
            if (!first) j += ",";
            first = false;
            j += "{\"name\":\"" + json_escape(name) + "\",";
            j += std::string("\"capture\":") + (can_cap ? "true" : "false") + ",";
            j += std::string("\"playback\":") + (can_play ? "true" : "false") + ",";
            j += std::string("\"active\":") + (active ? "true" : "false") + "}";
        }
        j += "],\"capture\":\"" + json_escape(cur_cap ? cur_cap : "") + "\",";
        j += "\"playback\":\"" + json_escape(cur_play ? cur_play : "") + "\",";
        j += std::string("\"configured_capture\":\"") + json_escape(a.cfg.capture_device) + "\",";
        j += std::string("\"configured_playback\":\"") + json_escape(a.cfg.playback_device) + "\"}";
        g_dbus_method_invocation_return_value(inv, g_variant_new("(s)", j.c_str()));
        return;
    }

    if (m == "GetAccount") {
        char buf[1024];
        snprintf(buf, sizeof(buf),
            "{\"user\":\"%s\",\"server\":\"%s\",\"display_name\":\"%s\","
            "\"sip_port\":%d,\"registered\":%s,\"reg_detail\":\"%s\"}",
            json_escape(a.cfg.user).c_str(), json_escape(a.cfg.server).c_str(),
            json_escape(a.cfg.display_name).c_str(), a.cfg.sip_port,
            a.registered ? "true" : "false", json_escape(a.reg_detail).c_str());
        g_dbus_method_invocation_return_value(inv, g_variant_new("(s)", buf));
        return;
    }

    if (m == "Logout") {
        if (active_call()) {
            g_dbus_method_invocation_return_value(inv, g_variant_new("(bs)", FALSE, "call_active"));
            return;
        }
        const Config previous = a.cfg;
        a.cfg.user.clear();
        a.cfg.secret.clear();
        a.cfg.display_name.clear();
        if (!save_config(a)) {
            a.cfg = previous;
            g_dbus_method_invocation_return_value(inv, g_variant_new("(bs)", FALSE, "save_failed"));
            return;
        }
        clear_chat(a);
        clear_accounts(a);
        a.registered = false;
        a.reg_detail = "Not configured";
        a.last_error.clear();
        set_state("idle", "");
        emit_signal("RegistrationChanged", g_variant_new("(bs)", FALSE, a.reg_detail.c_str()));
        g_message("account logged out");
        g_dbus_method_invocation_return_value(inv, g_variant_new("(bs)", TRUE, ""));
        return;
    }

    if (m == "SetAccount") {
        const char *server = nullptr, *user = nullptr, *secret = nullptr, *dispn = nullptr;
        g_variant_get(params, "(&s&s&s&s)", &server, &user, &secret, &dispn);
        std::string s_server = server ? server : "";
        std::string s_user = user ? user : "";
        std::string s_secret = secret ? secret : "";
        std::string s_disp = dispn ? dispn : "";
        if (s_server.empty() || s_user.empty()) {
            g_dbus_method_invocation_return_value(
                inv, g_variant_new("(bs)", FALSE, "server/user 不能为空"));
            return;
        }
        // 未带 sip: 前缀时自动补全，方便登录页直接填 host:port
        if (s_server.rfind("sip:", 0) != 0) s_server = "sip:" + s_server;

        // 通话中时先结束旧通话，避免换账号时音频被锁住
        if (LinphoneCall *cur = active_call()) {
            linphone_core_terminate_call(a.lc, cur);
            for (int i = 0; i < 25 && active_call(); ++i) { linphone_core_iterate(a.lc); g_usleep(20000); }
        }

        if (a.cfg.server != s_server || a.cfg.user != s_user) clear_chat(a);
        a.cfg.server = s_server;
        a.cfg.user = s_user;
        a.cfg.secret = s_secret;
        a.cfg.display_name = s_disp;

        bool ok = apply_account(a);
        if (ok) save_config(a);
        g_dbus_method_invocation_return_value(
            inv, g_variant_new("(bs)", ok, ok ? "" : "无效的 server/身份地址"));
        return;
    }

    g_dbus_method_invocation_return_error(inv, G_DBUS_ERROR, G_DBUS_ERROR_UNKNOWN_METHOD,
                                          "unknown method %s", method);
    return;
}

// ---------------------------------------------------------------- 主循环 tick

static gboolean tick(gpointer) {
    App &a = g_app;
    if (a.lc) linphone_core_iterate(a.lc);

    // 僵尸通话回收：状态停在通话态，但 core 里已经没有任何通话（对端异常消失且
    // 未收到 BYE），此时必须复位，否则新呼叫会被以 486 Busy 拒绝。
    if (a.lc && (a.state == "incall" || a.state == "connected" || a.state == "paused" ||
                 a.state == "outgoing" || a.state == "ringing" || a.state == "error")) {
        if (!active_call()) {
            a.stale_ticks++;
            if (a.stale_ticks > 50) {  // 约 1 秒
                g_message("stale call detected (state=%s), resetting", a.state.c_str());
                a.call_start = 0;
                a.call_ts = 0;
                a.answered = false;
            a.media_ready = false;
                a.muted = false;
                a.stale_ticks = 0;
                set_state("idle", "空闲");
            }
        } else {
            a.stale_ticks = 0;
        }
    }

    // 无应答超时：避免去电永远挂在 ringing，把音频资源锁死（liblinphone 会拒绝新呼叫）
    if (a.lc && a.call_ts && a.cfg.no_answer_timeout > 0 &&
        (a.state == "outgoing" || a.state == "ringing")) {
        long long age = (long long)time(nullptr) - a.call_ts;
        if (age > a.cfg.no_answer_timeout) {
            LinphoneCall *c = active_call();
            g_message("no answer after %llds, terminating", age);
            a.call_ts = 0;
            if (c) linphone_core_terminate_call(a.lc, c);
        }
    }
    return G_SOURCE_CONTINUE;
}

// ---------------------------------------------------------------- 配置加载

static bool load_config(App &a, const char *path) {
    GKeyFile *kf = g_key_file_new();
    GError *err = nullptr;
    bool ok = g_key_file_load_from_file(kf, path, G_KEY_FILE_NONE, &err);
    if (!ok) {
        g_printerr("config %s: %s\n", path, err ? err->message : "cannot load");
        if (err) g_error_free(err);
        g_key_file_unref(kf);
        return false;
    }
    auto str = [&](const char *key, const char *def) -> std::string {
        GError *e = nullptr;
        gchar *v = g_key_file_get_string(kf, "account", key, &e);
        if (e) { g_error_free(e); return def; }
        std::string out = v ? v : def;
        g_free(v);
        return out;
    };
    auto gstr = [&](const char *grp, const char *key, const char *def) -> std::string {
        GError *e = nullptr;
        gchar *v = g_key_file_get_string(kf, grp, key, &e);
        if (e) { g_error_free(e); return def; }
        std::string out = v ? v : def;
        g_free(v);
        return out;
    };
    a.cfg.user = str("user", a.cfg.user.c_str());
    a.cfg.secret = str("secret", "");
    a.cfg.server = str("server", a.cfg.server.c_str());
    a.cfg.display_name = str("display_name", "");
    a.cfg.realm = str("realm", "");
    a.cfg.capture_device = str("capture_device", "");
    a.cfg.data_resources_dir = str("data_resources_dir", "");
    a.cfg.playback_device = str("playback_device", "");

    GError *e = nullptr;
    int p = g_key_file_get_integer(kf, "account", "sip_port", &e);
    if (e) g_error_free(e); else a.cfg.sip_port = p;

    gboolean aa = g_key_file_get_boolean(kf, "general", "auto_answer", &e);
    if (e) g_error_free(e); else a.cfg.auto_answer = aa ? true : false;

    int nat = g_key_file_get_integer(kf, "general", "no_answer_timeout", &e);
    if (e) g_error_free(e); else a.cfg.no_answer_timeout = nat;

    auto gdbl = [&](const char *grp, const char *key, float def) -> float {
        GError *e2 = nullptr;
        double v = g_key_file_get_double(kf, grp, key, &e2);
        if (e2) { g_error_free(e2); return def; }
        return (float)v;
    };
    a.cfg.playback_gain_db = gdbl("audio", "playback_gain_db", 0.0f);
    a.cfg.mic_gain_db = gdbl("audio", "mic_gain_db", 0.0f);

    a.cfg.verbose = gstr("general", "verbose", "false") == "true";
    g_key_file_unref(kf);
    return true;
}

// 登录页 SetAccount 成功后把当前账号回写配置文件，下次启动 voipd 自动用新账号。
static bool save_config(App &a) {
    if (a.cfg_path.empty()) return false;
    GKeyFile *kf = g_key_file_new();
    GError *err = nullptr;
    // 先读回原文件，保留本次没有更改的其他字段（音频设备/增益等）
    g_key_file_load_from_file(kf, a.cfg_path.c_str(), G_KEY_FILE_NONE, nullptr);
    g_key_file_set_string(kf, "account", "user", a.cfg.user.c_str());
    g_key_file_set_string(kf, "account", "secret", a.cfg.secret.c_str());
    g_key_file_set_string(kf, "account", "server", a.cfg.server.c_str());
    g_key_file_set_string(kf, "account", "display_name", a.cfg.display_name.c_str());
    g_key_file_set_integer(kf, "account", "sip_port", a.cfg.sip_port);
    gsize len = 0;
    gchar *data = g_key_file_to_data(kf, &len, &err);
    bool ok = false;
    if (data) {
        ok = g_file_set_contents(a.cfg_path.c_str(), data, (gssize)len, nullptr);
        g_free(data);
    }
    if (err) g_error_free(err);
    g_key_file_unref(kf);
    return ok;
}

static void on_bus_acquired(GDBusConnection *conn, const gchar *name, gpointer) {
    App &a = g_app;
    a.bus = conn;
    GError *err = nullptr;
    GDBusNodeInfo *info = g_dbus_node_info_new_for_xml(kIntrospection, &err);
    if (!info) {
        g_printerr("introspection parse failed: %s\n", err->message);
        g_error_free(err);
        g_main_loop_quit(a.loop);
        return;
    }
    static const GDBusInterfaceVTable vtable = {handle_method, nullptr, nullptr, {0}};
    a.reg_id = g_dbus_connection_register_object(conn, kObjectPath, info->interfaces[0],
                                                 &vtable, nullptr, nullptr, &err);
    if (!a.reg_id) {
        g_printerr("register object failed: %s\n", err->message);
        g_error_free(err);
    }
    g_dbus_node_info_unref(info);
    g_message("D-Bus ready: %s on %s", name, kObjectPath);
}

static void on_name_lost(GDBusConnection *, const gchar *name, gpointer) {
    g_printerr("D-Bus name %s lost (is another voipd running?)\n", name);
    if (g_app.loop) g_main_loop_quit(g_app.loop);
}

// ---------------------------------------------------------------- main

int main(int argc, char **argv) {
    std::string cfg_path = std::string(g_get_user_config_dir()) + "/voipd/voipd.conf";
    std::string bus_name = kBusName;
    bool auto_answer = false;

    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        if (a == "--config" && i + 1 < argc) cfg_path = argv[++i];
        else if (a == "--bus-name" && i + 1 < argc) bus_name = argv[++i];
        else if (a == "--auto-answer") auto_answer = true;
        else if (a == "--data-dir" && i + 1 < argc) g_app.cfg.data_dir = argv[++i];
        else if (a == "--verbose") g_app.cfg.verbose = true;
        else if (a == "--help" || a == "-h") {
            printf("usage: %s [--config FILE] [--bus-name NAME] [--data-dir DIR]\n"
                   "          [--auto-answer] [--verbose]\n", argv[0]);
            printf("  default config: %s\n", cfg_path.c_str());
            return 0;
        }
    }

    // 配置不存在时生成模板
    if (!g_file_test(cfg_path.c_str(), G_FILE_TEST_EXISTS)) {
        gchar *dir = g_path_get_dirname(cfg_path.c_str());
        g_mkdir_with_parents(dir, 0700);
        g_free(dir);
        const char *tpl =
            "# voipd 配置\n[account]\nuser=\nsecret=\n"
            "server=\nsip_port=5060\n"
            "display_name=会议室话机\n"
            "# 留空用系统默认（PipeWire 默认源/汇，默认源含 AEC 降噪）\n"
            "capture_device=\nplayback_device=\n\n[general]\nauto_answer=false\nverbose=false\n";
        g_file_set_contents(cfg_path.c_str(), tpl, -1, nullptr);
        g_message("wrote template config to %s", cfg_path.c_str());
    }

    if (!load_config(g_app, cfg_path.c_str())) return 1;
    g_app.cfg_path = cfg_path;
    if (auto_answer) g_app.cfg.auto_answer = true;
    if (g_app.cfg.verbose) linphone_core_set_log_level(ORTP_DEBUG);
    g_app.bus_name = bus_name;
    g_message("voipd starting: user=%s server=%s port=%d (config %s)",
              g_app.cfg.user.c_str(), g_app.cfg.server.c_str(), g_app.cfg.sip_port,
              cfg_path.c_str());

    history_load(g_app);

    if (!core_init(g_app)) return 1;

    g_app.loop = g_main_loop_new(nullptr, FALSE);
    g_bus_own_name(G_BUS_TYPE_SESSION, bus_name.c_str(),
                   G_BUS_NAME_OWNER_FLAGS_NONE, on_bus_acquired, nullptr, on_name_lost,
                   nullptr, nullptr);

    g_timeout_add(20, tick, nullptr);

    g_message("voipd running");
    g_main_loop_run(g_app.loop);

    if (g_app.lc) {
        clear_chat(g_app);
        LinphoneCall *c = active_call();
        if (c) linphone_core_terminate_call(g_app.lc, c);
        for (int i = 0; i < 10; ++i) { linphone_core_iterate(g_app.lc); g_usleep(20000); }
        linphone_core_unref(g_app.lc);
    }
    g_main_loop_unref(g_app.loop);
    return 0;
}
