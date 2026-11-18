// Minimal liblinphone PoC: two instances call each other over LAN (direct IP, no registrar)
// build: g++ -O2 poc_call.cpp -o poc_call $(pkg-config --cflags --libs linphone) -llinphone
#include <linphone/core.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <thread>

static std::atomic<bool> g_streams{false};
static std::atomic<bool> g_done{false};

static void dump_stats(LinphoneCore *lc) {
    LinphoneCall *call = linphone_core_get_current_call(lc);
    if (!call) return;
    const LinphoneCallStats *st = linphone_call_get_audio_stats(call);
    if (!st) return;
    const rtp_stats_t *rtp = linphone_call_stats_get_rtp_stats(st);
    printf("[STATS] up=%.1fkbit/s down=%.1fkbit/s loss_rx=%.1f%% jb=%.0fms rtt=%.1fms quality=%.1f "
           "rtp_pkt_rcv=%llu sent=%llu\n",
           linphone_call_stats_get_upload_bandwidth(st), linphone_call_stats_get_download_bandwidth(st),
           linphone_call_stats_get_receiver_loss_rate(st), linphone_call_stats_get_jitter_buffer_size_ms(st),
           linphone_call_stats_get_round_trip_delay(st), linphone_call_get_average_quality(call),
           (unsigned long long)(rtp ? rtp->packet_recv : 0), (unsigned long long)(rtp ? rtp->packet_sent : 0));
    fflush(stdout);
}

static void on_call_state(LinphoneCore *lc, LinphoneCall *call, LinphoneCallState state, const char *msg) {
    printf("[STATE] %s %s\n", linphone_call_state_to_string(state), msg ? msg : "");
    fflush(stdout);
    switch (state) {
        case LinphoneCallIncomingReceived:
            printf("[STATE] auto-answer\n");
            linphone_call_accept(call);
            break;
        case LinphoneCallStreamsRunning:
            g_streams = true;
            dump_stats(lc);
            break;
        case LinphoneCallEnd:
        case LinphoneCallError:
        case LinphoneCallReleased:
            g_done = true;
            break;
        default:
            break;
    }
}

int main(int argc, char **argv) {
    if (argc < 3) {
        printf("usage: %s <user> <port> [--call sip:u@h] [--register sip:h:port] [--secret pwd]\n"
               "          [--secs N] [--capture dev] [--playback dev]\n", argv[0]);
        return 1;
    }
    std::string user = argv[1], port = argv[2], target, capture_dev, playback_dev;
    int secs = 15;  // 纯语音：不启用视频
    std::string proxy, secret;  // proxy 形如 sip:10.66.84.129:5060
    for (int i = 3; i < argc; i++) {
        std::string a = argv[i];
        if (a == "--call" && i + 1 < argc) target = argv[++i];
        else if (a == "--secs" && i + 1 < argc) secs = atoi(argv[++i]);
        else if (a == "--capture" && i + 1 < argc) capture_dev = argv[++i];
        else if (a == "--playback" && i + 1 < argc) playback_dev = argv[++i];
        else if (a == "--register" && i + 1 < argc) proxy = argv[++i];
        else if (a == "--secret" && i + 1 < argc) secret = argv[++i];
    }
    std::string cfg = "/tmp/poc_" + user + ".rc";
    remove(cfg.c_str());

    linphone_factory_get();  // init factory
    LinphoneCoreCbs *cbs = linphone_factory_create_core_cbs(linphone_factory_get());
    linphone_core_cbs_set_call_state_changed(cbs, on_call_state);
    g_done = false;
    LinphoneCore *lc = linphone_factory_create_core_3(linphone_factory_get(), cfg.c_str(), nullptr, nullptr);
    linphone_core_add_callbacks(lc, cbs);
    linphone_core_cbs_unref(cbs);

    linphone_core_enable_video_capture(lc, FALSE);
    linphone_core_enable_video_display(lc, FALSE);
    LinphoneSipTransports tr = {0};
    tr.udp_port = (int)strtol(port.c_str(), nullptr, 10);
    tr.tcp_port = LC_SIP_TRANSPORT_DISABLED;
    tr.tls_port = LC_SIP_TRANSPORT_DISABLED;
    linphone_core_set_sip_transports(lc, &tr);
    linphone_core_set_primary_contact(lc, ("sip:" + user + "@127.0.0.1:" + port).c_str());
    LinphoneVideoPolicy vpol = {FALSE, FALSE};   // 语音专用
    linphone_core_set_video_policy(lc, &vpol);
    linphone_core_start(lc);  // required in 5.x: nothing works until the core is started

    const char **devs = linphone_core_get_sound_devices(lc);
    printf("[CORE] sound devices:");
    for (int i = 0; devs && devs[i]; i++) printf(" [%s]", devs[i]);
    printf("\n[CORE] media codecs:");
    for (const bctbx_list_t *it = linphone_core_get_audio_codecs(lc); it; it = it->next) {
        const PayloadType *pt = (const PayloadType *)it->data;
        printf(" %s/%d", pt->mime_type, pt->clock_rate);
    }
    printf("\n");
    fflush(stdout);

    if (!proxy.empty()) {
        // proxy 形如 sip:10.66.84.129:5060 -> 域名部分作为 SIP domain
        std::string dom = proxy.substr(4);                 // 10.66.84.129:5060
        std::string host = dom.substr(0, dom.find(':'));   // 10.66.84.129
        std::string aor = "sip:" + user + "@" + dom;       // 注册 AOR 带端口
        LinphoneAddress *server = linphone_address_new(proxy.c_str());
        LinphoneAddress *identity = linphone_address_new(aor.c_str());
        if (linphone_address_get_port(identity) == 0) linphone_address_set_port(identity, 5060);

        LinphoneAccountParams *ap = linphone_account_params_new(lc);
        linphone_account_params_set_server_address(ap, server);
        linphone_account_params_set_identity_address(ap, identity);
        linphone_account_params_enable_register(ap, TRUE);
        linphone_account_params_set_expires(ap, 300);
        if (!secret.empty()) {
            LinphoneAuthInfo *ai = linphone_auth_info_new(user.c_str(), nullptr, secret.c_str(),
                                                          nullptr, nullptr, nullptr);
            linphone_core_add_auth_info(lc, ai);
            linphone_auth_info_unref(ai);

        }
        LinphoneAccount *acc = linphone_core_create_account(lc, ap);
        linphone_core_add_account(lc, acc);
        linphone_core_set_default_account(lc, acc);
        linphone_account_params_unref(ap);
        linphone_address_unref(server);
        linphone_address_unref(identity);
        printf("[CORE] account %s -> proxy %s (host %s)\n", aor.c_str(), proxy.c_str(), host.c_str());
        fflush(stdout);
    }
    if (!capture_dev.empty()) {
        printf("[CORE] capture device -> %s\n", capture_dev.c_str());
        if (linphone_core_set_capture_device(lc, capture_dev.c_str()) != 0)
            printf("[CORE] failed to set capture device\n");
    }
    if (!playback_dev.empty()) {
        printf("[CORE] playback device -> %s\n", playback_dev.c_str());
        if (linphone_core_set_playback_device(lc, playback_dev.c_str()) != 0)
            printf("[CORE] failed to set playback device\n");
    }
    if (!target.empty()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(800));
        printf("[CORE] calling %s\n", target.c_str());
        fflush(stdout);
        LinphoneCall *c = linphone_core_invite(lc, target.c_str());
        if (!c) printf("[CORE] invite failed\n");
    }

    auto t0 = std::chrono::steady_clock::now();
    int last_dump = -1;
    while (!g_done) {
        linphone_core_iterate(lc);
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
        long el = (long)std::chrono::duration_cast<std::chrono::seconds>(
                      std::chrono::steady_clock::now() - t0).count();
        if (g_streams && el / 2 != last_dump) { last_dump = (int)(el / 2); dump_stats(lc); }
        if (el > secs) {
            LinphoneCall *c = linphone_core_get_current_call(lc);
            printf("[CORE] timeout, hanging up\n");
            if (c) linphone_core_terminate_call(lc, c);
            break;
        }
    }
    for (int i = 0; i < 40; i++) { linphone_core_iterate(lc); std::this_thread::sleep_for(std::chrono::milliseconds(20)); }
    linphone_core_unref(lc);
    printf("[CORE] exit\n");
    return 0;
}
