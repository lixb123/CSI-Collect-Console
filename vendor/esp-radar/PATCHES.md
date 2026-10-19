# Local esp-radar 0.3.4 patch

This directory is based on Espressif's `esp-radar` version 0.3.4. The original license is in `LICENSE`. ESP-IDF uses it through the receiver component's `override_path` declaration.

Relative to the registry release, the project changes `include/esp_radar.h` and `src/esp_radar.c` to carry the Wi-Fi driver's `rx_seq` into filtered CSI records, preserve ACK candidates when ACK capture is enabled, and reduce diagnostic log noise during radar reconfiguration. The other source files and build metadata follow the upstream version.
