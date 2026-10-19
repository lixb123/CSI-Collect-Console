# esp-radar Component

[![Component Registry](https://components.espressif.com/components/espressif/esp-radar/badge.svg)](https://components.espressif.com/components/espressif/esp-radar)

- [User Guide](https://github.com/espressif/esp-csi/tree/master/README.md)
- [中文说明](./README_CN.md)

Wi-Fi CSI (Channel State Information) is the fine-grained channel information derived from Wi-Fi packets. `esp-radar` provides the core algorithms and examples for human presence and motion detection based on ESP-CSI data.

## Highlights in v0.3.3

- Added multi-peer CSI reception and dispatching. CSI packets can now be processed per source MAC address.
- Added peer-based APIs `esp_radar_new_peer()` and `esp_radar_del_peer()` for registering and unregistering CSI peers.
- Added the extended radar callback `wifi_radar_cb_ex_t`, which reports radar results together with the peer MAC address.
- Each peer keeps its own runtime state, including buffering and training/calibration data.
- Legacy single-peer behavior is preserved by default. If you do not register extra peers, `filter_mac` / `filter_dmac` and the legacy callback path continue to work as before.

## Requirements

- ESP-IDF `>= 5.4`
- Supported targets: `esp32`, `esp32s2`, `esp32s3`, `esp32c3`, `esp32c5`, `esp32c6`, `esp32c61`

## Add component to your project

Use the component manager command `add-dependency` to add `esp-radar` to your project. The component will be downloaded automatically during the `CMake` step.

```bash
idf.py add-dependency "espressif/esp-radar^0.3.2"
```

## Example

Use the component manager command `create-project-from-example` to create a project from the example template.

```bash
idf.py create-project-from-example "espressif/esp-radar=*:console_test"
```

Then the example will be downloaded to the current folder, and you can build and flash it directly.

You can also use the same command to download other examples, or browse them in the repository:

- [connect_rainmaker](https://github.com/espressif/esp-csi/tree/master/examples/esp-radar/connect_rainmaker): Add Wi-Fi CSI functionality to ESP RainMaker
- [console_test](https://github.com/espressif/esp-csi/tree/master/examples/esp-radar/console_test): A test platform for Wi-Fi CSI data display, acquisition, and analysis
- [wifi_sensing_demo](https://github.com/espressif/esp-csi/tree/master/examples/esp-radar/wifi_sensing_demo): A Wi-Fi sensing demo based on the `esp_wifi_sensing` component, with motion / presence detection, on-site training, LED feedback, and a browser-based Web Serial monitor

## Multi-peer CSI usage

`v0.3.3` introduces a multi-peer workflow for applications that need to receive CSI from more than one source MAC.

1. Initialize `esp-radar` as usual with `esp_radar_init()`.
2. Register `dec_config.wifi_radar_cb_ex` if you need to know which peer produced each radar result.
3. Create one peer handle for each source MAC by calling `esp_radar_new_peer()`.
4. Use `esp_radar_train_start_ex()` / `esp_radar_train_stop_ex()` when peer-specific calibration is required.
5. Stop radar before calling `esp_radar_del_peer()`.

```c
static void radar_cb_ex(void *ctx, const uint8_t mac[6], const wifi_radar_info_t *info)
{
    ESP_LOGI("esp-radar", MACSTR ", wander=%0.3f, jitter=%0.3f",
             MAC2STR(mac), info->waveform_wander, info->waveform_jitter);
}

void app_main(void)
{
    const uint8_t peer_mac[6] = {0x24, 0x6F, 0x28, 0x00, 0x00, 0x01};
    esp_radar_handle_t peer = NULL;
    esp_radar_config_t radar_config = ESP_RADAR_CONFIG_DEFAULT();

    radar_config.dec_config.wifi_radar_cb_ex = radar_cb_ex;

    ESP_ERROR_CHECK(esp_radar_init(&radar_config));
    ESP_ERROR_CHECK(esp_radar_new_peer(peer_mac, &peer));
    ESP_ERROR_CHECK(esp_radar_start());

    ESP_ERROR_CHECK(esp_radar_train_start_ex(peer));
    /* collect reference data here */
    ESP_ERROR_CHECK(esp_radar_train_stop_ex(peer, NULL, NULL));

    ESP_ERROR_CHECK(esp_radar_stop());
    ESP_ERROR_CHECK(esp_radar_del_peer(peer));
    ESP_ERROR_CHECK(esp_radar_deinit());
}
```

### Notes

- All peers share the same global radar configuration set by `esp_radar_init()`.
- Each peer maintains independent runtime state internally.
- You can register `FF:FF:FF:FF:FF:FF` as a wildcard peer. Exact-match peers take precedence over the wildcard peer.
- If you only use the legacy single-peer flow, the existing `wifi_radar_cb`, `filter_mac`, and `filter_dmac` behavior remains unchanged.

## Q&A

Q1. I encountered the following problem when using the package manager:

```text
  HINT: Please check manifest file of the following component(s): main

  ERROR: Because project depends on esp-radar (2.*) which doesn't match any
  versions, version solving failed.
```

A1. For examples downloaded with this command, comment out the `override_path` line in `main/idf_component.yml` of the example project.

Q2. I encountered the following problem when using the package manager:

```text
Executing action: create-project-from-example
CMakeLists.txt not found in project directory /home/username
```

A2. This is usually caused by an older package manager. Run `pip install -U idf-component-manager` in the ESP-IDF environment and try again.
