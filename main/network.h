#pragma once
#include <stdbool.h>
#include "esp_http_server.h"
#include "cJSON.h"
esp_err_t battery_network_start(httpd_handle_t server);
bool battery_wifi_online(void);
cJSON *battery_network_status(void);
