#include "center_client.h"
#include "app_http.h"
#include "network.h"
#include "storage_web.h"
#include "update_service.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "mqtt_client.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "mbedtls/sha256.h"
#include "nvs.h"

#define JOB_COUNT 4
#define RESPONSE_MAX 8192
typedef struct {uint32_t format; char url[384], credential[129], node_id[37], alias[65], mqtt_uri[256];} center_config_t;
typedef struct {char id[33], path[256], scope[32]; esp_http_client_method_t method; uint8_t *body; size_t size;
    char part_sha[65], response[RESPONSE_MAX+1]; int state, status; bool registration;} job_t;
static center_config_t config = {.format=1, .alias="A"};
static job_t jobs[JOB_COUNT];
static QueueHandle_t queue;
static SemaphoreHandle_t lock;
static nvs_handle_t settings;
static char instance[37], error_code[96];
static int session_epoch = -1;
static int64_t heartbeat_at;
static bool online, tasks_known;
static char active_tasks[16][37];
static unsigned active_count;
static SemaphoreHandle_t download_lock;
static bool download_active;
static esp_mqtt_client_handle_t mqtt_client;
static bool mqtt_online, mqtt_wake;
static int64_t mqtt_retry_at;
static char mqtt_topic[96];

static void uuid(char out[37])
{
    uint8_t bytes[16]; char raw[33]; esp_fill_random(bytes, 16); bytes[6]=(bytes[6]&15)|64; bytes[8]=(bytes[8]&63)|128; app_hex(bytes,16,raw);
    snprintf(out,37,"%.8s-%.4s-%.4s-%.4s-%.12s",raw,raw+8,raw+12,raw+16,raw+20);
}
static bool uuid_valid(const char *s)
{
    if (!s || strlen(s)!=36) return false;
    for (unsigned i=0;i<36;i++) if (i==8||i==13||i==18||i==23) {if(s[i]!='-')return false;} else if(!strchr("0123456789abcdef",s[i]))return false;
    return true;
}
static bool origin_valid(const char *url)
{
    if (!url || strncmp(url,"https://",8) || strlen(url)>=384 || strpbrk(url,"@#?\\ \r\n\t")) return false;
    const char *slash=strchr(url+8,'/');
    return url[8] && (!slash || (!strcmp(slash,"/") && slash>url+8));
}
bool center_client_busy(void)
{
    if (!lock) return false;
    xSemaphoreTake(lock,portMAX_DELAY);
    bool value=active_count>0 || (config.credential[0] && !tasks_known) || download_active || uxQueueMessagesWaiting(queue)>0;
    for(unsigned i=0;i<JOB_COUNT;i++)if(jobs[i].state==1||jobs[i].state==2)value=true;
    xSemaphoreGive(lock); return value;
}
cJSON *center_client_status(void)
{
    cJSON *j=cJSON_CreateObject(); if(!lock)return j;
    xSemaphoreTake(lock,portMAX_DELAY);
    cJSON_AddStringToObject(j,"node_id",config.node_id); cJSON_AddStringToObject(j,"alias",config.alias);
    cJSON_AddBoolToObject(j,"configured",config.url[0]!=0); cJSON_AddBoolToObject(j,"registered",config.credential[0]!=0);
    cJSON_AddBoolToObject(j,"online",online && esp_timer_get_time()-heartbeat_at<60000000 && battery_wifi_online());
    cJSON_AddStringToObject(j,"error",error_code); cJSON_AddNumberToObject(j,"active_tasks",active_count);
    cJSON_AddBoolToObject(j,"tasks_synchronized",tasks_known); cJSON_AddStringToObject(j,"control_transport","HTTPS authoritative polling");
    cJSON_AddStringToObject(j,"mqtt_status",mqtt_online?"connected":"not_connected"); cJSON_AddNumberToObject(j,"queue_depth",uxQueueMessagesWaiting(queue));
    xSemaphoreGive(lock); return j;
}
static int call(esp_http_client_method_t method, const char *path, const void *body, size_t size, const char *part_sha, char *out, size_t capacity, bool anonymous)
{
    center_config_t current; xSemaphoreTake(lock,portMAX_DELAY); current=config; xSemaphoreGive(lock);
    if (!current.url[0] || (!anonymous && !current.credential[0]) || !battery_wifi_online() || time(NULL)<1700000000) return -1;
    char url[704], authorization[144];
    snprintf(url,sizeof(url),"%s%s",current.url,path);
    esp_http_client_config_t options={.url=url,.crt_bundle_attach=esp_crt_bundle_attach,.timeout_ms=12000,.disable_auto_redirect=true,.buffer_size=4096,.buffer_size_tx=1024};
    esp_http_client_handle_t client=esp_http_client_init(&options); if(!client)return -1;
    esp_http_client_set_method(client,method); esp_http_client_set_header(client,"X-Client-Capabilities","esp32-light");
    if(!anonymous){snprintf(authorization,sizeof(authorization),"Bearer %s",current.credential);esp_http_client_set_header(client,"Authorization",authorization);}
    esp_http_client_set_header(client,"X-Instance-ID",instance);
    char epoch[16];snprintf(epoch,sizeof(epoch),"%d",session_epoch);esp_http_client_set_header(client,"X-Session-Epoch",epoch);
    if(part_sha && part_sha[0]) {esp_http_client_set_header(client,"Content-Type","application/octet-stream");esp_http_client_set_header(client,"X-Part-SHA256",part_sha);}
    else esp_http_client_set_header(client,"Content-Type","application/json");
    memset(authorization,0,sizeof(authorization));memset(current.credential,0,sizeof(current.credential));
    esp_err_t err=esp_http_client_open(client,size);size_t sent=0;
    while(!err && sent<size){int n=esp_http_client_write(client,(const char *)body+sent,size-sent);if(n<=0)err=ESP_FAIL;else sent+=n;}
    if(!err)esp_http_client_fetch_headers(client);
    int status=esp_http_client_get_status_code(client);size_t got=0;
    while(!err && got<capacity-1){int n=esp_http_client_read(client,out+got,capacity-1-got);if(n<0){err=ESP_FAIL;break;}if(!n)break;got+=n;}
    char extra;if(!err && got==capacity-1 && esp_http_client_read(client,&extra,1)!=0)err=ESP_ERR_INVALID_SIZE;
    out[got]=0;esp_http_client_cleanup(client);
    return err || status<100 ? -1 : status;
}
static void mqtt_event(void *arg,esp_event_base_t base,int32_t event_id,void *data)
{
    esp_mqtt_event_handle_t event=data;
    if(event_id==MQTT_EVENT_CONNECTED){esp_mqtt_client_subscribe(event->client,mqtt_topic,1);}
    xSemaphoreTake(lock,portMAX_DELAY);
    if(event_id==MQTT_EVENT_SUBSCRIBED){mqtt_online=true;mqtt_wake=true;}
    else if(event_id==MQTT_EVENT_DISCONNECTED||event_id==MQTT_EVENT_ERROR)mqtt_online=false;
    else if(event_id==MQTT_EVENT_DATA && event->total_data_len<=1024 && event->topic_len==(int)strlen(mqtt_topic) &&
            !memcmp(event->topic,mqtt_topic,event->topic_len))mqtt_wake=true;
    xSemaphoreGive(lock);
}
static void mqtt_start_if_ready(void)
{
    if(mqtt_client||esp_timer_get_time()<mqtt_retry_at)return;
    mqtt_retry_at=esp_timer_get_time()+60000000;
    char response[1024];int status=call(HTTP_METHOD_POST,"/api/v1/mqtt/session","{}",2,NULL,response,sizeof(response),false);
    if(status!=200)return;
    cJSON *reply=cJSON_Parse(response),*uri=cJSON_GetObjectItemCaseSensitive(reply,"uri"),*id=cJSON_GetObjectItemCaseSensitive(reply,"client_id");
    if(!cJSON_IsString(uri)||strncmp(uri->valuestring,"mqtts://",8)||strlen(uri->valuestring)>=256||strpbrk(uri->valuestring,"@#?\\ \r\n\t")||!cJSON_IsString(id)||strlen(id->valuestring)>64){cJSON_Delete(reply);return;}
    if(config.mqtt_uri[0]&&strcmp(config.mqtt_uri,uri->valuestring)){cJSON_Delete(reply);return;}
    snprintf(mqtt_topic,sizeof(mqtt_topic),"ab/v1/nodes/%s/down/events",config.node_id);
    esp_mqtt_client_config_t options={.broker.address.uri=uri->valuestring,.broker.verification.crt_bundle_attach=esp_crt_bundle_attach,
        .credentials.username=config.node_id,.credentials.client_id=id->valuestring,.credentials.authentication.password=config.credential,
        .session.keepalive=30,.buffer.size=2048,.task.stack_size=6144,.network.timeout_ms=8000};
    mqtt_client=esp_mqtt_client_init(&options);cJSON_Delete(reply);
    if(mqtt_client){esp_mqtt_client_register_event(mqtt_client,ESP_EVENT_ANY_ID,mqtt_event,NULL);
        if(esp_mqtt_client_start(mqtt_client)!=ESP_OK){esp_mqtt_client_destroy(mqtt_client);mqtt_client=NULL;}}
}
static bool terminal(const char *state)
{
    return !strcmp(state,"completed")||!strcmp(state,"failed")||!strcmp(state,"cancelled")||!strcmp(state,"rejected")||!strcmp(state,"access_revoked");
}
static void observe_tasks(cJSON *reply,bool replace)
{
    cJSON *tasks=cJSON_GetObjectItemCaseSensitive(reply,"tasks");
    if (!cJSON_IsArray(tasks) && cJSON_IsString(cJSON_GetObjectItemCaseSensitive(reply,"task_id"))) {tasks=cJSON_CreateArray();cJSON_AddItemToArray(tasks,cJSON_Duplicate(reply,true));}
    else tasks=cJSON_Duplicate(tasks,true);
    if(!cJSON_IsArray(tasks)){cJSON_Delete(tasks);return;}
    xSemaphoreTake(lock,portMAX_DELAY);
    if(replace){active_count=0;memset(active_tasks,0,sizeof(active_tasks));tasks_known=true;}
    cJSON *task;
    cJSON_ArrayForEach(task,tasks){
        cJSON *id=cJSON_GetObjectItemCaseSensitive(task,"task_id"), *state=cJSON_GetObjectItemCaseSensitive(task,"state");
        if(!cJSON_IsString(id)||!uuid_valid(id->valuestring)||!cJSON_IsString(state))continue;
        unsigned slot=16;
        for(unsigned i=0;i<16;i++)if(!strcmp(active_tasks[i],id->valuestring)){slot=i;break;}
        if(terminal(state->valuestring)){if(slot<16){active_tasks[slot][0]=0;if(active_count)active_count--;}continue;}
        if(slot==16)for(unsigned i=0;i<16;i++)if(!active_tasks[i][0]){slot=i;active_count++;break;}
        if(slot<16)strcpy(active_tasks[slot],id->valuestring);
    }
    xSemaphoreGive(lock);cJSON_Delete(tasks);
}
static void heartbeat(void)
{
    cJSON *body=cJSON_CreateObject();cJSON_AddStringToObject(body,"instance_id",instance);
    if(session_epoch<0)cJSON_AddNullToObject(body,"session_epoch");else cJSON_AddNumberToObject(body,"session_epoch",session_epoch);
    char *raw=cJSON_PrintUnformatted(body);cJSON_Delete(body);char *response=malloc(RESPONSE_MAX+1);
    if(!raw||!response){free(raw);free(response);return;}
    int status=call(HTTP_METHOD_POST,"/api/v1/heartbeat",raw,strlen(raw),NULL,response,RESPONSE_MAX+1,false);free(raw);
    cJSON *reply=cJSON_Parse(response),*epoch=cJSON_GetObjectItemCaseSensitive(reply,"session_epoch");
    xSemaphoreTake(lock,portMAX_DELAY);
    if(status==200 && cJSON_IsNumber(epoch)){session_epoch=epoch->valueint;online=true;heartbeat_at=esp_timer_get_time();error_code[0]=0;}
    else {online=false;cJSON *code=cJSON_GetObjectItemCaseSensitive(reply,"code");snprintf(error_code,sizeof(error_code),"%s",cJSON_IsString(code)?code->valuestring:"center_unreachable_or_time_not_ready");}
    xSemaphoreGive(lock);cJSON_Delete(reply);
    if(status==200){mqtt_start_if_ready();status=call(HTTP_METHOD_GET,"/api/v1/tasks?active=true&limit=16",NULL,0,NULL,response,RESPONSE_MAX+1,false);reply=cJSON_Parse(response);if(status==200)observe_tasks(reply,true);cJSON_Delete(reply);}
    free(response);
}
static void worker(void *arg)
{
    int64_t next_heartbeat=0;unsigned failures=0;
    for(;;){
        int index;
        if(xQueueReceive(queue,&index,pdMS_TO_TICKS(250))==pdTRUE){
            job_t *job=&jobs[index];xSemaphoreTake(lock,portMAX_DELAY);job->state=2;xSemaphoreGive(lock);
            char *response=calloc(1,RESPONSE_MAX+1);int status=response?call(job->method,job->path,job->body,job->size,job->part_sha,response,RESPONSE_MAX+1,job->registration||!strcmp(job->path,"/health")):-1;
            cJSON *reply=response?cJSON_Parse(response):NULL;
            if(job->registration && status==200){
                cJSON *credential=cJSON_GetObjectItemCaseSensitive(reply,"credential"),*id=cJSON_GetObjectItemCaseSensitive(reply,"node_id");
                if(!cJSON_IsString(credential)||strlen(credential->valuestring)>128||!cJSON_IsString(id)||strcmp(id->valuestring,config.node_id))status=-1;
                else {center_config_t next=config;strcpy(next.credential,credential->valuestring);
                    esp_err_t err=nvs_set_blob(settings,"config",&next,sizeof(next));if(!err)err=nvs_commit(settings);
                    if(err)status=-1;else{xSemaphoreTake(lock,portMAX_DELAY);config=next;tasks_known=false;xSemaphoreGive(lock);}
                    memset(next.credential,0,sizeof(next.credential));
                    cJSON_DeleteItemFromObjectCaseSensitive(reply,"credential");cJSON_AddBoolToObject(reply,"registered",status==200);
                }
            }
            if(status>=200 && status<300)observe_tasks(reply,false);
            char *sanitized=reply?cJSON_PrintUnformatted(reply):NULL;
            xSemaphoreTake(lock,portMAX_DELAY);job->status=status;job->state=status>=200&&status<300?3:4;
            snprintf(job->response,sizeof(job->response),"%s",sanitized?sanitized:"{\"code\":\"center_transport_error\",\"message\":\"C unavailable, TLS/time invalid, or response exceeded ESP32 limits\"}");
            if(job->body){memset(job->body,0,job->size);free(job->body);job->body=NULL;}
            xSemaphoreGive(lock);free(sanitized);cJSON_Delete(reply);if(response){memset(response,0,RESPONSE_MAX+1);free(response);}
        }else if(config.credential[0] && battery_wifi_online() && (mqtt_wake||esp_timer_get_time()>=next_heartbeat)){
            xSemaphoreTake(lock,portMAX_DELAY);mqtt_wake=false;xSemaphoreGive(lock);
            heartbeat();failures=online?0:(failures<5?failures+1:5);next_heartbeat=esp_timer_get_time()+(int64_t)(online?15:(1U<<failures))*1000000;
        }
    }
}
static esp_err_t status_handler(httpd_req_t *r)
{
    if(!app_api_authorize(r,"status:read",NULL))return app_error(r,"401 Unauthorized","authentication_required","USB session or scoped API credential required");
    return app_json(r,center_client_status());
}
static esp_err_t configuration(httpd_req_t *r)
{
    if(r->method==HTTP_GET){if(!app_session_read(r))return app_error(r,"401 Unauthorized","login_required","USB administrator session required");
        cJSON *j=center_client_status();cJSON_AddStringToObject(j,"url",config.url);cJSON_AddStringToObject(j,"mqtt_uri",config.mqtt_uri);return app_json(r,j);}
    cJSON *j;if(!app_admin_json(r,&j))return ESP_OK;
    cJSON *url=cJSON_GetObjectItemCaseSensitive(j,"url"),*alias=cJSON_GetObjectItemCaseSensitive(j,"alias"),*mqtt=cJSON_GetObjectItemCaseSensitive(j,"mqtt_uri");
    if(!cJSON_IsString(url)||!origin_valid(url->valuestring)||!cJSON_IsString(alias)||!alias->valuestring[0]||strlen(alias->valuestring)>64||
        (mqtt && (!cJSON_IsString(mqtt)||strlen(mqtt->valuestring)>=256||strncmp(mqtt->valuestring,"mqtts://",8)))){cJSON_Delete(j);return app_error(r,"422 Unprocessable Entity","center_config","Use a trusted HTTPS origin, alias, and optional mqtts:// endpoint");}
    if(config.credential[0]&&strcmp(config.url,url->valuestring)){cJSON_Delete(j);return app_error(r,"409 Conflict","identity_bound","Explicitly unregister or migrate the current identity before changing C");}
    center_config_t next=config;strcpy(next.url,url->valuestring);size_t n=strlen(next.url);if(n && next.url[n-1]=='/')next.url[n-1]=0;strcpy(next.alias,alias->valuestring);if(mqtt)strcpy(next.mqtt_uri,mqtt->valuestring);
    cJSON_Delete(j);esp_err_t err=nvs_set_blob(settings,"config",&next,sizeof(next));if(!err)err=nvs_commit(settings);
    if(err)return app_error(r,"500 Internal Server Error","nvs_error","Cannot save C configuration");
    xSemaphoreTake(lock,portMAX_DELAY);config=next;xSemaphoreGive(lock);return app_json(r,center_client_status());
}
static bool path_identifier(const char *path,const char *prefix,char id[37],const char **tail)
{
    size_t n=strlen(prefix);if(strncmp(path,prefix,n)||strlen(path+n)<36)return false;
    memcpy(id,path+n,36);id[36]=0;if(!uuid_valid(id))return false;*tail=path+n+36;return true;
}
static esp_err_t generic(httpd_req_t *r)
{
    char path[256],upstream[256],scope[32]="status:read",id[37],expected[65];const char *tail=NULL;
    size_t n=strcspn(r->uri,"?");if(n>=sizeof(path))return app_error(r,"414 URI Too Long","uri_limit","Local API path too long");memcpy(path,r->uri,n);path[n]=0;
    esp_http_client_method_t method=r->method==HTTP_GET?HTTP_METHOD_GET:r->method==HTTP_POST?HTTP_METHOD_POST:r->method==HTTP_PUT?HTTP_METHOD_PUT:HTTP_METHOD_DELETE;
    bool registration=!strcmp(path,"/api/v1/center/register");bool allowed=false;bool part=false;
    if(!strcmp(path,"/api/v1/nodes") && r->method==HTTP_GET){strcpy(upstream,"/api/v1/nodes?role=B");strcpy(scope,"catalog:read");allowed=true;}
    else if(!strcmp(path,"/api/v1/pairings")){strcpy(upstream,path);strcpy(scope,r->method==HTTP_GET?"catalog:read":"center:manage");allowed=r->method==HTTP_GET||r->method==HTTP_POST;}
    else if(path_identifier(path,"/api/v1/pairings/",id,&tail)&&!*tail&&r->method==HTTP_DELETE){strcpy(upstream,path);strcpy(scope,"center:manage");allowed=true;}
    else if(path_identifier(path,"/api/v1/nodes/",id,&tail)&&!strcmp(tail,"/deployments")&&r->method==HTTP_GET){
        unsigned offset=0;char query[96],value[16];if(httpd_req_get_url_query_str(r,query,sizeof(query))==ESP_OK&&httpd_query_key_value(query,"offset",value,sizeof(value))==ESP_OK)offset=strtoul(value,NULL,10);
        if(offset>100)offset=100;
        snprintf(upstream,sizeof(upstream),"/api/v1/nodes/%s/deployments?offset=%u&limit=2",id,offset);strcpy(scope,"catalog:read");allowed=true;}
    else if(!strcmp(path,"/api/v1/uploads")&&r->method==HTTP_POST){strcpy(upstream,path);strcpy(scope,"file:write:own");allowed=true;}
    else if(path_identifier(path,"/api/v1/uploads/",id,&tail)){
        allowed=(r->method==HTTP_GET&&!*tail)||(r->method==HTTP_POST&&!strcmp(tail,"/finalize"));
        if(r->method==HTTP_PUT&&!strncmp(tail,"/parts/",7)&&tail[7]){allowed=true;for(const char *p=tail+7;*p;p++)if(*p<'0'||*p>'9')allowed=false;part=allowed;}
        strcpy(upstream,path);strcpy(scope,"file:write:own");}
    else if(!strcmp(path,"/api/v1/task-groups")&&r->method==HTTP_POST){strcpy(upstream,path);strcpy(scope,"task:submit");allowed=true;}
    else if(path_identifier(path,"/api/v1/task-groups/",id,&tail)&&!*tail&&r->method==HTTP_GET){strcpy(upstream,path);strcpy(scope,"task:read:own");allowed=true;}
    else if(!strcmp(path,"/api/v1/tasks")&&r->method==HTTP_GET){strcpy(upstream,"/api/v1/tasks?limit=16");strcpy(scope,"task:read:own");allowed=true;}
    else if(path_identifier(path,"/api/v1/tasks/",id,&tail)){allowed=(r->method==HTTP_GET&&(!*tail||!strcmp(tail,"/results")))||(r->method==HTTP_POST&&!strcmp(tail,"/cancel"));strcpy(upstream,path);strcpy(scope,r->method==HTTP_POST?"task:cancel:own":"task:read:own");}
    else if(!strcmp(path,"/api/v1/events")&&r->method==HTTP_GET){unsigned after=0;char query[96],value[16];if(httpd_req_get_url_query_str(r,query,sizeof(query))==ESP_OK&&httpd_query_key_value(query,"after",value,sizeof(value))==ESP_OK)after=strtoul(value,NULL,10);snprintf(upstream,sizeof(upstream),"/api/v1/events?after=%u&limit=16",after);strcpy(scope,"task:read:own");allowed=true;}
    else if(registration&&r->method==HTTP_POST){strcpy(upstream,"/api/v1/devices/enroll");strcpy(scope,"center:manage");allowed=!config.credential[0];}
    else if(!strcmp(path,"/api/v1/center/test")&&r->method==HTTP_POST){strcpy(upstream,"/health");method=HTTP_METHOD_GET;strcpy(scope,"center:manage");allowed=true;}
    if(!allowed)return app_error(r,"404 Not Found","operation_not_supported","Only declared versioned C operations are supported; this is not a general URL proxy");
    if(!app_api_authorize(r,scope,expected))return app_error(r,"401 Unauthorized","authentication_required","USB session or appropriate API scope required");
    if(!config.url[0])return app_error(r,"409 Conflict","center_not_configured","Configure C first");
    if(!strcmp(scope,"task:submit")&&(active_count>=8||battery_update_busy()))return app_error(r,"409 Conflict","device_busy","Wait for current tasks or updates; at most sixteen tracked sub-tasks");
    if(r->content_len>(part?16384:8192))return app_error(r,"413 Content Too Large","body_limit","ESP32 uses bounded JSON and 16 KiB file parts");
    char *body=app_body(r,part?16384:8192);if(!body||!app_digest_matches(expected,body,r->content_len)){free(body);return app_error(r,"400 Bad Request","body_digest","Invalid request body or authentication digest");}
    size_t size=r->content_len;
    if(registration){cJSON *j=cJSON_Parse(body);cJSON_DeleteItemFromObjectCaseSensitive(j,"node_id");cJSON_DeleteItemFromObjectCaseSensitive(j,"role");cJSON_AddStringToObject(j,"node_id",config.node_id);cJSON_AddStringToObject(j,"role","A");if(!cJSON_GetObjectItemCaseSensitive(j,"alias"))cJSON_AddStringToObject(j,"alias",config.alias);memset(body,0,size);free(body);body=cJSON_PrintUnformatted(j);cJSON_Delete(j);if(!body)return app_error(r,"400 Bad Request","registration_json","Registration JSON required");size=strlen(body);}
    if(!storage_update_lock()){memset(body,0,size);free(body);return app_error(r,"409 Conflict","update_busy","Retry after the current update");}
    if(battery_update_busy()){storage_update_unlock();memset(body,0,size);free(body);return app_error(r,"409 Conflict","update_busy","Firmware update or reboot is pending");}
    xSemaphoreTake(lock,portMAX_DELAY);int index=-1;
    for(int i=0;i<JOB_COUNT;i++)if(jobs[i].state!=1&&jobs[i].state!=2){index=i;break;}
    if(index<0){xSemaphoreGive(lock);storage_update_unlock();memset(body,0,size);free(body);httpd_resp_set_hdr(r,"Retry-After","1");return app_error(r,"429 Too Many Requests","queue_full","Bounded C queue is full; retry the same logical operation");}
    job_t *job=&jobs[index];memset(job,0,sizeof(*job));uint8_t random[16];esp_fill_random(random,16);app_hex(random,16,job->id);
    strcpy(job->path,upstream);strcpy(job->scope,scope);job->method=method;job->body=(uint8_t *)body;job->size=size;job->state=1;job->registration=registration;
    if(part){uint8_t sha[32];mbedtls_sha256((uint8_t *)body,size,sha,0);app_hex(sha,32,job->part_sha);char supplied[65];if(httpd_req_get_hdr_value_str(r,"X-Part-SHA256",supplied,sizeof(supplied))!=ESP_OK||strcmp(supplied,job->part_sha)){job->state=0;free(job->body);job->body=NULL;xSemaphoreGive(lock);storage_update_unlock();return app_error(r,"422 Unprocessable Entity","part_hash","File part SHA-256 mismatch");}}
    char job_id[33];strcpy(job_id,job->id);bool queued=xQueueSend(queue,&index,0)==pdTRUE;if(!queued){job->state=0;free(job->body);job->body=NULL;}
    xSemaphoreGive(lock);storage_update_unlock();
    if(!queued)return app_error(r,"429 Too Many Requests","queue_full","Retry the same idempotent operation after the C queue drains");
    cJSON *reply=cJSON_CreateObject();cJSON_AddStringToObject(reply,"job_id",job_id);cJSON_AddStringToObject(reply,"state","queued");cJSON_AddStringToObject(reply,"ack_level","A_received_only");httpd_resp_set_status(r,"202 Accepted");return app_json(r,reply);
}
static esp_err_t job_status(httpd_req_t *r)
{
    const char *id=r->uri+strlen("/api/v1/center/jobs/");if(strlen(id)!=32)return app_error(r,"404 Not Found","job_missing","Job is unavailable");
    xSemaphoreTake(lock,portMAX_DELAY);job_t *job=NULL;for(int i=0;i<JOB_COUNT;i++)if(!strcmp(jobs[i].id,id)){job=&jobs[i];break;}
    if(!job){xSemaphoreGive(lock);return app_error(r,"410 Gone","job_expired","Job cache expired; query C authoritative state or repeat with the same idempotency key");}
    if(!app_api_authorize(r,job->scope,NULL)){xSemaphoreGive(lock);return app_error(r,"401 Unauthorized","scope_required","Appropriate local API scope required");}
    cJSON *j=cJSON_CreateObject();cJSON_AddStringToObject(j,"job_id",job->id);cJSON_AddStringToObject(j,"state",job->state==1?"queued":job->state==2?"working":job->state==3?"done":"failed");cJSON_AddNumberToObject(j,"http_status",job->status);
    if(job->state>=3){cJSON *value=cJSON_Parse(job->response);cJSON_AddItemToObject(j,"response",value?value:cJSON_CreateObject());}
    xSemaphoreGive(lock);return app_json(r,j);
}
typedef struct {httpd_req_t *req;char id[37],sha[65];} download_t;
static esp_err_t download_header(esp_http_client_event_t *event)
{
    download_t *work=event->user_data;
    if(event->event_id==HTTP_EVENT_ON_HEADER && !strcasecmp(event->header_key,"X-File-SHA256") && strlen(event->header_value)==64)
        memcpy(work->sha,event->header_value,65);
    return ESP_OK;
}
static void download_task(void *arg)
{
    download_t *work=arg;httpd_req_t *r=work->req;center_config_t current=config;char url[512],auth[144];snprintf(url,sizeof(url),"%s/api/v1/files/%s",current.url,work->id);
    esp_http_client_config_t options={.url=url,.crt_bundle_attach=esp_crt_bundle_attach,.timeout_ms=12000,.disable_auto_redirect=true,.buffer_size=4096,.event_handler=download_header,.user_data=work};
    esp_http_client_handle_t client=esp_http_client_init(&options);char *buffer=malloc(4096);esp_err_t err=client&&buffer?ESP_OK:ESP_ERR_NO_MEM;
    if(!err){snprintf(auth,sizeof(auth),"Bearer %s",current.credential);esp_http_client_set_header(client,"Authorization",auth);err=esp_http_client_open(client,0);}
    memset(auth,0,sizeof(auth));memset(current.credential,0,sizeof(current.credential));
    if(!err){esp_http_client_fetch_headers(client);if(esp_http_client_get_status_code(client)!=200)err=ESP_FAIL;}
    if(err)app_error(r,"502 Bad Gateway","private_download_failed","C denied this private file or could not be reached");
    else{httpd_resp_set_type(r,"application/octet-stream");httpd_resp_set_hdr(r,"Cache-Control","no-store");httpd_resp_set_hdr(r,"Content-Disposition","attachment");
        if(work->sha[0])httpd_resp_set_hdr(r,"X-File-SHA256",work->sha);
        for(;;){int n=esp_http_client_read(client,buffer,4096);if(n<0){err=ESP_FAIL;break;}if(!n)break;if(httpd_resp_send_chunk(r,buffer,n)!=ESP_OK){err=ESP_FAIL;break;}}
        if(!err)httpd_resp_send_chunk(r,NULL,0);}
    if(err)httpd_sess_trigger_close(r->handle,httpd_req_to_sockfd(r));
    if(client)esp_http_client_cleanup(client);
    free(buffer);httpd_req_async_handler_complete(r);free(work);
    xSemaphoreTake(lock,portMAX_DELAY);download_active=false;xSemaphoreGive(lock);xSemaphoreGive(download_lock);vTaskDelete(NULL);
}
static esp_err_t download(httpd_req_t *r)
{
    if(!app_api_authorize(r,"file:read:own",NULL))return app_error(r,"401 Unauthorized","scope_required","Private file read scope required");
    const char *prefix=!strncmp(r->uri,"/api/v1/artifacts/",18)?"/api/v1/artifacts/":"/api/v1/files/";char id[37];const char *tail;
    if(!path_identifier(r->uri,prefix,id,&tail)||*tail)return app_error(r,"404 Not Found","file_id","Valid private file ID required");
    if(xSemaphoreTake(download_lock,0)!=pdTRUE)return app_error(r,"429 Too Many Requests","download_busy","One bounded private download is already active");
    download_t *work=calloc(1,sizeof(*work));if(!work){xSemaphoreGive(download_lock);return app_error(r,"503 Service Unavailable","memory","Retry shortly");}
    if(!storage_update_lock()){free(work);xSemaphoreGive(download_lock);return app_error(r,"409 Conflict","update_busy","Retry after the update");}
    if(battery_update_busy()){storage_update_unlock();free(work);xSemaphoreGive(download_lock);return app_error(r,"409 Conflict","update_busy","Update or restart pending");}
    esp_err_t err=httpd_req_async_handler_begin(r,&work->req);if(err){storage_update_unlock();free(work);xSemaphoreGive(download_lock);return err;}strcpy(work->id,id);
    xSemaphoreTake(lock,portMAX_DELAY);download_active=true;xSemaphoreGive(lock);
    storage_update_unlock();
    if(xTaskCreate(download_task,"private_file",12288,work,4,NULL)!=pdPASS){httpd_req_async_handler_complete(work->req);free(work);download_active=false;xSemaphoreGive(download_lock);return ESP_ERR_NO_MEM;}
    return ESP_OK;
}
static esp_err_t api_info(httpd_req_t *r)
{
    cJSON *j=cJSON_CreateObject();cJSON_AddNumberToObject(j,"api_version",1);cJSON_AddStringToObject(j,"platform","generic_A_B_C");cJSON_AddNumberToObject(j,"file_part_max",16384);cJSON_AddNumberToObject(j,"response_max",RESPONSE_MAX);cJSON_AddStringToObject(j,"transport","USB to A; independent Wi-Fi to authorized C");return app_json(r,j);
}
esp_err_t center_client_start(httpd_handle_t server)
{
    lock=xSemaphoreCreateMutex();download_lock=xSemaphoreCreateBinary();queue=xQueueCreate(2,sizeof(int));if(!lock||!queue||!download_lock)return ESP_ERR_NO_MEM;
    xSemaphoreGive(download_lock);
    esp_err_t err=nvs_open("center_v1",NVS_READWRITE,&settings);if(err)return err;size_t size=sizeof(config);err=nvs_get_blob(settings,"config",&config,&size);
    if(!err){if(size!=sizeof(config)||config.format!=1||config.url[383]||config.credential[128]||config.node_id[36]||!uuid_valid(config.node_id)||(config.url[0]&&!origin_valid(config.url)))return ESP_FAIL;}
    else if(err==ESP_ERR_NVS_NOT_FOUND){uuid(config.node_id);err=nvs_set_blob(settings,"config",&config,sizeof(config));if(!err)err=nvs_commit(settings);if(err)return err;}
    else return err;
    uuid(instance);
    const httpd_uri_t routes[]={
        {.uri="/api/v1/center/status",.method=HTTP_GET,.handler=status_handler},
        {.uri="/api/v1/center/config",.method=HTTP_GET,.handler=configuration},
        {.uri="/api/v1/center/config",.method=HTTP_POST,.handler=configuration},
        {.uri="/api/v1/center/config",.method=HTTP_PUT,.handler=configuration},
        {.uri="/api/v1/center/jobs/*",.method=HTTP_GET,.handler=job_status},
        {.uri="/api/v1/api-info",.method=HTTP_GET,.handler=api_info},
        {.uri="/api/v1/files/*",.method=HTTP_GET,.handler=download},
        {.uri="/api/v1/artifacts/*",.method=HTTP_GET,.handler=download},
        {.uri="/api/v1/*",.method=HTTP_GET,.handler=generic},
        {.uri="/api/v1/*",.method=HTTP_POST,.handler=generic},
        {.uri="/api/v1/*",.method=HTTP_PUT,.handler=generic},
        {.uri="/api/v1/*",.method=HTTP_DELETE,.handler=generic},
    };
    for(unsigned i=0;i<sizeof(routes)/sizeof(routes[0]);i++)if((err=httpd_register_uri_handler(server,&routes[i])))return err;
    return xTaskCreate(worker,"center_jobs",14336,NULL,4,NULL)==pdPASS?ESP_OK:ESP_ERR_NO_MEM;
}
