#pragma once
#include <stdbool.h>
#include "esp_http_server.h"
esp_err_t web_update_start(httpd_handle_t server);
esp_err_t battery_update_init_source(void);
bool battery_update_busy(void);
