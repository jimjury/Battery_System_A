#include "relay.h"
#include "app_http.h"
#include "network.h"
#include "update_service.h"
#include "storage_web.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "lwip/inet.h"
#include "nvs.h"

#define RPC_MAX 8192
#define QUEUE_DEPTH 4
typedef struct {char url[384], token[129], device_id[49]; uint32_t allow_lan_http;} relay_config_t;
typedef struct {char *json; char operation[16], request_id[33], run_id[49]; int after;} work_t;
static relay_config_t config = {.device_id="A01"};
static nvs_handle_t settings;
static QueueHandle_t queue;
static SemaphoreHandle_t lock;
static char run_id[49], run_state[24] = "idle", error[160], last_request[33];
static char model_cache[4096] = "[]", result_cache[RPC_MAX] = "[]";
static int received_through = -1, confirmed_through = -1, cached_after = -2;
static uint32_t sample_count, result_count;
static int64_t last_seen, result_time;
static bool configured, in_flight;

static bool identifier(const char *s)
{
    size_t n = s ? strlen(s) : 0; if (!n || n > 48) return false;
    for (size_t i = 0; i < n; i++) if (!strchr("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-", s[i])) return false;
    return true;
}
static bool b_online(void) {return configured && battery_wifi_online() && last_seen && esp_timer_get_time()-last_seen < 30000000;}
bool battery_relay_busy(void)
{
    if (!lock) return false;
    xSemaphoreTake(lock, portMAX_DELAY);
    bool value = strcmp(run_state, "idle") && strcmp(run_state, "completed") && strcmp(run_state, "cancelled") && strcmp(run_state, "failed") && strcmp(run_state, "rejected");
    xSemaphoreGive(lock); return value;
}
cJSON *battery_relay_status(void)
{
    cJSON *j = cJSON_CreateObject(); if (!lock) return j;
    xSemaphoreTake(lock, portMAX_DELAY);
    cJSON_AddStringToObject(j, "transport", "https-rpc"); cJSON_AddBoolToObject(j, "configured", configured);
    cJSON_AddBoolToObject(j, "online", b_online()); cJSON_AddStringToObject(j, "device_id", config.device_id);
    cJSON_AddStringToObject(j, "run_id", run_id); cJSON_AddStringToObject(j, "state", run_state);
    cJSON_AddStringToObject(j, "error", error); cJSON_AddStringToObject(j, "request_id", last_request);
    cJSON_AddNumberToObject(j, "received_through_seq", received_through); cJSON_AddNumberToObject(j, "confirmed_through_seq", confirmed_through);
    cJSON_AddNumberToObject(j, "sample_count", sample_count); cJSON_AddNumberToObject(j, "result_count", result_count);
    cJSON_AddNumberToObject(j, "queue_depth", queue ? uxQueueMessagesWaiting(queue) : 0); cJSON_AddNumberToObject(j, "queue_capacity", QUEUE_DEPTH);
    cJSON_AddBoolToObject(j, "waiting_for_b", in_flight); cJSON_AddBoolToObject(j, "stale", !b_online());
    cJSON_AddNumberToObject(j, "heartbeat_age_s", last_seen ? (esp_timer_get_time()-last_seen)/1000000 : -1);
    xSemaphoreGive(lock); return j;
}
static bool valid_url(const char *url, bool lan)
{
    if (!url || strlen(url) >= 384 || strpbrk(url, "@#?\\ \r\n\t")) return false;
    const char *host = NULL;
    if (!strncmp(url, "https://", 8)) host = url+8;
    else if (lan && !strncmp(url, "http://", 7)) host = url+7;
    else return false;
    const char *end = strchr(host, '/'); size_t n = end ? (size_t)(end-host) : strlen(host);
    if (!n || n > 200 || (end && strcmp(end, "/"))) return false;
    if (!strncmp(url, "https://", 8)) return true;
    char address[16]; size_t length = strcspn(host, ":/"); if (length >= sizeof(address)) return false;
    memcpy(address, host, length); address[length] = 0; struct in_addr ip;
    if (inet_aton(address, &ip) != 1) return false;
    uint32_t value = ntohl(ip.s_addr);
    return (value >> 24) == 10 || (value >> 20) == 0xac1 || (value >> 16) == 0xc0a8;
}
static void token(char out[33]) {uint8_t random[16]; esp_fill_random(random, sizeof(random)); app_hex(random, 16, out);}
static bool enqueue(const char *operation, cJSON *body, int after)
{
    work_t item = {.after=after}; snprintf(item.operation, sizeof(item.operation), "%s", operation); token(item.request_id);
    cJSON_DeleteItemFromObjectCaseSensitive(body, "operation"); cJSON_AddStringToObject(body, "operation", operation);
    cJSON_DeleteItemFromObjectCaseSensitive(body, "request_id"); cJSON_AddStringToObject(body, "request_id", item.request_id);
    cJSON_DeleteItemFromObjectCaseSensitive(body, "schema"); cJSON_AddNumberToObject(body, "schema", 1);
    xSemaphoreTake(lock, portMAX_DELAY);
    cJSON_DeleteItemFromObjectCaseSensitive(body, "device_id"); cJSON_AddStringToObject(body, "device_id", config.device_id);
    cJSON *rid = cJSON_GetObjectItemCaseSensitive(body, "run_id");
    if (cJSON_IsString(rid) && identifier(rid->valuestring)) strcpy(item.run_id, rid->valuestring);
    xSemaphoreGive(lock);
    item.json = cJSON_PrintUnformatted(body);
    if (!item.json || strlen(item.json) >= RPC_MAX || xQueueSend(queue, &item, 0) != pdTRUE) {free(item.json); return false;}
    xSemaphoreTake(lock, portMAX_DELAY); strcpy(last_request, item.request_id); xSemaphoreGive(lock); return true;
}
static esp_err_t rpc(const work_t *work)
{
    relay_config_t current; xSemaphoreTake(lock, portMAX_DELAY); current = config; in_flight = true; xSemaphoreGive(lock);
    if (!current.url[0] || !battery_wifi_online()) return ESP_ERR_INVALID_STATE;
    char url[448], authorization[144];
    snprintf(url, sizeof(url), "%s%sapi/edge/rpc", current.url, current.url[strlen(current.url)-1] == '/' ? "" : "/");
    snprintf(authorization, sizeof(authorization), "Bearer %s", current.token);
    esp_http_client_config_t options = {.url=url, .timeout_ms=12000, .crt_bundle_attach=esp_crt_bundle_attach,
        .disable_auto_redirect=true, .buffer_size=4096, .buffer_size_tx=1024};
    esp_http_client_handle_t client = esp_http_client_init(&options); if (!client) return ESP_ERR_NO_MEM;
    esp_http_client_set_method(client, HTTP_METHOD_POST); esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_header(client, "Authorization", authorization);
    memset(authorization, 0, sizeof(authorization)); memset(current.token, 0, sizeof(current.token));
    size_t size = strlen(work->json); esp_err_t err = esp_http_client_open(client, size);
    size_t sent = 0;
    while (!err && sent < size) {int n = esp_http_client_write(client, work->json+sent, size-sent); if (n <= 0) err = ESP_FAIL; else sent += n;}
    if (!err) esp_http_client_fetch_headers(client);
    int status = esp_http_client_get_status_code(client);
    char *buffer = calloc(1, RPC_MAX+1); size_t got = 0;
    if (!buffer) err = ESP_ERR_NO_MEM;
    while (!err && got < RPC_MAX) {int n = esp_http_client_read(client, buffer+got, RPC_MAX-got); if (n < 0) {err = ESP_FAIL; break;} if (!n) break; got += n;}
    if (got == RPC_MAX) err = ESP_ERR_INVALID_SIZE;
    esp_http_client_cleanup(client);
    cJSON *j = !err ? cJSON_ParseWithLength(buffer, got) : NULL; free(buffer);
    cJSON *rid = cJSON_GetObjectItemCaseSensitive(j, "request_id"), *device = cJSON_GetObjectItemCaseSensitive(j, "device_id");
    if (!cJSON_IsString(rid) || strcmp(rid->valuestring, work->request_id) || !cJSON_IsString(device) || strcmp(device->valuestring, config.device_id)) err = ESP_ERR_INVALID_RESPONSE;
    xSemaphoreTake(lock, portMAX_DELAY);
    if (!err && status == 200 && cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(j, "ok"))) {
        last_seen = esp_timer_get_time(); error[0] = 0;
        cJSON *models = cJSON_GetObjectItemCaseSensitive(j, "models");
        if (cJSON_IsArray(models)) {char *raw = cJSON_PrintUnformatted(models); if (raw && strlen(raw) < sizeof(model_cache)) strcpy(model_cache, raw); free(raw);}
        cJSON *run = cJSON_GetObjectItemCaseSensitive(j, "run");
        cJSON *incoming_id = cJSON_GetObjectItemCaseSensitive(run, "run_id");
        if (cJSON_IsString(incoming_id) && !strcmp(incoming_id->valuestring, run_id) && !strcmp(work->run_id, run_id)) {
            cJSON *state = cJSON_GetObjectItemCaseSensitive(run, "state"), *confirmed = cJSON_GetObjectItemCaseSensitive(run, "confirmed_through_seq");
            cJSON *count = cJSON_GetObjectItemCaseSensitive(run, "result_count");
            if (cJSON_IsString(state) && strlen(state->valuestring) < sizeof(run_state)) strcpy(run_state, state->valuestring);
            if (cJSON_IsNumber(confirmed)) confirmed_through = confirmed->valueint;
            if (cJSON_IsNumber(count)) result_count = count->valueint;
        }
        cJSON *results = cJSON_GetObjectItemCaseSensitive(j, "results");
        if (!strcmp(work->operation, "results") && !strcmp(work->run_id, run_id) && cJSON_IsArray(results)) {
            char *raw = cJSON_PrintUnformatted(results);
            if (raw && strlen(raw) < sizeof(result_cache)) {strcpy(result_cache, raw); cached_after = work->after; result_time = esp_timer_get_time();}
            free(raw);
        }
    } else {
        cJSON *message = cJSON_GetObjectItemCaseSensitive(j, "message");
        snprintf(error, sizeof(error), "%s", cJSON_IsString(message) ? message->valuestring : "B request failed or response identity did not match");
        if (work->run_id[0] && !strcmp(work->run_id, run_id) && strcmp(run_state, "completed") && strcmp(run_state, "cancelled")) strcpy(run_state, status == 422 || status == 409 ? "rejected" : "paused");
        if (!err) err = ESP_FAIL;
    }
    in_flight = false; xSemaphoreGive(lock); cJSON_Delete(j); return err;
}
static void worker(void *arg)
{
    int64_t heartbeat_at = 0;
    for (;;) {
        work_t work;
        if (xQueueReceive(queue, &work, pdMS_TO_TICKS(500)) == pdTRUE) {
            for (unsigned attempt = 0; attempt < 3; attempt++) {
                if (rpc(&work) == ESP_OK) break;
                vTaskDelay(pdMS_TO_TICKS(1000U << attempt));
            }
            xSemaphoreTake(lock, portMAX_DELAY); in_flight = false; xSemaphoreGive(lock); free(work.json);
        } else if (configured && battery_wifi_online() && esp_timer_get_time() >= heartbeat_at) {
            cJSON *j = cJSON_CreateObject();
            enqueue("models", j, -1); cJSON_Delete(j);
            if (run_id[0]) {j = cJSON_CreateObject(); cJSON_AddStringToObject(j, "run_id", run_id); enqueue("status", j, -1); cJSON_Delete(j);}
            heartbeat_at = esp_timer_get_time()+10000000;
        }
    }
}
static bool readable(httpd_req_t *r)
{
    if (app_session_read(r)) return true;
    app_error(r, "401 Unauthorized", "authentication_required", "USB administrator session required"); return false;
}
static esp_err_t b_status(httpd_req_t *r)
{
    if (!app_usb(r)) return app_error(r, "403 Forbidden", "usb_required", "USB management only");
    return app_json(r, battery_relay_status());
}
static esp_err_t read_config(httpd_req_t *r)
{
    if (!readable(r)) return ESP_OK;
    cJSON *j = cJSON_CreateObject(); xSemaphoreTake(lock, portMAX_DELAY);
    cJSON_AddStringToObject(j, "url", config.url); cJSON_AddStringToObject(j, "device_id", config.device_id);
    cJSON_AddBoolToObject(j, "token_saved", config.token[0] != 0); cJSON_AddBoolToObject(j, "allow_lan_http", config.allow_lan_http);
    xSemaphoreGive(lock); return app_json(r, j);
}
static esp_err_t save_config(httpd_req_t *r)
{
    cJSON *j; if (!app_admin_json(r, &j)) return ESP_OK;
    if (battery_relay_busy() || battery_update_busy()) {cJSON_Delete(j); return app_error(r, "409 Conflict", "device_busy", "Stop the active experiment before changing B");}
    cJSON *url = cJSON_GetObjectItemCaseSensitive(j, "url"), *secret = cJSON_GetObjectItemCaseSensitive(j, "token"),
          *device = cJSON_GetObjectItemCaseSensitive(j, "device_id"), *lan = cJSON_GetObjectItemCaseSensitive(j, "allow_lan_http");
    relay_config_t next = {0}; bool ok = cJSON_IsString(url) && valid_url(url->valuestring, cJSON_IsTrue(lan)) && cJSON_IsString(secret) &&
        strlen(secret->valuestring) >= 32 && strlen(secret->valuestring) <= 128 && cJSON_IsString(device) && identifier(device->valuestring);
    if (!ok) {cJSON_Delete(j); return app_error(r, "422 Unprocessable Entity", "invalid_b_config", "Use HTTPS, a 32–128 byte device token, and a valid device ID. Plain HTTP is restricted to explicitly enabled private-LAN testing.");}
    strcpy(next.url, url->valuestring); strcpy(next.token, secret->valuestring); strcpy(next.device_id, device->valuestring); next.allow_lan_http = cJSON_IsTrue(lan);
    memset(secret->valuestring, 0, strlen(secret->valuestring)); cJSON_Delete(j);
    esp_err_t err = nvs_set_blob(settings, "config", &next, sizeof(next)); if (!err) err = nvs_commit(settings);
    if (!err) {xSemaphoreTake(lock, portMAX_DELAY); config = next; configured = true; last_seen = 0; strcpy(model_cache, "[]"); xSemaphoreGive(lock);}
    memset(&next, 0, sizeof(next));
    if (err) return app_error(r, "500 Internal Server Error", "nvs_error", "Cannot save B configuration");
    return b_status(r);
}
static esp_err_t test_b(httpd_req_t *r)
{
    cJSON *j; if (!app_admin_json(r, &j)) return ESP_OK;
    bool ok = enqueue("models", j, -1); cJSON_Delete(j);
    if (!ok) return app_error(r, "429 Too Many Requests", "queue_full", "Retry after pending B requests complete");
    httpd_resp_set_status(r, "202 Accepted"); return b_status(r);
}
static esp_err_t models(httpd_req_t *r)
{
    if (!readable(r)) return ESP_OK;
    xSemaphoreTake(lock, portMAX_DELAY); cJSON *j = cJSON_Parse(model_cache); bool online = b_online(); xSemaphoreGive(lock);
    cJSON *out = cJSON_CreateObject(); cJSON_AddBoolToObject(out, "online", online); cJSON_AddItemToObject(out, "models", j ? j : cJSON_CreateArray()); return app_json(r, out);
}
static esp_err_t run_start(httpd_req_t *r)
{
    cJSON *j; if (!app_admin_json(r, &j)) return ESP_OK;
    cJSON *id = cJSON_GetObjectItemCaseSensitive(j, "run_id"), *count = cJSON_GetObjectItemCaseSensitive(j, "sample_count");
    if (!cJSON_IsString(id) || !identifier(id->valuestring) || !cJSON_IsNumber(count) || count->valuedouble < 1 || count->valuedouble > 10000000 || floor(count->valuedouble) != count->valuedouble) {
        cJSON_Delete(j); return app_error(r, "422 Unprocessable Entity", "invalid_run", "Valid run_id and sample_count required");
    }
    if (!b_online() || battery_update_busy()) {cJSON_Delete(j); return app_error(r, "409 Conflict", "service_unavailable", "B must be online and updates idle before starting");}
    if (!storage_update_lock()) {cJSON_Delete(j); return app_error(r, "409 Conflict", "update_busy", "Wait for the active update");}
    if (battery_update_busy()) {storage_update_unlock(); cJSON_Delete(j); return app_error(r, "409 Conflict", "update_busy", "Wait for the active update");}
    xSemaphoreTake(lock, portMAX_DELAY);
    if (run_id[0] && strcmp(run_id, id->valuestring) && strcmp(run_state, "completed") && strcmp(run_state, "cancelled") && strcmp(run_state, "failed") && strcmp(run_state, "idle") && strcmp(run_state, "rejected")) {
        xSemaphoreGive(lock); storage_update_unlock(); cJSON_Delete(j); return app_error(r, "409 Conflict", "run_active", "Finish or cancel the current run");
    }
    strcpy(run_id, id->valuestring); strcpy(run_state, "created"); sample_count = count->valueint; received_through = confirmed_through = -1; result_count = 0; cached_after = -2; strcpy(result_cache, "[]");
    xSemaphoreGive(lock);
    bool ok = enqueue("start", j, -1); cJSON_Delete(j);
    storage_update_unlock();
    if (!ok) {
        xSemaphoreTake(lock, portMAX_DELAY); strcpy(run_state, "rejected"); strcpy(error, "Start request was not queued; retry after the queue drains"); xSemaphoreGive(lock);
        return app_error(r, "429 Too Many Requests", "queue_full", "Run not sent; retry after pending requests");
    }
    httpd_resp_set_status(r, "202 Accepted"); return b_status(r);
}
static esp_err_t samples(httpd_req_t *r)
{
    cJSON *j; if (!app_admin_json(r, &j)) return ESP_OK;
    cJSON *id = cJSON_GetObjectItemCaseSensitive(j, "run_id"), *values = cJSON_GetObjectItemCaseSensitive(j, "samples");
    xSemaphoreTake(lock, portMAX_DELAY);
    bool accepted = cJSON_IsString(id) && !strcmp(id->valuestring, run_id) && (!strcmp(run_state, "accepted") || !strcmp(run_state, "running")) && b_online();
    xSemaphoreGive(lock);
    if (!accepted) {cJSON_Delete(j); return app_error(r, "409 Conflict", "run_not_accepted", "Wait for B acceptance or resume the paused run");}
    int n = cJSON_GetArraySize(values);
    if (!cJSON_IsArray(values) || n < 1 || n > 8) {cJSON_Delete(j); return app_error(r, "422 Unprocessable Entity", "batch_limit", "A batch must contain 1–8 samples");}
    for (int i = 0; i < n; i++) {
        cJSON *sample = cJSON_GetArrayItem(values, i); const char *keys[] = {"seq", "sample_time_s", "voltage_v", "current_a", "temperature_c"};
        if (!cJSON_IsObject(sample) || cJSON_GetArraySize(sample) != 5) {cJSON_Delete(j); return app_error(r, "422 Unprocessable Entity", "sample_schema", "Send only seq, time, V, I, T; reference labels stay in the browser");}
        for (unsigned k = 0; k < 5; k++) {cJSON *v = cJSON_GetObjectItemCaseSensitive(sample, keys[k]); if (!cJSON_IsNumber(v) || !isfinite(v->valuedouble)) {cJSON_Delete(j); return app_error(r, "422 Unprocessable Entity", "invalid_sample", "Finite numeric V/I/T/time/seq values required");}}
    }
    int last = cJSON_GetObjectItemCaseSensitive(cJSON_GetArrayItem(values, n-1), "seq")->valueint;
    bool ok = enqueue("samples", j, -1); cJSON_Delete(j);
    if (!ok) {httpd_resp_set_hdr(r, "Retry-After", "1"); return app_error(r, "429 Too Many Requests", "queue_full", "Bounded forwarding queue is full; retry the same batch");}
    xSemaphoreTake(lock, portMAX_DELAY); if (last > received_through) received_through = last; xSemaphoreGive(lock);
    httpd_resp_set_status(r, "202 Accepted"); return b_status(r);
}
static esp_err_t control(httpd_req_t *r)
{
    cJSON *j; if (!app_admin_json(r, &j)) return ESP_OK;
    cJSON *id = cJSON_GetObjectItemCaseSensitive(j, "run_id");
    if (!cJSON_IsString(id) || strcmp(id->valuestring, run_id)) {cJSON_Delete(j); return app_error(r, "409 Conflict", "run_mismatch", "Control targets the active run only");}
    bool ok = enqueue("control", j, -1); cJSON_Delete(j);
    if (!ok) return app_error(r, "429 Too Many Requests", "queue_full", "Retry control after the queue drains");
    httpd_resp_set_status(r, "202 Accepted"); return b_status(r);
}
static esp_err_t results(httpd_req_t *r)
{
    if (!readable(r)) return ESP_OK;
    char query[96], after_text[16]; int after = -1;
    if (httpd_req_get_url_query_str(r, query, sizeof(query)) == ESP_OK && httpd_query_key_value(query, "after", after_text, sizeof(after_text)) == ESP_OK) {
        char *end; long parsed = strtol(after_text, &end, 10); if (*end || parsed < -1 || parsed > 10000000) return app_error(r, "400 Bad Request", "invalid_cursor", "Invalid result cursor"); after = parsed;
    }
    xSemaphoreTake(lock, portMAX_DELAY);
    bool fresh = cached_after == after && esp_timer_get_time()-result_time < 2000000;
    cJSON *j = cJSON_CreateObject(); cJSON_AddStringToObject(j, "run_id", run_id); cJSON_AddBoolToObject(j, "pending", !fresh);
    cJSON_AddItemToObject(j, "results", fresh ? cJSON_Parse(result_cache) : cJSON_CreateArray());
    char id[49]; strcpy(id, run_id); xSemaphoreGive(lock);
    if (!fresh && id[0] && !in_flight && uxQueueMessagesWaiting(queue) == 0) {
        cJSON *body = cJSON_CreateObject(); cJSON_AddStringToObject(body, "run_id", id); cJSON_AddNumberToObject(body, "after", after); cJSON_AddNumberToObject(body, "limit", 16);
        enqueue("results", body, after); cJSON_Delete(body);
    }
    if (!fresh) httpd_resp_set_status(r, "202 Accepted");
    return app_json(r, j);
}
static esp_err_t run_status(httpd_req_t *r) {if (!readable(r)) return ESP_OK; return b_status(r);}
esp_err_t battery_relay_start(httpd_handle_t server)
{
    lock = xSemaphoreCreateMutex(); queue = xQueueCreate(QUEUE_DEPTH, sizeof(work_t)); if (!lock || !queue) return ESP_ERR_NO_MEM;
    esp_err_t err = nvs_open("battery_b", NVS_READWRITE, &settings); if (err) return err;
    size_t size = sizeof(config); err = nvs_get_blob(settings, "config", &config, &size);
    if (!err) {
        if (size != sizeof(config) || config.url[383] || config.token[128] || config.device_id[48] || !valid_url(config.url, config.allow_lan_http) || !identifier(config.device_id)) return ESP_FAIL;
        configured = true;
    } else if (err != ESP_ERR_NVS_NOT_FOUND) return err;
    const httpd_uri_t routes[] = {
        {.uri="/api/b/status", .method=HTTP_GET, .handler=b_status},
        {.uri="/api/b/config", .method=HTTP_GET, .handler=read_config},
        {.uri="/api/b/config", .method=HTTP_POST, .handler=save_config},
        {.uri="/api/b/test", .method=HTTP_POST, .handler=test_b},
        {.uri="/api/models", .method=HTTP_GET, .handler=models},
        {.uri="/api/run/start", .method=HTTP_POST, .handler=run_start},
        {.uri="/api/run/samples", .method=HTTP_POST, .handler=samples},
        {.uri="/api/run/control", .method=HTTP_POST, .handler=control},
        {.uri="/api/run/status", .method=HTTP_GET, .handler=run_status},
        {.uri="/api/run/results", .method=HTTP_GET, .handler=results},
    };
    for (unsigned i = 0; i < sizeof(routes)/sizeof(routes[0]); i++) if ((err = httpd_register_uri_handler(server, &routes[i]))) return err;
    return xTaskCreate(worker, "b_relay", 14336, NULL, 4, NULL) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM;
}
