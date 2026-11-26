#pragma once
#include <stdbool.h>
#include "esp_http_server.h"
#include "cJSON.h"
esp_err_t battery_relay_start(httpd_handle_t server);
bool battery_relay_busy(void);
cJSON *battery_relay_status(void);
