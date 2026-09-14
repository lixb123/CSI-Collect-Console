idf_component_register(SRC_DIRS "."
                       INCLUDE_DIRS "."
                       REQUIRES research_protocol
                       PRIV_REQUIRES commands console esp_event esp_wifi
                                     espressif__esp-radar espressif__led_strip
                                     esp_app_format lwip mbedtls nvs_flash)
target_compile_options(${COMPONENT_LIB} PRIVATE "-Wno-format")

if(NOT CONFIG_RESEARCH_RX_ESPNOW_MODE)
    target_compile_definitions(${COMPONENT_LIB} PRIVATE WIFI_CSI_SEND_NULL_DATA_ENABLE=1)
endif()
