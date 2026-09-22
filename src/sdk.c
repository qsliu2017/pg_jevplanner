/* JEV Choice wire protocol and blocking HTTPS transport. No planner policy.
 * No libcurl objects exist pre-fork. NOSIGNAL preserves backend handlers.
 * A synchronous libcurl DNS resolver may exceed its timeout; use an async
 * resolver build when bounded DNS cancellation is required.
 */
#include "postgres.h"

#include <curl/curl.h>
#include <math.h>
#include <stdlib.h>

#include "lib/stringinfo.h"
#include "miscadmin.h"
#include "portability/instr_time.h"
#include "utils/builtins.h"
#include "utils/json.h"
#include "utils/jsonb.h"
#include "utils/numeric.h"
#include "sdk.h"

static double
elapsed_ms(instr_time start)
{
    instr_time now;
    INSTR_TIME_SET_CURRENT(now);
    INSTR_TIME_SUBTRACT(now, start);
    return INSTR_TIME_GET_MILLISEC(now);
}

static void
check_size(StringInfo buf)
{
    if (buf->len > JEV_SDK_MAX_MESSAGE_BYTES)
        ereport(ERROR, (errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
                       errmsg("JEV SDK message exceeds 256 KiB")));
}

static void
json_string(StringInfo buf, const char *s)
{
    size_t n;
    if (s == NULL)
        elog(ERROR, "JEV SDK missing message field");
    n = strnlen(s, JEV_SDK_MAX_MESSAGE_BYTES + 1);
    /* Bound expansion before escape_json allocates. */
    if (buf->len > JEV_SDK_MAX_MESSAGE_BYTES - 2 ||
        n > (size_t) (JEV_SDK_MAX_MESSAGE_BYTES - buf->len - 2) / 6)
        ereport(ERROR, (errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED),
                       errmsg("JEV SDK message exceeds safe encoding budget")));
    escape_json(buf, s);
    check_size(buf);
}

/* Callbacks use only libc/clock operations and signal flags: never invoke
 * CHECK_FOR_INTERRUPTS, ereport, palloc or anything that can longjmp. */
typedef struct HttpBuffer
{
    char *data;
    size_t len;
    bool too_large;
    instr_time start;
    long timeout_ms;
} HttpBuffer;

static int
http_progress(void *arg, curl_off_t dt, curl_off_t dn, curl_off_t ut, curl_off_t un)
{
    HttpBuffer *b = arg;
    (void) dt; (void) dn; (void) ut; (void) un;
    return elapsed_ms(b->start) >= b->timeout_ms ||
        InterruptPending || QueryCancelPending || ProcDiePending;
}

static size_t
http_write(char *data, size_t size, size_t count, void *arg)
{
    HttpBuffer *b = arg;
    size_t n;
    if (http_progress(arg, 0, 0, 0, 0))
        return 0;
    if (size && count > SIZE_MAX / size)
    {
        b->too_large = true;
        return 0;
    }
    n = size * count;
    if (n > JEV_SDK_MAX_MESSAGE_BYTES - b->len)
    {
        b->too_large = true;
        return 0;
    }
    memcpy(b->data + b->len, data, n);
    b->len += n;
    b->data[b->len] = '\0';
    return n;
}

static void
validate_endpoint(const char *endpoint)
{
    CURLU *url = curl_url();
    char *scheme = NULL, *host = NULL, *path = NULL, *extra = NULL;
    bool valid = false;
    CURLUPart forbidden[] = {CURLUPART_USER, CURLUPART_PASSWORD, CURLUPART_QUERY, CURLUPART_FRAGMENT};
    int i;

    if (url && endpoint &&
        curl_url_set(url, CURLUPART_URL, endpoint, 0) == CURLUE_OK &&
        curl_url_get(url, CURLUPART_SCHEME, &scheme, 0) == CURLUE_OK &&
        curl_url_get(url, CURLUPART_HOST, &host, 0) == CURLUE_OK &&
        curl_url_get(url, CURLUPART_PATH, &path, 0) == CURLUE_OK)
    {
        valid = strcmp(path, "/v1/systemone") == 0 &&
            (strcmp(scheme, "https") == 0 ||
             (strcmp(scheme, "http") == 0 &&
              (strcmp(host, "127.0.0.1") == 0 || strcmp(host, "[::1]") == 0)));
        for (i = 0; i < (int) lengthof(forbidden); i++)
        {
            if (curl_url_get(url, forbidden[i], &extra, 0) == CURLUE_OK)
                valid = false;
            curl_free(extra);
            extra = NULL;
        }
    }
    curl_free(scheme); curl_free(host); curl_free(path);
    if (url) curl_url_cleanup(url);
    if (!valid)
        ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                       errmsg("JEV SDK endpoint must be HTTPS /v1/systemone"),
                       errdetail("HTTP is allowed only for literal loopback addresses 127.0.0.1 and [::1]; credentials, query strings and fragments are forbidden.")));
}

static char *
post_request(const JevSdkConfig *config, const char *request, int length,
             instr_time start)
{
    const char *key = config->api_key;
    char *authorization;
    char *result;
    CURL *easy;
    struct curl_slist *headers = NULL, *added;
    CURLcode status = CURLE_FAILED_INIT;
    HttpBuffer body = {0};
    long http_status = 0;
    long timeout;
    size_t keylen;
    int i;
    bool setup_ok = true;

    CHECK_FOR_INTERRUPTS();
    validate_endpoint(config->endpoint);
    if (!key || !*key)
        ereport(ERROR, (errcode(ERRCODE_INVALID_AUTHORIZATION_SPECIFICATION),
                       errmsg("JEV SDK requires an API credential")));
    keylen = strnlen(key, 8193);
    if (keylen > 8192)
        elog(ERROR, "JEV SDK invalid API credential");
    for (i = 0; i < (int) keylen; i++)
        if ((unsigned char) key[i] <= 32 || (unsigned char) key[i] >= 127)
            elog(ERROR, "JEV SDK invalid API credential");
    if (config->trace)
        config->trace(JEV_SDK_TRACE_REQUEST, 0, request, length, config->trace_arg);
    authorization = palloc(keylen + 23);
    snprintf(authorization, keylen + 23, "Authorization: Bearer %s", key);
    /* Reserve PG memory before acquiring external resources. */
    result = palloc(JEV_SDK_MAX_MESSAGE_BYTES + 1);
    timeout = (long) floor(config->timeout_ms - elapsed_ms(start));
    if (timeout <= 0)
    {
        memset(authorization, 0, keylen + 23);
        pfree(authorization);
        ereport(ERROR, (errcode(ERRCODE_CONNECTION_FAILURE),
                       errmsg("JEV request failed: request timeout exceeded")));
    }
    body.start = start;
    body.timeout_ms = config->timeout_ms;
    /* No PG APIs from here until all easy/slist/malloc resources are freed. */
    body.data = malloc(JEV_SDK_MAX_MESSAGE_BYTES + 1);
    easy = curl_easy_init();
    if (!body.data || !easy)
        setup_ok = false;
    if (body.data) body.data[0] = '\0';
    headers = curl_slist_append(NULL, authorization);
    if (!headers) setup_ok = false;
    added = curl_slist_append(headers, "Content-Type: application/json");
    if (!added) setup_ok = false; else headers = added;
    added = curl_slist_append(headers, "Accept: application/json");
    if (!added) setup_ok = false; else headers = added;
    added = curl_slist_append(headers, "Expect:");
    if (!added) setup_ok = false; else headers = added;
#define SETOPT(opt, val) do { if (setup_ok && curl_easy_setopt(easy, opt, val) != CURLE_OK) setup_ok = false; } while (0)
    SETOPT(CURLOPT_URL, config->endpoint);
    SETOPT(CURLOPT_POST, 1L);
    SETOPT(CURLOPT_POSTFIELDS, request);
    SETOPT(CURLOPT_POSTFIELDSIZE, (long) length);
    SETOPT(CURLOPT_HTTPHEADER, headers);
    SETOPT(CURLOPT_FOLLOWLOCATION, 0L);
    SETOPT(CURLOPT_MAXREDIRS, 0L);
    SETOPT(CURLOPT_SSL_VERIFYPEER, 1L);
    SETOPT(CURLOPT_SSL_VERIFYHOST, 2L);
    SETOPT(CURLOPT_NOSIGNAL, 1L);
    SETOPT(CURLOPT_TIMEOUT_MS, timeout);
    SETOPT(CURLOPT_CONNECTTIMEOUT_MS, timeout);
    SETOPT(CURLOPT_WRITEFUNCTION, http_write);
    SETOPT(CURLOPT_WRITEDATA, &body);
    SETOPT(CURLOPT_XFERINFOFUNCTION, http_progress);
    SETOPT(CURLOPT_XFERINFODATA, &body);
    SETOPT(CURLOPT_NOPROGRESS, 0L);
    SETOPT(CURLOPT_NETRC, (long) CURL_NETRC_IGNORED);
    SETOPT(CURLOPT_PROXY, "");
#undef SETOPT
    if (setup_ok)
    {
        status = curl_easy_perform(easy); /* Exactly one attempt; no retry. */
        if (curl_easy_getinfo(easy, CURLINFO_RESPONSE_CODE, &http_status) != CURLE_OK)
            status = CURLE_HTTP_RETURNED_ERROR;
    }
    if (body.data)
        memcpy(result, body.data, body.len + 1);
    if (easy) curl_easy_cleanup(easy);
    curl_slist_free_all(headers);
    free(body.data);
    memset(authorization, 0, keylen + 23);
    pfree(authorization);
    CHECK_FOR_INTERRUPTS();
    /* Trace error bodies too, but never call application code from curl. */
    if (config->trace && (http_status != 0 || body.len != 0))
        config->trace(JEV_SDK_TRACE_RESPONSE, http_status, result, body.len,
                      config->trace_arg);
    if (body.too_large)
        elog(ERROR, "JEV response exceeds 256 KiB");
    if (!setup_ok || status != CURLE_OK || elapsed_ms(start) >= config->timeout_ms)
        ereport(ERROR, (errcode(ERRCODE_CONNECTION_FAILURE),
                       errmsg("JEV request failed"),
                       errdetail("Transport status %d.", (int) status)));
    if (http_status < 200 || http_status >= 300)
        ereport(ERROR, (errcode(ERRCODE_CONNECTION_FAILURE),
                       errmsg("JEV returned HTTP status %ld", http_status)));
    if (!body.len || memchr(result, '\0', body.len) != NULL)
        elog(ERROR, "JEV SDK invalid response body");
    return result;
}

static JsonbValue *
object_field(JsonbContainer *object, const char *name)
{
    JsonbValue key;
    JsonbValue *value;
    if (!JsonContainerIsObject(object))
        elog(ERROR, "JEV SDK invalid response object");
    key.type = jbvString;
    key.val.string.val = (char *) name;
    key.val.string.len = strlen(name);
    value = findJsonbValueFromContainer(object, JB_FOBJECT, &key);
    if (!value)
        elog(ERROR, "JEV SDK missing required response field");
    return value;
}

static JsonbContainer *
child_object(JsonbContainer *parent, const char *name)
{
    JsonbValue *v = object_field(parent, name);
    if (v->type != jbvBinary || !JsonContainerIsObject(v->val.binary.data))
        elog(ERROR, "JEV SDK invalid answer structure");
    {
        JsonbContainer *result = v->val.binary.data;
        pfree(v);
        return result;
    }
}

static JevSdkChoice
parse_choice(const char *response, const char *question_id, int count)
{
    Jsonb *volatile json = NULL;
    JsonbContainer *answer;
    JsonbValue *choice, *confidence, *type;
    MemoryContext oldcontext = CurrentMemoryContext;
    JevSdkChoice result = {-1, 0};
    int i;

    PG_TRY();
    {
        json = DatumGetJsonbP(DirectFunctionCall1(jsonb_in, CStringGetDatum(response)));
    }
    PG_CATCH();
    {
        ErrorData *error;

        MemoryContextSwitchTo(oldcontext);
        error = CopyErrorData();
        FlushErrorState();
        if (error->sqlerrcode == ERRCODE_QUERY_CANCELED ||
            error->sqlerrcode == ERRCODE_ADMIN_SHUTDOWN ||
            error->sqlerrcode == ERRCODE_CRASH_SHUTDOWN ||
            error->sqlerrcode == ERRCODE_OUT_OF_MEMORY)
            ReThrowError(error);
        FreeErrorData(error);
        /* Never expose provider response bytes in the parser's error detail. */
        elog(ERROR, "JEV response is not valid JSON");
    }
    PG_END_TRY();
    answer = child_object(child_object(&json->root, "answers"), question_id);
    type = object_field(answer, "type");
    if (type->type != jbvString || type->val.string.len != 6 ||
        memcmp(type->val.string.val, "choice", 6) != 0)
        elog(ERROR, "JEV answer is not a Choice");
    pfree(type);
    choice = object_field(answer, "choice");
    confidence = object_field(answer, "confidence");
    if (choice->type != jbvString || confidence->type != jbvNumeric)
        elog(ERROR, "JEV SDK invalid choice or confidence type");
    result.confidence = DatumGetFloat8(DirectFunctionCall1(numeric_float8, NumericGetDatum(confidence->val.numeric)));
    if (!isfinite(result.confidence) || result.confidence < 0 || result.confidence > 1)
        elog(ERROR, "JEV confidence is outside [0,1]");
    for (i = 0; i < count; i++)
    {
        char label[8];
        int n = snprintf(label, sizeof(label), "p%d", i);
        if (choice->val.string.len == n && memcmp(choice->val.string.val, label, n) == 0)
        {
            result.index = i;
            break;
        }
    }
    if (result.index < 0)
        elog(ERROR, "JEV selected an unknown alternative");
    pfree(choice);
    pfree(confidence);
    pfree(json);
    return result;
}

JevSdkChoice
jev_sdk_choice(const JevSdkConfig *config, const char *state_json,
               const char *question_id, const char *instructions,
               const char *const *options, int count)
{
    StringInfoData request;
    instr_time start;
    char *response;
    JevSdkChoice result;
    size_t state_len;
    int i;

    CHECK_FOR_INTERRUPTS();
    INSTR_TIME_SET_CURRENT(start);
    if (!config || config->timeout_ms <= 0 || !state_json || !question_id ||
        !*question_id || !instructions || !options ||
        count < 1 || count > JEV_SDK_MAX_OPTIONS)
        elog(ERROR, "JEV SDK invalid Choice request");
    state_len = strnlen(state_json, JEV_SDK_MAX_MESSAGE_BYTES + 1);
    if (state_len > JEV_SDK_MAX_MESSAGE_BYTES)
        elog(ERROR, "JEV SDK state exceeds 256 KiB");
    initStringInfo(&request);
    appendStringInfoString(&request, "{\"state\":");
    appendBinaryStringInfo(&request, state_json, (int) state_len);
    check_size(&request);
    appendStringInfoString(&request, ",\"model\":");
    json_string(&request, config->model);
    appendStringInfoString(&request, ",\"questions\":{");
    json_string(&request, question_id);
    appendStringInfoString(&request, ":{\"type\":\"choice\",\"instructions\":");
    json_string(&request, instructions);
    appendStringInfoString(&request, ",\"criteria\":{");
    for (i = 0; i < count; i++)
    {
        appendStringInfo(&request, "%s\"p%d\":", i ? "," : "", i);
        json_string(&request, options[i]);
    }
    appendStringInfoString(&request, "}}}}");
    check_size(&request);
    response = post_request(config, request.data, request.len, start);
    result = parse_choice(response, question_id, count);
    pfree(response);
    pfree(request.data);
    CHECK_FOR_INTERRUPTS();
    return result;
}
