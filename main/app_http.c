#include "app_http.h"
#include "network.h"
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <ctype.h>
#include "esp_random.h"
#include "esp_timer.h"
#include "lwip/sockets.h"
#include "mbedtls/md.h"
#include "mbedtls/sha256.h"
#include "nvs.h"

static char update_key[65], admin_hash[65], session[65], csrf[65], nonce[65];
static int64_t session_until, nonce_until, login_after;
static char mobile_session[65], mobile_csrf[65];
static int64_t mobile_session_until, mobile_login_after;
void app_mobile_invalidate(void)
{
    memset(mobile_session,0,sizeof(mobile_session));memset(mobile_csrf,0,sizeof(mobile_csrf));mobile_session_until=0;
}
static nvs_handle_t credentials;
typedef struct {char id[33]; uint8_t hash[32]; uint32_t scopes, revoked;} api_token_t;
static struct {uint32_t format, count; api_token_t entries[8];} api_tokens = {.format=1};
static const char *api_scopes[] = {"status:read", "catalog:read", "task:submit", "task:read:own", "task:cancel:own", "file:write:own", "file:read:own"};

void app_hex(const uint8_t *in, size_t size, char *out)
{
    for (size_t i = 0; i < size; i++) sprintf(out + 2*i, "%02x", in[i]);
    out[2*size] = 0;
}
bool app_equal(const void *left, const void *right, size_t size)
{
    const uint8_t *a = left, *b = right; unsigned diff = 0;
    for (size_t i = 0; i < size; i++) diff |= a[i] ^ b[i];
    return !diff;
}
static bool hex64(const char *s)
{
    if (!s || strlen(s) != 64) return false;
    for (int i = 0; i < 64; i++) if (!strchr("0123456789abcdef", s[i])) return false;
    return true;
}
static void random_token(char out[65])
{
    uint8_t raw[32]; esp_fill_random(raw, sizeof(raw)); app_hex(raw, 32, out);
}
esp_err_t app_json(httpd_req_t *r, cJSON *j)
{
    if (!j) return httpd_resp_send_err(r, HTTPD_500_INTERNAL_SERVER_ERROR, "Out of memory");
    char *data = cJSON_PrintUnformatted(j); cJSON_Delete(j);
    if (!data) return httpd_resp_send_err(r, HTTPD_500_INTERNAL_SERVER_ERROR, "Out of memory");
    httpd_resp_set_type(r, "application/json"); httpd_resp_set_hdr(r, "Cache-Control", "no-store");
    httpd_resp_set_hdr(r, "X-Content-Type-Options", "nosniff");
    esp_err_t err = httpd_resp_sendstr(r, data); free(data); return err;
}
esp_err_t app_error(httpd_req_t *r, const char *status, const char *code, const char *message)
{
    httpd_resp_set_status(r, status);
    cJSON *j = cJSON_CreateObject(); cJSON_AddStringToObject(j, "code", code); cJSON_AddStringToObject(j, "message", message);
    return app_json(r, j);
}
char *app_body(httpd_req_t *r, size_t limit)
{
    if (r->content_len > limit) return NULL;
    char *body = calloc(1, r->content_len + 1); if (!body) return NULL;
    size_t offset = 0; unsigned timeouts = 0;
    while (offset < r->content_len) {
        int n = httpd_req_recv(r, body + offset, r->content_len - offset);
        if (n == HTTPD_SOCK_ERR_TIMEOUT && ++timeouts < 3) continue;
        if (n <= 0) {free(body); return NULL;}
        timeouts = 0; offset += n;
    }
    return body;
}
static uint32_t socket_ipv4(int fd, bool local)
{
    struct sockaddr_storage address = {0}; socklen_t length = sizeof(address);
    int err = local ? getsockname(fd, (struct sockaddr *)&address, &length) : getpeername(fd, (struct sockaddr *)&address, &length);
    if (err) return 0;
    if (address.ss_family == AF_INET) return ((struct sockaddr_in *)&address)->sin_addr.s_addr;
#if CONFIG_LWIP_IPV6
    if (address.ss_family == AF_INET6) {
        const uint8_t *a = ((struct sockaddr_in6 *)&address)->sin6_addr.s6_addr;
        const uint8_t prefix[12] = {0,0,0,0,0,0,0,0,0,0,255,255}; uint32_t ip;
        if (memcmp(a, prefix, 12)) return 0;
        memcpy(&ip, a+12, 4); return ip;
    }
#endif
    return 0;
}
bool app_usb(httpd_req_t *r)
{
    int fd = httpd_req_to_sockfd(r);
    char host[48];
    return socket_ipv4(fd, true) == inet_addr("192.168.7.1") && socket_ipv4(fd, false) == inet_addr("192.168.7.2") &&
        httpd_req_get_hdr_value_str(r, "Host", host, sizeof(host)) == ESP_OK &&
        (!strcmp(host, "192.168.7.1") || !strcmp(host, "192.168.7.1:80"));
}
static bool origin_ok(httpd_req_t *r)
{
    char origin[64]; size_t n = httpd_req_get_hdr_value_len(r, "Origin");
    if (!n) return true; /* Native developer tooling. Browser writes include Origin. */
    const char *wanted=app_usb(r)?"http://192.168.7.1":app_mobile(r)?"http://192.168.8.1":"";
    return wanted[0] && n < sizeof(origin) && httpd_req_get_hdr_value_str(r, "Origin", origin, sizeof(origin)) == ESP_OK && !strcmp(origin,wanted);
}
bool app_mobile(httpd_req_t *r)
{
    int fd=httpd_req_to_sockfd(r);char host[48];
    uint32_t peer=ntohl(socket_ipv4(fd,false));
    return battery_mobile_enabled() && socket_ipv4(fd,true)==inet_addr("192.168.8.1") &&
        (peer&0xffffff00U)==0xc0a80800U && (peer&255U)>=2 && (peer&255U)<255 &&
        httpd_req_get_hdr_value_str(r,"Host",host,sizeof(host))==ESP_OK &&
        (!strcmp(host,"192.168.8.1")||!strcmp(host,"192.168.8.1:80"));
}
bool app_session_read(httpd_req_t *r)
{
    char cookie[256];
    bool usb=app_usb(r);if(!usb&&!app_mobile(r))return false;
    const char *token=usb?session:mobile_session;int64_t until=usb?session_until:mobile_session_until;
    if (!token[0] || esp_timer_get_time() > until ||
        httpd_req_get_hdr_value_str(r, "Cookie", cookie, sizeof(cookie)) != ESP_OK) return false;
    char *start = cookie;
    while (start && *start) {
        while (*start == ' ' || *start == ';') start++;
        char *end = strchr(start, ';'); size_t n = end ? (size_t)(end-start) : strlen(start);
        if (n == 80 && !memcmp(start, "battery_session=", 16) && app_equal(start+16, token, 64)) return true;
        start = end ? end+1 : NULL;
    }
    return false;
}
bool app_authorize(httpd_req_t *r, char expected[65])
{
    expected[0] = 0;
    if (!app_usb(r) || !origin_ok(r)) return false;
    char token[65];
    if (app_session_read(r) && httpd_req_get_hdr_value_str(r, "X-CSRF-Token", token, sizeof(token)) == ESP_OK &&
        strlen(token) == 64 && app_equal(token, csrf, 64)) return true;
    char n[65], signature[65], message[384], wanted[65]; uint8_t digest[32];
    if (!update_key[0] || !nonce[0] || esp_timer_get_time() > nonce_until || strlen(r->uri) >= 128 ||
        httpd_req_get_hdr_value_str(r, "X-Nonce", n, sizeof(n)) != ESP_OK || !hex64(n) || !app_equal(n, nonce, 64) ||
        httpd_req_get_hdr_value_str(r, "X-Signature", signature, sizeof(signature)) != ESP_OK || !hex64(signature) ||
        httpd_req_get_hdr_value_str(r, "X-SHA256", expected, 65) != ESP_OK || !hex64(expected)) return false;
    nonce[0] = 0;
    snprintf(message, sizeof(message), "%.64s\nPOST\n%.127s\n%.64s", n, r->uri, expected);
    if (mbedtls_md_hmac(mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), (uint8_t *)update_key, 64,
            (uint8_t *)message, strlen(message), digest)) return false;
    app_hex(digest, 32, wanted); return app_equal(wanted, signature, 64);
}
bool app_digest_matches(const char *expected, const void *body, size_t size)
{
    if (!expected[0]) return true;
    uint8_t digest[32]; char actual[65];
    if (mbedtls_sha256(body, size, digest, 0)) return false;
    app_hex(digest, 32, actual); return app_equal(actual, expected, 64);
}
bool app_api_authorize(httpd_req_t *r, const char *scope, char expected[65])
{
    char scratch[65]; if (!expected) expected = scratch; expected[0] = 0;
    if ((!app_usb(r) && !app_mobile(r)) || !origin_ok(r)) return false;
    if(app_mobile(r) && strcmp(scope,"status:read") && strcmp(scope,"catalog:read") && strcmp(scope,"task:read:own") && strcmp(scope,"file:read:own"))return false;
    if (r->method == HTTP_GET && app_session_read(r)) return true;
    if (!app_usb(r)) return false; /* Mobile session is read-only; no HMAC or API-key administration. */
    if (r->method != HTTP_GET && app_authorize(r, expected)) return true;
    unsigned bit = 32;
    for (unsigned i = 0; i < sizeof(api_scopes)/sizeof(api_scopes[0]); i++) if (!strcmp(scope, api_scopes[i])) bit = i;
    if (bit == 32) return false;
    char authorization[80]; uint8_t hash[32];
    if (httpd_req_get_hdr_value_str(r, "Authorization", authorization, sizeof(authorization)) != ESP_OK ||
        strncmp(authorization, "Bearer ", 7) || !hex64(authorization+7) ||
        mbedtls_sha256((uint8_t *)authorization+7, 64, hash, 0)) return false;
    for (unsigned i = 0; i < api_tokens.count; i++)
        if (!api_tokens.entries[i].revoked && (api_tokens.entries[i].scopes & (1U << bit)) && app_equal(hash, api_tokens.entries[i].hash, 32)) return true;
    return false;
}
static esp_err_t api_keys_handler(httpd_req_t *r)
{
    if (r->method == HTTP_GET) {
        if (!app_usb(r)||!app_session_read(r)) return app_error(r, "401 Unauthorized", "login_required", "USB administrator session required");
        cJSON *j = cJSON_CreateObject(), *items = cJSON_AddArrayToObject(j, "tokens");
        for (unsigned i = 0; i < api_tokens.count; i++) {
            cJSON *item = cJSON_CreateObject(); cJSON_AddStringToObject(item, "id", api_tokens.entries[i].id);
            cJSON_AddBoolToObject(item, "revoked", api_tokens.entries[i].revoked); cJSON_AddNumberToObject(item, "scope_mask", api_tokens.entries[i].scopes); cJSON_AddItemToArray(items, item);
        }
        return app_json(r, j);
    }
    cJSON *body; if (!app_admin_json(r, &body)) return ESP_OK;
    cJSON *revoke = cJSON_GetObjectItemCaseSensitive(body, "revoke_id");
    if (cJSON_IsString(revoke)) {
        for (unsigned i = 0; i < api_tokens.count; i++) if (!strcmp(revoke->valuestring, api_tokens.entries[i].id)) api_tokens.entries[i].revoked = 1;
        cJSON_Delete(body);
    } else {
        cJSON *scopes = cJSON_GetObjectItemCaseSensitive(body, "scopes"); uint32_t mask = 0;
        if (!cJSON_IsArray(scopes) || !cJSON_GetArraySize(scopes) || api_tokens.count >= 8) {
            cJSON_Delete(body); return app_error(r, "409 Conflict", "token_limit", "Choose supported scopes; at most eight local API tokens");
        }
        cJSON *scope;
        cJSON_ArrayForEach(scope, scopes) {
            bool found = false;
            for (unsigned i = 0; i < sizeof(api_scopes)/sizeof(api_scopes[0]); i++) if (cJSON_IsString(scope) && !strcmp(scope->valuestring, api_scopes[i])) {mask |= 1U << i; found = true;}
            if (!found) {cJSON_Delete(body); return app_error(r, "422 Unprocessable Entity", "invalid_scope", "Unsupported local API scope");}
        }
        cJSON_Delete(body); char secret[65]; uint8_t id[16]; random_token(secret); esp_fill_random(id, sizeof(id));
        api_token_t *item = &api_tokens.entries[api_tokens.count++]; app_hex(id, 16, item->id); item->scopes = mask; item->revoked = 0;
        mbedtls_sha256((uint8_t *)secret, 64, item->hash, 0);
        esp_err_t err = nvs_set_blob(credentials, "api_tokens", &api_tokens, sizeof(api_tokens)); if (!err) err = nvs_commit(credentials);
        if (err) {item->revoked = 1; return app_error(r, "500 Internal Server Error", "nvs_error", "Cannot persist API token");}
        cJSON *j = cJSON_CreateObject(); cJSON_AddStringToObject(j, "id", item->id); cJSON_AddStringToObject(j, "token", secret); cJSON_AddNumberToObject(j, "scope_mask", mask);
        memset(secret, 0, sizeof(secret)); return app_json(r, j);
    }
    esp_err_t err = nvs_set_blob(credentials, "api_tokens", &api_tokens, sizeof(api_tokens)); if (!err) err = nvs_commit(credentials);
    if (err) return app_error(r, "500 Internal Server Error", "nvs_error", "Cannot persist token revocation");
    cJSON *j = cJSON_CreateObject(); cJSON_AddBoolToObject(j, "revoked", true); return app_json(r, j);
}
static esp_err_t logout(httpd_req_t *r)
{
    if(app_mobile(r)){
        char token[65];
        if(!origin_ok(r)||!app_session_read(r)||httpd_req_get_hdr_value_str(r,"X-CSRF-Token",token,sizeof(token))!=ESP_OK||strlen(token)!=64||!app_equal(token,mobile_csrf,64))
            return app_error(r,"401 Unauthorized","mobile_session_required","Mobile session and CSRF required");
        mobile_session[0]=mobile_csrf[0]=0;mobile_session_until=0;
        httpd_resp_set_hdr(r,"Set-Cookie","battery_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0");
        cJSON *reply=cJSON_CreateObject();cJSON_AddBoolToObject(reply,"logged_out",true);return app_json(r,reply);
    }
    cJSON *j; if (!app_admin_json(r, &j)) return ESP_OK; cJSON_Delete(j);
    session[0] = csrf[0] = 0; session_until = 0;
    httpd_resp_set_hdr(r, "Set-Cookie", "battery_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0");
    j = cJSON_CreateObject(); cJSON_AddBoolToObject(j, "logged_out", true); return app_json(r, j);
}
bool app_admin_json(httpd_req_t *r, cJSON **out)
{
    *out = NULL; char expected[65];
    if (!app_authorize(r, expected)) {app_error(r, "401 Unauthorized", "authentication_required", "USB administrator session required"); return false;}
    char *body = app_body(r, 8192);
    if (!body || !app_digest_matches(expected, body, r->content_len)) {
        free(body); app_error(r, "400 Bad Request", "invalid_body", "Invalid request length or digest"); return false;
    }
    if (!r->content_len) *out = cJSON_CreateObject();
    else *out = cJSON_ParseWithLength(body, r->content_len);
    memset(body, 0, r->content_len); free(body);
    if (!cJSON_IsObject(*out)) {cJSON_Delete(*out); *out = NULL; app_error(r, "400 Bad Request", "invalid_json", "JSON object required"); return false;}
    return true;
}
static esp_err_t provision(httpd_req_t *r)
{
    char content[48];
    if (update_key[0] || esp_timer_get_time() > 300000000 || !app_usb(r) || !origin_ok(r) ||
        httpd_req_get_hdr_value_str(r, "Content-Type", content, sizeof(content)) != ESP_OK || strcmp(content, "application/json"))
        return app_error(r, "403 Forbidden", "provisioning_closed", "Provisioning requires an unpaired device on direct USB within five minutes of boot");
    char *body = app_body(r, 512); cJSON *j = body ? cJSON_Parse(body) : NULL;
    cJSON *key = cJSON_GetObjectItemCaseSensitive(j, "update_key"), *hash = cJSON_GetObjectItemCaseSensitive(j, "admin_hash");
    if (!cJSON_IsString(key) || !cJSON_IsString(hash) || !hex64(key->valuestring) || !hex64(hash->valuestring)) {
        cJSON_Delete(j); if (body) {memset(body, 0, r->content_len); free(body);}
        return app_error(r, "400 Bad Request", "invalid_provisioning", "Invalid credential format");
    }
    /* A single NVS blob commits both credentials atomically. */
    char record[130]; memcpy(record, key->valuestring, 65); memcpy(record+65, hash->valuestring, 65);
    esp_err_t err = nvs_set_blob(credentials, "record", record, sizeof(record));
    if (!err) err = nvs_commit(credentials);
    if (!err) {memcpy(update_key, record, 65); memcpy(admin_hash, record+65, 65);}
    memset(record, 0, sizeof(record)); memset(body, 0, r->content_len); free(body); cJSON_Delete(j);
    if (err) return app_error(r, "500 Internal Server Error", "nvs_error", "Cannot save credentials");
    cJSON *reply = cJSON_CreateObject(); cJSON_AddBoolToObject(reply, "provisioned", true); return app_json(r, reply);
}
static esp_err_t login(httpd_req_t *r)
{
    bool mobile=app_mobile(r);
    if ((!app_usb(r)&&!mobile) || !origin_ok(r) || !admin_hash[0]) return app_error(r, "403 Forbidden", "usb_pairing_required", "Provision through direct USB before local browser login");
    int64_t *after=mobile?&mobile_login_after:&login_after;
    if (esp_timer_get_time() < *after) return app_error(r, "429 Too Many Requests", "login_rate_limit", "Retry in three seconds");
    *after = esp_timer_get_time() + 3000000;
    char *body = app_body(r, 512); cJSON *j = body ? cJSON_Parse(body) : NULL;
    cJSON *password = cJSON_GetObjectItemCaseSensitive(j, "password"); uint8_t digest[32]; char actual[65] = "";
    if (cJSON_IsString(password) && strlen(password->valuestring) <= 128 &&
        !mbedtls_sha256((uint8_t *)password->valuestring, strlen(password->valuestring), digest, 0)) app_hex(digest, 32, actual);
    bool valid = strlen(actual) == 64 && app_equal(actual, admin_hash, 64);
    if (body) {memset(body, 0, r->content_len); free(body);} cJSON_Delete(j);
    if (!valid) return app_error(r, "401 Unauthorized", "invalid_credentials", "Invalid administrator password");
    char *token=mobile?mobile_session:session,*xsrf=mobile?mobile_csrf:csrf;
    random_token(token);random_token(xsrf);
    if(mobile)mobile_session_until=esp_timer_get_time()+3600000000LL;else session_until=esp_timer_get_time()+3600000000LL;
    char cookie[160]; snprintf(cookie, sizeof(cookie), "battery_session=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600", token);
    httpd_resp_set_hdr(r, "Set-Cookie", cookie);
    cJSON *reply = cJSON_CreateObject(); cJSON_AddStringToObject(reply, "csrf", xsrf);cJSON_AddBoolToObject(reply,"read_only",mobile);return app_json(r, reply);
}
static esp_err_t challenge(httpd_req_t *r)
{
    if (!app_usb(r)) return app_error(r, "403 Forbidden", "usb_required", "Direct USB management only");
    random_token(nonce); nonce_until = esp_timer_get_time() + 60000000;
    cJSON *j = cJSON_CreateObject(); cJSON_AddStringToObject(j, "nonce", nonce); return app_json(r, j);
}
static esp_err_t session_status(httpd_req_t *r)
{
    if (!app_usb(r)&&!app_mobile(r)) return app_error(r, "403 Forbidden", "local_required", "Direct USB or enabled local phone hotspot required");
    cJSON *j = cJSON_CreateObject(); cJSON_AddBoolToObject(j, "provisioned", update_key[0] != 0);
    bool authenticated = app_session_read(r); cJSON_AddBoolToObject(j, "authenticated", authenticated);
    cJSON_AddBoolToObject(j,"read_only",app_mobile(r));
    if (authenticated) cJSON_AddStringToObject(j, "csrf", app_mobile(r)?mobile_csrf:csrf);
    return app_json(r, j);
}
esp_err_t app_auth_start(httpd_handle_t server)
{
    esp_err_t err = nvs_open("battery_auth", NVS_READWRITE, &credentials); if (err) return err;
    char record[130]; size_t size = sizeof(record);
    err = nvs_get_blob(credentials, "record", record, &size);
    if (err == ESP_OK) {
        if (size != sizeof(record) || record[64] || record[129] || !hex64(record) || !hex64(record+65)) return ESP_FAIL;
        memcpy(update_key, record, 65); memcpy(admin_hash, record+65, 65);
    } else if (err != ESP_ERR_NVS_NOT_FOUND) return err;
    memset(record, 0, sizeof(record));
    size = sizeof(api_tokens);
    err = nvs_get_blob(credentials, "api_tokens", &api_tokens, &size);
    if (!err && (size != sizeof(api_tokens) || api_tokens.format != 1 || api_tokens.count > 8)) return ESP_FAIL;
    if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) return err;
    const httpd_uri_t routes[] = {
        {.uri="/api/provision", .method=HTTP_POST, .handler=provision},
        {.uri="/api/session", .method=HTTP_POST, .handler=login},
        {.uri="/api/session", .method=HTTP_GET, .handler=session_status},
        {.uri="/api/challenge", .method=HTTP_GET, .handler=challenge},
        {.uri="/api/v1/api-keys", .method=HTTP_GET, .handler=api_keys_handler},
        {.uri="/api/v1/api-keys", .method=HTTP_POST, .handler=api_keys_handler},
        {.uri="/api/v1/logout", .method=HTTP_POST, .handler=logout},
    };
    for (unsigned i = 0; i < sizeof(routes)/sizeof(routes[0]); i++) {
        err = httpd_register_uri_handler(server, &routes[i]); if (err) return err;
    }
    return ESP_OK;
}
