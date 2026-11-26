#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <time.h>
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_netif_sntp.h"
#include "esp_http_server.h"
#include "esp_http_client.h"
#include "esp_crt_bundle.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "lwip/sockets.h"
#include "cJSON.h"

static esp_netif_t *sta;
static bool want_connect;
static unsigned retries;
static bool ntp_started;
static const char *TAG = "battery_wifi";
#define MANIFEST_URL "https://raw.githubusercontent.com/jimjury/Battery_System_A/main/updates/manifest.json"

static bool usb_only(httpd_req_t *r) {
    struct sockaddr_storage s = {0};
    socklen_t n = sizeof(s);
    if (getsockname(httpd_req_to_sockfd(r), (struct sockaddr *)&s, &n) != 0) return false;
    if (s.ss_family == AF_INET)
        return ((struct sockaddr_in *)&s)->sin_addr.s_addr == inet_addr("192.168.7.1");
#if CONFIG_LWIP_IPV6
    if (s.ss_family == AF_INET6) {
        const unsigned char *a = ((struct sockaddr_in6 *)&s)->sin6_addr.s6_addr;
        const unsigned char expected[16] = {0,0,0,0,0,0,0,0,0,0,255,255,192,168,7,1};
        return memcmp(a,expected,16)==0;
    }
#endif
    return false;
}
static bool allowed(httpd_req_t *r) {
    if (usb_only(r)) return true;
    httpd_resp_send_err(r, HTTPD_403_FORBIDDEN, "USB only");
    return false;
}
static esp_err_t json_reply(httpd_req_t *r,cJSON *j) {
    if (!j) return httpd_resp_send_err(r,HTTPD_500_INTERNAL_SERVER_ERROR,"out of memory");
    char *s=cJSON_PrintUnformatted(j);
    cJSON_Delete(j);
    if (!s) return httpd_resp_send_err(r,HTTPD_500_INTERNAL_SERVER_ERROR,"out of memory");
    httpd_resp_set_type(r,"application/json");
    httpd_resp_set_hdr(r,"Cache-Control","no-store");
    esp_err_t ret=httpd_resp_sendstr(r,s);
    free(s); return ret;
}
static void wifi_event(void *arg,esp_event_base_t base,int32_t id,void *data) {
    if (base==WIFI_EVENT && id==WIFI_EVENT_STA_START && want_connect) esp_wifi_connect();
    if (base==WIFI_EVENT && id==WIFI_EVENT_STA_DISCONNECTED && want_connect && retries<5) {
        retries++; esp_wifi_connect();
    }
    if (base==IP_EVENT && id==IP_EVENT_STA_GOT_IP) {
        retries=0;
        if (!ntp_started) {
            esp_sntp_config_t cfg=ESP_NETIF_SNTP_DEFAULT_CONFIG("pool.ntp.org");
            ntp_started=esp_netif_sntp_init(&cfg)==ESP_OK;
        }
    }
}
static esp_err_t status_handler(httpd_req_t *r) {
    if (!allowed(r)) return ESP_OK;
    cJSON *j=cJSON_CreateObject();
    esp_netif_ip_info_t info={0};
    esp_netif_get_ip_info(sta,&info);
    wifi_ap_record_t ap={0};
    bool associated=esp_wifi_sta_get_ap_info(&ap)==ESP_OK;
    bool online=associated && info.ip.addr!=0;
    char ip[16]; snprintf(ip,sizeof(ip),IPSTR,IP2STR(&info.ip));
    cJSON_AddBoolToObject(j,"connected",online);
    cJSON_AddStringToObject(j,"ip",online?ip:"");
    cJSON_AddStringToObject(j,"ssid",associated?(char *)ap.ssid:"");
    return json_reply(r,j);
}
static esp_err_t scan_handler(httpd_req_t *r) {
    if (!allowed(r)) return ESP_OK;
    esp_err_t e=esp_wifi_scan_start(NULL,true);
    if(e!=ESP_OK) return httpd_resp_send_err(r,HTTPD_500_INTERNAL_SERVER_ERROR,esp_err_to_name(e));
    wifi_ap_record_t *ap=calloc(24,sizeof(*ap));
    if(!ap) { esp_wifi_clear_ap_list(); return httpd_resp_send_err(r,HTTPD_500_INTERNAL_SERVER_ERROR,"out of memory"); }
    uint16_t count=24;
    e=esp_wifi_scan_get_ap_records(&count,ap);
    if(e!=ESP_OK){free(ap);return httpd_resp_send_err(r,HTTPD_500_INTERNAL_SERVER_ERROR,esp_err_to_name(e));}
    cJSON *j=cJSON_CreateArray();
    for(int i=0;i<count;i++){
        cJSON *o=cJSON_CreateObject();
        cJSON_AddStringToObject(o,"ssid",(char *)ap[i].ssid);
        cJSON_AddNumberToObject(o,"rssi",ap[i].rssi);
        cJSON_AddItemToArray(j,o);
    }
    free(ap); return json_reply(r,j);
}
static esp_err_t connect_handler(httpd_req_t *r) {
    if (!allowed(r)) return ESP_OK;
    char origin[64]={0};
    if(httpd_req_get_hdr_value_str(r,"Origin",origin,sizeof(origin))!=ESP_OK ||
       strcmp(origin,"http://192.168.7.1")!=0)
        return httpd_resp_send_err(r,HTTPD_403_FORBIDDEN,"invalid origin");
    char b[512]={0};
    if(r->content_len<=0 || r->content_len>=sizeof(b)) return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,"invalid length");
    int got=0;
    while(got<r->content_len){int n=httpd_req_recv(r,b+got,r->content_len-got);if(n<=0)return ESP_FAIL;got+=n;}
    cJSON *j=cJSON_Parse(b);
    memset(b,0,sizeof(b));
    cJSON *s=cJSON_GetObjectItemCaseSensitive(j,"ssid"),*p=cJSON_GetObjectItemCaseSensitive(j,"password");
    if(!cJSON_IsString(s)||!cJSON_IsString(p)||strlen(s->valuestring)==0||strlen(s->valuestring)>32||strlen(p->valuestring)>63){
        cJSON_Delete(j);return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,"invalid Wi-Fi settings");
    }
    wifi_config_t cfg={0};
    memcpy(cfg.sta.ssid,s->valuestring,strlen(s->valuestring));
    memcpy(cfg.sta.password,p->valuestring,strlen(p->valuestring));
    memset(p->valuestring,0,strlen(p->valuestring));
    cJSON_Delete(j);
    want_connect=false;
    esp_wifi_disconnect();
    esp_err_t e=esp_wifi_stop();
    if(e==ESP_OK)e=esp_wifi_set_config(WIFI_IF_STA,&cfg);
    memset(&cfg,0,sizeof(cfg));
    if(e==ESP_OK){want_connect=true;retries=0;}
    esp_err_t start=esp_wifi_start();
    if(e!=ESP_OK || start!=ESP_OK)return httpd_resp_send_err(r,HTTPD_500_INTERNAL_SERVER_ERROR,"Wi-Fi configuration failed");
    return httpd_resp_sendstr(r,"connecting");
}
typedef struct { char buf[2049]; size_t len; } manifest_buf_t;
static esp_err_t manifest_event(esp_http_client_event_t *ev){
    if(ev->event_id==HTTP_EVENT_ON_DATA){
        manifest_buf_t *m=ev->user_data;
        if(m->len+ev->data_len>=sizeof(m->buf))return ESP_FAIL;
        memcpy(m->buf+m->len,ev->data,ev->data_len);
        m->len+=ev->data_len;
    } return ESP_OK;
}
static esp_err_t manifest_handler(httpd_req_t *r){
    if(!allowed(r))return ESP_OK;
    esp_netif_ip_info_t info={0};
    wifi_ap_record_t ap={0};
    esp_netif_get_ip_info(sta,&info);
    if(!info.ip.addr||esp_wifi_sta_get_ap_info(&ap)!=ESP_OK)
        return httpd_resp_send_err(r,HTTPD_400_BAD_REQUEST,"ESP32 Wi-Fi not connected");
    if(time(NULL)<1700000000) return httpd_resp_send_err(r,HTTPD_500_INTERNAL_SERVER_ERROR,"Waiting for NTP time; retry shortly");
    manifest_buf_t *m=calloc(1,sizeof(*m));
    if(!m)return httpd_resp_send_err(r,HTTPD_500_INTERNAL_SERVER_ERROR,"out of memory");
    esp_http_client_config_t cfg={.url=MANIFEST_URL,.crt_bundle_attach=esp_crt_bundle_attach,.timeout_ms=8000,.event_handler=manifest_event,.user_data=m,.disable_auto_redirect=true};
    esp_http_client_handle_t c=esp_http_client_init(&cfg);
    if(!c){free(m);return httpd_resp_send_err(r,HTTPD_500_INTERNAL_SERVER_ERROR,"HTTPS init failed");}
    esp_err_t e=esp_http_client_perform(c);
    int code=esp_http_client_get_status_code(c);
    esp_http_client_cleanup(c);
    cJSON *j=(e==ESP_OK && code==200)?cJSON_Parse(m->buf):NULL;
    free(m);
    if(!j)return httpd_resp_send_err(r,HTTPD_500_INTERNAL_SERVER_ERROR,"GitHub HTTPS request failed");
    return json_reply(r,j);
}
extern const unsigned char setup_start[] asm("_binary_setup_html_start");
extern const unsigned char setup_end[] asm("_binary_setup_html_end");
static esp_err_t setup_handler(httpd_req_t *r){
    if(!allowed(r))return ESP_OK;
    httpd_resp_set_type(r,"text/html; charset=utf-8");
    httpd_resp_set_hdr(r,"Cache-Control","no-store");
    return httpd_resp_send(r,(const char *)setup_start,setup_end-setup_start-1);
}
void battery_wifi_start(httpd_handle_t server){
    // Do not erase NVS automatically: it may contain existing device settings.
    ESP_ERROR_CHECK(nvs_flash_init());
    sta=esp_netif_create_default_wifi_sta();
    assert(sta);
    wifi_init_config_t cfg=WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_storage(WIFI_STORAGE_FLASH));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT,ESP_EVENT_ANY_ID,wifi_event,NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT,IP_EVENT_STA_GOT_IP,wifi_event,NULL));
    wifi_config_t saved={0};
    if(esp_wifi_get_config(WIFI_IF_STA,&saved)==ESP_OK)want_connect=saved.sta.ssid[0]!=0;
    memset(&saved,0,sizeof(saved));
    ESP_ERROR_CHECK(esp_wifi_start());
    const httpd_uri_t routes[]={
        {.uri="/setup",.method=HTTP_GET,.handler=setup_handler},
        {.uri="/api/wifi/status",.method=HTTP_GET,.handler=status_handler},
        {.uri="/api/wifi/scan",.method=HTTP_GET,.handler=scan_handler},
        {.uri="/api/wifi/connect",.method=HTTP_POST,.handler=connect_handler},
        {.uri="/api/update/check",.method=HTTP_GET,.handler=manifest_handler}
    };
    for(size_t i=0;i<sizeof(routes)/sizeof(routes[0]);i++)
        ESP_ERROR_CHECK(httpd_register_uri_handler(server,&routes[i]));
    ESP_LOGI(TAG,"USB setup: http://192.168.7.1/setup");
}
