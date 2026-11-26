#pragma once
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include "esp_err.h"
#define RELEASE_ENVELOPE_MAX 8192
#define WEB_PACKAGE_MAX (700 * 1024)
typedef struct {
    char version[32], sha256[65], url[512];
    uint32_t sequence, size;
} release_artifact_t;
/* ESP_ERR_NOT_FOUND means a valid signed manifest has no such artifact. */
esp_err_t release_verify(const char *envelope, size_t size, const char *kind, release_artifact_t *out);
bool release_url_allowed(const char *url);
bool release_set_source(const char *manifest_url);
