// 探针：打印 liblinphone 看到的音频设备 id / 名称 / 能力，用于确定设备选择策略
#include <linphone/core.h>
#include <cstdio>
#include <unistd.h>

int main() {
    linphone_factory_get();
    LinphoneCore *lc = linphone_factory_create_core_3(linphone_factory_get(), "/tmp/probe_dev.rc", nullptr, nullptr);
    linphone_core_start(lc);
    for (int i = 0; i < 20; i++) { linphone_core_iterate(lc); usleep(100000); }
    linphone_core_reload_sound_devices(lc);
    for (int i = 0; i < 10; i++) { linphone_core_iterate(lc); usleep(100000); }

    const bctbx_list_t *devs = linphone_core_get_audio_devices(lc);
    printf("=== audio devices (%d) ===\n", bctbx_list_size(devs));
    for (const bctbx_list_t *it = devs; it; it = it->next) {
        LinphoneAudioDevice *d = (LinphoneAudioDevice *)it->data;
        LinphoneAudioDeviceCapabilities cap = linphone_audio_device_get_capabilities(d);
        printf("id=%-70s | name=%-40s | caps=%s%s | type=%d\n",
               linphone_audio_device_get_id(d), linphone_audio_device_get_device_name(d),
               (cap & LinphoneAudioDeviceCapabilityRecord) ? "REC" : "",
               (cap & LinphoneAudioDeviceCapabilityPlay) ? "|PLAY" : "",
               (int)linphone_audio_device_get_type(d));
    }

    const LinphoneAudioDevice *in = linphone_core_get_input_audio_device(lc);
    const LinphoneAudioDevice *out = linphone_core_get_output_audio_device(lc);
    printf("=== current ===\n");
    printf("input : %s | %s\n", in ? linphone_audio_device_get_id(in) : "(null)",
           in ? linphone_audio_device_get_device_name(in) : "");
    printf("output: %s | %s\n", out ? linphone_audio_device_get_id(out) : "(null)",
           out ? linphone_audio_device_get_device_name(out) : "");

    linphone_core_unref(lc);
    return 0;
}
