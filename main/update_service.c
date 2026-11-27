#include "update_service.h"
#include "app_http.h"
#include "network.h"
#include "center_client.h"
#include "storage_web.h"
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>
#include <time.h>
#include "esp_app_desc.h"
#include "esp_crt_bundle.h"
#include "esp_flash.h"
#include "esp_heap_caps.h"
#include "esp_http_client.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mbedtls/sha256.h"
#include "nvs.h"

#define MANIFEST_URL "https://raw.githubusercontent.com/jimjury/Battery_System_A/main/updates/manifest.json"
typedef struct {esp_http_client_handle_t client; char location[2048]; size_t received;} http_stream_t;
typedef struct {uint32_t format, interval, auto_ui, auto_fw; char source[512];} update_settings_t;
typedef struct {httpd_req_t *req; mbedtls_sha256_context sha; size_t remaining;} usb_stream_t;
typedef struct {uint32_t size; uint8_t digest[32];} firmware_record_t;
static nvs_handle_t preferences;
static QueueHandle_t commands;
static SemaphoreHandle_t lock;
static char phase[32] = "idle", last_error[160], manifest[RELEASE_ENVELOPE_MAX+1], running_sha[65];
static char source[512] = MANIFEST_URL;
static release_artifact_t ui_release, firmware_release;
static bool ui_available, firmware_available, auto_ui = true, auto_firmware, busy, reboot_requested;
static uint32_t progress, total, interval_s = 900;
static int64_t last_check;
static size_t manifest_size;
static update_settings_t pending_settings;

esp_err_t battery_update_init_source(void)
{
    nvs_handle_t handle; esp_err_t err = nvs_open("updates", NVS_READONLY, &handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) return release_set_source(source) ? ESP_OK : ESP_FAIL;
    if (err) return err;
    update_settings_t value; size_t size = sizeof(value); err = nvs_get_blob(handle, "settings", &value, &size); nvs_close(handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) return release_set_source(source) ? ESP_OK : ESP_FAIL;
    if (err || size != sizeof(value) || value.format != 1 || value.source[511]) return ESP_FAIL;
    return release_set_source(value.source) ? ESP_OK : ESP_FAIL;
}

bool battery_update_busy(void)
{
    if (!lock) return true;
    xSemaphoreTake(lock, portMAX_DELAY); bool value = busy || reboot_requested; xSemaphoreGive(lock); return value;
}
static void update_state(const char *value, const char *error)
{
    xSemaphoreTake(lock, portMAX_DELAY);
    snprintf(phase, sizeof(phase), "%s", value); snprintf(last_error, sizeof(last_error), "%s", error ? error : "");
    xSemaphoreGive(lock);
}
static esp_err_t download_event(esp_http_client_event_t *event)
{
    http_stream_t *s = event->user_data;
    if (event->event_id == HTTP_EVENT_ON_HEADER && !strcasecmp(event->header_key, "Location")) {
        if (strlen(event->header_value) >= sizeof(s->location)) return ESP_FAIL;
        strcpy(s->location, event->header_value);
    }
    return ESP_OK;
}
static esp_err_t open_stream(const char *url, http_stream_t *stream)
{
    char current[2048]; if (!release_url_allowed(url)) return ESP_ERR_INVALID_ARG;
    strcpy(current, url); memset(stream, 0, sizeof(*stream));
    if (!battery_wifi_online() || time(NULL) < 1700000000) return ESP_ERR_INVALID_STATE;
    for (unsigned redirects = 0; redirects < 6; redirects++) {
        stream->location[0] = 0;
        esp_http_client_config_t config = {.url=current, .crt_bundle_attach=esp_crt_bundle_attach,
            .timeout_ms=12000, .buffer_size=4096, .buffer_size_tx=1024, .disable_auto_redirect=true,
            .event_handler=download_event, .user_data=stream};
        stream->client = esp_http_client_init(&config); if (!stream->client) return ESP_ERR_NO_MEM;
        esp_http_client_set_header(stream->client, "Accept-Encoding", "identity");
        esp_err_t err = esp_http_client_open(stream->client, 0);
        if (!err) esp_http_client_fetch_headers(stream->client);
        int status = esp_http_client_get_status_code(stream->client);
        if (!err && status == 200) return ESP_OK;
        char next[2048]; strcpy(next, stream->location);
        esp_http_client_cleanup(stream->client); stream->client = NULL;
        if (err || (status != 301 && status != 302 && status != 303 && status != 307 && status != 308) || !release_url_allowed(next)) return ESP_FAIL;
        strcpy(current, next);
    }
    return ESP_FAIL;
}
static void close_stream(http_stream_t *stream)
{
    if (stream->client) {esp_http_client_close(stream->client); esp_http_client_cleanup(stream->client); stream->client = NULL;}
}
static int download_read(void *context, void *buffer, size_t size)
{
    http_stream_t *stream = context;
    int n = esp_http_client_read(stream->client, buffer, size);
    if (n > 0) {
        stream->received += n;
        xSemaphoreTake(lock, portMAX_DELAY); progress = stream->received; xSemaphoreGive(lock);
    }
    return n;
}
static int usb_read(void *context, void *buffer, size_t size)
{
    usb_stream_t *stream = context; if (size > stream->remaining) size = stream->remaining;
    if (!size) return 0;
    int n; unsigned attempts = 0;
    do {n = httpd_req_recv(stream->req, buffer, size);} while (n == HTTPD_SOCK_ERR_TIMEOUT && ++attempts < 3);
    if (n > 0) {stream->remaining -= n; if (mbedtls_sha256_update(&stream->sha, buffer, n)) return -1;}
    return n;
}
static bool read_exact(artifact_read_t reader, void *context, void *buffer, size_t size)
{
    uint8_t *out = buffer;
    while (size) {int n = reader(context, out, size); if (n <= 0 || n > size) return false; out += n; size -= n;}
    return true;
}
static esp_err_t stage_firmware(artifact_read_t read, void *context, const release_artifact_t *artifact,
                                const esp_partition_t **target)
{
#ifndef CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE
    return ESP_ERR_NOT_SUPPORTED;
#endif
    uint32_t floor = 0; nvs_get_u32(preferences, "fw_floor", &floor);
    if (artifact->sequence <= floor) return ESP_ERR_INVALID_VERSION;
    const esp_partition_t *p = esp_ota_get_next_update_partition(NULL);
    if (!p || artifact->size > p->size) return ESP_ERR_INVALID_SIZE;
    unsigned char *buffer = malloc(4096); if (!buffer) return ESP_ERR_NO_MEM;
    esp_ota_handle_t ota; esp_err_t err = esp_ota_begin(p, artifact->size, &ota);
    if (err) {free(buffer); return err;}
    mbedtls_sha256_context sha; mbedtls_sha256_init(&sha); mbedtls_sha256_starts(&sha, 0);
    size_t left = artifact->size;
    while (!err && left) {
        size_t n = left < 4096 ? left : 4096;
        if (!read_exact(read, context, buffer, n)) {err = ESP_FAIL; break;}
        err = esp_ota_write(ota, buffer, n); if (!err && mbedtls_sha256_update(&sha, buffer, n)) err = ESP_FAIL;
        left -= n;
    }
    uint8_t digest[32]; char actual[65];
    if (!err) {if (mbedtls_sha256_finish(&sha, digest)) err = ESP_FAIL; app_hex(digest, 32, actual); if (strcmp(actual, artifact->sha256)) err = ESP_ERR_INVALID_CRC;}
    mbedtls_sha256_free(&sha); free(buffer);
    if (err) esp_ota_abort(ota); else err = esp_ota_end(ota);
    if (!err) *target = p;
    return err;
}
static esp_err_t select_firmware(const esp_partition_t *target, const release_artifact_t *artifact)
{
    firmware_record_t record = {.size=artifact->size};
    for (int i = 0; i < 32; i++) {unsigned value; if (sscanf(artifact->sha256+2*i, "%2x", &value) != 1) return ESP_ERR_INVALID_ARG; record.digest[i] = value;}
    esp_err_t err = nvs_set_blob(preferences, "firmware", &record, sizeof(record));
    if (!err) err = nvs_set_u32(preferences, "fw_floor", artifact->sequence);
    if (!err) err = nvs_commit(preferences);
    if (!err) err = esp_ota_set_boot_partition(target);
    if (!err) {xSemaphoreTake(lock, portMAX_DELAY); reboot_requested = true; xSemaphoreGive(lock);}
    return err;
}
static esp_err_t check_manifest(void)
{
    update_state("checking", NULL);
    http_stream_t stream; esp_err_t err = open_stream(source, &stream);
    if (err) {update_state("error", "ESP32 Wi-Fi, valid NTP time, and trusted HTTPS connectivity are required"); return err;}
    char *buffer = calloc(1, RELEASE_ENVELOPE_MAX+1);
    if (!buffer) {close_stream(&stream); return ESP_ERR_NO_MEM;}
    size_t size = 0; int n = 0;
    while (size < RELEASE_ENVELOPE_MAX && (n = download_read(&stream, buffer+size, RELEASE_ENVELOPE_MAX-size)) > 0) size += n;
    char extra; bool complete = n >= 0 && (size < RELEASE_ENVELOPE_MAX || download_read(&stream, &extra, 1) == 0);
    close_stream(&stream);
    release_artifact_t ui = {0}, fw = {0};
    esp_err_t u = release_verify(buffer, size, "ui", &ui), f = release_verify(buffer, size, "firmware", &fw);
    /* Backwards-compatible unsigned bootstrap manifest can only mean NO update. */
    bool empty_bootstrap = false; cJSON *j = cJSON_ParseWithLength(buffer, size);
    cJSON *schema = cJSON_GetObjectItemCaseSensitive(j, "schema_version"), *target = cJSON_GetObjectItemCaseSensitive(j, "target");
    empty_bootstrap = cJSON_IsNumber(schema) && schema->valuedouble == 1 && cJSON_IsString(target) && !strcmp(target->valuestring, "esp32s3") &&
        cJSON_IsNull(cJSON_GetObjectItemCaseSensitive(j, "ui")) && cJSON_IsNull(cJSON_GetObjectItemCaseSensitive(j, "firmware"));
    cJSON_Delete(j);
    bool valid = complete && size > 0 && (empty_bootstrap || ((u == ESP_OK || u == ESP_ERR_NOT_FOUND) && (f == ESP_OK || f == ESP_ERR_NOT_FOUND)));
    xSemaphoreTake(lock, portMAX_DELAY);
    last_check = time(NULL); ui_available = valid && u == ESP_OK && ui.url[0]; firmware_available = valid && f == ESP_OK && fw.url[0];
    if (valid) {memcpy(manifest, buffer, size); manifest[size] = 0; manifest_size = size; ui_release = ui; firmware_release = fw;}
    xSemaphoreGive(lock); free(buffer);
    update_state(valid ? (ui_available || firmware_available ? "available" : "no_update") : "error", valid ? NULL : "Manifest schema, target, or publisher signature is invalid");
    return valid ? ESP_OK : ESP_ERR_INVALID_CRC;
}
static esp_err_t install_remote(bool firmware)
{
    if (center_client_busy()) {update_state("deferred", "Tasks are active or C task state is unknown; installation is deferred"); return ESP_ERR_INVALID_STATE;}
    bool available = firmware ? firmware_available : ui_available;
    if (!available || !manifest_size) return ESP_ERR_NOT_FOUND;
    if (!storage_update_lock()) return ESP_ERR_INVALID_STATE;
    if (center_client_busy()) {storage_update_unlock();return ESP_ERR_INVALID_STATE;}
    xSemaphoreTake(lock, portMAX_DELAY); busy = true; progress = 0; total = firmware ? firmware_release.size : ui_release.size; xSemaphoreGive(lock);
    const release_artifact_t *artifact = firmware ? &firmware_release : &ui_release;
    update_state("downloading", NULL);
    http_stream_t stream; esp_err_t err = open_stream(artifact->url, &stream);
    int target = -1; const esp_partition_t *app_target = NULL;
    if (!err) {
        int64_t length = esp_http_client_get_content_length(stream.client);
        if (!esp_http_client_is_chunked_response(stream.client) && length >= 0 && length != artifact->size) err = ESP_ERR_INVALID_SIZE;
        if (!err) err = firmware ? stage_firmware(download_read, &stream, artifact, &app_target) :
            storage_web_stage(download_read, &stream, artifact, manifest, manifest_size, &target);
        char extra;
        if (!err && (stream.received != artifact->size || download_read(&stream, &extra, 1) != 0)) err = ESP_ERR_INVALID_SIZE;
        close_stream(&stream);
    }
    if (!err) {
        if (firmware) err = select_firmware(app_target, artifact);
        else {err = storage_web_activate(target, artifact->sequence, true); if (!err) err = storage_web_confirm();}
    }
    update_state(err ? "error" : firmware ? "rebooting" : "installed", err ? esp_err_to_name(err) : NULL);
    xSemaphoreTake(lock, portMAX_DELAY); busy = false; xSemaphoreGive(lock); storage_update_unlock(); return err;
}
static esp_err_t upload(httpd_req_t *r)
{
    char expected[65];
    if (!app_authorize(r, expected)) return app_error(r, "401 Unauthorized", "authentication_required", "USB administrator authorization required");
    if (center_client_busy() || !storage_update_lock()) return app_error(r, "409 Conflict", "device_busy", "Stop active tasks, synchronize C, or wait for the current update");
    if (center_client_busy()) {storage_update_unlock();return app_error(r,"409 Conflict","device_busy","A C operation started; retry after it completes");}
    bool firmware = !strcmp(r->uri, "/api/firmware");
    if (r->content_len < 4 || r->content_len > (firmware ? 4*1024*1024 : WEB_PACKAGE_MAX)+RELEASE_ENVELOPE_MAX+4) {
        storage_update_unlock(); return app_error(r, "413 Content Too Large", "image_size", "Invalid signed upload size");
    }
    xSemaphoreTake(lock, portMAX_DELAY); busy = true; xSemaphoreGive(lock);
    usb_stream_t stream = {.req=r, .remaining=r->content_len}; mbedtls_sha256_init(&stream.sha); mbedtls_sha256_starts(&stream.sha, 0);
    uint32_t envelope_size = 0; char *envelope = NULL; release_artifact_t artifact;
    esp_err_t err = ESP_FAIL; int target = -1; const esp_partition_t *app_target = NULL;
    if (!read_exact(usb_read, &stream, &envelope_size, sizeof(envelope_size)) || !envelope_size || envelope_size > RELEASE_ENVELOPE_MAX || envelope_size > stream.remaining) goto done;
    envelope = calloc(1, envelope_size+1); if (!envelope) {err = ESP_ERR_NO_MEM; goto done;}
    if (!read_exact(usb_read, &stream, envelope, envelope_size)) goto done;
    err = release_verify(envelope, envelope_size, firmware ? "firmware" : "ui", &artifact);
    if (err) goto done;
    if (stream.remaining != artifact.size) {err = ESP_ERR_INVALID_SIZE; goto done;}
    err = firmware ? stage_firmware(usb_read, &stream, &artifact, &app_target) :
        storage_web_stage(usb_read, &stream, &artifact, envelope, envelope_size, &target);
    uint8_t digest[32]; char actual[65];
    if (!err) {
        if (stream.remaining || mbedtls_sha256_finish(&stream.sha, digest)) err = ESP_FAIL;
        else {app_hex(digest, 32, actual); if (expected[0] && strcmp(actual, expected)) err = ESP_ERR_INVALID_CRC;}
    }
    if (!err) err = firmware ? select_firmware(app_target, &artifact) : storage_web_activate(target, artifact.sequence, true);
done:
    free(envelope); mbedtls_sha256_free(&stream.sha);
    xSemaphoreTake(lock, portMAX_DELAY); busy = false; xSemaphoreGive(lock); storage_update_unlock();
    if (err) {
        httpd_resp_set_hdr(r, "Connection", "close");
        app_error(r, "400 Bad Request", "update_rejected", esp_err_to_name(err)); return ESP_FAIL;
    }
    cJSON *j = cJSON_CreateObject(); cJSON_AddBoolToObject(j, firmware ? "reboot_required" : "pending", true); return app_json(r, j);
}
static esp_err_t web_control(httpd_req_t *r)
{
    cJSON *body; if (!app_admin_json(r, &body)) return ESP_OK; cJSON_Delete(body);
    if (!storage_update_lock()) return app_error(r, "409 Conflict", "update_busy", "Another update is active");
    esp_err_t err = strstr(r->uri, "/rollback") ? storage_web_rollback() : storage_web_confirm();
    storage_update_unlock();
    if (err) return app_error(r, "409 Conflict", "web_state", esp_err_to_name(err));
    return app_json(r, storage_web_status());
}
static bool firmware_pending(void)
{
    esp_ota_img_states_t state;
    return esp_ota_get_state_partition(esp_ota_get_running_partition(), &state) == ESP_OK && state == ESP_OTA_IMG_PENDING_VERIFY;
}
static esp_err_t device_status(httpd_req_t *r)
{
    if(!app_usb(r)&&!(app_mobile(r)&&app_session_read(r)))return app_error(r,"401 Unauthorized","local_session_required","Login through the local USB or phone interface");
    cJSON *j = storage_web_status();
    cJSON_AddStringToObject(j, "device_id", "A01"); cJSON_AddStringToObject(j, "usb_ip", "192.168.7.1");
    cJSON_AddStringToObject(j, "firmware", esp_app_get_description()->version); cJSON_AddStringToObject(j, "firmware_sha256", running_sha);
    cJSON_AddBoolToObject(j, "firmware_pending", firmware_pending()); cJSON_AddNumberToObject(j, "uptime_s", esp_timer_get_time()/1000000);
    uint32_t fw_floor = 0; nvs_get_u32(preferences, "fw_floor", &fw_floor); cJSON_AddNumberToObject(j, "firmware_sequence_floor", fw_floor);
    cJSON_AddNumberToObject(j, "free_heap", esp_get_free_heap_size()); cJSON_AddNumberToObject(j, "minimum_heap", esp_get_minimum_free_heap_size());
    cJSON_AddNumberToObject(j, "psram_total", heap_caps_get_total_size(MALLOC_CAP_SPIRAM));
    cJSON_AddNumberToObject(j, "psram_free", heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
    uint32_t flash = 0; esp_flash_get_size(NULL, &flash); cJSON_AddNumberToObject(j, "flash_bytes", flash);
    cJSON_AddBoolToObject(j, "wifi_online", battery_wifi_online()); cJSON_AddItemToObject(j, "center", center_client_status());
    return app_json(r, j);
}
static esp_err_t update_status(httpd_req_t *r)
{
    if (!app_usb(r)) return app_error(r, "403 Forbidden", "usb_required", "Direct USB management only");
    cJSON *j = storage_web_status(); xSemaphoreTake(lock, portMAX_DELAY);
    cJSON_AddStringToObject(j, "phase", phase); cJSON_AddStringToObject(j, "error", last_error);
    cJSON_AddStringToObject(j, "source", source); cJSON_AddNumberToObject(j, "last_check", last_check);
    cJSON_AddNumberToObject(j, "download_bytes", progress); cJSON_AddNumberToObject(j, "total_bytes", total);
    cJSON_AddBoolToObject(j, "auto_ui", auto_ui); cJSON_AddBoolToObject(j, "auto_firmware", auto_firmware);
    cJSON_AddNumberToObject(j, "interval_s", interval_s);
    cJSON_AddStringToObject(j, "latest_ui", ui_available ? ui_release.version : "");
    cJSON_AddStringToObject(j, "latest_firmware", firmware_available ? firmware_release.version : "");
    xSemaphoreGive(lock); return app_json(r, j);
}
static esp_err_t update_command(httpd_req_t *r)
{
    cJSON *j; if (!app_admin_json(r, &j)) return ESP_OK;
    int command = 1;
    if (strstr(r->uri, "/install")) {
        cJSON *kind = cJSON_GetObjectItemCaseSensitive(j, "kind");
        if (!cJSON_IsString(kind) || (strcmp(kind->valuestring, "ui") && strcmp(kind->valuestring, "firmware"))) {
            cJSON_Delete(j); return app_error(r, "422 Unprocessable Entity", "kind_required", "Choose ui or firmware");
        }
        command = !strcmp(kind->valuestring, "firmware") ? 3 : 2;
    }
    cJSON_Delete(j);
    if (xQueueSend(commands, &command, 0) != pdTRUE) return app_error(r, "409 Conflict", "update_busy", "Another update request is queued");
    httpd_resp_set_status(r, "202 Accepted"); return update_status(r);
}
static esp_err_t firmware_confirm(httpd_req_t *r)
{
    cJSON *j; if (!app_admin_json(r, &j)) return ESP_OK; cJSON_Delete(j);
    /* Local self-test confirms OTA automatically; remote connectivity is irrelevant. */
    return device_status(r);
}
static esp_err_t save_settings(httpd_req_t *r)
{
    cJSON *j; if (!app_admin_json(r, &j)) return ESP_OK;
    cJSON *url = cJSON_GetObjectItemCaseSensitive(j, "source"), *period = cJSON_GetObjectItemCaseSensitive(j, "interval_s"),
          *ui = cJSON_GetObjectItemCaseSensitive(j, "auto_ui"), *fw = cJSON_GetObjectItemCaseSensitive(j, "auto_firmware");
    if (!cJSON_IsString(url) || strncmp(url->valuestring, "https://", 8) || strlen(url->valuestring) >= 512 ||
        strpbrk(url->valuestring, "@#?\\ \r\n\t") || !strchr(url->valuestring+8, '/') ||
        !cJSON_IsNumber(period) || period->valuedouble < 60 || period->valuedouble > 86400 ||
        period->valuedouble != period->valueint || !cJSON_IsBool(ui) || !cJSON_IsBool(fw)) {
        cJSON_Delete(j); return app_error(r, "422 Unprocessable Entity", "invalid_update_settings", "Use a credential-free HTTPS manifest URL, 60–86400 seconds, and explicit boolean policies");
    }
    xSemaphoreTake(lock, portMAX_DELAY);
    if (pending_settings.format) {xSemaphoreGive(lock); cJSON_Delete(j); return app_error(r, "409 Conflict", "settings_busy", "A settings write is already queued");}
    pending_settings = (update_settings_t){.format=1, .interval=period->valueint, .auto_ui=cJSON_IsTrue(ui), .auto_fw=cJSON_IsTrue(fw)};
    strcpy(pending_settings.source, url->valuestring); xSemaphoreGive(lock); cJSON_Delete(j);
    int command = 4;
    if (xQueueSend(commands, &command, 0) != pdTRUE) {
        xSemaphoreTake(lock, portMAX_DELAY); pending_settings.format = 0; xSemaphoreGive(lock);
        return app_error(r, "409 Conflict", "update_busy", "Retry after the update queue drains");
    }
    httpd_resp_set_status(r, "202 Accepted"); return update_status(r);
}
static void apply_settings(void)
{
    xSemaphoreTake(lock, portMAX_DELAY); update_settings_t value = pending_settings; xSemaphoreGive(lock);
    esp_err_t err = release_set_source(value.source) ? nvs_set_blob(preferences, "settings", &value, sizeof(value)) : ESP_ERR_INVALID_ARG;
    if (!err) err = nvs_commit(preferences);
    xSemaphoreTake(lock, portMAX_DELAY);
    if (!err) {strcpy(source, value.source); interval_s = value.interval; auto_ui = value.auto_ui; auto_firmware = value.auto_fw;}
    else release_set_source(source);
    pending_settings.format = 0; xSemaphoreGive(lock);
    update_state(err ? "error" : "settings_saved", err ? "Cannot persist update settings; prior source retained" : NULL);
}
static void load_running_hash(void)
{
    firmware_record_t record; size_t size = sizeof(record); const esp_partition_t *p = esp_ota_get_running_partition();
    if (nvs_get_blob(preferences, "firmware", &record, &size) != ESP_OK || size != sizeof(record) || !record.size || record.size > p->size) return;
    unsigned char *buffer = malloc(4096); if (!buffer) return;
    mbedtls_sha256_context sha; mbedtls_sha256_init(&sha); mbedtls_sha256_starts(&sha, 0);
    size_t offset = 0; bool ok = true;
    while (ok && offset < record.size) {size_t n = record.size-offset; if (n > 4096) n = 4096; ok = esp_partition_read(p, offset, buffer, n) == ESP_OK && !mbedtls_sha256_update(&sha, buffer, n); offset += n;}
    uint8_t digest[32]; if (ok && !mbedtls_sha256_finish(&sha, digest)) app_hex(digest, 32, running_sha);
    mbedtls_sha256_free(&sha); free(buffer);
}
static bool local_health(void)
{
    esp_http_client_config_t config = {.url="http://127.0.0.1/recovery", .timeout_ms=3000};
    esp_http_client_handle_t client = esp_http_client_init(&config); if (!client) return false;
    bool ok = esp_http_client_open(client, 0) == ESP_OK;
    if (ok) {esp_http_client_fetch_headers(client); char prefix[16]; ok = esp_http_client_get_status_code(client) == 200 && esp_http_client_read(client, prefix, sizeof(prefix)) > 0;}
    esp_http_client_cleanup(client); return ok;
}
static void worker(void *arg)
{
    int64_t next_check = esp_timer_get_time()+30000000; unsigned failures = 0; bool boot_checked = false;
    for (;;) {
        if (!boot_checked && local_health()) {
            if (!firmware_pending() || esp_ota_mark_app_valid_cancel_rollback() == ESP_OK) boot_checked = true;
        }
        if (!boot_checked && esp_timer_get_time() > 90000000 && firmware_pending()) esp_ota_mark_app_invalid_rollback_and_reboot();
        if (reboot_requested) {vTaskDelay(pdMS_TO_TICKS(1500)); esp_restart();}
        storage_web_expire();
        int command = 0; xQueueReceive(commands, &command, pdMS_TO_TICKS(1000));
        if (command == 4) {apply_settings(); next_check = esp_timer_get_time()+1000000;}
        else if (command == 1 || (!command && battery_wifi_online() && esp_timer_get_time() >= next_check)) {
            esp_err_t err = check_manifest(); failures = err ? (failures < 6 ? failures+1 : 6) : 0;
            next_check = esp_timer_get_time() + (int64_t)(failures ? (30U << failures) : interval_s)*1000000;
            if (!err && !center_client_busy()) {
                cJSON *status = storage_web_status(); cJSON *floor = cJSON_GetObjectItemCaseSensitive(status, "sequence_floor");
                if (auto_ui && ui_available && ui_release.sequence > floor->valuedouble) install_remote(false);
                cJSON_Delete(status);
                uint32_t fw_floor = 0; nvs_get_u32(preferences, "fw_floor", &fw_floor);
                if (auto_firmware && firmware_available && firmware_release.sequence > fw_floor) install_remote(true);
            }
        } else if (command == 2 || command == 3) {
            if (check_manifest() == ESP_OK) install_remote(command == 3);
        }
    }
}
esp_err_t web_update_start(httpd_handle_t server)
{
    lock = xSemaphoreCreateMutex(); commands = xQueueCreate(1, sizeof(int)); if (!lock || !commands) return ESP_ERR_NO_MEM;
    esp_err_t err = nvs_open("updates", NVS_READWRITE, &preferences); if (err) return err;
    update_settings_t saved; size_t saved_size = sizeof(saved);
    err = nvs_get_blob(preferences, "settings", &saved, &saved_size);
    if (err == ESP_OK) {
        if (saved_size != sizeof(saved) || saved.format != 1 || saved.source[511] || saved.interval < 60 || saved.interval > 86400 || !release_set_source(saved.source)) return ESP_FAIL;
        strcpy(source, saved.source); interval_s = saved.interval; auto_ui = saved.auto_ui; auto_firmware = saved.auto_fw;
    } else if (err != ESP_ERR_NVS_NOT_FOUND) return err;
    else release_set_source(source);
    load_running_hash();
    const httpd_uri_t routes[] = {
        {.uri="/api/status", .method=HTTP_GET, .handler=device_status},
        {.uri="/api/device/status", .method=HTTP_GET, .handler=device_status},
        {.uri="/api/update/status", .method=HTTP_GET, .handler=update_status},
        {.uri="/api/update/check", .method=HTTP_POST, .handler=update_command},
        {.uri="/api/update/install", .method=HTTP_POST, .handler=update_command},
        {.uri="/api/update/settings", .method=HTTP_POST, .handler=save_settings},
        {.uri="/api/web", .method=HTTP_POST, .handler=upload},
        {.uri="/api/firmware", .method=HTTP_POST, .handler=upload},
        {.uri="/api/web/confirm", .method=HTTP_POST, .handler=web_control},
        {.uri="/api/web/rollback", .method=HTTP_POST, .handler=web_control},
        {.uri="/api/firmware/confirm", .method=HTTP_POST, .handler=firmware_confirm},
        {.uri="/api/v1/device/status", .method=HTTP_GET, .handler=device_status},
        {.uri="/api/v1/update/status", .method=HTTP_GET, .handler=update_status},
        {.uri="/api/v1/update/check", .method=HTTP_POST, .handler=update_command},
        {.uri="/api/v1/update/install", .method=HTTP_POST, .handler=update_command},
        {.uri="/api/v1/update/rollback", .method=HTTP_POST, .handler=web_control},
        {.uri="/api/v1/update/settings", .method=HTTP_POST, .handler=save_settings},
    };
    for (unsigned i = 0; i < sizeof(routes)/sizeof(routes[0]); i++) if ((err = httpd_register_uri_handler(server, &routes[i]))) return err;
    return xTaskCreate(worker, "signed_update", 16384, NULL, 4, NULL) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}
