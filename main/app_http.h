#pragma once
#include <stdbool.h>
#include "cJSON.h"
#include "esp_http_server.h"

esp_err_t app_json(httpd_req_t *req, cJSON *json);
esp_err_t app_error(httpd_req_t *req, const char *status, const char *code, const char *message);
char *app_body(httpd_req_t *req, size_t limit);
bool app_usb(httpd_req_t *req);
esp_err_t app_auth_start(httpd_handle_t server);
/* On success expected_sha is empty for a USB browser session, or the signed
 * request-body SHA for developer HMAC. Caller must verify the latter. */
bool app_authorize(httpd_req_t *req, char expected_sha[65]);
bool app_digest_matches(const char *expected_sha, const void *body, size_t size);
bool app_admin_json(httpd_req_t *req, cJSON **body);
bool app_session_read(httpd_req_t *req);
bool app_api_authorize(httpd_req_t *req, const char *scope, char expected_sha[65]);
void app_hex(const uint8_t *bytes, size_t size, char *hex);
bool app_equal(const void *left, const void *right, size_t size);
