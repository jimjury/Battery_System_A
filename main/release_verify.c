#include "release_verify.h"
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "cJSON.h"
#include "mbedtls/base64.h"
#include "mbedtls/pk.h"
#include "mbedtls/sha256.h"
#include "release_public.h"
static char mirror_origin[256];

bool release_set_source(const char *url)
{
    if (!url || strncmp(url, "https://", 8) || strlen(url) >= 512 || strpbrk(url, "@#?\\ \r\n\t")) return false;
    const char *slash = strchr(url+8, '/'); if (!slash || slash == url+8) return false;
    size_t length = slash-url+1; if (length >= sizeof(mirror_origin)) return false;
    memcpy(mirror_origin, url, length); mirror_origin[length] = 0; return true;
}

bool release_url_allowed(const char *url)
{
    if (!url || strlen(url) >= 2048 || strncmp(url, "https://", 8) || strpbrk(url, "@#\\ \r\n\t")) return false;
    if (mirror_origin[0] && !strncmp(url, mirror_origin, strlen(mirror_origin))) return true;
    static const char *prefixes[] = {
        "https://github.com/jimjury/Battery_System_A/releases/download/",
        "https://raw.githubusercontent.com/jimjury/Battery_System_A/",
        "https://release-assets.githubusercontent.com/",
        "https://objects.githubusercontent.com/",
    };
    for (unsigned i = 0; i < sizeof(prefixes)/sizeof(prefixes[0]); i++)
        if (!strncmp(url, prefixes[i], strlen(prefixes[i]))) return true;
    return false;
}
static bool string(cJSON *object, const char *key, char *out, size_t capacity)
{
    cJSON *j = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!cJSON_IsString(j) || strlen(j->valuestring) >= capacity) return false;
    strcpy(out, j->valuestring); return true;
}
static bool integer(cJSON *object, const char *key, uint32_t maximum, uint32_t *out)
{
    cJSON *j = cJSON_GetObjectItemCaseSensitive(object, key);
    if (!cJSON_IsNumber(j) || !isfinite(j->valuedouble) || j->valuedouble < 1 || j->valuedouble > maximum || floor(j->valuedouble) != j->valuedouble) return false;
    *out = (uint32_t)j->valuedouble; return true;
}
esp_err_t release_verify(const char *envelope, size_t size, const char *kind, release_artifact_t *out)
{
    if (!envelope || !size || size > RELEASE_ENVELOPE_MAX || (strcmp(kind, "ui") && strcmp(kind, "firmware"))) return ESP_ERR_INVALID_ARG;
    esp_err_t result = ESP_ERR_INVALID_CRC;
    cJSON *root = cJSON_ParseWithLength(envelope, size), *payload = NULL;
    cJSON *schema = cJSON_GetObjectItemCaseSensitive(root, "schema_version");
    cJSON *key = cJSON_GetObjectItemCaseSensitive(root, "key_id");
    cJSON *raw = cJSON_GetObjectItemCaseSensitive(root, "payload"), *sig = cJSON_GetObjectItemCaseSensitive(root, "signature");
    unsigned char *decoded = NULL, signature[256], digest[32]; size_t length = 0, signature_size = 0;
    mbedtls_pk_context pk; mbedtls_pk_init(&pk);
    if (!cJSON_IsNumber(schema) || schema->valuedouble != 2 || !cJSON_IsString(key) || strcmp(key->valuestring, "release-v1") ||
        !cJSON_IsString(raw) || !cJSON_IsString(sig) || strlen(raw->valuestring) > 5500 || strlen(sig->valuestring) != 344) goto done;
    decoded = calloc(1, 4097); if (!decoded) {result = ESP_ERR_NO_MEM; goto done;}
    if (mbedtls_base64_decode(decoded, 4096, &length, (uint8_t *)raw->valuestring, strlen(raw->valuestring)) ||
        mbedtls_base64_decode(signature, sizeof(signature), &signature_size, (uint8_t *)sig->valuestring, strlen(sig->valuestring)) || signature_size != 256) goto done;
    if (mbedtls_sha256(decoded, length, digest, 0) ||
        mbedtls_pk_parse_public_key(&pk, (uint8_t *)release_public_pem, sizeof(release_public_pem)) ||
        !mbedtls_pk_can_do(&pk, MBEDTLS_PK_RSA) || mbedtls_pk_get_bitlen(&pk) != 2048 ||
        mbedtls_pk_verify(&pk, MBEDTLS_MD_SHA256, digest, sizeof(digest), signature, signature_size)) goto done;
    payload = cJSON_ParseWithLength((char *)decoded, length);
    char target[32]; uint32_t payload_schema;
    if (!integer(payload, "schema_version", 2, &payload_schema) || payload_schema != 2 ||
        !string(payload, "target", target, sizeof(target)) || strcmp(target, "esp32s3-n16r8")) goto done;
    cJSON *item = cJSON_GetObjectItemCaseSensitive(payload, kind);
    if (cJSON_IsNull(item)) {result = ESP_ERR_NOT_FOUND; goto done;}
    uint32_t api, minimum; char format[24], delivery[16] = "https";
    cJSON *mode = cJSON_GetObjectItemCaseSensitive(item, "delivery");
    if (mode && !string(item, "delivery", delivery, sizeof(delivery))) goto done;
    if (!cJSON_IsObject(item) || !string(item, "version", out->version, sizeof(out->version)) || !out->version[0] ||
        !string(item, "sha256", out->sha256, sizeof(out->sha256)) || strlen(out->sha256) != 64 ||
        !string(item, "url", out->url, sizeof(out->url)) ||
        (!strcmp(delivery, "usb") ? out->url[0] != 0 : strcmp(delivery, "https") || !release_url_allowed(out->url)) ||
        !integer(item, "sequence", 2147483647, &out->sequence) ||
        !integer(item, "size", !strcmp(kind, "ui") ? WEB_PACKAGE_MAX : 4*1024*1024, &out->size) ||
        !integer(item, "api", 1, &api) || !integer(item, "min_firmware_api", 1, &minimum) ||
        !string(item, "format", format, sizeof(format)) || strcmp(format, !strcmp(kind, "ui") ? "bsaweb1-spiffs" : "esp-idf-app")) goto done;
    for (unsigned i = 0; i < 64; i++) if (!strchr("0123456789abcdef", out->sha256[i])) goto done;
    for (unsigned i = 0; out->version[i]; i++) if (!strchr("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-", out->version[i])) goto done;
    result = ESP_OK;
done:
    mbedtls_pk_free(&pk); free(decoded); cJSON_Delete(payload); cJSON_Delete(root); return result;
}
