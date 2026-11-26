#pragma once
#include <stdbool.h>
#include "esp_http_server.h"
#include "cJSON.h"
esp_err_t center_client_start(httpd_handle_t server);
bool center_client_busy(void);
cJSON *center_client_status(void);
