/*
 * FedTermRDP Native helper - framebuffer/input bridge for FedTermHub.
 *
 * Uses libfreerdp3 directly and never creates/reparents an X11 window.
 * The decoded framebuffer is copied to a shared memfd supplied by the parent.
 * Input is received through a private pipe. This design works on X11 and
 * Wayland because the UI is rendered by GTK, not by xfreerdp.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <poll.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <freerdp/client.h>
#include <freerdp/client/cmdline.h>
#include <freerdp/constants.h>
#include <freerdp/freerdp.h>
#include <freerdp/event.h>
#include <freerdp/channels/disp.h>
#include <freerdp/client/disp.h>
#include <freerdp/channels/cliprdr.h>
#include <freerdp/client/cliprdr.h>
#include <freerdp/client/client_cliprdr_file.h>
#include <freerdp/utils/cliprdr_utils.h>
#include <freerdp/gdi/gdi.h>
#include <freerdp/input.h>
#include <freerdp/locale/keyboard.h>
#include <freerdp/log.h>
#include <winpr/assert.h>
#include <winpr/clipboard.h>
#include <winpr/crt.h>
#include <winpr/synch.h>

#define FTH_MAGIC 0x46544852u /* FTHR */
#define FTH_VERSION 1u
#define FTH_HEADER_SIZE 1024u
#define TAG CLIENT_TAG("fedtermhub-native")

/* --- Clipboard bridge (cliprdr) -------------------------------------------
 * Second shared memfd, independent from the frame buffer, used to move text
 * clipboard content between this process (native RDP channel thread) and
 * FedTermHub's GTK main loop. Two one-way lanes, each guarded by its own
 * odd/even seqlock counter exactly like FthFrameHeader.seq:
 *   host_*   : written by Python (GTK clipboard), read by us      -> server
 *   remote_* : written by us (server clipboard), read by Python   -> host
 * The host lane carries either plain UTF-8 text (host_kind == TEXT) or a
 * text/uri-list of local files dropped onto the RDP canvas (host_kind ==
 * URILIST): the latter is announced to the server as FileGroupDescriptorW and
 * the actual bytes are streamed on demand through CB_FILECONTENTS_REQUEST /
 * RESPONSE by the client-common CliprdrFileContext helper. The remote lane
 * carries either text or, when files are copied inside the remote session and
 * FUSE support is available, a text/uri-list pointing into the CliprdrFileContext
 * mount (remote_kind == URILIST): reading those paths streams the bytes from
 * the server on demand. */
#define FTH_CLIP_MAGIC 0x46544843u /* FTHC */
#define FTH_CLIP_VERSION 3u
#define FTH_CLIP_CAPACITY (256u * 1024u) /* per-direction payload cap, UTF-8 bytes */
#define FTH_CF_UNICODETEXT 13u
#define FTH_CF_TEXT 1u
#define FTH_CLIP_KIND_TEXT 0u
#define FTH_CLIP_KIND_URILIST 1u

typedef struct
{
    uint32_t magic;
    uint32_t version;
    _Atomic uint32_t host_seq;   /* bumped by Python on every clipboard update (seqlock) */
    uint32_t host_available;     /* 1 if host_data currently holds usable text */
    uint32_t host_len;           /* valid bytes in host_data, meaningful when host_seq is even */
    _Atomic uint32_t remote_seq; /* bumped by us when remote_data holds a fresh paste (seqlock) */
    uint32_t remote_len;         /* valid bytes in remote_data, meaningful when remote_seq is even */
    uint32_t capacity;           /* == FTH_CLIP_CAPACITY, sanity check for both sides */
    uint32_t host_kind;          /* FTH_CLIP_KIND_*, meaningful when host_seq is even */
    uint32_t remote_kind;        /* FTH_CLIP_KIND_*, meaningful when remote_seq is even */
    uint32_t reserved[6];
} FthClipHeader;

_Static_assert(sizeof(FthClipHeader) <= 128, "clip header must stay well under one page");

/* Shared memory states. */
enum
{
    FTH_STATE_IDLE = 0,
    FTH_STATE_CONNECTING = 1,
    FTH_STATE_CONNECTED = 2,
    FTH_STATE_DISCONNECTED = 3,
    FTH_STATE_ERROR = 4,
    FTH_STATE_STOPPING = 5
};

/* Fixed-size input messages written by Python. */
enum
{
    FTH_CMD_STOP = 1,
    FTH_CMD_MOUSE_MOVE = 2,
    FTH_CMD_MOUSE_BUTTON = 3,
    FTH_CMD_MOUSE_WHEEL = 4,
    FTH_CMD_KEY_XKB = 5,
    FTH_CMD_UNICODE = 6,
    FTH_CMD_FOCUS = 7,
    FTH_CMD_REFRESH = 8,
    FTH_CMD_RESIZE = 9
};

typedef struct
{
    uint32_t type;
    uint32_t a;
    uint32_t b;
    uint32_t c;
} FthCommand;

typedef struct
{
    uint32_t magic;
    uint32_t version;
    _Atomic uint32_t seq;
    uint32_t state;
    uint32_t width;
    uint32_t height;
    uint32_t stride;
    uint32_t capacity;
    uint32_t error_code;
    uint32_t logon_data;
    uint32_t logon_type;
    uint32_t reserved[5];
    char message[512];
    uint8_t padding[448];
} FthFrameHeader;

_Static_assert(sizeof(FthFrameHeader) == FTH_HEADER_SIZE, "frame header must be 1024 bytes");

typedef struct
{
    int frame_fd;
    int command_fd;
    size_t mapping_size;
    uint8_t* mapping;
    FthFrameHeader* header;
    uint8_t* pixels;
    volatile sig_atomic_t stop_requested;
    uint32_t keyboard_layout;
    DispClientContext* disp;
    uint32_t pending_width;
    uint32_t pending_height;
    uint32_t pending_scale;

    /* Clipboard bridge */
    int clip_fd;                    /* -1 if the parent did not pass --clip-fd (clipboard disabled) */
    size_t clip_mapping_size;
    uint8_t* clip_mapping;
    FthClipHeader* clip_header;
    uint8_t* clip_host_data;        /* Python -> us */
    uint8_t* clip_remote_data;      /* us -> Python */
    uint32_t clip_last_host_seq;    /* last host_seq we already advertised to the server */
    uint32_t clip_requested_format; /* format id of our pending ClientFormatDataRequest */
    uint32_t clip_server_fgd;       /* id di FileGroupDescriptorW COME ANNUNCIATO DAL SERVER
                                     * (registrato dinamicamente lato server: non coincide con
                                     * l'id locale winpr, va usato nelle richieste al server) */
    CliprdrClientContext* cliprdr;
    CliprdrFileContext* clip_file;  /* client-common helper: serve i CB_FILECONTENTS_* */
    wClipboard* clip_system;        /* winpr: converte text/uri-list in FileGroupDescriptorW */
} NativeState;

typedef struct
{
    rdpClientContext common;
    NativeState* state;
} NativeContext;

static NativeState* g_state = NULL;

static void state_message(NativeState* state, uint32_t code, const char* message)
{
    if (!state || !state->header)
        return;
    state->header->state = code;
    if (message)
        snprintf(state->header->message, sizeof(state->header->message), "%s", message);
    else
        state->header->message[0] = '\0';
}

static void signal_handler(int sig)
{
    (void)sig;
    if (g_state)
        g_state->stop_requested = 1;
}

static BOOL native_begin_paint(rdpContext* context)
{
    rdpGdi* gdi = context ? context->gdi : NULL;
    if (!gdi || !gdi->primary || !gdi->primary->hdc || !gdi->primary->hdc->hwnd ||
        !gdi->primary->hdc->hwnd->invalid)
        return TRUE;
    gdi->primary->hdc->hwnd->invalid->null = TRUE;
    return TRUE;
}

static BOOL copy_frame(rdpContext* context)
{
    NativeContext* nctx = (NativeContext*)context;
    NativeState* state = nctx ? nctx->state : NULL;
    rdpGdi* gdi = context ? context->gdi : NULL;
    if (!state || !state->header || !gdi || !gdi->primary_buffer)
        return FALSE;

    const uint32_t width = (uint32_t)gdi->width;
    const uint32_t height = (uint32_t)gdi->height;
    const uint32_t dst_stride = width * 4u;
    const uint64_t required = (uint64_t)dst_stride * height;
    if ((width == 0) || (height == 0) || (required > state->header->capacity))
    {
        state->header->error_code = ERROR_INSUFFICIENT_BUFFER;
        state_message(state, FTH_STATE_ERROR, "Framebuffer remoto troppo grande per la memoria condivisa");
        return FALSE;
    }

    atomic_fetch_add_explicit(&state->header->seq, 1, memory_order_acq_rel); /* odd */
    state->header->width = width;
    state->header->height = height;
    state->header->stride = dst_stride;
    for (uint32_t y = 0; y < height; y++)
    {
        const uint8_t* src = gdi->primary_buffer + ((size_t)y * (size_t)gdi->stride);
        uint8_t* dst = state->pixels + ((size_t)y * (size_t)dst_stride);
        memcpy(dst, src, dst_stride);
    }
    atomic_thread_fence(memory_order_release);
    atomic_fetch_add_explicit(&state->header->seq, 1, memory_order_acq_rel); /* even */
    return TRUE;
}

static BOOL native_end_paint(rdpContext* context)
{
    return copy_frame(context);
}

static BOOL native_desktop_resize(rdpContext* context)
{
    if (!context || !context->gdi || !context->settings)
        return FALSE;
    const uint32_t width = freerdp_settings_get_uint32(context->settings, FreeRDP_DesktopWidth);
    const uint32_t height = freerdp_settings_get_uint32(context->settings, FreeRDP_DesktopHeight);
    if (!gdi_resize(context->gdi, width, height))
        return FALSE;
    return copy_frame(context);
}

static int native_logon_error_info(freerdp* instance, UINT32 data, UINT32 type)
{
    if (!instance || !instance->context)
        return -1;
    NativeContext* nctx = (NativeContext*)instance->context;
    if (nctx->state && nctx->state->header)
    {
        nctx->state->header->logon_data = data;
        nctx->state->header->logon_type = type;
        nctx->state->header->reserved[0]++; /* notification sequence */
        const char* ds = freerdp_get_logon_error_info_data(data);
        const char* ts = freerdp_get_logon_error_info_type(type);
        snprintf(nctx->state->header->message, sizeof(nctx->state->header->message),
                 "Windows: %s [%s]", ds ? ds : "evento sessione", ts ? ts : "sconosciuto");
    }
    return 1;
}

static BOOL native_pre_connect(freerdp* instance)
{
    if (!instance || !instance->context || !instance->context->settings)
        return FALSE;
    rdpSettings* settings = instance->context->settings;
    if (!freerdp_settings_set_uint32(settings, FreeRDP_OsMajorType, OSMAJORTYPE_UNIX))
        return FALSE;
    if (!freerdp_settings_set_uint32(settings, FreeRDP_OsMinorType, OSMINORTYPE_NATIVE_XSERVER))
        return FALSE;
    if (!freerdp_settings_set_bool(settings, FreeRDP_SoftwareGdi, TRUE))
        return FALSE;
    /* The GTK frontend owns the window. Enable the display-control channel so
     * it can request an exact remote resolution whenever the tab or the
     * fullscreen monitor changes size. */
    if (!freerdp_settings_set_bool(settings, FreeRDP_DynamicResolutionUpdate, TRUE))
        return FALSE;
    if (!freerdp_settings_set_bool(settings, FreeRDP_SupportDisplayControl, TRUE))
        return FALSE;
    if (!freerdp_settings_set_bool(settings, FreeRDP_DesktopResize, TRUE))
        return FALSE;
    return TRUE;
}

static BOOL native_post_connect(freerdp* instance)
{
    if (!instance || !instance->context)
        return FALSE;
    if (!gdi_init(instance, PIXEL_FORMAT_BGRA32))
        return FALSE;
    rdpContext* context = instance->context;
    context->update->BeginPaint = native_begin_paint;
    context->update->EndPaint = native_end_paint;
    context->update->DesktopResize = native_desktop_resize;
    NativeContext* nctx = (NativeContext*)context;
    state_message(nctx->state, FTH_STATE_CONNECTED, "Connesso - rendering nativo libfreerdp");
    return copy_frame(context);
}

static void native_post_disconnect(freerdp* instance)
{
    if (!instance || !instance->context)
        return;
    NativeContext* nctx = (NativeContext*)instance->context;
    gdi_free(instance);
    if (nctx->state && nctx->state->header->state != FTH_STATE_ERROR)
        state_message(nctx->state, FTH_STATE_DISCONNECTED, "Sessione RDP terminata");
}

static BOOL native_send_resize(NativeState* state, UINT32 width, UINT32 height, UINT32 scale)
{
    if (!state)
        return FALSE;
    if (width < DISPLAY_CONTROL_MIN_MONITOR_WIDTH)
        width = DISPLAY_CONTROL_MIN_MONITOR_WIDTH;
    if (height < DISPLAY_CONTROL_MIN_MONITOR_HEIGHT)
        height = DISPLAY_CONTROL_MIN_MONITOR_HEIGHT;
    if (width > DISPLAY_CONTROL_MAX_MONITOR_WIDTH)
        width = DISPLAY_CONTROL_MAX_MONITOR_WIDTH;
    if (height > DISPLAY_CONTROL_MAX_MONITOR_HEIGHT)
        height = DISPLAY_CONTROL_MAX_MONITOR_HEIGHT;
    if (scale < 100 || scale > 500)
        scale = 100;

    state->pending_width = width;
    state->pending_height = height;
    state->pending_scale = scale;

    if (!state->disp || !state->disp->SendMonitorLayout)
    {
        WLog_DBG(TAG, "Display-control channel not ready; queued resize to %" PRIu32
                      "x%" PRIu32, width, height);
        return TRUE;
    }

    DISPLAY_CONTROL_MONITOR_LAYOUT layout = { 0 };
    layout.Flags = DISPLAY_CONTROL_MONITOR_PRIMARY;
    layout.Left = 0;
    layout.Top = 0;
    layout.Width = width;
    layout.Height = height;
    layout.Orientation = ORIENTATION_LANDSCAPE;
    layout.DesktopScaleFactor = scale;
    layout.DeviceScaleFactor = scale;
    /* RDPEDISP expects approximate physical dimensions in millimetres.
     * 75 DPI matches the reference FreeRDP clients. */
    layout.PhysicalWidth = (UINT32)(((uint64_t)width * 254u + 375u) / 750u);
    layout.PhysicalHeight = (UINT32)(((uint64_t)height * 254u + 375u) / 750u);

    const UINT rc = state->disp->SendMonitorLayout(state->disp, 1, &layout);
    if (rc != CHANNEL_RC_OK)
    {
        WLog_WARN(TAG, "Remote display resize to %" PRIu32 "x%" PRIu32
                       " was rejected with channel code 0x%08" PRIx32, width, height, rc);
        return FALSE;
    }

    WLog_INFO(TAG, "Requested remote display resize to %" PRIu32 "x%" PRIu32,
              width, height);
    return TRUE;
}

/* ---------------------------------------------------------- clipboard bridge */

/* Reads the host (Python) clipboard mailbox with a seqlock retry, exactly
 * like the frame poller on the Python side does for pixel data. Returns TRUE
 * and fills out_data/out_len/out_available if a consistent snapshot was
 * read; FALSE if the mailbox isn't ready yet. */
static BOOL clip_read_host(NativeState* state, const uint8_t** out_data, uint32_t* out_len,
                            BOOL* out_available, uint32_t* out_kind)
{
    if (!state || !state->clip_header || !state->clip_host_data)
        return FALSE;
    for (int attempt = 0; attempt < 4; attempt++)
    {
        uint32_t seq_before = atomic_load_explicit(&state->clip_header->host_seq, memory_order_acquire);
        if (seq_before % 2 != 0)
            continue; /* Python is mid-write; retry */
        uint32_t len = state->clip_header->host_len;
        BOOL available = state->clip_header->host_available != 0;
        uint32_t kind = state->clip_header->host_kind;
        if (len > FTH_CLIP_CAPACITY)
            len = FTH_CLIP_CAPACITY;
        atomic_thread_fence(memory_order_acquire);
        uint32_t seq_after = atomic_load_explicit(&state->clip_header->host_seq, memory_order_acquire);
        if (seq_after != seq_before)
            continue; /* changed while reading; retry */
        *out_data = state->clip_host_data;
        *out_len = len;
        *out_available = available;
        if (out_kind)
            *out_kind = kind;
        return TRUE;
    }
    return FALSE;
}

/* cliprdr_file_context_init() prende possesso di cliprdr->custom per i propri
 * callback (ServerFileContentsRequest, lock/unlock): quando il file-context è
 * attivo, il NativeState va recuperato dal suo user pointer; senza
 * file-context custom punta direttamente allo stato come prima. */
static NativeState* native_state_from_cliprdr(CliprdrClientContext* context)
{
    if (!context || !context->custom)
        return NULL;
    if (g_state && g_state->clip_file && context->custom == (void*)g_state->clip_file)
        return (NativeState*)cliprdr_file_context_get_context(g_state->clip_file);
    return (NativeState*)context->custom;
}

/* TRUE se possiamo davvero offrire file al server: helper file inizializzato,
 * conversione winpr uri-list -> FileGroupDescriptorW disponibile e server che
 * ha annunciato CB_STREAM_FILECLIP_ENABLED nelle sue capability. */
static BOOL clip_file_transfer_ready(NativeState* state)
{
    return state && state->clip_file && state->clip_system &&
           ClipboardGetFormatId(state->clip_system, "FileGroupDescriptorW") != 0 &&
           cliprdr_file_context_current_flags(state->clip_file) != 0;
}

/* Publishes freshly-arrived server clipboard text into the remote mailbox for
 * Python to pick up on its next poll tick. */
static void clip_write_remote_kind(NativeState* state, const uint8_t* data, uint32_t len, uint32_t kind)
{
    if (!state || !state->clip_header || !state->clip_remote_data)
        return;
    if (len > FTH_CLIP_CAPACITY)
        len = FTH_CLIP_CAPACITY;
    atomic_fetch_add_explicit(&state->clip_header->remote_seq, 1, memory_order_acq_rel); /* odd: writing */
    if (len > 0 && data)
        memcpy(state->clip_remote_data, data, len);
    state->clip_header->remote_len = len;
    state->clip_header->remote_kind = kind;
    atomic_thread_fence(memory_order_release);
    atomic_fetch_add_explicit(&state->clip_header->remote_seq, 1, memory_order_acq_rel); /* even: valid */
}

static void clip_write_remote(NativeState* state, const uint8_t* data, uint32_t len)
{
    clip_write_remote_kind(state, data, len, FTH_CLIP_KIND_TEXT);
}

/* Converts a NUL-terminated UTF-16LE buffer (as received from the server) to
 * UTF-8 and normalises CRLF -> LF, which is what Linux/GTK text widgets and
 * VTE expect. Returns a newly malloc'd, NUL-terminated UTF-8 string, or NULL
 * on failure; *out_len excludes the terminator. */
static char* clip_wtext_to_utf8(const BYTE* wdata, size_t wlen_bytes, size_t* out_len)
{
    if (!wdata || wlen_bytes < 2)
        return NULL;
    /* Drop a trailing UTF-16 NUL terminator if present, ConvertWCharNToUtf8Alloc
     * below already NUL-terminates its own output. */
    size_t wchar_count = wlen_bytes / sizeof(WCHAR);
    if (wchar_count > 0 && ((const WCHAR*)wdata)[wchar_count - 1] == 0)
        wchar_count--;
    if (wchar_count == 0)
    {
        char* empty = calloc(1, 1);
        if (out_len)
            *out_len = 0;
        return empty;
    }
    /* WinPR helper: allocates, NUL-terminates, returns length excluding NUL via
     * out-param (API changed from the older 4-arg/SSIZE_T-return form). */
    size_t rc = 0;
    char* utf8 = ConvertWCharNToUtf8Alloc((const WCHAR*)wdata, wchar_count, &rc);
    if (!utf8)
        return NULL;
    /* CRLF -> LF, in place (result is always <= input length). */
    size_t write_pos = 0;
    for (size_t i = 0; i < (size_t)rc; i++)
    {
        if (utf8[i] == '\r' && i + 1 < (size_t)rc && utf8[i + 1] == '\n')
            continue;
        utf8[write_pos++] = utf8[i];
    }
    utf8[write_pos] = '\0';
    if (out_len)
        *out_len = write_pos;
    return utf8;
}

/* Converts host UTF-8 text (LF line endings) to a NUL-terminated UTF-16LE
 * buffer with CRLF line endings, as CF_UNICODETEXT requires on the wire.
 * Returns a newly malloc'd buffer sized *out_bytes (including the trailing
 * UTF-16 NUL), or NULL on failure. */
static WCHAR* clip_utf8_to_wtext(const char* utf8, size_t utf8_len, size_t* out_bytes)
{
    if (!utf8)
        return NULL;
    /* Expand LF -> CRLF first so the size we allocate already accounts for it. */
    char* crlf = malloc(utf8_len * 2 + 1);
    if (!crlf)
        return NULL;
    size_t crlf_len = 0;
    for (size_t i = 0; i < utf8_len; i++)
    {
        if (utf8[i] == '\n' && (i == 0 || utf8[i - 1] != '\r'))
            crlf[crlf_len++] = '\r';
        crlf[crlf_len++] = utf8[i];
    }
    crlf[crlf_len] = '\0';

    size_t wchar_count = 0;
    WCHAR* wtext = ConvertUtf8NToWCharAlloc(crlf, crlf_len, &wchar_count);
    free(crlf);
    if (!wtext)
        return NULL;
    /* ConvertUtf8NToWCharAlloc NUL-terminates and wchar_count excludes it. */
    if (out_bytes)
        *out_bytes = (wchar_count + 1) * sizeof(WCHAR);
    return wtext;
}

/* Annuncia al server il contenuto corrente della mailbox host: testo come
 * CF_UNICODETEXT, una uri-list (file trascinati sul canvas) come
 * FileGroupDescriptorW se il trasferimento file è negoziabile. Una uri-list
 * senza supporto file (server o winpr) produce una format list vuota. */
static UINT clip_send_host_format_list(NativeState* state)
{
    if (!state || !state->cliprdr || !state->cliprdr->ClientFormatList)
        return CHANNEL_RC_OK;

    const uint8_t* data = NULL;
    uint32_t len = 0;
    BOOL available = FALSE;
    uint32_t kind = FTH_CLIP_KIND_TEXT;
    CLIPRDR_FORMAT format = { 0 };
    CLIPRDR_FORMAT_LIST formatList = { 0 };
    if (clip_read_host(state, &data, &len, &available, &kind) && available && len > 0)
    {
        if (kind == FTH_CLIP_KIND_URILIST)
        {
            if (clip_file_transfer_ready(state))
            {
                format.formatId = ClipboardGetFormatId(state->clip_system, "FileGroupDescriptorW");
                format.formatName = (char*)"FileGroupDescriptorW";
                formatList.numFormats = 1;
                formatList.formats = &format;
            }
            else
            {
                WLog_WARN(TAG, "File drop ignorato: il server non supporta il trasferimento file via cliprdr");
                /* Anche su stderr: il genitore Python inoltra stdout/stderr nel
                 * log della scheda, quindi l'utente vede davvero il motivo
                 * invece di un drop che sembra semplicemente non funzionare. */
                fprintf(stderr,
                        "[FedTermHub] Il server RDP non consente il trasferimento file via clipboard: "
                        "usa una \"Cartella condivisa\" nelle impostazioni della sessione.\n");
                fflush(stderr);
            }
        }
        else
        {
            format.formatId = FTH_CF_UNICODETEXT;
            format.formatName = NULL;
            formatList.numFormats = 1;
            formatList.formats = &format;
        }
    }
    return state->cliprdr->ClientFormatList(state->cliprdr, &formatList);
}

static UINT native_cliprdr_server_capabilities(CliprdrClientContext* context,
                                                 const CLIPRDR_CAPABILITIES* capabilities)
{
    if (!context || !capabilities)
        return ERROR_INVALID_PARAMETER;
    NativeState* state = native_state_from_cliprdr(context);
    if (!state)
        return ERROR_INVALID_PARAMETER;
    if (state->clip_file)
    {
        if (!cliprdr_file_context_remote_set_flags(state->clip_file, 0))
            return ERROR_INTERNAL_ERROR;
        const BYTE* capsPtr = (const BYTE*)capabilities->capabilitySets;
        for (UINT32 i = 0; capsPtr && i < capabilities->cCapabilitiesSets; i++)
        {
            const CLIPRDR_CAPABILITY_SET* caps = (const CLIPRDR_CAPABILITY_SET*)capsPtr;
            if (caps->capabilitySetType == CB_CAPSTYPE_GENERAL)
            {
                const CLIPRDR_GENERAL_CAPABILITY_SET* generalCaps =
                    (const CLIPRDR_GENERAL_CAPABILITY_SET*)caps;
                if (!cliprdr_file_context_remote_set_flags(state->clip_file, generalCaps->generalFlags))
                    return ERROR_INTERNAL_ERROR;
            }
            capsPtr += caps->capabilitySetLength;
        }
    }
    return CHANNEL_RC_OK;
}

static UINT native_cliprdr_monitor_ready(CliprdrClientContext* context, const CLIPRDR_MONITOR_READY* monitorReady)
{
    (void)monitorReady;
    if (!context)
        return ERROR_INVALID_PARAMETER;
    NativeState* state = native_state_from_cliprdr(context);
    if (!state)
        return ERROR_INVALID_PARAMETER;

    /* MS-RDPECLIP: il client DEVE annunciare le proprie capability prima di
     * qualsiasi format list, altrimenti molti server ignorano il canale. */
    CLIPRDR_GENERAL_CAPABILITY_SET generalCapabilitySet = { 0 };
    generalCapabilitySet.capabilitySetType = CB_CAPSTYPE_GENERAL;
    generalCapabilitySet.capabilitySetLength = 12;
    generalCapabilitySet.version = CB_CAPS_VERSION_2;
    generalCapabilitySet.generalFlags = CB_USE_LONG_FORMAT_NAMES;
    if (state->clip_file)
        generalCapabilitySet.generalFlags |= cliprdr_file_context_current_flags(state->clip_file);
    CLIPRDR_CAPABILITIES capabilities = { 0 };
    capabilities.cCapabilitiesSets = 1;
    capabilities.capabilitySets = (CLIPRDR_CAPABILITY_SET*)&generalCapabilitySet;
    if (context->ClientCapabilities)
    {
        const UINT caps_rc = context->ClientCapabilities(context, &capabilities);
        if (caps_rc != CHANNEL_RC_OK)
            return caps_rc;
    }

    /* Advertise whatever the host clipboard currently holds, so a paste
     * inside the remote session works immediately without waiting for the
     * next local clipboard change. */
    if (state->clip_header)
        state->clip_last_host_seq = atomic_load_explicit(&state->clip_header->host_seq, memory_order_acquire);
    return clip_send_host_format_list(state);
}

static UINT native_cliprdr_server_format_list(CliprdrClientContext* context, const CLIPRDR_FORMAT_LIST* formatList)
{
    if (!context || !formatList)
        return ERROR_INVALID_PARAMETER;
    NativeState* state = native_state_from_cliprdr(context);
    if (!state)
        return ERROR_INVALID_PARAMETER;

    BOOL has_text = FALSE;
    UINT32 chosen = 0;
    /* File copiati dentro la sessione Windows: il server annuncia
     * FileGroupDescriptorW. Possiamo esporli localmente solo se
     * CliprdrFileContext ha il supporto FUSE compilato, altrimenti non
     * esisterebbero percorsi leggibili da incollare in Nautilus. */
    const UINT32 fgd_fmt =
        state->clip_system ? ClipboardGetFormatId(state->clip_system, "FileGroupDescriptorW") : 0;
    BOOL has_files = FALSE;
    UINT32 server_fgd = 0;
    for (UINT32 i = 0; i < formatList->numFormats; i++)
    {
        const UINT32 id = formatList->formats[i].formatId;
        const char* name = formatList->formats[i].formatName;
        if (id == FTH_CF_UNICODETEXT)
        {
            chosen = FTH_CF_UNICODETEXT;
            has_text = TRUE;
        }
        if (id == FTH_CF_TEXT && !has_text)
        {
            chosen = FTH_CF_TEXT;
            has_text = TRUE;
        }
        /* FileGroupDescriptorW è un formato a nome lungo, registrato
         * dinamicamente: l'id che usa il SERVER non coincide con quello
         * locale di winpr. Va riconosciuto dal nome e va conservato l'id
         * del server, perché è quello da mettere nella FORMAT_DATA_REQUEST. */
        if (name && strcmp(name, "FileGroupDescriptorW") == 0)
        {
            has_files = TRUE;
            server_fgd = id;
        }
    }

    CLIPRDR_FORMAT_LIST_RESPONSE listResponse = { 0 };
    listResponse.common.msgFlags = CB_RESPONSE_OK;

    /* I file hanno la precedenza sul testo: quando si copia un file in
     * Explorer il server annuncia anche un fallback testuale (il percorso),
     * ma l'utente si aspetta il file, non la stringa. */
    state->clip_server_fgd = 0;
    if (has_files && state->clip_file && fgd_fmt && server_fgd &&
        cliprdr_file_context_has_local_support(state->clip_file))
    {
        const UINT notify_rc = cliprdr_file_context_notify_new_server_format_list(state->clip_file);
        if (notify_rc != CHANNEL_RC_OK)
            return notify_rc;
        state->clip_server_fgd = server_fgd;
        chosen = server_fgd; /* id del server, non quello locale */
        has_text = TRUE;     /* riusa il percorso di richiesta qui sotto */
        WLog_INFO(TAG, "Clipboard: il server offre file (formato %" PRIu32 "), li richiedo", server_fgd);
    }
    else if (has_files && !state->clip_file)
    {
        fprintf(stderr,
                "[FedTermHub] Il server offre file, ma il supporto file della clipboard non e' "
                "attivo in questa sessione: usa la \"Cartella condivisa\".\n");
        fflush(stderr);
    }
    else if (has_files && !cliprdr_file_context_has_local_support(state->clip_file))
    {
        /* Nessun modo di esporre i file localmente: spiegalo invece di
         * lasciare l'incolla misteriosamente vuoto. */
        fprintf(stderr,
                "[FedTermHub] File copiati nella sessione remota, ma questo sistema non puo' "
                "esporli negli appunti (manca il supporto FUSE di FreeRDP): usa la "
                "\"Cartella condivisa\" per portarli su Linux.\n");
        fflush(stderr);
    }

    if (!has_text)
    {
        /* Server clipboard has no text right now (e.g. a file was cut) —
         * nothing for us to mirror to the host clipboard. */
        if (context->ClientFormatListResponse)
            return context->ClientFormatListResponse(context, &listResponse);
        return CHANNEL_RC_OK;
    }

    /* Eagerly fetch the data now: as soon as something is copied inside the
     * remote session it becomes available on the host clipboard too, which
     * matches how RDP clipboard sync behaves in mstsc/xfreerdp. */
    CLIPRDR_FORMAT_DATA_REQUEST request = { 0 };
    request.requestedFormatId = chosen;
    state->clip_requested_format = chosen;
    UINT rc = CHANNEL_RC_OK;
    if (context->ClientFormatListResponse)
        rc = context->ClientFormatListResponse(context, &listResponse);
    if (rc != CHANNEL_RC_OK)
        return rc;
    if (context->ClientFormatDataRequest)
        return context->ClientFormatDataRequest(context, &request);
    return CHANNEL_RC_OK;
}

static UINT native_cliprdr_server_format_data_response(CliprdrClientContext* context,
                                                         const CLIPRDR_FORMAT_DATA_RESPONSE* response)
{
    if (!context || !response)
        return ERROR_INVALID_PARAMETER;
    NativeState* state = native_state_from_cliprdr(context);
    if (!state)
        return ERROR_INVALID_PARAMETER;
    if (response->common.msgFlags & CB_RESPONSE_FAIL)
        return CHANNEL_RC_OK;

    /* Lista file dal server: la giriamo a CliprdrFileContext, che la mappa su
     * percorsi locali (mount FUSE). Da lì ricaviamo una text/uri-list vera e
     * la passiamo a Python per metterla negli appunti GTK: leggendo quei
     * percorsi FUSE emette i CB_FILECONTENTS_REQUEST verso il server. */
    const UINT32 fgd_fmt =
        state->clip_system ? ClipboardGetFormatId(state->clip_system, "FileGroupDescriptorW") : 0;
    if (state->clip_file && fgd_fmt && state->clip_server_fgd &&
        state->clip_requested_format == state->clip_server_fgd)
    {
        const void* fgd_data = response->requestedFormatData;
        const UINT32 fgd_len = response->common.dataLen;
        if (!cliprdr_file_context_update_server_data(state->clip_file, state->clip_system,
                                                     fgd_data, fgd_len))
        {
            fprintf(stderr, "[FedTermHub] Impossibile leggere la lista file dalla sessione remota.\n");
            fflush(stderr);
            return CHANNEL_RC_OK;
        }
        /* La conversione FileGroupDescriptorW -> text/uri-list la fa winpr:
         * bisogna prima DEPOSITARE i byte grezzi nel clipboard locale con
         * l'id locale del formato, altrimenti ClipboardGetData non ha nulla
         * da convertire e restituisce NULL. */
        if (!ClipboardSetData(state->clip_system, fgd_fmt, fgd_data, fgd_len))
        {
            fprintf(stderr, "[FedTermHub] Conversione della lista file remota fallita.\n");
            fflush(stderr);
            return CHANNEL_RC_OK;
        }
        const UINT32 uri_fmt = ClipboardGetFormatId(state->clip_system, "text/uri-list");
        UINT32 uri_len = 0;
        char* uri_list =
            uri_fmt ? (char*)ClipboardGetData(state->clip_system, uri_fmt, &uri_len) : NULL;
        if (uri_list && uri_len > 0)
        {
            /* ClipboardGetData può includere il NUL finale: non deve finire
             * negli appunti dell'host. */
            while (uri_len > 0 && uri_list[uri_len - 1] == '\0')
                uri_len--;
            WLog_INFO(TAG, "Clipboard: %" PRIu32 " byte di uri-list dai file remoti", uri_len);
            clip_write_remote_kind(state, (const uint8_t*)uri_list, uri_len, FTH_CLIP_KIND_URILIST);
        }
        else
        {
            fprintf(stderr,
                    "[FedTermHub] La sessione remota ha copiato file, ma non e' stato possibile "
                    "esporli localmente (uri-list vuota): usa la \"Cartella condivisa\".\n");
            fflush(stderr);
        }
        free(uri_list);
        return CHANNEL_RC_OK;
    }

    if (state->clip_requested_format == FTH_CF_TEXT)
    {
        /* Testo ANSI da server legacy: normalizza CRLF -> LF, scarta il NUL
         * finale. I byte 0x80-0xFF vengono passati com'è (best effort). */
        const BYTE* raw = response->requestedFormatData;
        UINT32 raw_len = response->common.dataLen;
        if (raw && raw_len > 0)
        {
            char* out = malloc(raw_len + 1);
            if (out)
            {
                size_t out_len = 0;
                for (UINT32 i = 0; i < raw_len && raw[i] != '\0'; i++)
                {
                    if (raw[i] == '\r' && i + 1 < raw_len && raw[i + 1] == '\n')
                        continue;
                    out[out_len++] = (char)raw[i];
                }
                out[out_len] = '\0';
                clip_write_remote(state, (const uint8_t*)out, (uint32_t)out_len);
                free(out);
            }
        }
        return CHANNEL_RC_OK;
    }

    size_t utf8_len = 0;
    char* utf8 = clip_wtext_to_utf8(response->requestedFormatData, response->common.dataLen, &utf8_len);
    if (utf8)
    {
        clip_write_remote(state, (const uint8_t*)utf8, (uint32_t)utf8_len);
        free(utf8);
    }
    return CHANNEL_RC_OK;
}

/* Costruisce il payload FileGroupDescriptorW (wire format CLIPRDR_FILELIST)
 * dalla uri-list nella mailbox e aggiorna il CliprdrFileContext, che servirà
 * i successivi CB_FILECONTENTS_REQUEST leggendo i byte dai file locali. */
static BYTE* clip_build_file_list_payload(NativeState* state, const uint8_t* data, uint32_t len,
                                           size_t* out_bytes)
{
    BYTE* payload = NULL;
    if (!clip_file_transfer_ready(state) || !data || len == 0)
        return NULL;
    /* La mailbox non è NUL-terminata; winpr e il file-context vogliono una
     * stringa. */
    char* uri_list = malloc((size_t)len + 1);
    if (!uri_list)
        return NULL;
    memcpy(uri_list, data, len);
    uri_list[len] = '\0';

    ClipboardLock(state->clip_system);
    const UINT32 uri_fmt = ClipboardRegisterFormat(state->clip_system, "text/uri-list");
    const UINT32 fgd_fmt = ClipboardGetFormatId(state->clip_system, "FileGroupDescriptorW");
    UINT32 fgd_size = 0;
    FILEDESCRIPTORW* file_array = NULL;
    if (uri_fmt && fgd_fmt && ClipboardSetData(state->clip_system, uri_fmt, uri_list, len + 1))
        file_array = (FILEDESCRIPTORW*)ClipboardGetData(state->clip_system, fgd_fmt, &fgd_size);
    ClipboardUnlock(state->clip_system);

    if (file_array && fgd_size >= sizeof(FILEDESCRIPTORW))
    {
        const UINT32 file_count = fgd_size / sizeof(FILEDESCRIPTORW);
        const UINT32 flags = cliprdr_file_context_remote_get_flags(state->clip_file);
        UINT32 payload_len = 0;
        const UINT error =
            cliprdr_serialize_file_list_ex(flags, file_array, file_count, &payload, &payload_len);
        if (error)
        {
            WLog_ERR(TAG, "serializzazione CLIPRDR_FILELIST fallita: 0x%08X", error);
            free(payload);
            payload = NULL;
        }
        else if (!cliprdr_file_context_update_client_data(state->clip_file, uri_list, len))
        {
            WLog_ERR(TAG, "aggiornamento stream locali cliprdr fallito");
            free(payload);
            payload = NULL;
        }
        else if (out_bytes)
            *out_bytes = payload_len;
    }
    free(file_array);
    free(uri_list);
    return payload;
}

static UINT native_cliprdr_server_format_data_request(CliprdrClientContext* context,
                                                        const CLIPRDR_FORMAT_DATA_REQUEST* request)
{
    if (!context || !request)
        return ERROR_INVALID_PARAMETER;
    NativeState* state = native_state_from_cliprdr(context);
    CLIPRDR_FORMAT_DATA_RESPONSE response = { 0 };

    const uint8_t* data = NULL;
    uint32_t len = 0;
    BOOL available = FALSE;
    uint32_t kind = FTH_CLIP_KIND_TEXT;
    BYTE* payload = NULL;
    size_t payload_bytes = 0;

    if (state && clip_read_host(state, &data, &len, &available, &kind) && available && len > 0)
    {
        if (kind == FTH_CLIP_KIND_URILIST)
        {
            if (state->clip_system &&
                request->requestedFormatId ==
                    ClipboardGetFormatId(state->clip_system, "FileGroupDescriptorW"))
                payload = clip_build_file_list_payload(state, data, len, &payload_bytes);
        }
        else if (request->requestedFormatId == FTH_CF_UNICODETEXT)
        {
            payload = (BYTE*)clip_utf8_to_wtext((const char*)data, len, &payload_bytes);
        }
        else if (request->requestedFormatId == FTH_CF_TEXT)
        {
            /* Server legacy che chiede testo ANSI: degradiamo l'UTF-8 in
             * Latin-1 best effort (i byte non ASCII diventano '?'), con CRLF
             * e terminatore NUL come da CF_TEXT. */
            BYTE* ansi = malloc((size_t)len * 2 + 1);
            if (ansi)
            {
                size_t out = 0;
                for (uint32_t i = 0; i < len; i++)
                {
                    const uint8_t ch = data[i];
                    if (ch == '\n' && (i == 0 || data[i - 1] != '\r'))
                        ansi[out++] = '\r';
                    ansi[out++] = (ch < 0x80) ? ch : '?';
                }
                ansi[out++] = '\0';
                payload = ansi;
                payload_bytes = out;
            }
        }
    }

    if (payload)
    {
        response.common.msgFlags = CB_RESPONSE_OK;
        response.common.dataLen = (UINT32)payload_bytes;
        response.requestedFormatData = payload;
    }
    else
    {
        response.common.msgFlags = CB_RESPONSE_FAIL;
        response.common.dataLen = 0;
        response.requestedFormatData = NULL;
    }

    UINT rc = context->ClientFormatDataResponse ? context->ClientFormatDataResponse(context, &response)
                                                 : CHANNEL_RC_OK;
    free(payload);
    return rc;
}

/* Called on every main-loop iteration: if the host clipboard changed since
 * the last time we told the server about it, send a fresh format list. We
 * don't push the bytes here — ServerFormatDataRequest will pull them lazily
 * from the mailbox via clip_read_host() when (and if) the remote side
 * actually asks for a paste, keeping this cheap to call frequently. */
static void clip_poll_host_changes(NativeState* state)
{
    if (!state || !state->clip_header || !state->cliprdr || !state->cliprdr->ClientFormatList)
        return;
    const uint32_t seq = atomic_load_explicit(&state->clip_header->host_seq, memory_order_acquire);
    if (seq % 2 != 0 || seq == state->clip_last_host_seq)
        return;
    state->clip_last_host_seq = seq;
    (void)clip_send_host_format_list(state);
}

static void native_channel_connected(void* context, const ChannelConnectedEventArgs* e)
{
    NativeContext* nctx = (NativeContext*)context;
    if (nctx && nctx->state && e && e->name &&
        strcmp(e->name, DISP_DVC_CHANNEL_NAME) == 0)
    {
        nctx->state->disp = (DispClientContext*)e->pInterface;
        WLog_INFO(TAG, "Display-control channel connected");
        if (nctx->state->pending_width > 0 && nctx->state->pending_height > 0)
        {
            (void)native_send_resize(nctx->state, nctx->state->pending_width,
                                     nctx->state->pending_height,
                                     nctx->state->pending_scale);
        }
        return;
    }
    if (nctx && nctx->state && e && e->name && strcmp(e->name, CLIPRDR_SVC_CHANNEL_NAME) == 0)
    {
        CliprdrClientContext* cliprdr = (CliprdrClientContext*)e->pInterface;
        NativeState* state = nctx->state;
        state->cliprdr = cliprdr;
        if (cliprdr)
        {
            /* Prima il file-context (drag&drop di file verso la sessione):
             * cliprdr_file_context_init() imposta cliprdr->custom su di sé e
             * aggancia ServerFileContentsRequest/Lock/Unlock; i nostri handler
             * risalgono allo stato con native_state_from_cliprdr(). Se manca
             * qualcosa si degrada al solo testo, custom = stato come prima. */
            if (!state->clip_system)
                state->clip_system = ClipboardCreate();
            if (!state->clip_file)
                state->clip_file = cliprdr_file_context_new(state);
            BOOL file_ok = FALSE;
            if (state->clip_file && state->clip_system &&
                ClipboardGetFormatId(state->clip_system, "FileGroupDescriptorW") != 0)
            {
                file_ok = cliprdr_file_context_init(state->clip_file, cliprdr);
                if (file_ok)
                    file_ok = cliprdr_file_context_set_locally_available(state->clip_file, TRUE);
            }
            if (!file_ok)
            {
                if (state->clip_file)
                {
                    cliprdr_file_context_free(state->clip_file);
                    state->clip_file = NULL;
                }
                cliprdr->custom = state;
                WLog_WARN(TAG, "Trasferimento file cliprdr non disponibile: bridge limitato al testo");
            }
            else if (!cliprdr_file_context_has_local_support(state->clip_file))
            {
                /* Senza FUSE i file copiati NELLA sessione remota non possono
                 * essere esposti come percorsi locali: l'utente deve saperlo,
                 * altrimenti l'incolla su Linux sembra semplicemente rotto. */
                fprintf(stderr,
                        "[FedTermHub] Incolla file da Windows a Linux non disponibile (FreeRDP "
                        "senza supporto FUSE). La direzione Linux -> Windows funziona; per "
                        "l'altra usa la \"Cartella condivisa\".\n");
                fflush(stderr);
            }
            else
            {
                fprintf(stderr,
                        "[FedTermHub] Clipboard file attiva in entrambe le direzioni "
                        "(supporto FUSE presente).\n");
                fflush(stderr);
            }
            cliprdr->MonitorReady = native_cliprdr_monitor_ready;
            cliprdr->ServerCapabilities = native_cliprdr_server_capabilities;
            cliprdr->ServerFormatList = native_cliprdr_server_format_list;
            cliprdr->ServerFormatDataRequest = native_cliprdr_server_format_data_request;
            cliprdr->ServerFormatDataResponse = native_cliprdr_server_format_data_response;
        }
        WLog_INFO(TAG, "Clipboard channel connected");
        return;
    }
    freerdp_client_OnChannelConnectedEventHandler(context, e);
}

static void native_channel_disconnected(void* context, const ChannelDisconnectedEventArgs* e)
{
    NativeContext* nctx = (NativeContext*)context;
    if (nctx && nctx->state && e && e->name &&
        strcmp(e->name, DISP_DVC_CHANNEL_NAME) == 0)
    {
        nctx->state->disp = NULL;
        WLog_INFO(TAG, "Display-control channel disconnected");
        return;
    }
    if (nctx && nctx->state && e && e->name && strcmp(e->name, CLIPRDR_SVC_CHANNEL_NAME) == 0)
    {
        if (nctx->state->clip_file)
            (void)cliprdr_file_context_uninit(nctx->state->clip_file, nctx->state->cliprdr);
        nctx->state->cliprdr = NULL;
        WLog_INFO(TAG, "Clipboard channel disconnected");
        return;
    }
    freerdp_client_OnChannelDisconnectedEventHandler(context, e);
}

static BOOL native_client_new(freerdp* instance, rdpContext* context)
{
    if (!instance || !context)
        return FALSE;
    NativeContext* nctx = (NativeContext*)context;
    nctx->state = g_state;
    if (context->pubSub)
    {
        (void)PubSub_SubscribeChannelConnected(context->pubSub, native_channel_connected);
        (void)PubSub_SubscribeChannelDisconnected(context->pubSub, native_channel_disconnected);
    }
    instance->PreConnect = native_pre_connect;
    instance->PostConnect = native_post_connect;
    instance->PostDisconnect = native_post_disconnect;
    instance->LogonErrorInfo = native_logon_error_info;
    return TRUE;
}

static void native_client_free(freerdp* instance, rdpContext* context)
{
    (void)instance;
    if (context && context->pubSub)
    {
        (void)PubSub_UnsubscribeChannelConnected(context->pubSub, native_channel_connected);
        (void)PubSub_UnsubscribeChannelDisconnected(context->pubSub, native_channel_disconnected);
    }
}

static BOOL native_global_init(void)
{
    return TRUE;
}

static void native_global_uninit(void)
{
}

static int native_client_start(rdpContext* context)
{
    (void)context;
    return 0;
}

static int native_client_stop(rdpContext* context)
{
    (void)context;
    return 0;
}

static void entry_points(RDP_CLIENT_ENTRY_POINTS* ep)
{
    memset(ep, 0, sizeof(*ep));
    ep->Version = RDP_CLIENT_INTERFACE_VERSION;
    ep->Size = sizeof(RDP_CLIENT_ENTRY_POINTS_V1);
    ep->GlobalInit = native_global_init;
    ep->GlobalUninit = native_global_uninit;
    ep->ContextSize = sizeof(NativeContext);
    ep->ClientNew = native_client_new;
    ep->ClientFree = native_client_free;
    ep->ClientStart = native_client_start;
    ep->ClientStop = native_client_stop;
}

static int read_config_argv(int fd, char*** out_argv, int* out_argc, char** out_storage,
                            size_t* out_storage_size)
{
    struct stat st;
    if (fstat(fd, &st) != 0 || st.st_size <= 0 || st.st_size > (1024 * 1024))
        return -1;
    char* storage = calloc(1, (size_t)st.st_size + 2);
    if (!storage)
        return -1;
    if (lseek(fd, 0, SEEK_SET) < 0)
    {
        free(storage);
        return -1;
    }
    ssize_t got = read(fd, storage, (size_t)st.st_size);
    if (got <= 0)
    {
        free(storage);
        return -1;
    }
    storage[got] = '\0';

    int count = 1;
    for (ssize_t i = 0; i < got; i++)
        if (storage[i] == '\n')
            count++;
    char** argv = calloc((size_t)count + 2, sizeof(char*));
    if (!argv)
    {
        explicit_bzero(storage, (size_t)st.st_size + 2);
        free(storage);
        return -1;
    }
    int argc = 0;
    argv[argc++] = strdup("fedtermrdp-native");
    char* save = NULL;
    for (char* line = strtok_r(storage, "\n", &save); line; line = strtok_r(NULL, "\n", &save))
    {
        while (*line == ' ' || *line == '\t' || *line == '\r')
            line++;
        size_t len = strlen(line);
        while (len > 0 && (line[len - 1] == '\r' || line[len - 1] == ' ' || line[len - 1] == '\t'))
            line[--len] = '\0';
        if (*line)
            argv[argc++] = line;
    }
    argv[argc] = NULL;
    *out_argv = argv;
    *out_argc = argc;
    *out_storage = storage;
    *out_storage_size = (size_t)st.st_size + 2;
    return 0;
}

static void process_command(freerdp* instance, const FthCommand* cmd)
{
    if (!instance || !instance->context || !instance->context->input || !cmd)
        return;
    rdpInput* input = instance->context->input;
    const UINT16 x = (UINT16)(cmd->b > UINT16_MAX ? UINT16_MAX : cmd->b);
    const UINT16 y = (UINT16)(cmd->c > UINT16_MAX ? UINT16_MAX : cmd->c);

    switch (cmd->type)
    {
        case FTH_CMD_STOP:
            if (g_state)
                g_state->stop_requested = 1;
            freerdp_abort_connect_context(instance->context);
            break;
        case FTH_CMD_MOUSE_MOVE:
            (void)freerdp_input_send_mouse_event(input, PTR_FLAGS_MOVE, (UINT16)cmd->a, (UINT16)cmd->b);
            break;
        case FTH_CMD_MOUSE_BUTTON:
        {
            UINT16 flag = 0;
            if (cmd->a == 1)
                flag = PTR_FLAGS_BUTTON1;
            else if (cmd->a == 2)
                flag = PTR_FLAGS_BUTTON3;
            else if (cmd->a == 3)
                flag = PTR_FLAGS_BUTTON2;
            if (cmd->b)
                flag |= PTR_FLAGS_DOWN;
            (void)freerdp_input_send_mouse_event(input, flag, (UINT16)(cmd->c >> 16), (UINT16)(cmd->c & 0xFFFF));
            break;
        }
        case FTH_CMD_MOUSE_WHEEL:
        {
            int32_t delta = (int32_t)cmd->a;
            UINT16 flags = PTR_FLAGS_WHEEL;
            UINT16 amount = (UINT16)(abs(delta) > 0xFF ? 0xFF : abs(delta));
            if (delta < 0)
            {
                /* MS-RDPBCGR 2.2.8.1.1.3.1.1: la rotazione è un valore a 9 bit
                 * in complemento a due; con PTR_FLAGS_WHEEL_NEGATIVE gli 8 bit
                 * bassi devono contenere il complemento (per -120 -> 0x88),
                 * come fanno i client FreeRDP di riferimento. */
                flags |= PTR_FLAGS_WHEEL_NEGATIVE;
                amount = (UINT16)((0x100u - amount) & 0xFFu);
            }
            flags |= amount;
            (void)freerdp_input_send_mouse_event(input, flags, x, y);
            break;
        }
        case FTH_CMD_KEY_XKB:
        {
            const DWORD scan = freerdp_keyboard_get_rdp_scancode_from_x11_keycode(cmd->a);
            if (scan != RDP_SCANCODE_UNKNOWN)
                (void)freerdp_input_send_keyboard_event_ex(input, cmd->b ? TRUE : FALSE,
                                                           cmd->c ? TRUE : FALSE, scan);
            break;
        }
        case FTH_CMD_UNICODE:
        {
            UINT16 flags = cmd->b ? 0 : KBD_FLAGS_RELEASE;
            (void)freerdp_input_send_unicode_keyboard_event(input, flags, (UINT16)cmd->a);
            break;
        }
        case FTH_CMD_FOCUS:
            (void)freerdp_input_send_focus_in_event(input, (UINT16)cmd->a);
            break;
        case FTH_CMD_REFRESH:
            (void)copy_frame(instance->context);
            break;
        case FTH_CMD_RESIZE:
            if (g_state)
                (void)native_send_resize(g_state, cmd->a, cmd->b, cmd->c);
            break;
        default:
            break;
    }
}

static void drain_commands(freerdp* instance, int fd)
{
    for (;;)
    {
        FthCommand cmd;
        ssize_t n = read(fd, &cmd, sizeof(cmd));
        if (n == (ssize_t)sizeof(cmd))
            process_command(instance, &cmd);
        else
        {
            if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR))
                return;
            if (n == 0 && g_state)
                g_state->stop_requested = 1;
            return;
        }
    }
}

static int parse_int_arg(int argc, char** argv, const char* name)
{
    for (int i = 1; i + 1 < argc; i++)
        if (strcmp(argv[i], name) == 0)
            return atoi(argv[i + 1]);
    return -1;
}

int main(int argc, char** argv)
{
    int rc = 1;
    const int config_fd = parse_int_arg(argc, argv, "--config-fd");
    const int frame_fd = parse_int_arg(argc, argv, "--frame-fd");
    const int command_fd = parse_int_arg(argc, argv, "--command-fd");
    const int clip_fd = parse_int_arg(argc, argv, "--clip-fd"); /* optional: -1 disables clipboard bridging */
    if (config_fd < 0 || frame_fd < 0 || command_fd < 0)
    {
        fprintf(stderr, "Usage: fedtermrdp-native --config-fd N --frame-fd N --command-fd N [--clip-fd N]\n");
        return 2;
    }

    struct stat fst;
    if (fstat(frame_fd, &fst) != 0 || fst.st_size < (off_t)FTH_HEADER_SIZE)
    {
        perror("fstat frame");
        return 3;
    }

    NativeState state = { 0 };
    state.frame_fd = frame_fd;
    state.command_fd = command_fd;
    state.clip_fd = -1;
    state.mapping_size = (size_t)fst.st_size;
    state.mapping = mmap(NULL, state.mapping_size, PROT_READ | PROT_WRITE, MAP_SHARED, frame_fd, 0);
    if (state.mapping == MAP_FAILED)
    {
        perror("mmap frame");
        return 4;
    }
    state.header = (FthFrameHeader*)state.mapping;
    state.pixels = state.mapping + FTH_HEADER_SIZE;
    memset(state.header, 0, sizeof(*state.header));
    state.header->magic = FTH_MAGIC;
    state.header->version = FTH_VERSION;
    state.header->capacity = (uint32_t)(state.mapping_size - FTH_HEADER_SIZE);

    if (clip_fd >= 0)
    {
        struct stat clip_st;
        const size_t needed = sizeof(FthClipHeader) + (size_t)FTH_CLIP_CAPACITY * 2u;
        if (fstat(clip_fd, &clip_st) == 0 && (size_t)clip_st.st_size >= needed)
        {
            state.clip_mapping_size = (size_t)clip_st.st_size;
            state.clip_mapping = mmap(NULL, state.clip_mapping_size, PROT_READ | PROT_WRITE, MAP_SHARED, clip_fd, 0);
            if (state.clip_mapping != MAP_FAILED)
            {
                state.clip_fd = clip_fd;
                state.clip_header = (FthClipHeader*)state.clip_mapping;
                state.clip_host_data = state.clip_mapping + sizeof(FthClipHeader);
                state.clip_remote_data = state.clip_host_data + FTH_CLIP_CAPACITY;
                /* Header content (magic/version/capacity/seqs) is written by
                 * the Python side before spawning us; we only read/append. */
            }
            else
            {
                state.clip_mapping = NULL;
                WLog_WARN(TAG, "mmap clipboard mailbox failed: %s", strerror(errno));
            }
        }
        else
        {
            WLog_WARN(TAG, "Clipboard mailbox fd too small or unreadable; clipboard bridging disabled");
        }
    }

    state_message(&state, FTH_STATE_CONNECTING, "Connessione RDP in corso");
    g_state = &state;

    signal(SIGTERM, signal_handler);
    signal(SIGINT, signal_handler);
    fcntl(command_fd, F_SETFL, fcntl(command_fd, F_GETFL, 0) | O_NONBLOCK);

    char** rdp_argv = NULL;
    char* storage = NULL;
    size_t storage_size = 0;
    int rdp_argc = 0;
    if (read_config_argv(config_fd, &rdp_argv, &rdp_argc, &storage, &storage_size) != 0)
    {
        state_message(&state, FTH_STATE_ERROR, "Configurazione RDP non leggibile");
        goto cleanup;
    }

    RDP_CLIENT_ENTRY_POINTS ep;
    entry_points(&ep);
    rdpContext* context = freerdp_client_context_new(&ep);
    if (!context)
    {
        state_message(&state, FTH_STATE_ERROR, "Impossibile creare il contesto libfreerdp");
        goto cleanup_args;
    }

    int status = freerdp_client_settings_parse_command_line(context->settings, rdp_argc, rdp_argv, FALSE);
    if (status)
    {
        state.header->error_code = (uint32_t)status;
        state_message(&state, FTH_STATE_ERROR, "Opzioni RDP non valide");
        goto cleanup_context;
    }

    /* Settings now own copies of parsed values. Remove the plaintext config as
     * early as possible and close the inherited anonymous memfd. */
    if (rdp_argv)
    {
        free(rdp_argv[0]);
        free(rdp_argv);
        rdp_argv = NULL;
    }
    if (storage)
    {
        explicit_bzero(storage, storage_size);
        free(storage);
        storage = NULL;
        storage_size = 0;
    }
    close(config_fd);

    state.keyboard_layout = freerdp_settings_get_uint32(context->settings, FreeRDP_KeyboardLayout);
    (void)freerdp_keyboard_init(state.keyboard_layout);

    if (freerdp_client_start(context) != 0)
    {
        state_message(&state, FTH_STATE_ERROR, "Avvio client libfreerdp fallito");
        goto cleanup_context;
    }

    state_message(&state, FTH_STATE_CONNECTING, "Autenticazione RDP in corso");
    if (!freerdp_connect(context->instance))
    {
        state.header->error_code = freerdp_get_last_error(context);
        const char* name = freerdp_get_last_error_name(state.header->error_code);
        const char* detail = freerdp_get_last_error_string(state.header->error_code);
        char msg[512];
        snprintf(msg, sizeof(msg), "Connessione RDP fallita: %s (0x%08" PRIx32 ")%s%s",
                 name ? name : "errore sconosciuto", state.header->error_code,
                 detail ? " - " : "", detail ? detail : "");
        state_message(&state, FTH_STATE_ERROR, msg);
        goto stop_client;
    }

    HANDLE handles[MAXIMUM_WAIT_OBJECTS] = { 0 };
    /* La pipe dei comandi entra nel ciclo eventi come handle winpr: mouse e
     * tastiera vengono processati appena arrivano invece che al timeout dei
     * 20 ms, riducendo la latenza di input percepita. Se la creazione
     * dell'evento fallisce si torna al polling con timeout. */
    HANDLE command_event = CreateFileDescriptorEvent(NULL, FALSE, FALSE, command_fd, WINPR_FD_READ);
    while (!state.stop_requested && !freerdp_shall_disconnect_context(context))
    {
        DWORD nCount = freerdp_get_event_handles(context, handles, ARRAYSIZE(handles) - 1);
        if (nCount == 0)
        {
            state_message(&state, FTH_STATE_ERROR, "Nessun event handle FreeRDP disponibile");
            break;
        }
        DWORD timeout = 20;
        if (command_event && nCount < ARRAYSIZE(handles))
        {
            handles[nCount++] = command_event;
            timeout = 200;
        }
        const DWORD wait = WaitForMultipleObjects(nCount, handles, FALSE, timeout);
        if (wait == WAIT_FAILED)
        {
            state.header->error_code = GetLastError();
            state_message(&state, FTH_STATE_ERROR, "Errore nel ciclo eventi FreeRDP");
            break;
        }
        if (!freerdp_check_event_handles(context))
        {
            state.header->error_code = freerdp_get_last_error(context);
            if (state.header->error_code != FREERDP_ERROR_SUCCESS)
            {
                const char* name = freerdp_get_last_error_name(state.header->error_code);
                const char* detail = freerdp_get_last_error_string(state.header->error_code);
                char msg[512];
                snprintf(msg, sizeof(msg), "Sessione RDP interrotta: %s (0x%08" PRIx32 ")%s%s",
                         name ? name : "errore protocollo", state.header->error_code,
                         detail ? " - " : "", detail ? detail : "");
                state_message(&state, FTH_STATE_ERROR, msg);
            }
            break;
        }
        drain_commands(context->instance, command_fd);
        clip_poll_host_changes(&state);
    }

    if (state.stop_requested)
        state_message(&state, FTH_STATE_STOPPING, "Chiusura sessione RDP");
    if (command_event)
        CloseHandle(command_event);
    freerdp_disconnect(context->instance);
    rc = (state.header->state == FTH_STATE_ERROR) ? 1 : 0;

stop_client:
    (void)freerdp_client_stop(context);
cleanup_context:
    freerdp_client_context_free(context);
cleanup_args:
    if (rdp_argv)
    {
        free(rdp_argv[0]);
        free(rdp_argv);
    }
    if (storage)
    {
        explicit_bzero(storage, storage_size);
        free(storage);
    }
cleanup:
    if (state.clip_file)
        cliprdr_file_context_free(state.clip_file);
    if (state.clip_system)
        ClipboardDestroy(state.clip_system);
    if (state.clip_mapping && state.clip_mapping != MAP_FAILED)
        munmap(state.clip_mapping, state.clip_mapping_size);
    if (state.mapping && state.mapping != MAP_FAILED)
        munmap(state.mapping, state.mapping_size);
    return rc;
}
