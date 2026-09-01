idf_component_register(SRC_DIRS "."
                       INCLUDE_DIRS "."
                       REQUIRES research_protocol
                       PRIV_REQUIRES commands console esp_event esp_wifi
                                     espressif__esp-radar espressif__led_strip
                                     lwip mbedtls nvs_flash)
target_compile_options(${COMPONENT_LIB} PRIVATE "-Wno-format")
