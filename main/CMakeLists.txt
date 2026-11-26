idf_component_register(
    SRCS "tusb_ncm_main.c" "app_http.c" "network.c" "release_verify.c"
         "storage_web.c" "update_service.c" "center_client.c"
    INCLUDE_DIRS "" "../updates"
    PRIV_REQUIRES esp_netif esp_event esp_http_server app_update nvs_flash mbedtls esp_timer
                  esp_wifi esp_http_client esp-tls json spiffs esp_partition esp_hw_support spi_flash
    EMBED_TXTFILES "recovery.html"
)
