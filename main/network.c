#include "network.h"
#include "app_http.h"
#include <string.h>
#include <stdio.h>
#include <stdlib.h>
#include <time.h>
#include "esp_event.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_timer.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "nvs.h"

typedef struct { char ssid[33], password[65]; } wifi_saved_t;
static esp_netif_t *sta;
static nvs_handle_t settings;
static wifi_saved_t saved, candidate;
static bool connecting, online, testing, sntp_started, scanning;
static unsigned retries;
static int last_reason;
static esp_timer_handle_t reconnect_timer;
static SemaphoreHandle_t wifi_lock;
static wifi_ap_record_t aps[16];
static uint16_t ap_count;

bool battery_wifi_online(void) {return online;}
static void reconnect(void *arg)
{
    if (connecting && !online) esp_wifi_connect();
}
static esp_err_t apply(const wifi_saved_t *config)
{
    wifi_config_t value = {0};
    memcpy(value.sta.ssid, config->ssid, strnlen(config->ssid, 32));
    memcpy(value.sta.password, config->password, strnlen(config->password, 64));
    value.sta.pmf_cfg.capable = true;
    esp_err_t err = esp_wifi_set_config(WIFI_IF_STA, &value);
    memset(&value, 0, sizeof(value)); return err;
}
static void network_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    xSemaphoreTake(wifi_lock, portMAX_DELAY);
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START && connecting) esp_wifi_connect();
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        online = false;
        last_reason = ((wifi_event_sta_disconnected_t *)data)->reason;
        if (connecting) {
            retries++;
            if (testing && retries >= 5) {
                testing = false; retries = 0; memset(&candidate, 0, sizeof(candidate));
                connecting = saved.ssid[0] != 0;
                if (connecting) apply(&saved);
            }
            if (connecting) {
                unsigned seconds = 1U << (retries < 6 ? retries : 6);
                esp_timer_stop(reconnect_timer);
                esp_timer_start_once(reconnect_timer, (uint64_t)seconds * 1000000);
            }
        }
    }
    if (base == WIFI_EVENT && id == WIFI_EVENT_SCAN_DONE) {
        ap_count = 16;
        if (esp_wifi_scan_get_ap_records(&ap_count, aps) != ESP_OK) ap_count = 0;
        scanning = false;
    }
    if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = data;
        uint32_t ip = esp_ip4_addr_get_byte(&event->ip_info.ip, 0);
        bool conflict = ip == 192 && esp_ip4_addr_get_byte(&event->ip_info.ip, 1) == 168 &&
                        esp_ip4_addr_get_byte(&event->ip_info.ip, 2) == 7;
        if (conflict) {
            online = false; connecting = false; last_reason = -1001;
            esp_wifi_disconnect();
        } else {
            online = true; retries = 0; last_reason = 0;
            esp_netif_set_default_netif(sta);
            if (testing) {
                if (nvs_set_blob(settings, "wifi", &candidate, sizeof(candidate)) == ESP_OK && nvs_commit(settings) == ESP_OK)
                    saved = candidate;
                else last_reason = -1002;
                testing = false; memset(&candidate, 0, sizeof(candidate));
            }
            if (!sntp_started) {
                esp_sntp_config_t config = ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
                sntp_started = esp_netif_sntp_init(&config) == ESP_OK;
            }
        }
    }
    xSemaphoreGive(wifi_lock);
}
cJSON *battery_network_status(void)
{
    cJSON *j = cJSON_CreateObject();
    xSemaphoreTake(wifi_lock, portMAX_DELAY);
    esp_netif_ip_info_t info = {0}; esp_netif_get_ip_info(sta, &info);
    wifi_ap_record_t ap = {0}; bool associated = esp_wifi_sta_get_ap_info(&ap) == ESP_OK;
    char ip[16]; snprintf(ip, sizeof(ip), IPSTR, IP2STR(&info.ip));
    cJSON_AddBoolToObject(j, "connected", online); cJSON_AddBoolToObject(j, "connecting", connecting && !online);
    cJSON_AddStringToObject(j, "ip", online ? ip : ""); cJSON_AddStringToObject(j, "ssid", associated ? (char *)ap.ssid : "");
    cJSON_AddNumberToObject(j, "rssi", associated ? ap.rssi : 0); cJSON_AddNumberToObject(j, "reason", last_reason);
    cJSON_AddBoolToObject(j, "saved", saved.ssid[0] != 0); cJSON_AddBoolToObject(j, "testing", testing);
    cJSON_AddBoolToObject(j, "scanning", scanning); cJSON_AddBoolToObject(j, "time_ready", time(NULL) >= 1700000000);
    cJSON *networks = cJSON_AddArrayToObject(j, "networks");
    for (unsigned i = 0; i < ap_count; i++) {
        cJSON *entry = cJSON_CreateObject(); char ssid[33]; memcpy(ssid, aps[i].ssid, 32); ssid[32] = 0;
        cJSON_AddStringToObject(entry, "ssid", ssid); cJSON_AddNumberToObject(entry, "rssi", aps[i].rssi);
        cJSON_AddNumberToObject(entry, "authmode", aps[i].authmode); cJSON_AddItemToArray(networks, entry);
    }
    xSemaphoreGive(wifi_lock); return j;
}
static esp_err_t wifi_status(httpd_req_t *r)
{
    if (!app_usb(r)) return app_error(r, "403 Forbidden", "usb_required", "Direct USB management only");
    return app_json(r, battery_network_status());
}
static esp_err_t scan(httpd_req_t *r)
{
    cJSON *j; if (!app_admin_json(r, &j)) return ESP_OK; cJSON_Delete(j);
    xSemaphoreTake(wifi_lock, portMAX_DELAY);
    esp_err_t err = scanning ? ESP_ERR_INVALID_STATE : esp_wifi_scan_start(NULL, false);
    if (err == ESP_OK) scanning = true;
    xSemaphoreGive(wifi_lock);
    if (err) return app_error(r, "409 Conflict", "scan_busy", "Wi-Fi is connecting or scanning; retry after it finishes");
    httpd_resp_set_status(r, "202 Accepted"); return app_json(r, battery_network_status());
}
static esp_err_t connect_handler(httpd_req_t *r)
{
    cJSON *j; if (!app_admin_json(r, &j)) return ESP_OK;
    cJSON *ssid = cJSON_GetObjectItemCaseSensitive(j, "ssid"), *password = cJSON_GetObjectItemCaseSensitive(j, "password");
    if (!cJSON_IsString(ssid) || !cJSON_IsString(password) || !strlen(ssid->valuestring) || strlen(ssid->valuestring) > 32 ||
        strlen(password->valuestring) > 63 || (strlen(password->valuestring) && strlen(password->valuestring) < 8)) {
        cJSON_Delete(j); return app_error(r, "422 Unprocessable Entity", "invalid_wifi", "SSID must be 1–32 UTF-8 bytes; personal-network password is empty or 8–63 bytes");
    }
    xSemaphoreTake(wifi_lock, portMAX_DELAY);
    connecting = false; esp_timer_stop(reconnect_timer); esp_wifi_disconnect();
    memset(&candidate, 0, sizeof(candidate)); strcpy(candidate.ssid, ssid->valuestring); strcpy(candidate.password, password->valuestring);
    memset(password->valuestring, 0, strlen(password->valuestring)); cJSON_Delete(j);
    esp_err_t err = apply(&candidate);
    if (!err) {connecting = true; testing = true; online = false; retries = 0; last_reason = 0; err = esp_wifi_connect();}
    xSemaphoreGive(wifi_lock);
    if (err) return app_error(r, "409 Conflict", "wifi_busy", "Wi-Fi configuration could not start; saved network retained");
    httpd_resp_set_status(r, "202 Accepted"); return app_json(r, battery_network_status());
}
static esp_err_t disconnect_handler(httpd_req_t *r)
{
    cJSON *j; if (!app_admin_json(r, &j)) return ESP_OK; cJSON_Delete(j);
    xSemaphoreTake(wifi_lock, portMAX_DELAY);
    connecting = false; online = false; testing = false; esp_timer_stop(reconnect_timer); esp_wifi_disconnect();
    memset(&candidate, 0, sizeof(candidate)); xSemaphoreGive(wifi_lock);
    return app_json(r, battery_network_status());
}
esp_err_t battery_network_start(httpd_handle_t server)
{
    wifi_lock = xSemaphoreCreateMutex(); if (!wifi_lock) return ESP_ERR_NO_MEM;
    esp_err_t err = nvs_open("battery_net", NVS_READWRITE, &settings); if (err) return err;
    size_t size = sizeof(saved); err = nvs_get_blob(settings, "wifi", &saved, &size);
    if (err == ESP_OK && (size != sizeof(saved) || saved.ssid[32] || saved.password[64])) return ESP_FAIL;
    if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) return err;
    sta = esp_netif_create_default_wifi_sta(); if (!sta) return ESP_ERR_NO_MEM;
    wifi_init_config_t config = WIFI_INIT_CONFIG_DEFAULT();
    if ((err = esp_wifi_init(&config))) return err;
    if ((err = esp_wifi_set_storage(WIFI_STORAGE_RAM))) return err;
    if ((err = esp_wifi_set_mode(WIFI_MODE_STA))) return err;
    /* Original NCM descriptor uses the base STA MAC. Give Wi-Fi the unique
     * SOFTAP-derived MAC without changing any verified USB transport code. */
    uint8_t mac[6]; if ((err = esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP))) return err;
    if ((err = esp_wifi_set_mac(WIFI_IF_STA, mac))) return err;
    esp_timer_create_args_t timer = {.callback=reconnect, .name="wifi_retry"};
    if ((err = esp_timer_create(&timer, &reconnect_timer))) return err;
    if ((err = esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, network_event, NULL))) return err;
    if ((err = esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, network_event, NULL))) return err;
    connecting = saved.ssid[0] != 0;
    if (connecting && (err = apply(&saved))) return err;
    if ((err = esp_wifi_start())) return err;
    const httpd_uri_t routes[] = {
        {.uri="/api/wifi/status", .method=HTTP_GET, .handler=wifi_status},
        {.uri="/api/wifi/scan", .method=HTTP_POST, .handler=scan},
        {.uri="/api/wifi/connect", .method=HTTP_POST, .handler=connect_handler},
        {.uri="/api/wifi/disconnect", .method=HTTP_POST, .handler=disconnect_handler},
        {.uri="/api/v1/wifi/status", .method=HTTP_GET, .handler=wifi_status},
        {.uri="/api/v1/wifi/scan", .method=HTTP_POST, .handler=scan},
        {.uri="/api/v1/wifi/connect", .method=HTTP_POST, .handler=connect_handler},
        {.uri="/api/v1/wifi/disconnect", .method=HTTP_POST, .handler=disconnect_handler},
    };
    for (unsigned i = 0; i < sizeof(routes)/sizeof(routes[0]); i++) if ((err = httpd_register_uri_handler(server, &routes[i]))) return err;
    return ESP_OK;
}
