#ifndef JEV_SDK_H
#define JEV_SDK_H

#include <stddef.h>

#define JEV_SDK_MAX_OPTIONS 255
#define JEV_SDK_MAX_MESSAGE_BYTES (256 * 1024)

/* Optional body-only tracing. Called outside libcurl, with no live transport
 * resources. No HTTP headers are passed. Response bytes may be
 * non-JSON or partial on failure; length, not a NUL terminator, is authoritative.
 * The caller owns logging, redaction and access policy. */
typedef enum JevSdkTraceEvent
{
    JEV_SDK_TRACE_REQUEST,
    JEV_SDK_TRACE_RESPONSE
} JevSdkTraceEvent;

typedef void (*JevSdkTrace)(JevSdkTraceEvent event, long http_status,
                            const char *body, size_t length, void *arg);

/* Zero-initialize before assigning request configuration. */
typedef struct JevSdkConfig
{
    const char *endpoint;
    const char *model;
    const char *api_key;
    long timeout_ms;
    JevSdkTrace trace;
    void *trace_arg;
} JevSdkConfig;

typedef struct JevSdkChoice
{
    int index; /* Index in the supplied options array. */
    double confidence;
} JevSdkChoice;

/* One synchronous Choice request; no retries or application-specific policy.
 * state_json is a serialized JSON value. The other strings are JSON-escaped.
 * Options receive opaque wire IDs p0, p1, ...; the response ID is validated.
 * Uses PostgreSQL allocation/error handling, but no planner types or state.
 * timeout_ms bounds this request, including encoding and transport work.
 */
extern JevSdkChoice jev_sdk_choice(const JevSdkConfig *config,
                                  const char *state_json,
                                  const char *question_id,
                                  const char *instructions,
                                  const char *const *options, int count);

#endif
