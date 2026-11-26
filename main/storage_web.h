#pragma once
#include "esp_http_server.h"
#include "cJSON.h"
#include "release_verify.h"
typedef int (*artifact_read_t)(void *context, void *buffer, size_t size);
esp_err_t storage_web_start(void);
esp_err_t storage_web_routes(httpd_handle_t server);
cJSON *storage_web_status(void);
bool storage_web_ready(void);
esp_err_t storage_web_stage(artifact_read_t reader, void *context, const release_artifact_t *artifact,
                            const char *envelope, size_t envelope_size, int *target);
esp_err_t storage_web_activate(int target, uint32_t sequence, bool pending);
esp_err_t storage_web_confirm(void);
esp_err_t storage_web_rollback(void);
void storage_web_expire(void);
/* A global write lock serializes USB, remote update, and task-related writes. */
bool storage_update_lock(void);
void storage_update_unlock(void);
