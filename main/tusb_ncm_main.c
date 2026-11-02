/*
 * SPDX-FileCopyrightText: 2023 Espressif Systems (Shanghai) CO LTD
 *
 * SPDX-License-Identifier: Unlicense OR CC0-1.0
 */

/* DESCRIPTION:
 * This example demonstrates using ESP32-S3 as a USB network device. It initializes WiFi in station mode,
 * connects and bridges the WiFi and USB networks, so the USB host can obtain an IP address
 * from the AP/router which the WiFi station connects to.
 */

#include <stdio.h>
#include <stdatomic.h>

#include "esp_log.h"
#include "esp_event.h"
#include "esp_check.h"
#include "nvs_flash.h"
#include "esp_mac.h"

#include "esp_wifi.h"
#include "esp_private/wifi.h"

#include "tinyusb.h"
#include "tinyusb_net.h"
#include "class/net/net_device.h"

#ifndef CONFIG_IDF_TARGET_ESP32S3
#error This USB NCM Wi-Fi bridge is for ESP32-S3 only.
#endif

static const char *TAG = "USB_NCM";
static atomic_bool s_is_wifi_connected = ATOMIC_VAR_INIT(false);

/* TinyUSB calls this on USB enumeration/reset. The link must stay down until
 * the station has joined the AP, even when the host enumerates first. */
bool tud_network_default_link_state_cb(void)
{
    return atomic_load(&s_is_wifi_connected);
}

static esp_err_t usb_recv_callback(void *buffer, uint16_t len, void *ctx)
{
    atomic_bool *is_wifi_connected = ctx;
    if (atomic_load(is_wifi_connected)) {
        esp_err_t err = esp_wifi_internal_tx(ESP_IF_WIFI_STA, buffer, len);
        if (err != ESP_OK) {
            ESP_LOGW(TAG, "USB to Wi-Fi packet failed: %s", esp_err_to_name(err));
        }
        return err;
    }
    return ESP_ERR_INVALID_STATE;
}

static void wifi_pkt_free(void *eb, void *ctx)
{
    esp_wifi_internal_free_rx_buffer(eb);
}

static esp_err_t pkt_wifi2usb(void *buffer, uint16_t len, void *eb)
{
    if (tinyusb_net_send_sync(buffer, len, eb, portMAX_DELAY) != ESP_OK) {
        esp_wifi_internal_free_rx_buffer(eb);
    }
    return ESP_OK;
}

static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    atomic_bool *is_connected = arg;

    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *event = event_data;
        ESP_LOGW(TAG, "WiFi STA disconnected (reason %u); retrying", event->reason);
        atomic_store(is_connected, false);
        tud_network_link_state(0, false);
        esp_wifi_internal_reg_rxcb(ESP_IF_WIFI_STA, NULL);
        esp_err_t err = esp_wifi_connect();
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "WiFi reconnect failed: %s", esp_err_to_name(err));
        }
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_CONNECTED) {
        const wifi_event_sta_connected_t *event = event_data;
        esp_err_t err = esp_wifi_internal_reg_rxcb(ESP_IF_WIFI_STA, pkt_wifi2usb);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "WiFi receive callback registration failed: %s", esp_err_to_name(err));
            return;
        }
        atomic_store(is_connected, true);
        tud_network_link_state(0, true);
        ESP_LOGI(TAG, "WiFi STA connected on channel %u; USB NCM link up", event->channel);
    }
}

static esp_err_t start_wifi(atomic_bool *is_connected)
{
    ESP_RETURN_ON_ERROR(esp_event_loop_create_default(), TAG, "Cannot initialize event loop");

    wifi_init_config_t wifi_cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_RETURN_ON_ERROR(esp_wifi_init(&wifi_cfg), TAG, "Failed to initialize WiFi library");
    ESP_RETURN_ON_ERROR(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, wifi_event_handler, is_connected),
                        TAG, "Failed to register handler for wifi events");
    ESP_RETURN_ON_ERROR(esp_wifi_set_mode(WIFI_MODE_STA), TAG, "Failed to set WiFi station mode");
    ESP_RETURN_ON_ERROR(esp_wifi_start(), TAG, "Failed to start WiFi library");

    wifi_config_t wifi_config = {
        .sta = {
            .ssid = CONFIG_ESP_WIFI_SSID,
            .password = CONFIG_ESP_WIFI_PASSWORD,
        },
    };

    ESP_RETURN_ON_ERROR(esp_wifi_set_config(WIFI_IF_STA, &wifi_config), TAG, "Failed to set WiFi config");
    return esp_wifi_connect();
}

void app_main(void)
{
    /* Initialize NVS — it is used to store PHY calibration data */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    ESP_LOGI(TAG, "USB NCM device initialization");
    const tinyusb_config_t tusb_cfg = {
        .external_phy = false,
    };
    ESP_GOTO_ON_ERROR(tinyusb_driver_install(&tusb_cfg), err, TAG, "Failed to install TinyUSB driver");

    tinyusb_net_config_t net_config = {
        .on_recv_callback = usb_recv_callback,
        .free_tx_buffer = wifi_pkt_free,
        .user_context = &s_is_wifi_connected
    };
    esp_read_mac(net_config.mac_addr, ESP_MAC_WIFI_STA);
    uint8_t *mac = net_config.mac_addr;
    ESP_LOGI(TAG, "Network interface HW address: %02x:%02x:%02x:%02x:%02x:%02x", mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    ESP_GOTO_ON_ERROR(tinyusb_net_init(TINYUSB_USBDEV_0, &net_config), err, TAG, "Failed to initialize TinyUSB NCM device class");
    tud_network_link_state(0, false);

    ESP_LOGI(TAG, "WiFi initialization");
    ESP_GOTO_ON_ERROR(start_wifi(&s_is_wifi_connected), err, TAG, "Failed to init and start WiFi");

    ESP_LOGI(TAG, "USB NCM and WiFi started; waiting for WiFi STA association");
    return;

err:
    ESP_LOGE(TAG, "USB-WiFi bridge example failed!");
}
