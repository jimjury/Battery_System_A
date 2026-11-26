#include "storage_web.h"
#include "app_http.h"
#include <ctype.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_spiffs.h"
#include "esp_partition.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "mbedtls/sha256.h"
#include "nvs.h"

#define FILES_MAX 32
#define IO_SIZE 4096
typedef struct __attribute__((packed)) { char magic[8]; uint32_t count, body_size; uint8_t digest[32]; } package_header_t;
typedef struct __attribute__((packed)) { char path[96]; uint32_t offset, size; uint8_t digest[32]; } file_entry_t;
typedef struct { uint32_t format; int32_t active, previous; uint32_t pending, generation, floor; } web_state_t;
typedef struct { bool mounted, valid; char asset_set[65]; release_artifact_t release; package_header_t header; file_entry_t files[FILES_MAX]; } slot_t;
_Static_assert(sizeof(package_header_t) == 48 && sizeof(file_entry_t) == 136, "Package ABI");
static const char *labels[] = {"web_a", "web_b"};
static const char *bases[] = {"/web_a", "/web_b"};
static slot_t slots[2];
static web_state_t state = {.format=2, .active=-1, .previous=-1};
static int64_t pending_until;
static nvs_handle_t storage;
static SemaphoreHandle_t state_lock, update_lock, slot_locks[2];
extern const unsigned char recovery_start[] asm("_binary_recovery_html_start");
extern const unsigned char recovery_end[] asm("_binary_recovery_html_end");

bool storage_update_lock(void) {return xSemaphoreTake(update_lock, 0) == pdTRUE;}
void storage_update_unlock(void) {xSemaphoreGive(update_lock);}
static web_state_t snapshot(void)
{
    xSemaphoreTake(state_lock, portMAX_DELAY); web_state_t copy = state; xSemaphoreGive(state_lock); return copy;
}
static esp_err_t save(web_state_t next)
{
    xSemaphoreTake(state_lock, portMAX_DELAY);
    esp_err_t err = nvs_set_blob(storage, "state", &next, sizeof(next));
    if (!err) err = nvs_commit(storage);
    if (!err) state = next;
    xSemaphoreGive(state_lock); return err;
}
static bool valid_path(const char *path)
{
    size_t n = strnlen(path, 96);
    if (n < 2 || n >= 96 || path[0] != '/' || strstr(path, "..") || strstr(path, "//") || strstr(path, "/.") || !strncmp(path, "/api/", 5)) return false;
    for (size_t i = 1; i < n; i++) if (!(isalnum((unsigned char)path[i]) || strchr("/._-", path[i]))) return false;
    return true;
}
static bool valid_table(const package_header_t *h, const file_entry_t *files)
{
    if (memcmp(h->magic, "BSAWEB1", 8) || !h->count || h->count > FILES_MAX || h->body_size < h->count*sizeof(*files) ||
        h->body_size > WEB_PACKAGE_MAX - sizeof(*h)) return false;
    uint32_t end = h->count*sizeof(*files); bool index = false;
    for (unsigned i = 0; i < h->count; i++) {
        if (!valid_path(files[i].path) || files[i].offset != end || files[i].size > h->body_size-end) return false;
        for (unsigned j = 0; j < i; j++) if (!strcmp(files[j].path, files[i].path)) return false;
        if (!strcmp(files[i].path, "/index.html")) index = true;
        end += files[i].size;
    }
    return index && end == h->body_size;
}
static FILE *open_file(int slot, const char *name, const char *mode)
{
    char full[112]; int n = snprintf(full, sizeof(full), "%s%s", bases[slot], name);
    return n > 0 && n < sizeof(full) ? fopen(full, mode) : NULL;
}
static bool write_file(int slot, const char *name, const void *data, size_t size)
{
    FILE *f = open_file(slot, name, "wb"); if (!f) return false;
    bool ok = fwrite(data, 1, size, f) == size; if (fclose(f)) ok = false; return ok;
}
static esp_err_t mount_slot(int slot, bool allow_format)
{
    if (slots[slot].mounted) return ESP_OK;
    esp_vfs_spiffs_conf_t config = {.base_path=bases[slot], .partition_label=labels[slot], .max_files=5, .format_if_mount_failed=allow_format};
    esp_err_t err = esp_vfs_spiffs_register(&config);
    if (!err) slots[slot].mounted = true;
    return err;
}
static bool verify_files(int slot, slot_t *info)
{
    unsigned char *buffer = malloc(IO_SIZE); if (!buffer) return false;
    mbedtls_sha256_context whole, file; mbedtls_sha256_init(&whole); mbedtls_sha256_init(&file);
    bool ok = !mbedtls_sha256_starts(&whole, 0);
    ok = ok && !mbedtls_sha256_update(&whole, (uint8_t *)&info->header, sizeof(info->header));
    ok = ok && !mbedtls_sha256_update(&whole, (uint8_t *)info->files, info->header.count*sizeof(file_entry_t));
    for (unsigned i = 0; ok && i < info->header.count; i++) {
        FILE *f = open_file(slot, info->files[i].path, "rb");
        if (!f) {ok = false; break;}
        ok = !mbedtls_sha256_starts(&file, 0); size_t left = info->files[i].size;
        while (ok && left) {
            size_t n = left < IO_SIZE ? left : IO_SIZE;
            ok = fread(buffer, 1, n, f) == n && !mbedtls_sha256_update(&whole, buffer, n) && !mbedtls_sha256_update(&file, buffer, n);
            left -= n;
        }
        uint8_t digest[32];
        ok = ok && fgetc(f) == EOF && !ferror(f) && !mbedtls_sha256_finish(&file, digest) && !memcmp(digest, info->files[i].digest, 32);
        fclose(f);
    }
    uint8_t digest[32]; char actual[65];
    if (ok) {ok = !mbedtls_sha256_finish(&whole, digest); app_hex(digest, 32, actual); ok = ok && !strcmp(actual, info->release.sha256);}
    mbedtls_sha256_free(&file); mbedtls_sha256_free(&whole); free(buffer); return ok;
}
static bool load_slot(int slot)
{
    if (!slots[slot].mounted) return false;
    slot_t *info = &slots[slot]; FILE *f = open_file(slot, "/.table", "rb"); if (!f) return false;
    bool ok = fread(&info->header, 1, sizeof(info->header), f) == sizeof(info->header) && info->header.count <= FILES_MAX && info->header.count > 0;
    if (ok) ok = fread(info->files, sizeof(file_entry_t), info->header.count, f) == info->header.count && fgetc(f) == EOF;
    fclose(f); if (!ok || !valid_table(&info->header, info->files)) return false;
    char *envelope = calloc(1, RELEASE_ENVELOPE_MAX+1); if (!envelope) return false;
    f = open_file(slot, "/.signed", "rb"); if (!f) {free(envelope); return false;}
    size_t size = fread(envelope, 1, RELEASE_ENVELOPE_MAX, f); ok = fgetc(f) == EOF && !ferror(f); fclose(f);
    ok = ok && release_verify(envelope, size, "ui", &info->release) == ESP_OK &&
        info->release.size == sizeof(info->header)+info->header.body_size;
    free(envelope);
    if (!ok || !verify_files(slot, info)) return false;
    f = open_file(slot, "/asset-set.json", "rb"); if (!f) return false;
    char token[128] = {0}; size_t got = fread(token, 1, sizeof(token)-1, f); fclose(f);
    cJSON *asset = cJSON_ParseWithLength(token, got), *value = cJSON_GetObjectItemCaseSensitive(asset, "asset_set");
    ok = cJSON_IsString(value) && strlen(value->valuestring) == 64;
    if (ok) strcpy(info->asset_set, value->valuestring);
    cJSON_Delete(asset); return ok;
}
static bool read_exact(artifact_read_t read, void *context, void *out, size_t size)
{
    uint8_t *p = out;
    while (size) {int n = read(context, p, size); if (n <= 0 || n > size) return false; p += n; size -= n;}
    return true;
}
bool storage_web_ready(void)
{
    return esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_SPIFFS, "web_a") &&
           esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_SPIFFS, "web_b");
}
esp_err_t storage_web_stage(artifact_read_t reader, void *context, const release_artifact_t *artifact,
                            const char *envelope, size_t envelope_size, int *target)
{
    web_state_t before = snapshot();
    if (before.pending || artifact->sequence <= before.floor) return ESP_ERR_INVALID_VERSION;
    if (!storage_web_ready()) return ESP_ERR_NOT_FOUND;
    int slot = before.active == 0 ? 1 : 0;
    if (xSemaphoreTake(slot_locks[slot], 0) != pdTRUE) return ESP_ERR_INVALID_STATE;
    esp_err_t err = ESP_FAIL;
    slot_t *info = calloc(1, sizeof(*info)); unsigned char *buffer = malloc(IO_SIZE);
    if (!info || !buffer) {err = ESP_ERR_NO_MEM; goto done;}
    info->release = *artifact;
    if (!read_exact(reader, context, &info->header, sizeof(info->header)) || info->header.count > FILES_MAX || !info->header.count ||
        sizeof(info->header)+info->header.body_size != artifact->size ||
        !read_exact(reader, context, info->files, info->header.count*sizeof(file_entry_t)) || !valid_table(&info->header, info->files)) goto done;
    if (mount_slot(slot, true) != ESP_OK) goto done;
    xSemaphoreTake(state_lock, portMAX_DELAY); slots[slot].valid = false; xSemaphoreGive(state_lock);
    if (esp_spiffs_format(labels[slot]) != ESP_OK) goto done;
    for (unsigned i = 0; i < info->header.count; i++) {
        FILE *f = open_file(slot, info->files[i].path, "wb"); if (!f) goto done;
        size_t left = info->files[i].size; bool ok = true;
        while (ok && left) {
            size_t n = left < IO_SIZE ? left : IO_SIZE;
            ok = read_exact(reader, context, buffer, n) && fwrite(buffer, 1, n, f) == n; left -= n;
        }
        if (fclose(f)) ok = false;
        if (!ok) goto done;
    }
    if (!verify_files(slot, info)) {err = ESP_ERR_INVALID_CRC; goto done;}
    size_t table_size = sizeof(info->header)+info->header.count*sizeof(file_entry_t);
    unsigned char *table = malloc(table_size); if (!table) {err = ESP_ERR_NO_MEM; goto done;}
    memcpy(table, &info->header, sizeof(info->header)); memcpy(table+sizeof(info->header), info->files, info->header.count*sizeof(file_entry_t));
    bool written = write_file(slot, "/.table", table, table_size) && write_file(slot, "/.signed", envelope, envelope_size); free(table);
    if (!written) goto done;
    xSemaphoreTake(state_lock, portMAX_DELAY);
    info->mounted = true; info->valid = true; slots[slot] = *info;
    xSemaphoreGive(state_lock);
    slots[slot].valid = load_slot(slot);
    if (!slots[slot].valid) {err = ESP_ERR_INVALID_CRC; goto done;}
    *target = slot; err = ESP_OK;
done:
    free(buffer); free(info); xSemaphoreGive(slot_locks[slot]); return err;
}
esp_err_t storage_web_activate(int target, uint32_t sequence, bool pending)
{
    web_state_t next = snapshot();
    if (target < 0 || target > 1 || !slots[target].valid || next.pending || sequence <= next.floor) return ESP_ERR_INVALID_STATE;
    next.previous = next.active; next.active = target; next.pending = pending; next.generation++; next.floor = sequence;
    pending_until = esp_timer_get_time() + 120000000;
    return save(next);
}
esp_err_t storage_web_confirm(void)
{
    web_state_t next = snapshot();
    if (next.active < 0 || !slots[next.active].valid) return ESP_ERR_INVALID_STATE;
    next.pending = 0; return save(next);
}
esp_err_t storage_web_rollback(void)
{
    web_state_t next = snapshot();
    if ((next.previous < 0 || !slots[next.previous].valid) && !(next.pending && next.previous == -1)) return ESP_ERR_NOT_FOUND;
    int old = next.active; next.active = next.previous; next.previous = old; next.pending = 0; next.generation++;
    return save(next);
}
void storage_web_expire(void)
{
    if (!storage_update_lock()) return;
    web_state_t next = snapshot();
    if (next.pending && esp_timer_get_time() >= pending_until) storage_web_rollback();
    storage_update_unlock();
}
cJSON *storage_web_status(void)
{
    cJSON *j = cJSON_CreateObject(); xSemaphoreTake(state_lock, portMAX_DELAY);
    bool active = state.active >= 0 && slots[state.active].valid;
    bool previous = state.previous >= 0 && slots[state.previous].valid;
    cJSON_AddNumberToObject(j, "protocol", 1); cJSON_AddBoolToObject(j, "storage_ready", storage_web_ready());
    cJSON_AddBoolToObject(j, "web_ready", active); cJSON_AddBoolToObject(j, "pending", state.pending);
    cJSON_AddNumberToObject(j, "generation", state.generation); cJSON_AddNumberToObject(j, "sequence_floor", state.floor);
    cJSON_AddStringToObject(j, "version", active ? slots[state.active].release.sha256 : "");
    cJSON_AddStringToObject(j, "ui_version", active ? slots[state.active].release.version : "recovery");
    cJSON_AddStringToObject(j, "previous", previous ? slots[state.previous].release.sha256 : "");
    xSemaphoreGive(state_lock); return j;
}
static esp_err_t recovery(httpd_req_t *r)
{
    httpd_resp_set_type(r, "text/html; charset=utf-8"); httpd_resp_set_hdr(r, "Cache-Control", "no-store");
    return httpd_resp_send(r, (char *)recovery_start, recovery_end-recovery_start-1);
}
static const char *mime(const char *name)
{
    const char *s = strrchr(name, '.'); if (!s) return "application/octet-stream";
    if (!strcmp(s, ".html")) return "text/html; charset=utf-8";
    if (!strcmp(s, ".css")) return "text/css; charset=utf-8";
    if (!strcmp(s, ".js")) return "text/javascript; charset=utf-8";
    if (!strcmp(s, ".json")) return "application/json";
    if (!strcmp(s, ".svg")) return "image/svg+xml";
    if (!strcmp(s, ".png")) return "image/png";
    if (!strcmp(s, ".ico")) return "image/x-icon";
    if (!strcmp(s, ".woff2")) return "font/woff2";
    return "application/octet-stream";
}
static esp_err_t serve(httpd_req_t *r)
{
    char name[96]; size_t n = strcspn(r->uri, "?");
    if (n >= sizeof(name)) return app_error(r, "404 Not Found", "not_found", "Resource not found");
    memcpy(name, r->uri, n); name[n] = 0; if (!strcmp(name, "/")) strcpy(name, "/index.html");
    if (!valid_path(name)) return app_error(r, "404 Not Found", "not_found", "Resource not found");
    web_state_t current = snapshot(); int slot = current.active;
    char query[96], version[65];
    if (httpd_req_get_url_query_str(r, query, sizeof(query)) == ESP_OK && httpd_query_key_value(query, "v", version, sizeof(version)) == ESP_OK) {
        if (slot < 0 || strcmp(slots[slot].asset_set, version)) {
            if (current.previous >= 0 && slots[current.previous].valid && !strcmp(slots[current.previous].asset_set, version)) slot = current.previous;
            else return app_error(r, "409 Conflict", "asset_version_unavailable", "Refresh the page to obtain one consistent resource version");
        }
    }
    if (slot < 0 || !slots[slot].valid) return !strcmp(name, "/index.html") ? recovery(r) : app_error(r, "404 Not Found", "no_ui", "Install a signed web release first");
    if (xSemaphoreTake(slot_locks[slot], pdMS_TO_TICKS(1000)) != pdTRUE) return app_error(r, "503 Service Unavailable", "storage_busy", "Retry shortly");
    file_entry_t *entry = NULL;
    for (unsigned i = 0; i < slots[slot].header.count; i++) if (!strcmp(name, slots[slot].files[i].path)) {entry = &slots[slot].files[i]; break;}
    if (!entry) {xSemaphoreGive(slot_locks[slot]); return app_error(r, "404 Not Found", "not_found", "Resource not found");}
    FILE *f = open_file(slot, name, "rb"); char *buffer = malloc(IO_SIZE);
    if (!f || !buffer) {if (f) fclose(f); free(buffer); xSemaphoreGive(slot_locks[slot]); return app_error(r, "503 Service Unavailable", "storage_error", "Cannot open resource; use /recovery");}
    char etag[67]; etag[0] = '"'; app_hex(entry->digest, 32, etag+1); etag[65] = '"'; etag[66] = 0;
    httpd_resp_set_type(r, mime(name)); httpd_resp_set_hdr(r, "Cache-Control", "no-store"); httpd_resp_set_hdr(r, "ETag", etag);
    httpd_resp_set_hdr(r, "X-Content-Type-Options", "nosniff");
    httpd_resp_set_hdr(r, "Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'");
    size_t left = entry->size; esp_err_t err = ESP_OK;
    while (!err && left) {size_t size = left < IO_SIZE ? left : IO_SIZE; if (fread(buffer, 1, size, f) != size) {err = ESP_FAIL; break;} err = httpd_resp_send_chunk(r, buffer, size); left -= size;}
    free(buffer); fclose(f); xSemaphoreGive(slot_locks[slot]);
    return err ? err : httpd_resp_send_chunk(r, NULL, 0);
}
esp_err_t storage_web_start(void)
{
    state_lock = xSemaphoreCreateMutex(); update_lock = xSemaphoreCreateMutex();
    for (int i = 0; i < 2; i++) slot_locks[i] = xSemaphoreCreateMutex();
    if (!state_lock || !update_lock || !slot_locks[0] || !slot_locks[1]) return ESP_ERR_NO_MEM;
    esp_err_t err = nvs_open("web_store", NVS_READWRITE, &storage); if (err) return err;
    web_state_t stored; size_t size = sizeof(stored); err = nvs_get_blob(storage, "state", &stored, &size);
    if (!err) {
        if (size != sizeof(stored) || stored.format != 2 || stored.active < -1 || stored.active > 1 || stored.previous < -1 || stored.previous > 1) return ESP_FAIL;
        state = stored;
    } else if (err != ESP_ERR_NVS_NOT_FOUND) return err;
    for (int i = 0; i < 2; i++) if (mount_slot(i, false) == ESP_OK) slots[i].valid = load_slot(i);
    if (state.pending || (state.active >= 0 && !slots[state.active].valid)) {
        web_state_t next = state; next.active = next.previous >= 0 && slots[next.previous].valid ? next.previous : -1;
        next.previous = -1; next.pending = 0; next.generation++; return save(next);
    }
    return ESP_OK;
}
esp_err_t storage_web_routes(httpd_handle_t server)
{
    const httpd_uri_t routes[] = {
        {.uri="/recovery", .method=HTTP_GET, .handler=recovery},
        {.uri="/setup", .method=HTTP_GET, .handler=recovery},
        {.uri="/*", .method=HTTP_GET, .handler=serve},
    };
    for (unsigned i = 0; i < sizeof(routes)/sizeof(routes[0]); i++) {esp_err_t err = httpd_register_uri_handler(server, &routes[i]); if (err) return err;}
    return ESP_OK;
}
