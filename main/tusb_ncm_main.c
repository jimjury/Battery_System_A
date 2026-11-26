
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_log.h"
#include "esp_event.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_netif_defaults.h"
#include "esp_http_server.h"
#include "freertos/FreeRTOS.h"
#include "lwip/ip4_addr.h"
#include "tinyusb.h"
#include "tinyusb_default_config.h"
#include "tinyusb_net.h"
#include "app_http.h"
#include "network.h"
#include "center_client.h"
#include "storage_web.h"
#include "update_service.h"
#include "nvs_flash.h"

static const char *TAG = "Battery_A";
static esp_netif_t *usb_netif;
static int usb_driver_handle;

static esp_err_t usb_tx(void *h, void *buffer, size_t len)
{
    return tinyusb_net_send_sync(
        buffer, (uint16_t)len, NULL, pdMS_TO_TICKS(100));
}

static void usb_rx_free(void *h, void *buffer)
{
    free(buffer);
}

static esp_err_t usb_rx(void *buffer, uint16_t len, void *ctx)
{
    void *copy = malloc(len);
    if (!copy) {
        return ESP_ERR_NO_MEM;
    }
    memcpy(copy, buffer, len);
    return esp_netif_receive(usb_netif, copy, len, NULL);
}

void app_main(void)
{
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());

    esp_netif_ip_info_t ip = {0};
    IP4_ADDR(&ip.ip, 192, 168, 7, 1);
    IP4_ADDR(&ip.netmask, 255, 255, 255, 0);

    esp_netif_inherent_config_t base = {
        .flags = ESP_NETIF_FLAG_AUTOUP,
        .ip_info = &ip,
        .if_key = "USB_LOCAL",
        .if_desc = "usb-local",
        .route_prio = 10
    };

    ESP_ERROR_CHECK(esp_read_mac(base.mac, ESP_MAC_ETH));

    esp_netif_driver_ifconfig_t driver = {
        .handle = &usb_driver_handle,
        .transmit = usb_tx,
        .driver_free_rx_buffer = usb_rx_free
    };

    esp_netif_config_t cfg = {
        .base = &base,
        .driver = &driver,
        .stack = ESP_NETIF_NETSTACK_DEFAULT_ETH
    };

    usb_netif = esp_netif_new(&cfg);
    assert(usb_netif != NULL);

    const tinyusb_config_t tusb_cfg = TINYUSB_DEFAULT_CONFIG();
    ESP_ERROR_CHECK(tinyusb_driver_install(&tusb_cfg));

    tinyusb_net_config_t net_cfg = {
        .on_recv_callback = usb_rx
    };

    ESP_ERROR_CHECK(esp_read_mac(net_cfg.mac_addr, ESP_MAC_WIFI_STA));
    ESP_ERROR_CHECK(tinyusb_net_init(&net_cfg));

    esp_netif_action_start(usb_netif, NULL, 0, NULL);
    esp_netif_action_connected(usb_netif, NULL, 0, NULL);

    httpd_config_t http_cfg = HTTPD_DEFAULT_CONFIG();
    http_cfg.uri_match_fn = httpd_uri_match_wildcard;
    http_cfg.max_uri_handlers = 80;
    http_cfg.stack_size = 12288;
    http_cfg.recv_wait_timeout = 10;
    http_cfg.send_wait_timeout = 10;
    httpd_handle_t server = NULL;

    ESP_ERROR_CHECK(httpd_start(&server, &http_cfg));

    ESP_ERROR_CHECK(nvs_flash_init()); /* Never erase NVS automatically. */
    ESP_ERROR_CHECK(app_auth_start(server));
    ESP_ERROR_CHECK(battery_update_init_source());
    ESP_ERROR_CHECK(storage_web_start());
    ESP_ERROR_CHECK(battery_network_start(server));
    ESP_ERROR_CHECK(web_update_start(server));
    ESP_ERROR_CHECK(center_client_start(server));
    ESP_ERROR_CHECK(storage_web_routes(server));

    ESP_LOGI(TAG, "System A web server started");
    ESP_LOGI(TAG, "URL: http://192.168.7.1");
}
