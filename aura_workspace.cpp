#define UNICODE
#define _UNICODE
#define WIN32_LEAN_AND_MEAN

#include <windows.h>
#include <windowsx.h>
#include <commdlg.h>
#include <bcrypt.h>
#include <shellapi.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <string>
#include <vector>
#include <chrono>
#include <cctype>
#include <cstdio>
#include <ctime>
#include <fstream>
#include <iomanip>
#include <filesystem>
#include <memory>
#include <sstream>

#include "core/enterprise_database.h"
#include "contracts/aura_ffi.h"

// ============================================================================
// 1. DYNAMISCHE GDI-BINDUNG (ZERO LINK-TIME DEPENDENCY)
// ============================================================================
namespace DynGdi {
    typedef HPEN      (WINAPI *Fn_CreatePen)(int, int, COLORREF);
    typedef HBRUSH    (WINAPI *Fn_CreateSolidBrush)(COLORREF);
    typedef HGDIOBJ   (WINAPI *Fn_SelectObject)(HDC, HGDIOBJ);
    typedef BOOL      (WINAPI *Fn_DeleteObject)(HGDIOBJ);
    typedef BOOL      (WINAPI *Fn_RoundRect)(HDC, int, int, int, int, int, int);
    typedef BOOL      (WINAPI *Fn_MoveToEx)(HDC, int, int, LPPOINT);
    typedef BOOL      (WINAPI *Fn_LineTo)(HDC, int, int);
    typedef COLORREF  (WINAPI *Fn_SetTextColor)(HDC, COLORREF);
    typedef int       (WINAPI *Fn_SetBkMode)(HDC, int);
    typedef HFONT     (WINAPI *Fn_CreateFontW)(int, int, int, int, int, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, LPCWSTR);
    typedef HDC       (WINAPI *Fn_CreateCompatibleDC)(HDC);
    typedef HBITMAP   (WINAPI *Fn_CreateCompatibleBitmap)(HDC, int, int);
    typedef BOOL      (WINAPI *Fn_BitBlt)(HDC, int, int, int, int, HDC, int, int, DWORD);
    typedef BOOL      (WINAPI *Fn_DeleteDC)(HDC);
    typedef HGDIOBJ   (WINAPI *Fn_GetStockObject)(int);
    typedef BOOL      (WINAPI *Fn_Ellipse)(HDC, int, int, int, int);

    inline Fn_CreatePen               pCreatePen = nullptr;
    inline Fn_CreateSolidBrush        pCreateSolidBrush = nullptr;
    inline Fn_SelectObject            pSelectObject = nullptr;
    inline Fn_DeleteObject            pDeleteObject = nullptr;
    inline Fn_RoundRect               pRoundRect = nullptr;
    inline Fn_MoveToEx                pMoveToEx = nullptr;
    inline Fn_LineTo                  pLineTo = nullptr;
    inline Fn_SetTextColor            pSetTextColor = nullptr;
    inline Fn_SetBkMode               pSetBkMode = nullptr;
    inline Fn_CreateFontW             pCreateFontW = nullptr;
    inline Fn_CreateCompatibleDC      pCreateCompatibleDC = nullptr;
    inline Fn_CreateCompatibleBitmap  pCreateCompatibleBitmap = nullptr;
    inline Fn_BitBlt                  pBitBlt = nullptr;
    inline Fn_DeleteDC                pDeleteDC = nullptr;
    inline Fn_GetStockObject          pGetStockObject = nullptr;
    inline Fn_Ellipse                 pEllipse = nullptr;

    inline bool Init() {
        HMODULE hGdi = LoadLibraryA("gdi32.dll");
        if (!hGdi) return false;

        pCreatePen              = (Fn_CreatePen)GetProcAddress(hGdi, "CreatePen");
        pCreateSolidBrush        = (Fn_CreateSolidBrush)GetProcAddress(hGdi, "CreateSolidBrush");
        pSelectObject            = (Fn_SelectObject)GetProcAddress(hGdi, "SelectObject");
        pDeleteObject            = (Fn_DeleteObject)GetProcAddress(hGdi, "DeleteObject");
        pRoundRect               = (Fn_RoundRect)GetProcAddress(hGdi, "RoundRect");
        pMoveToEx                = (Fn_MoveToEx)GetProcAddress(hGdi, "MoveToEx");
        pLineTo                  = (Fn_LineTo)GetProcAddress(hGdi, "LineTo");
        pSetTextColor            = (Fn_SetTextColor)GetProcAddress(hGdi, "SetTextColor");
        pSetBkMode               = (Fn_SetBkMode)GetProcAddress(hGdi, "SetBkMode");
        pCreateFontW             = (Fn_CreateFontW)GetProcAddress(hGdi, "CreateFontW");
        pCreateCompatibleDC      = (Fn_CreateCompatibleDC)GetProcAddress(hGdi, "CreateCompatibleDC");
        pCreateCompatibleBitmap  = (Fn_CreateCompatibleBitmap)GetProcAddress(hGdi, "CreateCompatibleBitmap");
        pBitBlt                  = (Fn_BitBlt)GetProcAddress(hGdi, "BitBlt");
        pDeleteDC                = (Fn_DeleteDC)GetProcAddress(hGdi, "DeleteDC");
        pGetStockObject          = (Fn_GetStockObject)GetProcAddress(hGdi, "GetStockObject");
        pEllipse                 = (Fn_Ellipse)GetProcAddress(hGdi, "Ellipse");

        return (pCreatePen && pCreateSolidBrush && pSelectObject && pDeleteObject &&
                pRoundRect && pMoveToEx && pLineTo && pSetTextColor && pSetBkMode &&
                pCreateFontW && pCreateCompatibleDC && pCreateCompatibleBitmap &&
                pBitBlt && pDeleteDC && pGetStockObject && pEllipse);
    }
}

// Makro-Mapping auf dynamische Funktionszeiger
#ifdef CreateFontW
#undef CreateFontW
#endif

#define CreatePen               DynGdi::pCreatePen
#define CreateSolidBrush        DynGdi::pCreateSolidBrush
#define SelectObject            DynGdi::pSelectObject
#define DeleteObject            DynGdi::pDeleteObject
#define RoundRect               DynGdi::pRoundRect
#define MoveToEx                DynGdi::pMoveToEx
#define LineTo                  DynGdi::pLineTo
#define SetTextColor            DynGdi::pSetTextColor
#define SetBkMode               DynGdi::pSetBkMode
#define CreateFontW             DynGdi::pCreateFontW
#define CreateCompatibleDC      DynGdi::pCreateCompatibleDC
#define CreateCompatibleBitmap  DynGdi::pCreateCompatibleBitmap
#define BitBlt                  DynGdi::pBitBlt
#define DeleteDC                DynGdi::pDeleteDC
#define GetStockObject          DynGdi::pGetStockObject
#define Ellipse                 DynGdi::pEllipse

// ============================================================================
// 2. FARBPALETTE & DESIGN-SYSTEM
// ============================================================================
namespace Theme {
    constexpr COLORREF BG_DARK        = RGB(15, 23, 42);      // Slate 900
    constexpr COLORREF BG_SIDEBAR     = RGB(10, 15, 30);      // Deep Slate
    constexpr COLORREF BG_CARD        = RGB(24, 33, 54);      // Slate 850
    constexpr COLORREF BG_CARD_HOVER  = RGB(33, 45, 74);      // Slate 800
    constexpr COLORREF BORDER         = RGB(45, 59, 88);      // Slate 700
    constexpr COLORREF BORDER_MUTED   = RGB(30, 41, 65);
    
    constexpr COLORREF ACCENT         = RGB(59, 130, 246);    // Blue 500
    constexpr COLORREF ACCENT_HOVER   = RGB(96, 165, 250);    // Blue 400
    constexpr COLORREF ACCENT_BG      = RGB(30, 58, 110);     // Subtle Accent
    
    constexpr COLORREF TEXT_WHITE     = RGB(248, 250, 252);   // Slate 50
    constexpr COLORREF TEXT_SECONDARY = RGB(203, 213, 225);   // Slate 300
    constexpr COLORREF TEXT_MUTED     = RGB(148, 163, 184);   // Slate 400
    
    constexpr COLORREF STATUS_GREEN   = RGB(34, 197, 94);     // Emerald 500
    constexpr COLORREF STATUS_YELLOW  = RGB(245, 158, 11);    // Amber 500
    constexpr COLORREF STATUS_RED     = RGB(239, 68, 68);     // Rose 500
}

// ============================================================================
// 3. MODELLE & APPLIKATIONS-STATUS
// ============================================================================
enum class NavTab {
    DASHBOARD = 0,
    ROOMS,
    TASKS,
    TEAM,
    HELPDESK,
    ADMIN
};

enum class PresenceStatus {
    OFFICE = 0,
    REMOTE,
    MEETING,
    AWAY
};

struct Meeting {
    std::wstring time;
    std::wstring title;
    std::wstring room;
    std::wstring host;
};

struct OfficeRoom {
    std::wstring name;
    std::wstring capacity;
    bool isBooked;
    std::wstring until;
    int64_t id = 0;
    int capacityLimit = 0;
    std::wstring location;
    std::wstring equipment;
    bool archived = false;
    int64_t bookingId = 0;
    bool blocked = false;
};

struct OfficeTask {
    std::wstring text;
    std::wstring department;
    std::wstring priority;
    bool done;
    int64_t id = 0;
    std::wstring status;
    std::wstring due;
};

struct TeamMember {
    std::wstring name;
    std::wstring role;
    std::wstring status;
    COLORREF statusColor;
    int64_t id = 0;
};

struct HelpTicket {
    std::wstring title;
    std::wstring timestamp;
    std::wstring state;
    int64_t id = 0;
    std::wstring priority;
};

struct AppState {
    NavTab activeTab = NavTab::DASHBOARD;
    PresenceStatus userStatus = PresenceStatus::OFFICE;
    int hoveredNavIndex = -1;
    int hoveredActionIndex = -1;

    std::vector<Meeting> meetings = {
        { L"09:30 - 10:15", L"Sprint Planning & Architektur Q4", L"Konferenz Alpha", L"Ralf Kr\u00fcmmel" },
        { L"11:00 - 11:30", L"1-on-1 Sync Systems Engineering", L"Fokus-Box 1", L"Sarah Weber" },
        { L"14:00 - 15:30", L"Cross-Department Budget Allokation", L"Lounge S\u00fcd", L"Michael Schmidt" },
        { L"16:15 - 17:00", L"Release Review: Bare-Metal Driver v4", L"Konferenz Beta", L"Ralf Kr\u00fcmmel" }
    };

    std::vector<OfficeRoom> rooms = {
        { L"Konferenzraum Alpha", L"12 Personen \u2022 4K Display, Beamer", true, L"Belegt bis 10:15" },
        { L"Fokus-Box 1", L"2 Personen \u2022 Soundproof, Whiteboard", false, L"Verf\u00fcgbar" },
        { L"Lounge S\u00fcd (Kreativzone)", L"8 Personen \u2022 Casual Seating, Screen", false, L"Verf\u00fcgbar" },
        { L"Konferenzraum Beta", L"18 Personen \u2022 Dual Screen, Hybrid-Mic", true, L"Belegt bis 12:00" }
    };

    std::vector<OfficeTask> tasks = {
        { L"Quartalsbericht 2026 auditieren & freigeben", L"Architektur", L"Hoch", true },
        { L"Server-Rack 03 Abw\u00e4rme-Logging kalibrieren", L"Infrastruktur", L"Hoch", false },
        { L"Home-Office Antr\u00e4ge KW 45 gegenzeichnen", L"HR & Team", L"Mittel", false },
        { L"Neuen Arbeitsplatz Desk 14 Hardware einrichten", L"IT-Support", L"Niedrig", true },
        { L"Dokumentation Notfall-Stromaggregat aktualisieren", L"Facility", L"Mittel", false }
    };

    std::vector<TeamMember> team = {
        { L"Ralf Kr\u00fcmmel", L"Deep-Substrate Architect", L"Im B\u00fcro (Desk 01)", Theme::STATUS_GREEN },
        { L"Sarah Weber", L"Senior Product Lead", L"Home-Office", Theme::ACCENT },
        { L"Michael Schmidt", L"Head of Operations", L"Im Meeting (Alpha)", Theme::STATUS_YELLOW },
        { L"Elena Rostova", L"People & Culture", L"Im B\u00fcro (Desk 08)", Theme::STATUS_GREEN },
        { L"Jonas Becker", L"Low-Level Systems Eng.", L"Abwesend (Urlaub)", Theme::STATUS_RED }
    };

    std::vector<HelpTicket> tickets = {
        { L"Kaffeemaschine K\u00fcche Ost: Entkalkung erforderlich", L"Heute, 08:14", L"In Bearbeitung" },
        { L"HDMI-Adapter an Pr\u00e4sentations-Tisch Alpha fehlt", L"Heute, 08:45", L"Offen" },
        { L"Druckerpapier Tray 2 im 2. OG leer", L"Gestern, 16:30", L"Gel\u00f6st" }
    };
} g_State;

std::unique_ptr<EnterpriseDatabase> g_Database;
bool ApplyIncomingRoomChanges(const std::string& batch);
std::filesystem::path FindWorkspaceRoot();
int64_t g_CurrentEmployeeId = 0;
bool g_Authenticated = false;

std::wstring EnsureSyncClientInstanceId(const std::filesystem::path& root) {
    const std::filesystem::path identityFile = root / L"KR_BUILD" / L"sync-client-instance.id";
    std::ifstream input(identityFile, std::ios::binary);
    std::string value;
    if (input) std::getline(input, value);
    const bool valid = value.size() == 32 && std::all_of(value.begin(), value.end(), [](unsigned char character) { return std::isxdigit(character) != 0; });
    if (!valid) {
        std::array<unsigned char, 16> random{};
        if (BCryptGenRandom(nullptr, random.data(), static_cast<ULONG>(random.size()), BCRYPT_USE_SYSTEM_PREFERRED_RNG) != 0) return L"";
        static constexpr char digits[] = "0123456789abcdef";
        value.clear(); value.reserve(random.size() * 2);
        for (unsigned char byte : random) { value.push_back(digits[byte >> 4]); value.push_back(digits[byte & 0x0f]); }
        std::ofstream output(identityFile, std::ios::binary | std::ios::trunc);
        if (!output) return L"";
        output << value << "\n";
    }
    return std::wstring(value.begin(), value.end());
}

std::string g_SyncApplyError;
class SyncServiceBridge {
public:
    ~SyncServiceBridge() { if (module_) FreeLibrary(module_); }
    void initialize(const std::filesystem::path& root) {
        root_ = root;
        const std::wstring journal = (root / L"KR_BUILD" / L"sync-outbox.ndjson").wstring();
        SetEnvironmentVariableW(L"AURA_SYNC_JOURNAL", journal.c_str());
        const std::wstring clientId = EnsureSyncClientInstanceId(root);
        clientId_ = clientId;
        if (!clientId.empty()) SetEnvironmentVariableW(L"AURA_SYNC_CLIENT_ID", clientId.c_str());
        wchar_t executable[MAX_PATH]{}; GetModuleFileNameW(nullptr, executable, MAX_PATH);
        module_ = LoadLibraryW((std::filesystem::path(executable).parent_path() / L"aura_services.dll").wstring().c_str());
        if (!module_) return;
        publish_ = reinterpret_cast<PublishFn>(GetProcAddress(module_, "aura_publish_event"));
        preflight_ = reinterpret_cast<PreflightFn>(GetProcAddress(module_, "aura_preflight_booking"));
        reserve_ = reinterpret_cast<ReserveFn>(GetProcAddress(module_, "aura_reserve_booking"));
        commit_ = reinterpret_cast<CommitFn>(GetProcAddress(module_, "aura_commit_booking"));
        release_ = reinterpret_cast<ReleaseFn>(GetProcAddress(module_, "aura_release_booking"));
        flush_ = reinterpret_cast<FlushFn>(GetProcAddress(module_, "aura_flush_outbox"));
        fetchChanges_ = reinterpret_cast<FetchChangesFn>(GetProcAddress(module_, "aura_fetch_changes"));
        status_ = reinterpret_cast<StatusFn>(GetProcAddress(module_, "aura_service_status"));
        uploadAttachment_ = reinterpret_cast<UploadAttachmentFn>(GetProcAddress(module_, "aura_upload_attachment"));
        downloadAttachment_ = reinterpret_cast<DownloadAttachmentFn>(GetProcAddress(module_, "aura_download_attachment"));
        if (!publish_ || !preflight_ || !reserve_ || !commit_ || !release_ || !flush_ || !fetchChanges_ || !status_) { FreeLibrary(module_); module_ = nullptr; publish_ = nullptr; preflight_ = nullptr; reserve_ = nullptr; commit_ = nullptr; release_ = nullptr; flush_ = nullptr; fetchChanges_ = nullptr; status_ = nullptr; uploadAttachment_ = nullptr; downloadAttachment_ = nullptr; return; }
        flush_();
        collectChanges(root);
    }
    bool synchronize() const {
        if (!flush_ || root_.empty()) return false;
        flush_();
        return collectChanges(root_);
    }
    bool publish(const std::string& topic, const std::string& payload) const { return publish_ && publish_(topic.c_str(), payload.c_str()) == 0; }
    // A missing upload symbol only occurs with an older bundled service DLL;
    // keep the local evidence object intact and report the missing central copy.
    int32_t uploadAttachment(const std::wstring& path) const {
        if (!uploadAttachment_) return 2;
        const int bytes = WideCharToMultiByte(CP_UTF8, 0, path.c_str(), -1, nullptr, 0, nullptr, nullptr);
        if (bytes <= 1) return 1;
        std::string encoded(static_cast<size_t>(bytes), '\0');
        WideCharToMultiByte(CP_UTF8, 0, path.c_str(), -1, encoded.data(), bytes, nullptr, nullptr);
        return uploadAttachment_(encoded.c_str());
    }
    int32_t downloadAttachment(const std::wstring& sha256, const std::wstring& destination) const {
        if (!downloadAttachment_) return 2;
        const int hashBytes=WideCharToMultiByte(CP_UTF8,0,sha256.c_str(),-1,nullptr,0,nullptr,nullptr),pathBytes=WideCharToMultiByte(CP_UTF8,0,destination.c_str(),-1,nullptr,0,nullptr,nullptr);
        if(hashBytes<=1||pathBytes<=1)return 1;std::string hash(static_cast<size_t>(hashBytes),'\0'),path(static_cast<size_t>(pathBytes),'\0');WideCharToMultiByte(CP_UTF8,0,sha256.c_str(),-1,hash.data(),hashBytes,nullptr,nullptr);WideCharToMultiByte(CP_UTF8,0,destination.c_str(),-1,path.data(),pathBytes,nullptr,nullptr);return downloadAttachment_(hash.c_str(),path.c_str());
    }
    const std::wstring& clientId() const { return clientId_; }
    int32_t preflightBooking(int64_t bookingId, int64_t roomId, int64_t start, int64_t end) const { return preflight_ ? preflight_(bookingId, roomId, start, end) : 0; }
    int32_t reserveBooking(int64_t reservationId, int64_t bookingId, int64_t roomId, int64_t start, int64_t end) const { return reserve_ ? reserve_(reservationId, bookingId, roomId, start, end) : 0; }
    int32_t commitBooking(int64_t reservationId, int64_t bookingId, bool reschedule) const { return commit_ ? commit_(reservationId, bookingId, reschedule ? 1 : 0) : 0; }
    int32_t releaseBooking(int64_t reservationId) const { return release_ ? release_(reservationId) : 0; }
    std::wstring status() const {
        if (!status_) return L"Synchronisationsdienst nicht geladen";
        char text[256]{}; return status_(text, static_cast<int32_t>(sizeof(text))) == 0 ? Utf8ToWide(text) : L"Synchronisationsdienst nicht verfügbar";
    }
private:
    using PublishFn = int32_t(*)(const char*, const char*);
    using PreflightFn = int32_t(*)(int64_t, int64_t, int64_t, int64_t);
    using ReserveFn = int32_t(*)(int64_t, int64_t, int64_t, int64_t, int64_t);
    using CommitFn = int32_t(*)(int64_t, int64_t, int32_t);
    using ReleaseFn = int32_t(*)(int64_t);
    using FlushFn = int32_t(*)();
    using FetchChangesFn = int32_t(*)(int64_t, char*, int32_t, int64_t*);
    using StatusFn = int32_t(*)(char*, int32_t);
    using UploadAttachmentFn = int32_t(*)(const char*);
    using DownloadAttachmentFn = int32_t(*)(const char*, const char*);
    static std::wstring Utf8ToWide(const char* source) {
        if (!source || !*source) return L"";
        const int chars = MultiByteToWideChar(CP_UTF8, 0, source, -1, nullptr, 0);
        std::wstring result(static_cast<size_t>(chars), L'\0');
        if (chars > 1) MultiByteToWideChar(CP_UTF8, 0, source, -1, result.data(), chars);
        if (!result.empty()) result.pop_back();
        return result;
    }
    HMODULE module_ = nullptr;
    PublishFn publish_ = nullptr;
    PreflightFn preflight_ = nullptr;
    ReserveFn reserve_ = nullptr;
    CommitFn commit_ = nullptr;
    ReleaseFn release_ = nullptr;
    FlushFn flush_ = nullptr;
    StatusFn status_ = nullptr;
    UploadAttachmentFn uploadAttachment_ = nullptr;
    DownloadAttachmentFn downloadAttachment_ = nullptr;
    FetchChangesFn fetchChanges_ = nullptr;
    std::wstring clientId_;
    std::filesystem::path root_;
    bool collectChanges(const std::filesystem::path& root) const {
        if (!fetchChanges_ || GetEnvironmentVariableW(L"AURA_SYNC_URL", nullptr, 0) == 0) return false;
        const std::filesystem::path cursorFile = root / L"KR_BUILD" / L"sync-cursor.txt";
        const std::filesystem::path inboxFile = root / L"KR_BUILD" / L"sync-inbox.ndjson";
        int64_t cursor = 0; { std::ifstream input(cursorFile); if (input) input >> cursor; if (cursor < 0) cursor = 0; }
        std::vector<char> buffer(1024 * 1024, '\0'); int64_t next = cursor;
        if (fetchChanges_(cursor, buffer.data(), static_cast<int32_t>(buffer.size()), &next) != 0 || next < cursor) return false;
        if (!ApplyIncomingRoomChanges(buffer.data())) { std::ofstream diagnostic(root / L"KR_BUILD" / L"sync-last-error.log", std::ios::binary | std::ios::trunc); if(diagnostic){if(!g_SyncApplyError.empty())diagnostic<<g_SyncApplyError;else if(g_Database){const std::wstring error=g_Database->error();diagnostic<<std::string(error.begin(),error.end());}} return false; }
        std::ofstream inbox(inboxFile, std::ios::binary | std::ios::app); if (!inbox) return false;
        inbox << buffer.data() << "\n"; inbox.close();
        std::ofstream output(cursorFile, std::ios::binary | std::ios::trunc); if (output) output << next << "\n";
        return next > cursor;
    }
};

SyncServiceBridge g_SyncService;
std::atomic<int64_t> g_NextCentralReservation{-(static_cast<int64_t>(std::time(nullptr)) * 100000 + GetCurrentProcessId())};
int64_t NextCentralReservationId() { return g_NextCentralReservation.fetch_sub(1); }

std::string WideToUtf8(const std::wstring& source) {
    if (source.empty()) return {};
    const int bytes = WideCharToMultiByte(CP_UTF8, 0, source.data(), static_cast<int>(source.size()), nullptr, 0, nullptr, nullptr);
    std::string result(static_cast<size_t>(bytes), '\0');
    if (bytes > 0) WideCharToMultiByte(CP_UTF8, 0, source.data(), static_cast<int>(source.size()), result.data(), bytes, nullptr, nullptr);
    return result;
}

std::wstring Utf8ToWide(const std::string& source) {
    if (source.empty()) return {};
    const int chars = MultiByteToWideChar(CP_UTF8, 0, source.data(), static_cast<int>(source.size()), nullptr, 0);
    std::wstring result(static_cast<size_t>(chars), L'\0');
    if (chars > 0) MultiByteToWideChar(CP_UTF8, 0, source.data(), static_cast<int>(source.size()), result.data(), chars);
    return result;
}

std::string JsonString(const std::wstring& source) {
    std::string result = "\"";
    for (unsigned char value : WideToUtf8(source)) {
        switch (value) {
        case '\\': result += "\\\\"; break;
        case '\"': result += "\\\""; break;
        case '\n': result += "\\n"; break;
        case '\r': result += "\\r"; break;
        case '\t': result += "\\t"; break;
        default:
            if (value < 0x20) { char encoded[7]{}; std::snprintf(encoded, sizeof(encoded), "\\u%04x", static_cast<unsigned int>(value)); result += encoded; }
            else result.push_back(static_cast<char>(value));
        }
    }
    return result + "\"";
}

std::string StableSyncObjectId(const std::wstring& objectType,int64_t localId);
void PublishProjectChange(const EnterpriseProject& project, const char* topic) {
    const std::wstring clientId = g_SyncService.clientId();
    if (clientId.empty() || !topic) return;
    const std::string sourceId = StableSyncObjectId(L"project",project.id); if(sourceId.empty())return;
    const std::string payload = "{\"id\":" + JsonString(std::wstring(sourceId.begin(), sourceId.end())) +
        ",\"name\":" + JsonString(project.name) + ",\"department\":" + JsonString(project.department) +
        ",\"description\":" + JsonString(project.description) + ",\"status\":" + JsonString(project.status) +
        ",\"ownerId\":" + std::to_string(project.ownerId) + ",\"active\":" + (project.active ? "true" : "false") + "}";
    g_SyncService.publish(topic, payload);
}

void PublishTaskChange(const EnterpriseTask& task, const char* topic) {
    const std::wstring clientId = g_SyncService.clientId();
    if (clientId.empty() || !topic) return;
    const std::string sourceId = StableSyncObjectId(L"task",task.id); if(sourceId.empty())return;
    const std::string payload = "{\"id\":" + JsonString(std::wstring(sourceId.begin(), sourceId.end())) +
        ",\"title\":" + JsonString(task.title) + ",\"department\":" + JsonString(task.department) +
        ",\"priority\":" + JsonString(task.priority) + ",\"status\":" + JsonString(task.status) +
        ",\"assigneeId\":" + std::to_string(task.assigneeId) + ",\"createdBy\":" + std::to_string(g_CurrentEmployeeId) +
        ",\"active\":" + (task.active ? "true" : "false") + "}";
    g_SyncService.publish(topic, payload);
}

void PublishTicketChange(const EnterpriseTicket& ticket, const char* topic) {
    const std::wstring clientId = g_SyncService.clientId();
    if (clientId.empty() || !topic) return;
    const std::string sourceId = StableSyncObjectId(L"ticket",ticket.id); if(sourceId.empty())return;
    const std::string payload = "{\"id\":" + JsonString(std::wstring(sourceId.begin(), sourceId.end())) +
        ",\"title\":" + JsonString(ticket.title) + ",\"category\":" + JsonString(ticket.category) +
        ",\"priority\":" + JsonString(ticket.priority) + ",\"status\":" + JsonString(ticket.status) +
        ",\"assigneeId\":" + std::to_string(ticket.assigneeId) + ",\"createdBy\":" + std::to_string(g_CurrentEmployeeId) +
        ",\"active\":" + (ticket.active ? "true" : "false") + "}";
    g_SyncService.publish(topic, payload);
}

void PublishRoomChange(const EnterpriseRoom& room, const char* topic) {
    const std::wstring clientId = g_SyncService.clientId();
    if (clientId.empty() || !topic) return;
    const std::string sourceId = StableSyncObjectId(L"room",room.id); if(sourceId.empty())return;
    const std::string payload = "{\"id\":" + JsonString(std::wstring(sourceId.begin(), sourceId.end())) +
        ",\"name\":" + JsonString(room.name) + ",\"location\":" + JsonString(room.location) +
        ",\"capacity\":" + std::to_string(room.capacity) + ",\"equipment\":" + JsonString(room.equipment) +
        ",\"active\":" + (room.active ? "true" : "false") + "}";
    g_SyncService.publish(topic, payload);
}

void PublishEmployeeChange(const EnterpriseEmployee& employee, const char* topic) {
    const std::wstring clientId = g_SyncService.clientId();
    if (clientId.empty() || !topic || employee.id <= 0) return;
    const std::string sourceId = StableSyncObjectId(L"employee",employee.id); if(sourceId.empty())return;
    const std::string payload = "{\"id\":" + JsonString(std::wstring(sourceId.begin(), sourceId.end())) +
        ",\"name\":" + JsonString(employee.name) + ",\"role\":" + JsonString(employee.role) +
        ",\"department\":" + JsonString(employee.department) + ",\"presence\":" + JsonString(employee.presence) +
        ",\"active\":" + (employee.active ? "true" : "false") + "}";
    g_SyncService.publish(topic, payload);
}

void PublishOrganizationChange(const EnterpriseOrganizationUnit& unit, const char* topic) {
    const std::wstring clientId = g_SyncService.clientId();
    if (clientId.empty() || !topic || unit.id <= 0) return;
    const std::string sourceId = StableSyncObjectId(L"organization",unit.id); if(sourceId.empty())return;
    const std::string payload = "{\"id\":" + JsonString(std::wstring(sourceId.begin(), sourceId.end())) + ",\"name\":" + JsonString(unit.name) + ",\"code\":" + JsonString(unit.code) + ",\"managerId\":" + std::to_string(unit.managerId) + ",\"active\":" + (unit.active ? "true" : "false") + "}";
    g_SyncService.publish(topic, payload);
}

void PublishLeaveChange(const EnterpriseLeaveRequest& leave, const char* topic) {
    const std::wstring clientId=g_SyncService.clientId(); if(clientId.empty()||!topic||leave.id<=0)return;
    const std::string sourceId=StableSyncObjectId(L"leave",leave.id);if(sourceId.empty())return;
    const std::string payload="{\"id\":"+JsonString(std::wstring(sourceId.begin(),sourceId.end()))+",\"employeeId\":"+std::to_string(leave.employeeId)+",\"startsAt\":"+std::to_string(leave.startsAt)+",\"endsAt\":"+std::to_string(leave.endsAt)+",\"reason\":"+JsonString(leave.reason)+",\"status\":"+JsonString(leave.status)+",\"decidedBy\":"+std::to_string(leave.decidedBy)+"}";
    g_SyncService.publish(topic,payload);
}

void PublishFacilityAssetChange(const EnterpriseFacilityAsset& asset, const char* topic) {
    const std::wstring clientId=g_SyncService.clientId();if(clientId.empty()||!topic||asset.id<=0)return;
    const std::string sourceId=StableSyncObjectId(L"facility_asset",asset.id);if(sourceId.empty())return;
    const std::string payload="{\"id\":"+JsonString(std::wstring(sourceId.begin(),sourceId.end()))+",\"name\":"+JsonString(asset.name)+",\"category\":"+JsonString(asset.category)+",\"location\":"+JsonString(asset.location)+",\"status\":"+JsonString(asset.status)+",\"active\":"+(asset.active?"true":"false")+"}";
    g_SyncService.publish(topic,payload);
}

std::string StableSyncObjectId(const std::wstring& objectType,int64_t localId){
    if(localId<=0)return {};if(g_Database){const std::wstring mapped=g_Database->syncedExternalId(objectType,localId);if(!mapped.empty())return WideToUtf8(mapped);}const std::wstring clientId=g_SyncService.clientId();if(clientId.empty())return {};const std::wstring generated=clientId+L":"+std::to_wstring(localId);if(g_Database&&!g_Database->ensureSyncedExternalId(objectType,localId,generated))return {};return WideToUtf8(generated);
}
void PublishMaintenanceChange(const EnterpriseFacilityAsset& asset,const EnterpriseMaintenanceOrder& order,const char* topic){
    if(!topic||order.id<=0)return;const std::string id=StableSyncObjectId(L"maintenance_order",order.id),assetId=StableSyncObjectId(L"facility_asset",asset.id);if(id.empty()||assetId.empty())return;
    const std::string payload="{\"id\":"+JsonString(std::wstring(id.begin(),id.end()))+",\"assetId\":"+JsonString(std::wstring(assetId.begin(),assetId.end()))+",\"title\":"+JsonString(order.title)+",\"status\":"+JsonString(order.status)+",\"dueAt\":"+std::to_string(order.dueAt)+",\"recurrenceDays\":"+std::to_string(order.recurrenceDays)+",\"active\":"+(order.active?"true":"false")+"}";g_SyncService.publish(topic,payload);
}
void PublishTaskApprovalChange(const EnterpriseTaskApproval& approval,const char* topic){
    if(!topic||approval.id<=0)return;
    const std::string id=StableSyncObjectId(L"task_approval",approval.id),taskId=StableSyncObjectId(L"task",approval.taskId),requestedBy=StableSyncObjectId(L"employee",approval.requestedBy),approverId=StableSyncObjectId(L"employee",approval.approverId),decidedBy=StableSyncObjectId(L"employee",approval.decidedBy);
    if(id.empty()||taskId.empty()||requestedBy.empty()||approverId.empty()||(approval.status!=L"pending"&&decidedBy.empty()))return;
    const std::string payload="{\"id\":"+JsonString(std::wstring(id.begin(),id.end()))+",\"taskId\":"+JsonString(std::wstring(taskId.begin(),taskId.end()))+",\"requestedBy\":"+JsonString(std::wstring(requestedBy.begin(),requestedBy.end()))+",\"approverId\":"+JsonString(std::wstring(approverId.begin(),approverId.end()))+",\"status\":"+JsonString(approval.status)+",\"decidedBy\":"+JsonString(std::wstring(decidedBy.begin(),decidedBy.end()))+",\"active\":"+(approval.active?"true":"false")+"}";
    g_SyncService.publish(topic,payload);
}
void PublishCommentChange(const EnterpriseComment& comment,const char* topic){
    if(!topic||comment.id<=0||(comment.subjectType!=L"task"&&comment.subjectType!=L"ticket"))return;
    const std::string id=StableSyncObjectId(L"comment",comment.id),subjectId=StableSyncObjectId(comment.subjectType,comment.subjectId),authorId=StableSyncObjectId(L"employee",comment.authorId);if(id.empty()||subjectId.empty()||authorId.empty())return;
    const std::string payload="{\"id\":"+JsonString(std::wstring(id.begin(),id.end()))+",\"subjectType\":"+JsonString(comment.subjectType)+",\"subjectId\":"+JsonString(std::wstring(subjectId.begin(),subjectId.end()))+",\"authorId\":"+JsonString(std::wstring(authorId.begin(),authorId.end()))+",\"body\":"+JsonString(comment.body)+"}";g_SyncService.publish(topic,payload);
}
void PublishAttachmentChange(const EnterpriseAttachment& attachment,const char* topic){
    if(!topic||attachment.id<=0||(attachment.subjectType!=L"task"&&attachment.subjectType!=L"ticket"))return;
    const std::string id=StableSyncObjectId(L"attachment",attachment.id),subjectId=StableSyncObjectId(attachment.subjectType,attachment.subjectId),uploaderId=StableSyncObjectId(L"employee",attachment.uploadedBy);if(id.empty()||subjectId.empty()||uploaderId.empty())return;
    const std::string payload="{\"id\":"+JsonString(std::wstring(id.begin(),id.end()))+",\"subjectType\":"+JsonString(attachment.subjectType)+",\"subjectId\":"+JsonString(std::wstring(subjectId.begin(),subjectId.end()))+",\"originalName\":"+JsonString(attachment.originalName)+",\"sizeBytes\":"+std::to_string(attachment.sizeBytes)+",\"sha256\":"+JsonString(attachment.sha256)+",\"uploadedBy\":"+JsonString(std::wstring(uploaderId.begin(),uploaderId.end()))+",\"active\":"+(attachment.active?"true":"false")+"}";g_SyncService.publish(topic,payload);
}

bool ReadJsonString(const std::string& text, size_t& position, std::string& value) {
    while (position < text.size() && std::isspace(static_cast<unsigned char>(text[position]))) ++position;
    if (position >= text.size() || text[position++] != '"') return false;
    value.clear();
    while (position < text.size()) {
        char current = text[position++];
        if (current == '"') return true;
        if (current != '\\') { value.push_back(current); continue; }
        if (position >= text.size()) return false;
        const char escaped = text[position++];
        switch (escaped) { case '"': value.push_back('"'); break; case '\\': value.push_back('\\'); break; case '/': value.push_back('/'); break; case 'b': value.push_back('\b'); break; case 'f': value.push_back('\f'); break; case 'n': value.push_back('\n'); break; case 'r': value.push_back('\r'); break; case 't': value.push_back('\t'); break; case 'u': { if(position+4>text.size())return false; unsigned int code=0; for(int i=0;i<4;++i){const char digit=text[position++];const int valueOf=digit>='0'&&digit<='9'?digit-'0':digit>='a'&&digit<='f'?digit-'a'+10:digit>='A'&&digit<='F'?digit-'A'+10:-1;if(valueOf<0)return false;code=(code<<4)|static_cast<unsigned int>(valueOf);}if(code<0x80)value.push_back(static_cast<char>(code));else if(code<0x800){value.push_back(static_cast<char>(0xC0|(code>>6)));value.push_back(static_cast<char>(0x80|(code&0x3F)));}else{value.push_back(static_cast<char>(0xE0|(code>>12)));value.push_back(static_cast<char>(0x80|((code>>6)&0x3F)));value.push_back(static_cast<char>(0x80|(code&0x3F)));}break;} default: return false; }
    }
    return false;
}

bool JsonValuePosition(const std::string& text, const char* key, size_t start, size_t& position) { const std::string marker="\""+std::string(key)+"\""; size_t keyAt=start; while((keyAt=text.find(marker,keyAt))!=std::string::npos){size_t next=keyAt+marker.size();while(next<text.size()&&std::isspace(static_cast<unsigned char>(text[next])))++next;if(next<text.size()&&text[next]==':'){position=next+1;return true;}keyAt+=marker.size();}return false; }
bool JsonStringField(const std::string& text, const char* key, std::string& value, size_t start = 0) { size_t position=0; return JsonValuePosition(text,key,start,position)&&ReadJsonString(text,position,value); }
bool JsonIntegerField(const std::string& text, const char* key, int64_t& value) {
    size_t position=0;if(!JsonValuePosition(text,key,0,position))return false;while(position<text.size()&&std::isspace(static_cast<unsigned char>(text[position])))++position; size_t end=position; while(end<text.size()&&(text[end]=='-'||std::isdigit(static_cast<unsigned char>(text[end]))))++end; try{value=std::stoll(text.substr(position,end-position));return end>position;}catch(...){return false;}
}
bool JsonBooleanField(const std::string& text, const char* key, bool& value) {
    size_t position=0;if(!JsonValuePosition(text,key,0,position))return false;while(position<text.size()&&std::isspace(static_cast<unsigned char>(text[position])))++position;if(text.compare(position,4,"true")==0){value=true;return true;}if(text.compare(position,5,"false")==0){value=false;return true;}return false;
}

bool ApplyIncomingRoomChanges(const std::string& batch) {
    g_SyncApplyError.clear();
    if (!g_Database || !g_Database->ready() || batch == "[]") return g_Database && g_Database->ready();
    const std::string ownPrefix = WideToUtf8(g_SyncService.clientId()) + ":"; size_t searchAt=0;
    while (true) {
        const size_t topicAt=batch.find("\"topic\"",searchAt); if(topicAt==std::string::npos) break; std::string topic,payload;
        if(!JsonStringField(batch,"topic",topic,topicAt)||!JsonStringField(batch,"payload",payload,topicAt)){g_SyncApplyError="Cursor-Ereignis konnte nicht gelesen werden";return false;}
        g_SyncApplyError="Cursor-Ereignis wird verarbeitet: "+topic;
        searchAt=topicAt+7;
        if(topic!="room.created"&&topic!="room.updated"&&topic!="room.archived"&&topic!="project.created"&&topic!="project.updated"&&topic!="project.archived"&&topic!="task.created"&&topic!="task.updated"&&topic!="task.status.changed"&&topic!="task.archived"&&topic!="task.approval.requested"&&topic!="task.approval.decided"&&topic!="comment.created"&&topic!="attachment.created"&&topic!="attachment.archived"&&topic!="ticket.created"&&topic!="ticket.updated"&&topic!="ticket.status.changed"&&topic!="ticket.archived"&&topic!="employee.created"&&topic!="employee.updated"&&topic!="employee.presence.changed"&&topic!="employee.archived"&&topic!="organization.created"&&topic!="organization.updated"&&topic!="organization.archived"&&topic!="leave.requested"&&topic!="leave.decided"&&topic!="facility.asset.created"&&topic!="facility.asset.updated"&&topic!="facility.asset.archived"&&topic!="facility.maintenance.created"&&topic!="facility.maintenance.updated"&&topic!="facility.maintenance.status.changed"&&topic!="facility.maintenance.archived")continue;
        std::string id; if(!JsonStringField(payload,"id",id)){g_SyncApplyError="Cursor-Ereignis enthält keine Objekt-ID: "+topic;return false;}
        if(!ownPrefix.empty()&&id.rfind(ownPrefix,0)==0)continue;
        bool active=true; if(!JsonBooleanField(payload,"active",active) && topic.rfind("leave.",0)!=0 && topic!="comment.created"){g_SyncApplyError="Cursor-Ereignis enthält kein aktives Flag: "+topic+" payload="+payload;return false;}
        if(topic.rfind("facility.maintenance.",0)==0){std::string assetId,title,status;int64_t dueAt=0,recurrenceDays=0;if(!JsonStringField(payload,"assetId",assetId)||!JsonStringField(payload,"title",title)||!JsonStringField(payload,"status",status)||!JsonIntegerField(payload,"dueAt",dueAt)||!JsonIntegerField(payload,"recurrenceDays",recurrenceDays))return false;EnterpriseMaintenanceOrder order{0,0,Utf8ToWide(title),Utf8ToWide(status),dueAt,static_cast<int>(recurrenceDays),active};if(!g_Database->applySyncedMaintenanceOrder(Utf8ToWide(id),Utf8ToWide(assetId),order,L"sync.remote"))return false;}
        else if(topic.rfind("facility.asset.",0)==0){std::string name,category,location,status;if(!JsonStringField(payload,"name",name)||!JsonStringField(payload,"category",category)||!JsonStringField(payload,"location",location)||!JsonStringField(payload,"status",status))return false;EnterpriseFacilityAsset asset{0,Utf8ToWide(name),Utf8ToWide(category),Utf8ToWide(location),Utf8ToWide(status),active};if(!g_Database->applySyncedFacilityAsset(Utf8ToWide(id),asset,L"sync.remote"))return false;}
        else if(topic.rfind("leave.",0)==0){int64_t employeeId=0,startsAt=0,endsAt=0,decidedBy=0;std::string reason,status;if(!JsonIntegerField(payload,"employeeId",employeeId)||!JsonIntegerField(payload,"startsAt",startsAt)||!JsonIntegerField(payload,"endsAt",endsAt)||!JsonStringField(payload,"reason",reason)||!JsonStringField(payload,"status",status))return false;JsonIntegerField(payload,"decidedBy",decidedBy);EnterpriseLeaveRequest leave{0,employeeId,startsAt,endsAt,Utf8ToWide(reason),Utf8ToWide(status),decidedBy,active};if(!g_Database->applySyncedLeaveRequest(Utf8ToWide(id),leave,L"sync.remote"))return false;}
        else if(topic.rfind("organization.",0)==0){std::string name,code;int64_t managerId=0;if(!JsonStringField(payload,"name",name)||!JsonStringField(payload,"code",code)||!JsonIntegerField(payload,"managerId",managerId))return false;EnterpriseOrganizationUnit unit{0,Utf8ToWide(name),Utf8ToWide(code),managerId,active};if(!g_Database->applySyncedOrganizationUnit(Utf8ToWide(id),unit,L"sync.remote"))return false;}
        else if(topic.rfind("employee.",0)==0){std::string name,role,department,presence;if(!JsonStringField(payload,"name",name)||!JsonStringField(payload,"role",role)||!JsonStringField(payload,"department",department)||!JsonStringField(payload,"presence",presence))return false;EnterpriseEmployee employee{0,Utf8ToWide(name),Utf8ToWide(role),Utf8ToWide(department),Utf8ToWide(presence),active};if(!g_Database->applySyncedEmployee(Utf8ToWide(id),employee,L"sync.remote"))return false;}
        else if(topic.rfind("room.",0)==0){std::string name,location,equipment;int64_t capacity=0;if(!JsonStringField(payload,"name",name)||!JsonStringField(payload,"location",location)||!JsonStringField(payload,"equipment",equipment)||!JsonIntegerField(payload,"capacity",capacity))return false;EnterpriseRoom room{0,Utf8ToWide(name),Utf8ToWide(location),static_cast<int>(capacity),Utf8ToWide(equipment),active};if(!g_Database->applySyncedRoom(Utf8ToWide(id),room,L"sync.remote"))return false;}
        else if(topic.rfind("project.",0)==0) {std::string name,department,description,status;int64_t ownerId=0;if(!JsonStringField(payload,"name",name)||!JsonStringField(payload,"department",department)||!JsonStringField(payload,"description",description)||!JsonStringField(payload,"status",status)||!JsonIntegerField(payload,"ownerId",ownerId)){g_SyncApplyError="Projektereignis enthält unvollständige Felder: "+payload;return false;}EnterpriseProject project{0,Utf8ToWide(name),Utf8ToWide(department),Utf8ToWide(description),Utf8ToWide(status),ownerId,active};if(!g_Database->applySyncedProject(Utf8ToWide(id),project,L"sync.remote")){g_SyncApplyError="Projektübernahme fehlgeschlagen: "+WideToUtf8(g_Database->error());return false;}}
        else if(topic.rfind("attachment.",0)==0){std::string subjectType,subjectId,originalName,sha256,uploadedBy;int64_t sizeBytes=0;if(!JsonStringField(payload,"subjectType",subjectType)||!JsonStringField(payload,"subjectId",subjectId)||!JsonStringField(payload,"originalName",originalName)||!JsonStringField(payload,"sha256",sha256)||!JsonStringField(payload,"uploadedBy",uploadedBy)||!JsonIntegerField(payload,"sizeBytes",sizeBytes))return false;const int64_t localSubject=g_Database->syncedLocalId(Utf8ToWide(subjectType),Utf8ToWide(subjectId));if(localSubject<=0)return false;const std::filesystem::path localPath=FindWorkspaceRoot()/L"KR_BUILD"/L"attachments"/Utf8ToWide(subjectType)/std::to_wstring(localSubject)/(L"remote_"+Utf8ToWide(sha256));EnterpriseAttachment attachment{0,Utf8ToWide(subjectType),0,Utf8ToWide(originalName),localPath.wstring(),sizeBytes,Utf8ToWide(sha256),0,L"",active};if(!g_Database->applySyncedAttachment(Utf8ToWide(id),Utf8ToWide(subjectId),Utf8ToWide(uploadedBy),attachment,L"sync.remote"))return false;}
        else if(topic=="comment.created"){std::string subjectType,subjectId,authorId,body;if(!JsonStringField(payload,"subjectType",subjectType)||!JsonStringField(payload,"subjectId",subjectId)||!JsonStringField(payload,"authorId",authorId)||!JsonStringField(payload,"body",body))return false;EnterpriseComment comment{0,Utf8ToWide(subjectType),0,0,Utf8ToWide(body),L""};if(!g_Database->applySyncedComment(Utf8ToWide(id),Utf8ToWide(subjectId),Utf8ToWide(authorId),comment,L"sync.remote"))return false;}
        else if(topic.rfind("task.approval.",0)==0){std::string taskId,requestedBy,approverId,status,decidedBy;if(!JsonStringField(payload,"taskId",taskId)||!JsonStringField(payload,"requestedBy",requestedBy)||!JsonStringField(payload,"approverId",approverId)||!JsonStringField(payload,"status",status)||!JsonStringField(payload,"decidedBy",decidedBy))return false;EnterpriseTaskApproval approval{0,0,0,0,Utf8ToWide(status),0,active};if(!g_Database->applySyncedTaskApproval(Utf8ToWide(id),Utf8ToWide(taskId),Utf8ToWide(requestedBy),Utf8ToWide(approverId),Utf8ToWide(decidedBy),approval,L"sync.remote"))return false;}
        else if(topic.rfind("task.",0)==0) {std::string title,department,priority,status;int64_t assignee=0;if(!JsonStringField(payload,"title",title)||!JsonStringField(payload,"department",department)||!JsonStringField(payload,"priority",priority)||!JsonStringField(payload,"status",status)||!JsonIntegerField(payload,"assigneeId",assignee))return false;EnterpriseTask task{0,Utf8ToWide(title),Utf8ToWide(department),Utf8ToWide(priority),Utf8ToWide(status),assignee,active};if(!g_Database->applySyncedTask(Utf8ToWide(id),task,L"sync.remote"))return false;}
        else {std::string title,category,priority,status;int64_t assignee=0;if(!JsonStringField(payload,"title",title)||!JsonStringField(payload,"category",category)||!JsonStringField(payload,"priority",priority)||!JsonStringField(payload,"status",status)||!JsonIntegerField(payload,"assigneeId",assignee))return false;EnterpriseTicket ticket{0,Utf8ToWide(title),Utf8ToWide(category),Utf8ToWide(priority),Utf8ToWide(status),assignee,active};if(!g_Database->applySyncedTicket(Utf8ToWide(id),ticket,L"sync.remote"))return false;}
    }
    return true;
}

bool HasEnterprisePermission(const std::wstring& permission) {
    return g_Authenticated && g_Database && g_CurrentEmployeeId > 0 && g_Database->hasPermission(g_CurrentEmployeeId, permission);
}

bool RequireEnterprisePermission(HWND owner, const std::wstring& permission) {
    if (HasEnterprisePermission(permission)) return true;
    MessageBoxW(owner, (L"Für diese Aktion fehlt die Berechtigung: " + permission).c_str(), L"Zugriff verweigert", MB_ICONWARNING);
    return false;
}

bool RequireWorkflowTransition(HWND owner, const char* kind, const char* from, const char* to) {
    if (aura_validate_workflow_transition(kind, from, to) == 0) return true;
    MessageBoxA(owner, aura_rule_message(5), "Workflow durch Regelwerk abgelehnt", MB_ICONWARNING);
    return false;
}

std::filesystem::path FindWorkspaceRoot() {
    wchar_t modulePath[MAX_PATH] = {};
    GetModuleFileNameW(nullptr, modulePath, MAX_PATH);
    for (std::filesystem::path candidate = std::filesystem::path(modulePath).parent_path(); !candidate.empty(); candidate = candidate.parent_path()) {
        if (std::filesystem::exists(candidate / L"aura.krproject")) return candidate;
        if (candidate == candidate.root_path()) break;
    }
    return std::filesystem::current_path();
}

void RefreshRoomsFromDatabase() {
    if (!g_Database || !g_Database->ready()) return;
    g_State.rooms.clear();
    for (const EnterpriseRoom& room : g_Database->rooms()) {
        OfficeRoom view;
        view.id = room.id;
        view.name = room.name;
        view.capacityLimit = room.capacity;
        view.location = room.location;
        view.equipment = room.equipment;
        view.capacity = std::to_wstring(room.capacity) + L" Personen • " + room.location + L" • " + room.equipment;
        view.isBooked = false;
        view.until = L"Verfügbar";
        EnterpriseBooking booking;
        if (g_Database->activeBookingForRoom(room.id, std::time(nullptr), booking)) {
            view.isBooked = true;
            view.bookingId = booking.id;
            view.blocked = booking.status == L"blocked";
            view.until = booking.status == L"blocked" ? L"Wartungssperre aktiv" : L"Aktuell gebucht";
        }
        g_State.rooms.push_back(view);
    }
}

std::wstring FormatBookingDateTime(int64_t unixTime);

void RefreshTasksFromDatabase() {
    if (!g_Database || !g_Database->ready()) return;
    g_State.tasks.clear();
    for (const EnterpriseTask& task : g_Database->tasks()) {
        g_State.tasks.push_back({task.title, task.department, task.priority, task.status == L"done", task.id, task.status, task.dueAt > 0 ? FormatBookingDateTime(task.dueAt) : L""});
    }
}

void RefreshTicketsFromDatabase() {
    if (!g_Database || !g_Database->ready()) return;
    g_State.tickets.clear();
    for (const EnterpriseTicket& ticket : g_Database->tickets()) {
        std::wstring state = ticket.status == L"open" ? L"Offen" : ticket.status == L"in_progress" ? L"In Bearbeitung" : ticket.status == L"resolved" ? L"Gelöst" : L"Geschlossen";
        g_State.tickets.push_back({ticket.title, ticket.category, state, ticket.id, ticket.priority});
    }
}

void RefreshTeamFromDatabase() {
    if (!g_Database || !g_Database->ready()) return;
    g_State.team.clear();
    for (const EnterpriseEmployee& employee : g_Database->employees()) {
        const bool remote = employee.presence == L"remote";
        const bool meeting = employee.presence == L"meeting";
        const bool away = employee.presence == L"away";
        const std::wstring status = remote ? L"Home-Office" : meeting ? L"Im Meeting" : away ? L"Abwesend" : L"Im Büro";
        g_State.team.push_back({employee.name, employee.role + L" • " + employee.department, status, away ? Theme::STATUS_RED : meeting ? Theme::STATUS_YELLOW : remote ? Theme::ACCENT : Theme::STATUS_GREEN, employee.id});
    }
}

void InitializeEnterpriseData() {
    const std::filesystem::path root = FindWorkspaceRoot();
    std::filesystem::create_directories(root / L"KR_BUILD");
    g_Database = std::make_unique<EnterpriseDatabase>((root / L"KR_BUILD" / L"aura-enterprise.db").wstring());
    if (!g_Database->ready()) return;
    g_SyncService.initialize(root);
    if (g_Database->rooms().empty()) {
        const EnterpriseRoom defaults[] = {
            {0, L"Konferenzraum Alpha", L"Berlin / 2. OG", 12, L"4K Display, Beamer", true},
            {0, L"Fokus-Box 1", L"Berlin / 2. OG", 2, L"Schallisoliert, Whiteboard", true},
            {0, L"Lounge Süd", L"Berlin / 1. OG", 8, L"Casual Seating, Screen", true},
            {0, L"Konferenzraum Beta", L"Berlin / 3. OG", 18, L"Dual Screen, Hybrid-Mic", true}
        };
        for (const EnterpriseRoom& room : defaults) { int64_t ignored = 0; g_Database->createRoom(room, ignored, L"system-seed"); }
    }
    if (g_Database->tasks().empty()) {
        const EnterpriseTask defaults[] = {
            {0, L"Quartalsbericht 2026 auditieren & freigeben", L"Architektur", L"Hoch", L"done", 1, true},
            {0, L"Server-Rack 03 Abwärme-Logging kalibrieren", L"Infrastruktur", L"Hoch", L"in_progress", 1, true},
            {0, L"Home-Office Anträge KW 45 gegenzeichnen", L"HR & Team", L"Mittel", L"open", 1, true},
            {0, L"Neuen Arbeitsplatz Desk 14 Hardware einrichten", L"IT-Support", L"Niedrig", L"done", 1, true},
            {0, L"Dokumentation Notfall-Stromaggregat aktualisieren", L"Facility", L"Mittel", L"open", 1, true}
        };
        for (const EnterpriseTask& task : defaults) { int64_t ignored = 0; g_Database->createTask(task, ignored, L"system-seed"); }
    }
    if (g_Database->projects().empty()) {
        const EnterpriseProject defaults[] = {
            {0, L"Standort Berlin 2026", L"Operations", L"Arbeitsplatz- und Facility-Modernisierung", L"active", 1, true},
            {0, L"Resiliente Office-IT", L"Engineering", L"Netzwerk, Endgeräte und Supportprozesse", L"planned", 1, true}
        };
        for (const EnterpriseProject& project : defaults) { int64_t ignored = 0; g_Database->createProject(project, ignored, L"system-seed"); }
    }
    if (g_Database->tickets().empty()) {
        const EnterpriseTicket defaults[] = {
            {0, L"Kaffeemaschine Küche Ost: Entkalkung erforderlich", L"Facility", L"Mittel", L"in_progress", 1, true},
            {0, L"HDMI-Adapter an Präsentations-Tisch Alpha fehlt", L"IT-Support", L"Niedrig", L"open", 1, true},
            {0, L"Druckerpapier Tray 2 im 2. OG leer", L"Facility", L"Niedrig", L"resolved", 1, true}
        };
        for (const EnterpriseTicket& ticket : defaults) { int64_t ignored = 0; g_Database->createTicket(ticket, ignored, L"system-seed"); }
    }
    if (g_Database->employees().empty()) {
        const EnterpriseEmployee defaults[] = {
            {0, L"Ralf Krümmel", L"Deep-Substrate Architect", L"Architektur", L"office", true},
            {0, L"Sarah Weber", L"Senior Product Lead", L"Produkt", L"remote", true},
            {0, L"Michael Schmidt", L"Head of Operations", L"Operations", L"meeting", true},
            {0, L"Elena Rostova", L"People & Culture", L"HR", L"office", true},
            {0, L"Jonas Becker", L"Low-Level Systems Engineer", L"Engineering", L"away", true}
        };
        for (const EnterpriseEmployee& employee : defaults) { int64_t ignored = 0; g_Database->createEmployee(employee, ignored, L"system-seed"); }
    }
    for (const EnterpriseEmployee& employee : g_Database->employees()) {
        if (employee.name == L"Ralf Krümmel") { g_CurrentEmployeeId = employee.id; g_Database->assignRole(employee.id, L"admin", L"system-seed"); g_Database->setAuditActor(employee.name); break; }
    }
    for(const EnterpriseEmployee& employee:g_Database->employees())if(g_Database->syncedExternalId(L"employee",employee.id).empty())PublishEmployeeChange(employee,"employee.created");
    for(const EnterpriseRoom& room:g_Database->rooms())if(g_Database->syncedExternalId(L"room",room.id).empty())PublishRoomChange(room,"room.created");
    for(const EnterpriseProject& project:g_Database->projects())if(g_Database->syncedExternalId(L"project",project.id).empty())PublishProjectChange(project,"project.created");
    for(const EnterpriseTask& task:g_Database->tasks())if(g_Database->syncedExternalId(L"task",task.id).empty())PublishTaskChange(task,"task.created");
    for(const EnterpriseTicket& ticket:g_Database->tickets())if(g_Database->syncedExternalId(L"ticket",ticket.id).empty())PublishTicketChange(ticket,"ticket.created");
    if(g_Database->accounts(true).empty() && g_CurrentEmployeeId>0){wchar_t bootstrap[256]{};const DWORD count=GetEnvironmentVariableW(L"AURA_BOOTSTRAP_PASSWORD",bootstrap,static_cast<DWORD>(std::size(bootstrap)));if(count>=12&&count<std::size(bootstrap))g_Database->createOrResetAccount(g_CurrentEmployeeId,L"ralf",bootstrap,true,L"system-seed");}
    RefreshRoomsFromDatabase();
    RefreshTasksFromDatabase();
    RefreshTicketsFromDatabase();
    RefreshTeamFromDatabase();
}

// ============================================================================
// 3b. LOKALE PERSISTENZ & AUDIT (C++20 APPLICATION ENGINE)
// ============================================================================
namespace EnterpriseStore {
    std::filesystem::path StateFile() { return FindWorkspaceRoot() / L"KR_BUILD" / L"aura-enterprise-state.txt"; }
    std::filesystem::path AuditFile() { return FindWorkspaceRoot() / L"KR_BUILD" / L"aura-enterprise-audit.log"; }

    std::wstring Timestamp() {
        const std::time_t now = std::time(nullptr);
        std::tm local{};
        localtime_s(&local, &now);
        std::wostringstream value;
        value << std::put_time(&local, L"%Y-%m-%d %H:%M:%S");
        return value.str();
    }

    void Audit(const std::wstring& action, const std::wstring& subject) {
        std::wofstream log(AuditFile(), std::ios::app);
        if (log) log << Timestamp() << L"|system-user|" << action << L"|" << subject << L"\n";
    }

    void Save() {
        std::filesystem::create_directories(FindWorkspaceRoot() / L"KR_BUILD");
        std::wofstream state(StateFile(), std::ios::trunc);
        if (!state) return;
        state << L"AURA_ENTERPRISE_STATE|1\n";
        state << L"PRESENCE|" << static_cast<int>(g_State.userStatus) << L"\n";
        for (size_t i = 0; i < g_State.tasks.size(); ++i)
            state << L"TASK|" << i << L"|" << (g_State.tasks[i].done ? 1 : 0) << L"\n";
    }

    void Load() {
        std::wifstream state(StateFile());
        std::wstring line;
        while (std::getline(state, line)) {
            std::wistringstream fields(line);
            std::wstring kind, index, value, text;
            std::getline(fields, kind, L'|');
            if (kind == L"PRESENCE") {
                std::getline(fields, value, L'|');
                const int status = std::stoi(value);
                if (status >= 0 && status <= 3) g_State.userStatus = static_cast<PresenceStatus>(status);
            } else if (kind == L"TASK") {
                std::getline(fields, index, L'|'); std::getline(fields, value, L'|');
                const size_t i = static_cast<size_t>(std::stoul(index));
                if (i < g_State.tasks.size()) g_State.tasks[i].done = value == L"1";
            }
        }
    }
}

// ============================================================================
// 4. FONT- & RESSOURCEN-VERWALTUNG
// ============================================================================
struct FontManager {
    HFONT hTitle    = nullptr;
    HFONT hSubtitle = nullptr;
    HFONT hSection  = nullptr;
    HFONT hBody     = nullptr;
    HFONT hBodyBold = nullptr;
    HFONT hSmall    = nullptr;
    HFONT hStatBig  = nullptr;

    void Init() {
        hTitle    = CreateCustomFont(22, FW_BOLD);
        hSubtitle = CreateCustomFont(16, FW_SEMIBOLD);
        hSection  = CreateCustomFont(14, FW_BOLD);
        hBody     = CreateCustomFont(13, FW_NORMAL);
        hBodyBold = CreateCustomFont(13, FW_BOLD);
        hSmall    = CreateCustomFont(11, FW_NORMAL);
        hStatBig  = CreateCustomFont(26, FW_BOLD);
    }

    void Cleanup() {
        if (hTitle) DeleteObject(hTitle);
        if (hSubtitle) DeleteObject(hSubtitle);
        if (hSection) DeleteObject(hSection);
        if (hBody) DeleteObject(hBody);
        if (hBodyBold) DeleteObject(hBodyBold);
        if (hSmall) DeleteObject(hSmall);
        if (hStatBig) DeleteObject(hStatBig);
    }

private:
    static HFONT CreateCustomFont(int size, int weight) {
        return CreateFontW(-size, 0, 0, 0, weight, FALSE, FALSE, FALSE,
            DEFAULT_CHARSET, OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS,
            CLEARTYPE_QUALITY, DEFAULT_PITCH | FF_DONTCARE, L"Segoe UI");
    }
} g_Fonts;

// ============================================================================
// 4b. NATIVE RAUMSTAMMDATEN-DIALOGE
// ============================================================================
constexpr int IDC_ROOM_NAME = 5101;
constexpr int IDC_ROOM_LOCATION = 5102;
constexpr int IDC_ROOM_CAPACITY = 5103;
constexpr int IDC_ROOM_EQUIPMENT = 5104;
constexpr int IDC_ROOM_SAVE = 5105;
constexpr int IDC_ROOM_CANCEL = 5106;
constexpr int IDC_ROOM_SEARCH_QUERY = 5107;
constexpr int IDC_ROOM_SEARCH_RESULTS = 5108;
constexpr int IDC_ROOM_SEARCH_EXECUTE = 5109;
constexpr int IDC_ROOM_SEARCH_BOOK = 5110;
constexpr int IDC_ROOM_SEARCH_CLOSE = 5111;
constexpr int IDC_TASK_TITLE = 5201;
constexpr int IDC_TASK_DEPARTMENT = 5202;
constexpr int IDC_TASK_PRIORITY = 5203;
constexpr int IDC_TASK_SAVE = 5204;
constexpr int IDC_TASK_CANCEL = 5205;
constexpr int IDC_TASK_PROJECT = 5206;
constexpr int IDC_TASK_DEPENDENCIES = 5207;
constexpr int IDC_TASK_DUE_AT = 5208;
constexpr int IDC_TASK_RECURRENCE_DAYS = 5209;
constexpr int IDC_TASK_SUBTASKS = 5210;
constexpr int IDC_TICKET_TITLE = 5301;
constexpr int IDC_TICKET_CATEGORY = 5302;
constexpr int IDC_TICKET_PRIORITY = 5303;
constexpr int IDC_TICKET_SAVE = 5304;
constexpr int IDC_TICKET_CANCEL = 5305;
constexpr int IDC_TICKET_ASSIGNEE = 5306;
constexpr int IDC_BOOK_DURATION = 5401;
constexpr int IDC_BOOK_ATTENDEES = 5402;
constexpr int IDC_BOOK_SAVE = 5403;
constexpr int IDC_BOOK_CANCEL = 5404;
constexpr int IDC_BOOK_CANCEL_BOOKING = 5405;
constexpr int IDC_BOOK_SERIES_OCCURRENCES = 5406;
constexpr int IDC_BOOK_SERIES_INTERVAL_DAYS = 5407;
constexpr int IDC_BLOCK_START = 5410;
constexpr int IDC_BLOCK_END = 5411;
constexpr int IDC_BLOCK_REASON = 5412;
constexpr int IDC_BLOCK_SAVE = 5413;
constexpr int IDC_BLOCK_CANCEL = 5414;
constexpr int IDC_EMP_NAME = 5501;
constexpr int IDC_EMP_ROLE = 5502;
constexpr int IDC_EMP_DEPT = 5503;
constexpr int IDC_EMP_SAVE = 5504;
constexpr int IDC_EMP_CANCEL = 5505;
constexpr int IDC_EMP_SYSTEM_ROLE = 5506;
constexpr int IDC_PROJECT_NAME = 5601;
constexpr int IDC_PROJECT_DEPARTMENT = 5602;
constexpr int IDC_PROJECT_DESCRIPTION = 5603;
constexpr int IDC_PROJECT_SAVE = 5604;
constexpr int IDC_PROJECT_CANCEL = 5605;
constexpr int IDC_ORG_NAME = 5701;
constexpr int IDC_ORG_CODE = 5702;
constexpr int IDC_ORG_MANAGER = 5703;
constexpr int IDC_ORG_SAVE = 5704;
constexpr int IDC_ORG_CANCEL = 5705;
constexpr int IDC_ORG_LIST = 5710;
constexpr int IDC_ORG_NEW = 5711;
constexpr int IDC_ORG_EDIT = 5712;
constexpr int IDC_ORG_ARCHIVE = 5713;
constexpr int IDC_ORG_CLOSE = 5714;
constexpr int IDC_SESSION_LIST = 5801;
constexpr int IDC_SESSION_SELECT = 5802;
constexpr int IDC_SESSION_CANCEL = 5803;
constexpr int IDC_SESSION_PASSWORD = 5804;
constexpr int IDC_PASSWORD_NEW = 5811;
constexpr int IDC_PASSWORD_CONFIRM = 5812;
constexpr int IDC_PASSWORD_SAVE = 5813;
constexpr int IDC_PASSWORD_CANCEL = 5814;
constexpr int IDC_ROLE_CODE = 5901;
constexpr int IDC_ROLE_NAME = 5902;
constexpr int IDC_ROLE_DESCRIPTION = 5903;
constexpr int IDC_ROLE_PERMISSIONS = 5904;
constexpr int IDC_ROLE_SAVE = 5905;
constexpr int IDC_ROLE_CANCEL = 5906;
constexpr int IDC_ROLE_LIST = 5910;
constexpr int IDC_ACCOUNT_LIST = 6001;
constexpr int IDC_ACCOUNT_TOGGLE = 6002;
constexpr int IDC_ACCOUNT_RESET = 6003;
constexpr int IDC_ACCOUNT_CLOSE = 6004;
constexpr int IDC_ACCOUNT_NEW = 6005;
constexpr int IDC_ACCOUNT_PASSWORD = 6011;
constexpr int IDC_ACCOUNT_CONFIRM = 6012;
constexpr int IDC_ACCOUNT_SAVE = 6013;
constexpr int IDC_ACCOUNT_CANCEL = 6014;
constexpr int IDC_ACCOUNT_EMPLOYEE = 6015;
constexpr int IDC_ACCOUNT_USERNAME = 6016;
constexpr int IDC_ROLE_NEW = 5911;
constexpr int IDC_ROLE_EDIT = 5912;
constexpr int IDC_ROLE_ARCHIVE = 5913;
constexpr int IDC_ROLE_CLOSE = 5914;
constexpr int IDC_LEAVE_START = 6001;
constexpr int IDC_LEAVE_END = 6002;
constexpr int IDC_LEAVE_REASON = 6003;
constexpr int IDC_LEAVE_SAVE = 6004;
constexpr int IDC_LEAVE_CANCEL = 6005;
constexpr int IDC_LEAVE_LIST = 6010;
constexpr int IDC_LEAVE_APPROVE = 6011;
constexpr int IDC_LEAVE_REJECT = 6012;
constexpr int IDC_LEAVE_CLOSE = 6013;
constexpr int IDC_APPROVAL_LIST = 6101;
constexpr int IDC_APPROVAL_APPROVE = 6102;
constexpr int IDC_APPROVAL_REJECT = 6103;
constexpr int IDC_APPROVAL_CLOSE = 6104;
constexpr int IDC_ASSET_NAME = 6201;
constexpr int IDC_ASSET_CATEGORY = 6202;
constexpr int IDC_ASSET_LOCATION = 6203;
constexpr int IDC_ASSET_STATUS = 6204;
constexpr int IDC_ASSET_SAVE = 6205;
constexpr int IDC_ASSET_CANCEL = 6206;
constexpr int IDC_ASSET_LIST = 6210;
constexpr int IDC_ASSET_NEW = 6211;
constexpr int IDC_ASSET_EDIT = 6212;
constexpr int IDC_ASSET_ARCHIVE = 6213;
constexpr int IDC_ASSET_CLOSE = 6214;
constexpr int IDC_TICKET_COMMENT_LIST = 6301;
constexpr int IDC_TICKET_COMMENT_BODY = 6302;
constexpr int IDC_TICKET_COMMENT_ADD = 6303;
constexpr int IDC_TICKET_COMMENT_CLOSE = 6304;
constexpr int IDC_TASK_COMMENT_LIST = 6311;
constexpr int IDC_TASK_COMMENT_BODY = 6312;
constexpr int IDC_TASK_COMMENT_ADD = 6313;
constexpr int IDC_TASK_COMMENT_CLOSE = 6314;
constexpr int IDC_DEPENDENCY_LIST = 6321;
constexpr int IDC_DEPENDENCY_CANDIDATE = 6322;
constexpr int IDC_DEPENDENCY_ADD = 6323;
constexpr int IDC_DEPENDENCY_REMOVE = 6324;
constexpr int IDC_DEPENDENCY_CLOSE = 6325;
constexpr int IDC_SUBTASK_LIST = 6331;
constexpr int IDC_SUBTASK_TITLE = 6332;
constexpr int IDC_SUBTASK_DUE = 6333;
constexpr int IDC_SUBTASK_ASSIGNEE = 6334;
constexpr int IDC_SUBTASK_STATUS = 6335;
constexpr int IDC_ATTACHMENT_LIST = 6341;
constexpr int IDC_ATTACHMENT_ADD = 6342;
constexpr int IDC_ATTACHMENT_OPEN = 6343;
constexpr int IDC_ATTACHMENT_ARCHIVE = 6344;
constexpr int IDC_ATTACHMENT_CLOSE = 6345;
constexpr int IDC_SUBTASK_SAVE = 6336;
constexpr int IDC_SUBTASK_NEW = 6337;
constexpr int IDC_SUBTASK_ARCHIVE = 6338;
constexpr int IDC_SUBTASK_CLOSE = 6339;
constexpr int IDC_MAINT_TITLE = 6401;
constexpr int IDC_MAINT_DUE = 6402;
constexpr int IDC_MAINT_RECURRENCE = 6403;
constexpr int IDC_MAINT_STATUS = 6404;
constexpr int IDC_MAINT_SAVE = 6405;
constexpr int IDC_MAINT_CANCEL = 6406;
constexpr int IDC_MAINT_LIST = 6410;
constexpr int IDC_MAINT_NEW = 6411;
constexpr int IDC_MAINT_EDIT = 6412;
constexpr int IDC_MAINT_ADVANCE = 6413;
constexpr int IDC_MAINT_ARCHIVE = 6414;
constexpr int IDC_MAINT_CLOSE = 6415;
constexpr int IDC_ASSET_MAINTENANCE = 6215;

struct RoomEditorContext {
    HWND owner = nullptr;
    OfficeRoom room;
    bool editing = false;
};

std::wstring ControlText(HWND hwnd, int id) {
    wchar_t buffer[1024] = {};
    GetWindowTextW(GetDlgItem(hwnd, id), buffer, 1024);
    return buffer;
}

void RoomEditorLabel(HWND parent, const wchar_t* text, int x, int y) {
    CreateWindowW(L"STATIC", text, WS_CHILD | WS_VISIBLE, x, y, 120, 20, parent, nullptr, nullptr, nullptr);
}

LRESULT CALLBACK RoomEditorProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    auto* context = reinterpret_cast<RoomEditorContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    switch (msg) {
    case WM_NCCREATE:
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams));
        return TRUE;
    case WM_CREATE: {
        context = reinterpret_cast<RoomEditorContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
        const wchar_t* title = context->editing ? L"Raum bearbeiten" : L"Neuen Raum anlegen";
        SetWindowTextW(hwnd, title);
        RoomEditorLabel(hwnd, L"Name *", 22, 24);
        RoomEditorLabel(hwnd, L"Standort *", 22, 72);
        RoomEditorLabel(hwnd, L"Kapazität *", 22, 120);
        RoomEditorLabel(hwnd, L"Ausstattung", 22, 168);
        CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", context->room.name.c_str(), WS_CHILD | WS_VISIBLE | ES_AUTOHSCROLL, 22, 44, 390, 24, hwnd, reinterpret_cast<HMENU>(IDC_ROOM_NAME), nullptr, nullptr);
        CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", context->room.location.c_str(), WS_CHILD | WS_VISIBLE | ES_AUTOHSCROLL, 22, 92, 390, 24, hwnd, reinterpret_cast<HMENU>(IDC_ROOM_LOCATION), nullptr, nullptr);
        CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", context->room.capacityLimit ? std::to_wstring(context->room.capacityLimit).c_str() : L"", WS_CHILD | WS_VISIBLE | ES_NUMBER | ES_AUTOHSCROLL, 22, 140, 390, 24, hwnd, reinterpret_cast<HMENU>(IDC_ROOM_CAPACITY), nullptr, nullptr);
        CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", context->room.equipment.c_str(), WS_CHILD | WS_VISIBLE | ES_AUTOHSCROLL, 22, 188, 390, 24, hwnd, reinterpret_cast<HMENU>(IDC_ROOM_EQUIPMENT), nullptr, nullptr);
        CreateWindowW(L"BUTTON", L"Speichern", WS_CHILD | WS_VISIBLE | BS_DEFPUSHBUTTON, 220, 238, 92, 30, hwnd, reinterpret_cast<HMENU>(IDC_ROOM_SAVE), nullptr, nullptr);
        CreateWindowW(L"BUTTON", L"Abbrechen", WS_CHILD | WS_VISIBLE, 320, 238, 92, 30, hwnd, reinterpret_cast<HMENU>(IDC_ROOM_CANCEL), nullptr, nullptr);
        SendMessageW(GetDlgItem(hwnd, IDC_ROOM_NAME), WM_SETFONT, reinterpret_cast<WPARAM>(g_Fonts.hBody), TRUE);
        SetFocus(GetDlgItem(hwnd, IDC_ROOM_NAME));
        return 0;
    }
    case WM_COMMAND:
        if (LOWORD(wParam) == IDC_ROOM_CANCEL) { DestroyWindow(hwnd); return 0; }
        if (LOWORD(wParam) == IDC_ROOM_SAVE && context && g_Database) {
            EnterpriseRoom room;
            room.id = context->room.id;
            room.name = ControlText(hwnd, IDC_ROOM_NAME);
            room.location = ControlText(hwnd, IDC_ROOM_LOCATION);
            room.equipment = ControlText(hwnd, IDC_ROOM_EQUIPMENT);
            try { room.capacity = std::stoi(ControlText(hwnd, IDC_ROOM_CAPACITY)); } catch (...) { room.capacity = 0; }
            int64_t ignored = 0;
            const bool ok = context->editing ? g_Database->updateRoom(room, L"Ralf Krümmel") : g_Database->createRoom(room, ignored, L"Ralf Krümmel");
            if (!ok) { MessageBoxW(hwnd, g_Database->error().c_str(), L"Raum konnte nicht gespeichert werden", MB_ICONERROR); return 0; }
            if (!context->editing) room.id = ignored;
            PublishRoomChange(room, context->editing ? "room.updated" : "room.created");
            RefreshRoomsFromDatabase();
            InvalidateRect(context->owner, nullptr, FALSE);
            DestroyWindow(hwnd); return 0;
        }
        break;
    case WM_CLOSE:
        DestroyWindow(hwnd); return 0;
    case WM_DESTROY:
        if (context && context->owner) { EnableWindow(context->owner, TRUE); SetForegroundWindow(context->owner); }
        delete context;
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wParam, lParam);
}

void ShowRoomEditor(HWND owner, const OfficeRoom* room = nullptr) {
    auto* context = new RoomEditorContext{};
    context->owner = owner;
    context->editing = room != nullptr;
    if (room) context->room = *room;
    EnableWindow(owner, FALSE);
    HWND editor = CreateWindowExW(WS_EX_DLGMODALFRAME, L"AuraRoomEditor", L"Raum", WS_CAPTION | WS_SYSMENU | WS_POPUP | WS_VISIBLE,
        CW_USEDEFAULT, CW_USEDEFAULT, 450, 320, owner, nullptr, GetModuleHandleW(nullptr), context);
    if (!editor) { EnableWindow(owner, TRUE); delete context; }
}

std::wstring FormatBookingDateTime(int64_t unixTime);
bool ParseBookingDateTime(const std::wstring& text, int64_t& unixTime);

struct TaskDependencyContext { HWND owner = nullptr; EnterpriseTask task; std::vector<EnterpriseTask> candidates; std::vector<EnterpriseTaskDependency> dependencies; };
void RefreshTaskDependencyList(HWND hwnd, TaskDependencyContext* context) {
    if (!context || !g_Database) return;
    context->dependencies = g_Database->taskDependencies(context->task.id);
    HWND list = GetDlgItem(hwnd, IDC_DEPENDENCY_LIST); SendMessageW(list, LB_RESETCONTENT, 0, 0);
    for (const EnterpriseTaskDependency& dependency : context->dependencies) {
        std::wstring name = L"Aufgabe #" + std::to_wstring(dependency.dependsOnTaskId);
        for (const EnterpriseTask& task : g_Database->tasks()) if (task.id == dependency.dependsOnTaskId) { name = task.title; break; }
        SendMessageW(list, LB_ADDSTRING, 0, reinterpret_cast<LPARAM>(name.c_str()));
    }
}
LRESULT CALLBACK TaskDependencyProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    auto* context = reinterpret_cast<TaskDependencyContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    switch (msg) {
    case WM_NCCREATE: SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams)); return TRUE;
    case WM_CREATE: {
        context = reinterpret_cast<TaskDependencyContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA)); const std::wstring title = L"Abhängigkeiten: " + context->task.title; SetWindowTextW(hwnd, title.c_str());
        CreateWindowW(L"STATIC", L"Diese Aufgabe ist blockiert durch:", WS_CHILD | WS_VISIBLE, 20, 18, 420, 20, hwnd, nullptr, nullptr, nullptr);
        CreateWindowExW(WS_EX_CLIENTEDGE, L"LISTBOX", L"", WS_CHILD | WS_VISIBLE | WS_VSCROLL, 20, 40, 500, 150, hwnd, reinterpret_cast<HMENU>(IDC_DEPENDENCY_LIST), nullptr, nullptr);
        CreateWindowW(L"STATIC", L"Neue Voraussetzung", WS_CHILD | WS_VISIBLE, 20, 204, 200, 20, hwnd, nullptr, nullptr, nullptr);
        HWND candidate = CreateWindowW(L"COMBOBOX", L"", WS_CHILD | WS_VISIBLE | CBS_DROPDOWNLIST, 20, 224, 500, 130, hwnd, reinterpret_cast<HMENU>(IDC_DEPENDENCY_CANDIDATE), nullptr, nullptr);
        if (g_Database) for (const EnterpriseTask& task : g_Database->tasks()) if (task.id != context->task.id) { context->candidates.push_back(task); const std::wstring label = task.title + L" — " + task.department; SendMessageW(candidate, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(label.c_str())); }
        if (!context->candidates.empty()) SendMessageW(candidate, CB_SETCURSEL, 0, 0);
        CreateWindowW(L"BUTTON", L"Hinzufügen", WS_CHILD | WS_VISIBLE | BS_DEFPUSHBUTTON, 218, 274, 100, 30, hwnd, reinterpret_cast<HMENU>(IDC_DEPENDENCY_ADD), nullptr, nullptr);
        CreateWindowW(L"BUTTON", L"Entfernen", WS_CHILD | WS_VISIBLE, 328, 274, 90, 30, hwnd, reinterpret_cast<HMENU>(IDC_DEPENDENCY_REMOVE), nullptr, nullptr);
        CreateWindowW(L"BUTTON", L"Schließen", WS_CHILD | WS_VISIBLE, 428, 274, 92, 30, hwnd, reinterpret_cast<HMENU>(IDC_DEPENDENCY_CLOSE), nullptr, nullptr);
        RefreshTaskDependencyList(hwnd, context); return 0;
    }
    case WM_COMMAND:
        if (LOWORD(wParam) == IDC_DEPENDENCY_CLOSE) { DestroyWindow(hwnd); return 0; }
        if (LOWORD(wParam) == IDC_DEPENDENCY_ADD && context && g_Database) {
            const int selected = static_cast<int>(SendMessageW(GetDlgItem(hwnd, IDC_DEPENDENCY_CANDIDATE), CB_GETCURSEL, 0, 0));
            if (selected < 0 || static_cast<size_t>(selected) >= context->candidates.size()) { MessageBoxW(hwnd, L"Bitte eine Voraussetzung auswählen.", L"Abhängigkeit", MB_ICONINFORMATION); return 0; }
            if (!g_Database->addTaskDependency(context->task.id, context->candidates[static_cast<size_t>(selected)].id, L"Ralf Krümmel")) { MessageBoxW(hwnd, g_Database->error().c_str(), L"Abhängigkeit", MB_ICONERROR); return 0; }
            RefreshTaskDependencyList(hwnd, context); return 0;
        }
        if (LOWORD(wParam) == IDC_DEPENDENCY_REMOVE && context && g_Database) {
            const int selected = static_cast<int>(SendMessageW(GetDlgItem(hwnd, IDC_DEPENDENCY_LIST), LB_GETCURSEL, 0, 0));
            if (selected < 0 || static_cast<size_t>(selected) >= context->dependencies.size()) { MessageBoxW(hwnd, L"Bitte zuerst eine Abhängigkeit auswählen.", L"Abhängigkeit", MB_ICONINFORMATION); return 0; }
            if (!g_Database->removeTaskDependency(context->task.id, context->dependencies[static_cast<size_t>(selected)].dependsOnTaskId, L"Ralf Krümmel")) { MessageBoxW(hwnd, g_Database->error().c_str(), L"Abhängigkeit", MB_ICONERROR); return 0; }
            RefreshTaskDependencyList(hwnd, context); return 0;
        }
        break;
    case WM_CLOSE: DestroyWindow(hwnd); return 0;
    case WM_DESTROY: if (context && context->owner) { EnableWindow(context->owner, TRUE); SetForegroundWindow(context->owner); } delete context; return 0;
    }
    return DefWindowProcW(hwnd, msg, wParam, lParam);
}
void ShowTaskDependencyManager(HWND owner, const EnterpriseTask& task) { auto* context = new TaskDependencyContext{owner, task}; EnableWindow(owner, FALSE); if (!CreateWindowExW(WS_EX_DLGMODALFRAME, L"AuraTaskDependencies", L"Aufgabenabhängigkeiten", WS_CAPTION | WS_SYSMENU | WS_POPUP | WS_VISIBLE, CW_USEDEFAULT, CW_USEDEFAULT, 550, 350, owner, nullptr, GetModuleHandleW(nullptr), context)) { EnableWindow(owner, TRUE); delete context; } }

struct SubtaskManagerContext { HWND owner = nullptr; EnterpriseTask task; std::vector<EnterpriseSubtask> entries; std::vector<EnterpriseEmployee> employees; };
void RefreshSubtaskList(HWND hwnd, SubtaskManagerContext* context) {
    if (!context || !g_Database) return;
    context->entries = g_Database->subtasks(context->task.id);
    HWND list = GetDlgItem(hwnd, IDC_SUBTASK_LIST); SendMessageW(list, LB_RESETCONTENT, 0, 0);
    for (const EnterpriseSubtask& item : context->entries) {
        const std::wstring state = item.status == L"in_progress" ? L"In Bearbeitung" : item.status == L"done" ? L"Erledigt" : item.status == L"cancelled" ? L"Storniert" : L"Offen";
        SendMessageW(list, LB_ADDSTRING, 0, reinterpret_cast<LPARAM>((state + L" | " + item.title + (item.dueAt > 0 ? L" | " + FormatBookingDateTime(item.dueAt) : L"")).c_str()));
    }
}
void PopulateSubtaskEditor(HWND hwnd, SubtaskManagerContext* context, const EnterpriseSubtask* item) {
    SetWindowTextW(GetDlgItem(hwnd, IDC_SUBTASK_TITLE), item ? item->title.c_str() : L"");
    SetWindowTextW(GetDlgItem(hwnd, IDC_SUBTASK_DUE), item && item->dueAt > 0 ? FormatBookingDateTime(item->dueAt).c_str() : L"");
    int employeeSelection = 0; if (item) for (size_t index = 0; index < context->employees.size(); ++index) if (context->employees[index].id == item->assigneeId) employeeSelection = static_cast<int>(index + 1);
    SendMessageW(GetDlgItem(hwnd, IDC_SUBTASK_ASSIGNEE), CB_SETCURSEL, employeeSelection, 0);
    const int statusSelection = item && item->status == L"in_progress" ? 1 : item && item->status == L"done" ? 2 : item && item->status == L"cancelled" ? 3 : 0;
    SendMessageW(GetDlgItem(hwnd, IDC_SUBTASK_STATUS), CB_SETCURSEL, statusSelection, 0);
}
LRESULT CALLBACK SubtaskManagerProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    auto* context = reinterpret_cast<SubtaskManagerContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    switch (msg) {
    case WM_NCCREATE: SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams)); return TRUE;
    case WM_CREATE: {
        context = reinterpret_cast<SubtaskManagerContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA)); SetWindowTextW(hwnd, (L"Unteraufgaben: " + context->task.title).c_str()); if (g_Database) context->employees = g_Database->employees();
        CreateWindowExW(WS_EX_CLIENTEDGE, L"LISTBOX", L"", WS_CHILD | WS_VISIBLE | WS_VSCROLL | LBS_NOTIFY, 18, 18, 584, 130, hwnd, reinterpret_cast<HMENU>(IDC_SUBTASK_LIST), nullptr, nullptr);
        RoomEditorLabel(hwnd,L"Titel *",18,164); RoomEditorLabel(hwnd,L"Fällig am",18,212); RoomEditorLabel(hwnd,L"Zuständig",312,164); RoomEditorLabel(hwnd,L"Status",312,212);
        CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",L"",WS_CHILD|WS_VISIBLE,18,184,278,24,hwnd,reinterpret_cast<HMENU>(IDC_SUBTASK_TITLE),nullptr,nullptr);
        CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",L"",WS_CHILD|WS_VISIBLE,18,232,278,24,hwnd,reinterpret_cast<HMENU>(IDC_SUBTASK_DUE),nullptr,nullptr);
        HWND assignee=CreateWindowW(L"COMBOBOX",L"",WS_CHILD|WS_VISIBLE|CBS_DROPDOWNLIST,312,184,290,120,hwnd,reinterpret_cast<HMENU>(IDC_SUBTASK_ASSIGNEE),nullptr,nullptr); SendMessageW(assignee,CB_ADDSTRING,0,reinterpret_cast<LPARAM>(L"Nicht zugewiesen")); for(const EnterpriseEmployee& employee:context->employees)SendMessageW(assignee,CB_ADDSTRING,0,reinterpret_cast<LPARAM>(employee.name.c_str())); SendMessageW(assignee,CB_SETCURSEL,0,0);
        HWND status=CreateWindowW(L"COMBOBOX",L"",WS_CHILD|WS_VISIBLE|CBS_DROPDOWNLIST,312,232,290,120,hwnd,reinterpret_cast<HMENU>(IDC_SUBTASK_STATUS),nullptr,nullptr); for(const wchar_t* label:{L"Offen",L"In Bearbeitung",L"Erledigt",L"Storniert"})SendMessageW(status,CB_ADDSTRING,0,reinterpret_cast<LPARAM>(label)); SendMessageW(status,CB_SETCURSEL,0,0);
        CreateWindowW(L"BUTTON",L"Neu",WS_CHILD|WS_VISIBLE,18,284,80,30,hwnd,reinterpret_cast<HMENU>(IDC_SUBTASK_NEW),nullptr,nullptr); CreateWindowW(L"BUTTON",L"Speichern",WS_CHILD|WS_VISIBLE|BS_DEFPUSHBUTTON,328,284,90,30,hwnd,reinterpret_cast<HMENU>(IDC_SUBTASK_SAVE),nullptr,nullptr); CreateWindowW(L"BUTTON",L"Archivieren",WS_CHILD|WS_VISIBLE,428,284,90,30,hwnd,reinterpret_cast<HMENU>(IDC_SUBTASK_ARCHIVE),nullptr,nullptr); CreateWindowW(L"BUTTON",L"Schließen",WS_CHILD|WS_VISIBLE,528,284,74,30,hwnd,reinterpret_cast<HMENU>(IDC_SUBTASK_CLOSE),nullptr,nullptr); RefreshSubtaskList(hwnd,context); return 0;
    }
    case WM_COMMAND: {
        const int command=LOWORD(wParam);
        if(command==IDC_SUBTASK_CLOSE){DestroyWindow(hwnd);return 0;}
        if(command==IDC_SUBTASK_LIST && HIWORD(wParam)==LBN_SELCHANGE && context){const int selection=static_cast<int>(SendMessageW(GetDlgItem(hwnd,IDC_SUBTASK_LIST),LB_GETCURSEL,0,0));if(selection>=0&&static_cast<size_t>(selection)<context->entries.size())PopulateSubtaskEditor(hwnd,context,&context->entries[static_cast<size_t>(selection)]);return 0;}
        if(command==IDC_SUBTASK_NEW&&context){SendMessageW(GetDlgItem(hwnd,IDC_SUBTASK_LIST),LB_SETCURSEL,static_cast<WPARAM>(-1),0);PopulateSubtaskEditor(hwnd,context,nullptr);return 0;}
        if(command==IDC_SUBTASK_ARCHIVE&&context&&g_Database){const int selection=static_cast<int>(SendMessageW(GetDlgItem(hwnd,IDC_SUBTASK_LIST),LB_GETCURSEL,0,0));if(selection<0||static_cast<size_t>(selection)>=context->entries.size()){MessageBoxW(hwnd,L"Bitte eine Unteraufgabe auswählen.",L"Unteraufgaben",MB_ICONINFORMATION);return 0;}if(!g_Database->archiveSubtask(context->entries[static_cast<size_t>(selection)].id,L"Ralf Krümmel")){MessageBoxW(hwnd,g_Database->error().c_str(),L"Unteraufgaben",MB_ICONERROR);return 0;}RefreshSubtaskList(hwnd,context);PopulateSubtaskEditor(hwnd,context,nullptr);return 0;}
        if(command==IDC_SUBTASK_SAVE&&context&&g_Database){const int selection=static_cast<int>(SendMessageW(GetDlgItem(hwnd,IDC_SUBTASK_LIST),LB_GETCURSEL,0,0));EnterpriseSubtask item=selection>=0&&static_cast<size_t>(selection)<context->entries.size()?context->entries[static_cast<size_t>(selection)]:EnterpriseSubtask{};item.taskId=context->task.id;item.title=ControlText(hwnd,IDC_SUBTASK_TITLE);item.dueAt=0;const std::wstring due=ControlText(hwnd,IDC_SUBTASK_DUE);if(!due.empty()&&!ParseBookingDateTime(due,item.dueAt)){MessageBoxW(hwnd,L"Fälligkeit im Format JJJJ-MM-TT SS:MM eingeben.",L"Unteraufgaben",MB_ICONERROR);return 0;}const int employeeSelection=static_cast<int>(SendMessageW(GetDlgItem(hwnd,IDC_SUBTASK_ASSIGNEE),CB_GETCURSEL,0,0));item.assigneeId=employeeSelection>0&&static_cast<size_t>(employeeSelection-1)<context->employees.size()?context->employees[static_cast<size_t>(employeeSelection-1)].id:0;const int state=static_cast<int>(SendMessageW(GetDlgItem(hwnd,IDC_SUBTASK_STATUS),CB_GETCURSEL,0,0));item.status=state==1?L"in_progress":state==2?L"done":state==3?L"cancelled":L"open";int64_t newId=0;const bool ok=item.id>0?g_Database->updateSubtask(item,L"Ralf Krümmel"):g_Database->createSubtask(item,newId,L"Ralf Krümmel");if(!ok){MessageBoxW(hwnd,g_Database->error().c_str(),L"Unteraufgaben",MB_ICONERROR);return 0;}RefreshSubtaskList(hwnd,context);PopulateSubtaskEditor(hwnd,context,nullptr);return 0;}
        break;
    }
    case WM_CLOSE: DestroyWindow(hwnd);return 0;
    case WM_DESTROY: if(context&&context->owner){EnableWindow(context->owner,TRUE);SetForegroundWindow(context->owner);}delete context;return 0;
    } return DefWindowProcW(hwnd,msg,wParam,lParam);
}
void ShowSubtaskManager(HWND owner,const EnterpriseTask& task){auto* context=new SubtaskManagerContext{owner,task};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraSubtaskManager",L"Unteraufgaben",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,630,355,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}

struct TaskEditorContext { HWND owner = nullptr; EnterpriseTask task; bool editing = false; int64_t projectId = 0; std::vector<EnterpriseProject> projects; };
void ShowAttachmentManager(HWND owner, const std::wstring& subjectType, int64_t subjectId, const std::wstring& subjectTitle);

LRESULT CALLBACK TaskEditorProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    auto* context = reinterpret_cast<TaskEditorContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    switch (msg) {
    case WM_NCCREATE:
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams)); return TRUE;
    case WM_CREATE: {
        context = reinterpret_cast<TaskEditorContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA)); SetWindowTextW(hwnd, context && context->editing ? L"Aufgabe bearbeiten" : L"Neue Aufgabe anlegen"); if (g_Database) { context->projects = g_Database->projects(); if(context->editing) for(const EnterpriseProject& item:context->projects) for(const EnterpriseTask& linked:g_Database->tasksForProject(item.id)) if(linked.id==context->task.id){context->projectId=item.id;break;} }
        RoomEditorLabel(hwnd, L"Titel *", 22, 24); RoomEditorLabel(hwnd, L"Bereich *", 22, 72); RoomEditorLabel(hwnd, L"Priorität *", 22, 120); RoomEditorLabel(hwnd, L"Projekt", 22, 168); RoomEditorLabel(hwnd, L"Fällig am (JJJJ-MM-TT SS:MM)", 22, 216); RoomEditorLabel(hwnd, L"Wiederholung (Tage, 0 = keine)", 22, 264);
        CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", context ? context->task.title.c_str() : L"", WS_CHILD | WS_VISIBLE | ES_AUTOHSCROLL, 22, 44, 390, 24, hwnd, reinterpret_cast<HMENU>(IDC_TASK_TITLE), nullptr, nullptr);
        CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", context ? context->task.department.c_str() : L"", WS_CHILD | WS_VISIBLE | ES_AUTOHSCROLL, 22, 92, 390, 24, hwnd, reinterpret_cast<HMENU>(IDC_TASK_DEPARTMENT), nullptr, nullptr);
        HWND priority = CreateWindowW(L"COMBOBOX", L"", WS_CHILD | WS_VISIBLE | CBS_DROPDOWNLIST, 22, 140, 390, 150, hwnd, reinterpret_cast<HMENU>(IDC_TASK_PRIORITY), nullptr, nullptr);
        SendMessageW(priority, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(L"Niedrig")); SendMessageW(priority, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(L"Mittel")); SendMessageW(priority, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(L"Hoch")); SendMessageW(priority, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(L"Kritisch")); SendMessageW(priority, CB_SETCURSEL, context && context->task.priority == L"Niedrig" ? 0 : context && context->task.priority == L"Hoch" ? 2 : context && context->task.priority == L"Kritisch" ? 3 : 1, 0);
        HWND project = CreateWindowW(L"COMBOBOX", L"", WS_CHILD | WS_VISIBLE | CBS_DROPDOWNLIST, 22, 188, 390, 150, hwnd, reinterpret_cast<HMENU>(IDC_TASK_PROJECT), nullptr, nullptr); SendMessageW(project, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(L"Ohne Projekt")); int projectSelection=0; for(size_t index=0;index<context->projects.size();++index){const EnterpriseProject& item=context->projects[index];SendMessageW(project,CB_ADDSTRING,0,reinterpret_cast<LPARAM>(item.name.c_str()));if(item.id==context->projectId)projectSelection=static_cast<int>(index+1);} SendMessageW(project,CB_SETCURSEL,projectSelection,0);
        const std::wstring dueAt = context && context->task.dueAt > 0 ? FormatBookingDateTime(context->task.dueAt) : L"";
        CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", dueAt.c_str(), WS_CHILD | WS_VISIBLE | ES_AUTOHSCROLL, 22, 236, 390, 24, hwnd, reinterpret_cast<HMENU>(IDC_TASK_DUE_AT), nullptr, nullptr);
        CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", std::to_wstring(context ? context->task.recurrenceDays : 0).c_str(), WS_CHILD | WS_VISIBLE | ES_NUMBER, 22, 284, 390, 24, hwnd, reinterpret_cast<HMENU>(IDC_TASK_RECURRENCE_DAYS), nullptr, nullptr);
        if (context && context->editing) { CreateWindowW(L"BUTTON", L"Abhängigkeiten", WS_CHILD | WS_VISIBLE, 22, 332, 130, 30, hwnd, reinterpret_cast<HMENU>(IDC_TASK_DEPENDENCIES), nullptr, nullptr); CreateWindowW(L"BUTTON", L"Unteraufgaben", WS_CHILD | WS_VISIBLE, 160, 332, 105, 30, hwnd, reinterpret_cast<HMENU>(IDC_TASK_SUBTASKS), nullptr, nullptr); CreateWindowW(L"BUTTON", L"Anhänge", WS_CHILD | WS_VISIBLE, 275, 332, 105, 30, hwnd, reinterpret_cast<HMENU>(IDC_ATTACHMENT_ADD), nullptr, nullptr); }
        const int actionY=context&&context->editing?370:332;
        CreateWindowW(L"BUTTON", L"Speichern", WS_CHILD | WS_VISIBLE | BS_DEFPUSHBUTTON, 220, actionY, 92, 30, hwnd, reinterpret_cast<HMENU>(IDC_TASK_SAVE), nullptr, nullptr);
        CreateWindowW(L"BUTTON", L"Abbrechen", WS_CHILD | WS_VISIBLE, 320, actionY, 92, 30, hwnd, reinterpret_cast<HMENU>(IDC_TASK_CANCEL), nullptr, nullptr);
        SetFocus(GetDlgItem(hwnd, IDC_TASK_TITLE)); return 0;
    }
    case WM_COMMAND:
        if (LOWORD(wParam) == IDC_TASK_CANCEL) { DestroyWindow(hwnd); return 0; }
        if (LOWORD(wParam) == IDC_TASK_DEPENDENCIES && context && context->editing) { ShowTaskDependencyManager(hwnd, context->task); return 0; }
        if (LOWORD(wParam) == IDC_TASK_SUBTASKS && context && context->editing) { ShowSubtaskManager(hwnd, context->task); return 0; }
        if (LOWORD(wParam) == IDC_ATTACHMENT_ADD && context && context->editing) { ShowAttachmentManager(hwnd, L"task", context->task.id, context->task.title); return 0; }
        if (LOWORD(wParam) == IDC_TASK_SAVE && context && g_Database) {
            EnterpriseTask task=context->task; task.title = ControlText(hwnd, IDC_TASK_TITLE); task.department = ControlText(hwnd, IDC_TASK_DEPARTMENT); task.priority = ControlText(hwnd, IDC_TASK_PRIORITY); if(task.status.empty())task.status = L"open"; if(task.assigneeId==0)task.assigneeId = g_CurrentEmployeeId;
            const std::wstring dueText = ControlText(hwnd, IDC_TASK_DUE_AT); task.dueAt = 0; if (!dueText.empty() && !ParseBookingDateTime(dueText, task.dueAt)) { MessageBoxW(hwnd, L"Die Fälligkeit muss im Format JJJJ-MM-TT SS:MM angegeben werden.", L"Ungültige Fälligkeit", MB_ICONERROR); return 0; } try { task.recurrenceDays = std::stoi(ControlText(hwnd, IDC_TASK_RECURRENCE_DAYS)); } catch (...) { task.recurrenceDays = -1; }
            int64_t ignored = 0;
            const bool ok=context->editing?g_Database->updateTask(task,L"Ralf Krümmel"):g_Database->createTask(task, ignored, L"Ralf Krümmel"); if (!ok) { MessageBoxW(hwnd, g_Database->error().c_str(), L"Aufgabe konnte nicht gespeichert werden", MB_ICONERROR); return 0; } if(!context->editing)task.id=ignored;
            const int selection = static_cast<int>(SendMessageW(GetDlgItem(hwnd, IDC_TASK_PROJECT), CB_GETCURSEL, 0, 0));
            const int64_t taskId=context->editing?task.id:ignored; const int64_t projectId=selection>0?context->projects[static_cast<size_t>(selection-1)].id:0;
            if (!g_Database->setTaskProject(taskId, projectId, L"Ralf Krümmel")) { MessageBoxW(hwnd, g_Database->error().c_str(), L"Projektzuordnung", MB_ICONERROR); return 0; }
            PublishTaskChange(task,context->editing?"task.updated":"task.created");
            RefreshTasksFromDatabase(); InvalidateRect(context->owner, nullptr, FALSE); DestroyWindow(hwnd); return 0;
        }
        break;
    case WM_CLOSE: DestroyWindow(hwnd); return 0;
    case WM_DESTROY: if (context && context->owner) { EnableWindow(context->owner, TRUE); SetForegroundWindow(context->owner); } delete context; SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0); return 0;
    }
    return DefWindowProcW(hwnd, msg, wParam, lParam);
}

void ShowTaskEditor(HWND owner,const EnterpriseTask* task=nullptr) {
    auto* context = new TaskEditorContext{owner,task?*task:EnterpriseTask{},task!=nullptr,0}; EnableWindow(owner, FALSE);
    if (!CreateWindowExW(WS_EX_DLGMODALFRAME, L"AuraTaskEditor", L"Aufgabe", WS_CAPTION | WS_SYSMENU | WS_POPUP | WS_VISIBLE, CW_USEDEFAULT, CW_USEDEFAULT, 450, 455, owner, nullptr, GetModuleHandleW(nullptr), context)) { EnableWindow(owner, TRUE); delete context; }
}

struct TicketEditorContext { HWND owner = nullptr; EnterpriseTicket ticket; bool editing = false; std::vector<EnterpriseEmployee> employees; };
LRESULT CALLBACK TicketEditorProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    auto* context = reinterpret_cast<TicketEditorContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    switch (msg) {
    case WM_NCCREATE: SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams)); return TRUE;
    case WM_CREATE: {
        context = reinterpret_cast<TicketEditorContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
        if(g_Database)context->employees=g_Database->employees(); SetWindowTextW(hwnd, context && context->editing ? L"Helpdesk-Ticket bearbeiten" : L"Neues Helpdesk-Ticket");
        RoomEditorLabel(hwnd, L"Titel *", 22, 24); RoomEditorLabel(hwnd, L"Kategorie *", 22, 72); RoomEditorLabel(hwnd, L"Priorität *", 22, 120); RoomEditorLabel(hwnd, L"Zuständig *", 22, 168);
        CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", context ? context->ticket.title.c_str() : L"", WS_CHILD | WS_VISIBLE | ES_AUTOHSCROLL, 22, 44, 390, 24, hwnd, reinterpret_cast<HMENU>(IDC_TICKET_TITLE), nullptr, nullptr);
        HWND category = CreateWindowW(L"COMBOBOX", L"", WS_CHILD | WS_VISIBLE | CBS_DROPDOWNLIST, 22, 92, 390, 120, hwnd, reinterpret_cast<HMENU>(IDC_TICKET_CATEGORY), nullptr, nullptr);
        SendMessageW(category, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(L"IT-Support")); SendMessageW(category, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(L"Facility")); SendMessageW(category, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(L"Sicherheit")); SendMessageW(category, CB_SETCURSEL, context && context->ticket.category == L"Facility" ? 1 : context && context->ticket.category == L"Sicherheit" ? 2 : 0, 0);
        HWND priority = CreateWindowW(L"COMBOBOX", L"", WS_CHILD | WS_VISIBLE | CBS_DROPDOWNLIST, 22, 140, 390, 120, hwnd, reinterpret_cast<HMENU>(IDC_TICKET_PRIORITY), nullptr, nullptr);
        SendMessageW(priority, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(L"Niedrig")); SendMessageW(priority, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(L"Mittel")); SendMessageW(priority, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(L"Hoch")); SendMessageW(priority, CB_ADDSTRING, 0, reinterpret_cast<LPARAM>(L"Kritisch")); SendMessageW(priority, CB_SETCURSEL, context && context->ticket.priority == L"Niedrig" ? 0 : context && context->ticket.priority == L"Hoch" ? 2 : context && context->ticket.priority == L"Kritisch" ? 3 : 1, 0);
        HWND assignee=CreateWindowW(L"COMBOBOX",L"",WS_CHILD|WS_VISIBLE|CBS_DROPDOWNLIST,22,188,390,120,hwnd,reinterpret_cast<HMENU>(IDC_TICKET_ASSIGNEE),nullptr,nullptr);int selected=0;for(size_t index=0;index<context->employees.size();++index){const EnterpriseEmployee& employee=context->employees[index];SendMessageW(assignee,CB_ADDSTRING,0,reinterpret_cast<LPARAM>(employee.name.c_str()));if(employee.id==(context->ticket.assigneeId?context->ticket.assigneeId:g_CurrentEmployeeId))selected=static_cast<int>(index);}SendMessageW(assignee,CB_SETCURSEL,selected,0);
        if(context&&context->editing)CreateWindowW(L"BUTTON",L"Anhänge",WS_CHILD|WS_VISIBLE,22,253,105,30,hwnd,reinterpret_cast<HMENU>(IDC_ATTACHMENT_ADD),nullptr,nullptr);
        CreateWindowW(L"BUTTON", L"Speichern", WS_CHILD | WS_VISIBLE | BS_DEFPUSHBUTTON, 220, 295, 92, 30, hwnd, reinterpret_cast<HMENU>(IDC_TICKET_SAVE), nullptr, nullptr); CreateWindowW(L"BUTTON", L"Abbrechen", WS_CHILD | WS_VISIBLE, 320, 295, 92, 30, hwnd, reinterpret_cast<HMENU>(IDC_TICKET_CANCEL), nullptr, nullptr); return 0;
    }
    case WM_COMMAND:
        if (LOWORD(wParam) == IDC_TICKET_CANCEL) { DestroyWindow(hwnd); return 0; }
        if (LOWORD(wParam) == IDC_ATTACHMENT_ADD && context && context->editing) { ShowAttachmentManager(hwnd,L"ticket",context->ticket.id,context->ticket.title); return 0; }
        if (LOWORD(wParam) == IDC_TICKET_SAVE && context && g_Database) { EnterpriseTicket ticket=context->ticket; ticket.title = ControlText(hwnd, IDC_TICKET_TITLE); ticket.category = ControlText(hwnd, IDC_TICKET_CATEGORY); ticket.priority = ControlText(hwnd, IDC_TICKET_PRIORITY); const int selected=static_cast<int>(SendMessageW(GetDlgItem(hwnd,IDC_TICKET_ASSIGNEE),CB_GETCURSEL,0,0)); if(selected<0||static_cast<size_t>(selected)>=context->employees.size()){MessageBoxW(hwnd,L"Bitte eine zuständige Person auswählen.",L"Ticket",MB_ICONERROR);return 0;}ticket.assigneeId=context->employees[static_cast<size_t>(selected)].id; if(ticket.status.empty())ticket.status = L"open"; int64_t ignored = 0; const bool ok=context->editing?g_Database->updateTicket(ticket,L"Ralf Krümmel"):g_Database->createTicket(ticket, ignored, L"Ralf Krümmel"); if (!ok) { MessageBoxW(hwnd, g_Database->error().c_str(), L"Ticket konnte nicht gespeichert werden", MB_ICONERROR); return 0; } if(!context->editing)ticket.id=ignored; PublishTicketChange(ticket,context->editing?"ticket.updated":"ticket.created"); RefreshTicketsFromDatabase(); InvalidateRect(context->owner, nullptr, FALSE); DestroyWindow(hwnd); return 0; }
        break;
    case WM_CLOSE: DestroyWindow(hwnd); return 0;
    case WM_DESTROY: if (context && context->owner) { EnableWindow(context->owner, TRUE); SetForegroundWindow(context->owner); } delete context; SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0); return 0;
    } return DefWindowProcW(hwnd, msg, wParam, lParam);
}
void ShowTicketEditor(HWND owner,const EnterpriseTicket* ticket=nullptr) { auto* context = new TicketEditorContext{owner,ticket?*ticket:EnterpriseTicket{},ticket!=nullptr}; EnableWindow(owner, FALSE); if (!CreateWindowExW(WS_EX_DLGMODALFRAME, L"AuraTicketEditor", L"Ticket", WS_CAPTION | WS_SYSMENU | WS_POPUP | WS_VISIBLE, CW_USEDEFAULT, CW_USEDEFAULT, 450, 380, owner, nullptr, GetModuleHandleW(nullptr), context)) { EnableWindow(owner, TRUE); delete context; } }

struct TicketCommentContext { HWND owner = nullptr; EnterpriseTicket ticket; };
void RefreshTicketComments(HWND hwnd, TicketCommentContext* context) {
    if (!context || !g_Database) return;
    HWND list = GetDlgItem(hwnd, IDC_TICKET_COMMENT_LIST); SendMessageW(list, LB_RESETCONTENT, 0, 0);
    for (const EnterpriseComment& comment : g_Database->commentsFor(L"ticket", context->ticket.id)) {
        std::wstring author = L"Mitarbeiter #" + std::to_wstring(comment.authorId);
        for (const EnterpriseEmployee& employee : g_Database->employees()) if (employee.id == comment.authorId) { author = employee.name; break; }
        const std::wstring row = comment.createdAt + L" — " + author + L": " + comment.body;
        SendMessageW(list, LB_ADDSTRING, 0, reinterpret_cast<LPARAM>(row.c_str()));
    }
}
LRESULT CALLBACK TicketCommentProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    auto* context = reinterpret_cast<TicketCommentContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    switch (msg) {
    case WM_NCCREATE: SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams)); return TRUE;
    case WM_CREATE: {
        context = reinterpret_cast<TicketCommentContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA)); const std::wstring title = L"Ticket-Verlauf: " + context->ticket.title; SetWindowTextW(hwnd, title.c_str());
        CreateWindowExW(WS_EX_CLIENTEDGE, L"LISTBOX", L"", WS_CHILD | WS_VISIBLE | WS_VSCROLL, 18, 18, 540, 170, hwnd, reinterpret_cast<HMENU>(IDC_TICKET_COMMENT_LIST), nullptr, nullptr);
        CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", L"", WS_CHILD | WS_VISIBLE | ES_MULTILINE | ES_AUTOVSCROLL | WS_VSCROLL, 18, 204, 540, 70, hwnd, reinterpret_cast<HMENU>(IDC_TICKET_COMMENT_BODY), nullptr, nullptr);
        CreateWindowW(L"BUTTON", L"Kommentar hinzufügen", WS_CHILD | WS_VISIBLE | BS_DEFPUSHBUTTON, 308, 292, 150, 30, hwnd, reinterpret_cast<HMENU>(IDC_TICKET_COMMENT_ADD), nullptr, nullptr);
        CreateWindowW(L"BUTTON", L"Schließen", WS_CHILD | WS_VISIBLE, 468, 292, 90, 30, hwnd, reinterpret_cast<HMENU>(IDC_TICKET_COMMENT_CLOSE), nullptr, nullptr);
        RefreshTicketComments(hwnd, context); return 0; }
    case WM_COMMAND:
        if (LOWORD(wParam) == IDC_TICKET_COMMENT_CLOSE) { DestroyWindow(hwnd); return 0; }
        if (LOWORD(wParam) == IDC_TICKET_COMMENT_ADD && context && g_Database) {
            int64_t id = 0; EnterpriseComment comment{0, L"ticket", context->ticket.id, g_CurrentEmployeeId, ControlText(hwnd, IDC_TICKET_COMMENT_BODY), L""};
            if (!g_Database->createComment(comment, id, L"Ralf Krümmel")) { MessageBoxW(hwnd, g_Database->error().c_str(), L"Kommentar", MB_ICONERROR); return 0; }
            comment.id=id; PublishCommentChange(comment,"comment.created");
            SetWindowTextW(GetDlgItem(hwnd, IDC_TICKET_COMMENT_BODY), L""); RefreshTicketComments(hwnd, context); InvalidateRect(context->owner, nullptr, FALSE); return 0;
        }
        break;
    case WM_CLOSE: DestroyWindow(hwnd); return 0;
    case WM_DESTROY: if (context && context->owner) { EnableWindow(context->owner, TRUE); SetForegroundWindow(context->owner); } delete context; return 0;
    }
    return DefWindowProcW(hwnd, msg, wParam, lParam);
}
void ShowTicketComments(HWND owner, const EnterpriseTicket& ticket) { auto* context = new TicketCommentContext{owner, ticket}; EnableWindow(owner, FALSE); if (!CreateWindowExW(WS_EX_DLGMODALFRAME, L"AuraTicketComments", L"Ticket-Verlauf", WS_CAPTION | WS_SYSMENU | WS_POPUP | WS_VISIBLE, CW_USEDEFAULT, CW_USEDEFAULT, 580, 370, owner, nullptr, GetModuleHandleW(nullptr), context)) { EnableWindow(owner, TRUE); delete context; } }

struct TaskCommentContext { HWND owner = nullptr; EnterpriseTask task; };
void RefreshTaskComments(HWND hwnd, TaskCommentContext* context) { if (!context || !g_Database) return; HWND list=GetDlgItem(hwnd,IDC_TASK_COMMENT_LIST); SendMessageW(list,LB_RESETCONTENT,0,0); for(const EnterpriseComment& comment:g_Database->commentsFor(L"task",context->task.id)){std::wstring author=L"Mitarbeiter #"+std::to_wstring(comment.authorId);for(const EnterpriseEmployee& employee:g_Database->employees())if(employee.id==comment.authorId){author=employee.name;break;}const std::wstring row=comment.createdAt+L" — "+author+L": "+comment.body;SendMessageW(list,LB_ADDSTRING,0,reinterpret_cast<LPARAM>(row.c_str()));} }
LRESULT CALLBACK TaskCommentProc(HWND hwnd,UINT msg,WPARAM wParam,LPARAM lParam){auto* context=reinterpret_cast<TaskCommentContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));switch(msg){case WM_NCCREATE:SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams));return TRUE;case WM_CREATE:{context=reinterpret_cast<TaskCommentContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));const std::wstring title=L"Aufgabenverlauf: "+context->task.title;SetWindowTextW(hwnd,title.c_str());CreateWindowExW(WS_EX_CLIENTEDGE,L"LISTBOX",L"",WS_CHILD|WS_VISIBLE|WS_VSCROLL,18,18,540,170,hwnd,reinterpret_cast<HMENU>(IDC_TASK_COMMENT_LIST),nullptr,nullptr);CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",L"",WS_CHILD|WS_VISIBLE|ES_MULTILINE|ES_AUTOVSCROLL|WS_VSCROLL,18,204,540,70,hwnd,reinterpret_cast<HMENU>(IDC_TASK_COMMENT_BODY),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Kommentar hinzufügen",WS_CHILD|WS_VISIBLE|BS_DEFPUSHBUTTON,308,292,150,30,hwnd,reinterpret_cast<HMENU>(IDC_TASK_COMMENT_ADD),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Schließen",WS_CHILD|WS_VISIBLE,468,292,90,30,hwnd,reinterpret_cast<HMENU>(IDC_TASK_COMMENT_CLOSE),nullptr,nullptr);RefreshTaskComments(hwnd,context);return 0;}case WM_COMMAND:if(LOWORD(wParam)==IDC_TASK_COMMENT_CLOSE){DestroyWindow(hwnd);return 0;}if(LOWORD(wParam)==IDC_TASK_COMMENT_ADD&&context&&g_Database){int64_t id=0;EnterpriseComment comment{0,L"task",context->task.id,g_CurrentEmployeeId,ControlText(hwnd,IDC_TASK_COMMENT_BODY),L""};if(!g_Database->createComment(comment,id,L"Ralf Krümmel")){MessageBoxW(hwnd,g_Database->error().c_str(),L"Kommentar",MB_ICONERROR);return 0;}comment.id=id;PublishCommentChange(comment,"comment.created");SetWindowTextW(GetDlgItem(hwnd,IDC_TASK_COMMENT_BODY),L"");RefreshTaskComments(hwnd,context);InvalidateRect(context->owner,nullptr,FALSE);return 0;}break;case WM_CLOSE:DestroyWindow(hwnd);return 0;case WM_DESTROY:if(context&&context->owner){EnableWindow(context->owner,TRUE);SetForegroundWindow(context->owner);}delete context;return 0;}return DefWindowProcW(hwnd,msg,wParam,lParam);}
void ShowTaskComments(HWND owner,const EnterpriseTask& task){auto* context=new TaskCommentContext{owner,task};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraTaskComments",L"Aufgabenverlauf",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,580,370,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}

// Attachments are copied into the workspace-controlled object store before
// metadata is committed.  The source may be anywhere on the workstation, but
// the product never persists objects, cache files or temporary payloads on C:.
struct AttachmentManagerContext { HWND owner=nullptr; std::wstring subjectType; int64_t subjectId=0; std::wstring subjectTitle; std::vector<EnterpriseAttachment> entries; };

std::wstring AttachmentSafeName(const std::wstring& name) {
    std::wstring result; result.reserve(name.size());
    for (wchar_t character : name) result += (character==L'<'||character==L'>'||character==L':'||character==L'"'||character==L'/'||character==L'\\'||character==L'|'||character==L'?'||character==L'*'||character<32) ? L'_' : character;
    while (!result.empty() && (result.back()==L'.' || result.back()==L' ')) result.pop_back();
    return result.empty()?L"datei":result;
}

bool AttachmentSha256(const std::filesystem::path& path, std::wstring& result) {
    BCRYPT_ALG_HANDLE algorithm=nullptr; BCRYPT_HASH_HANDLE hash=nullptr; DWORD objectSize=0, bytes=0, hashSize=0; std::vector<unsigned char> object; std::vector<unsigned char> digest;
    if(BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA256_ALGORITHM,nullptr,0)!=0) return false;
    const bool ready=BCryptGetProperty(algorithm,BCRYPT_OBJECT_LENGTH,reinterpret_cast<PUCHAR>(&objectSize),sizeof(objectSize),&bytes,0)==0 && BCryptGetProperty(algorithm,BCRYPT_HASH_LENGTH,reinterpret_cast<PUCHAR>(&hashSize),sizeof(hashSize),&bytes,0)==0;
    if(ready){object.resize(objectSize);digest.resize(hashSize);}
    bool ok=ready && BCryptCreateHash(algorithm,&hash,object.data(),objectSize,nullptr,0,0)==0;
    std::ifstream input(path,std::ios::binary); std::array<char,65536> buffer{};
    while(ok && input.good()){input.read(buffer.data(),static_cast<std::streamsize>(buffer.size()));const std::streamsize count=input.gcount();if(count>0)ok=BCryptHashData(hash,reinterpret_cast<PUCHAR>(buffer.data()),static_cast<ULONG>(count),0)==0;}
    if(ok)ok=BCryptFinishHash(hash,digest.data(),static_cast<ULONG>(digest.size()),0)==0;
    if(hash)BCryptDestroyHash(hash); if(algorithm)BCryptCloseAlgorithmProvider(algorithm,0); if(!ok)return false;
    static constexpr wchar_t hex[]=L"0123456789abcdef"; result.clear();result.reserve(digest.size()*2);for(unsigned char value:digest){result+=hex[value>>4];result+=hex[value&15];}return true;
}

void RefreshAttachmentList(HWND hwnd, AttachmentManagerContext* context) {
    if(!context||!g_Database)return; context->entries=g_Database->attachmentsFor(context->subjectType,context->subjectId); HWND list=GetDlgItem(hwnd,IDC_ATTACHMENT_LIST);SendMessageW(list,LB_RESETCONTENT,0,0);
    for(const EnterpriseAttachment& attachment:context->entries){const std::wstring row=attachment.originalName+L"  ("+std::to_wstring(attachment.sizeBytes/1024)+L" KB, "+attachment.createdAt+L")";SendMessageW(list,LB_ADDSTRING,0,reinterpret_cast<LPARAM>(row.c_str()));}
}

bool AddAttachmentFromDialog(HWND hwnd, AttachmentManagerContext* context) {
    if(!context||!g_Database)return false; std::array<wchar_t,32768> sourceBuffer{}; OPENFILENAMEW dialog{};dialog.lStructSize=sizeof(dialog);dialog.hwndOwner=hwnd;dialog.lpstrFile=sourceBuffer.data();dialog.nMaxFile=static_cast<DWORD>(sourceBuffer.size());dialog.lpstrFilter=L"Alle Dateien\0*.*\0\0";dialog.Flags=OFN_FILEMUSTEXIST|OFN_PATHMUSTEXIST|OFN_HIDEREADONLY;
    if(!GetOpenFileNameW(&dialog))return true;
    const std::filesystem::path source(sourceBuffer.data());std::error_code error;const auto size=std::filesystem::file_size(source,error);if(error||!std::filesystem::is_regular_file(source,error)){MessageBoxW(hwnd,L"Die ausgewählte Datei ist nicht lesbar.",L"Anhang",MB_ICONERROR);return false;}if(size>100ULL*1024ULL*1024ULL){MessageBoxW(hwnd,L"Ein Anhang darf höchstens 100 MB groß sein.",L"Anhang",MB_ICONERROR);return false;}
    const std::filesystem::path directory=FindWorkspaceRoot()/L"KR_BUILD"/L"attachments"/context->subjectType/std::to_wstring(context->subjectId);std::filesystem::create_directories(directory,error);if(error){MessageBoxW(hwnd,L"Der Arbeitsbereich für Anhänge konnte nicht angelegt werden.",L"Anhang",MB_ICONERROR);return false;}
    const std::wstring original=source.filename().wstring();const std::filesystem::path target=directory/(std::to_wstring(GetTickCount64())+L"_"+AttachmentSafeName(original));
    if(!std::filesystem::copy_file(source,target,std::filesystem::copy_options::none,error)||error){MessageBoxW(hwnd,L"Die Datei konnte nicht in den Arbeitsbereich übernommen werden.",L"Anhang",MB_ICONERROR);return false;}
    std::wstring hash;if(!AttachmentSha256(target,hash)){std::filesystem::remove(target,error);MessageBoxW(hwnd,L"Für die Datei konnte kein Integritätshash erzeugt werden.",L"Anhang",MB_ICONERROR);return false;}
    int64_t newId=0;const EnterpriseAttachment attachment{0,context->subjectType,context->subjectId,original,target.wstring(),static_cast<int64_t>(size),hash,g_CurrentEmployeeId,L"",true};if(!g_Database->createAttachment(attachment,newId,L"Ralf Krümmel")){std::filesystem::remove(target,error);MessageBoxW(hwnd,g_Database->error().c_str(),L"Anhang",MB_ICONERROR);return false;}
    // The durable local object is committed first. With a configured central
    // endpoint the identical SHA-256 object is uploaded immediately. Metadata
    // is published only after that binary object exists centrally, so another
    // client never receives an attachment it cannot retrieve.
    if(GetEnvironmentVariableW(L"AURA_SYNC_URL",nullptr,0)>0){const int32_t transferred=g_SyncService.uploadAttachment(target.wstring());if(transferred!=0)MessageBoxW(hwnd,L"Der Anhang wurde lokal revisionssicher gespeichert, konnte aber noch nicht in die Zentrale übertragen werden.",L"Anhang-Synchronisation",MB_ICONWARNING);else{EnterpriseAttachment published=attachment;published.id=newId;PublishAttachmentChange(published,"attachment.created");}}
    RefreshAttachmentList(hwnd,context);return true;
}

LRESULT CALLBACK AttachmentManagerProc(HWND hwnd, UINT message, WPARAM wParam, LPARAM lParam) {
    auto* context=reinterpret_cast<AttachmentManagerContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));switch(message){
    case WM_NCCREATE:SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams));return TRUE;
    case WM_CREATE:{context=reinterpret_cast<AttachmentManagerContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));SetWindowTextW(hwnd,(L"Anhänge: "+context->subjectTitle).c_str());CreateWindowExW(WS_EX_CLIENTEDGE,L"LISTBOX",L"",WS_CHILD|WS_VISIBLE|WS_VSCROLL|LBS_NOTIFY,18,18,604,210,hwnd,reinterpret_cast<HMENU>(IDC_ATTACHMENT_LIST),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Datei hinzufügen",WS_CHILD|WS_VISIBLE|BS_DEFPUSHBUTTON,18,248,125,30,hwnd,reinterpret_cast<HMENU>(IDC_ATTACHMENT_ADD),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Öffnen",WS_CHILD|WS_VISIBLE,153,248,90,30,hwnd,reinterpret_cast<HMENU>(IDC_ATTACHMENT_OPEN),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Archivieren",WS_CHILD|WS_VISIBLE,253,248,100,30,hwnd,reinterpret_cast<HMENU>(IDC_ATTACHMENT_ARCHIVE),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Schließen",WS_CHILD|WS_VISIBLE,532,248,90,30,hwnd,reinterpret_cast<HMENU>(IDC_ATTACHMENT_CLOSE),nullptr,nullptr);RefreshAttachmentList(hwnd,context);return 0;}
    case WM_COMMAND:{const int command=LOWORD(wParam);if(command==IDC_ATTACHMENT_CLOSE){DestroyWindow(hwnd);return 0;}if(command==IDC_ATTACHMENT_ADD){AddAttachmentFromDialog(hwnd,context);return 0;}const int selected=static_cast<int>(SendMessageW(GetDlgItem(hwnd,IDC_ATTACHMENT_LIST),LB_GETCURSEL,0,0));if((command==IDC_ATTACHMENT_OPEN||command==IDC_ATTACHMENT_ARCHIVE)&&(selected<0||!context||static_cast<size_t>(selected)>=context->entries.size())){MessageBoxW(hwnd,L"Bitte zuerst einen Anhang auswählen.",L"Anhänge",MB_ICONINFORMATION);return 0;}if(command==IDC_ATTACHMENT_OPEN){const EnterpriseAttachment& item=context->entries[static_cast<size_t>(selected)];if(!std::filesystem::exists(item.storedPath)){if(GetEnvironmentVariableW(L"AURA_SYNC_URL",nullptr,0)==0||g_SyncService.downloadAttachment(item.sha256,item.storedPath)!=0){MessageBoxW(hwnd,L"Die Datei ist lokal nicht vorhanden und konnte nicht aus der Zentrale geladen werden.",L"Anhänge",MB_ICONERROR);return 0;}}std::wstring localHash;if(!AttachmentSha256(item.storedPath,localHash)||localHash!=item.sha256){MessageBoxW(hwnd,L"Die Integritätsprüfung des Anhangs ist fehlgeschlagen.",L"Anhänge",MB_ICONERROR);return 0;}if(reinterpret_cast<INT_PTR>(ShellExecuteW(hwnd,L"open",item.storedPath.c_str(),nullptr,nullptr,SW_SHOWNORMAL))<=32)MessageBoxW(hwnd,L"Die Datei konnte nicht geöffnet werden.",L"Anhänge",MB_ICONERROR);return 0;}if(command==IDC_ATTACHMENT_ARCHIVE){const EnterpriseAttachment& item=context->entries[static_cast<size_t>(selected)];if(MessageBoxW(hwnd,L"Metadaten archivieren? Die Datei bleibt aus Revisionsgründen im Arbeitsbereich erhalten.",L"Anhang archivieren",MB_ICONQUESTION|MB_YESNO)!=IDYES)return 0;if(!g_Database->archiveAttachment(item.id,L"Ralf Krümmel")){MessageBoxW(hwnd,g_Database->error().c_str(),L"Anhänge",MB_ICONERROR);return 0;}EnterpriseAttachment archived=item;archived.active=false;PublishAttachmentChange(archived,"attachment.archived");RefreshAttachmentList(hwnd,context);return 0;}break;}
    case WM_CLOSE:DestroyWindow(hwnd);return 0;case WM_DESTROY:if(context&&context->owner){EnableWindow(context->owner,TRUE);SetForegroundWindow(context->owner);}delete context;return 0;}return DefWindowProcW(hwnd,message,wParam,lParam);
}
void ShowAttachmentManager(HWND owner,const std::wstring& subjectType,int64_t subjectId,const std::wstring& subjectTitle){auto* context=new AttachmentManagerContext{owner,subjectType,subjectId,subjectTitle};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraAttachmentManager",L"Anhänge",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,660,330,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}

std::wstring FormatBookingDateTime(int64_t unixTime) {
    std::tm local{}; const std::time_t value = static_cast<std::time_t>(unixTime); localtime_s(&local, &value);
    wchar_t buffer[32]{}; std::wcsftime(buffer, std::size(buffer), L"%Y-%m-%d %H:%M", &local); return buffer;
}

bool ParseBookingDateTime(const std::wstring& text, int64_t& unixTime) {
    std::tm local{}; int year = 0, month = 0, day = 0, hour = 0, minute = 0;
    if (swscanf(text.c_str(), L"%d-%d-%d %d:%d", &year, &month, &day, &hour, &minute) != 5) return false;
    local.tm_year = year - 1900; local.tm_mon = month - 1; local.tm_mday = day; local.tm_hour = hour; local.tm_min = minute; local.tm_isdst = -1;
    const std::time_t converted = std::mktime(&local);
    if (converted == static_cast<std::time_t>(-1)) return false;
    unixTime = static_cast<int64_t>(converted); return true;
}

struct LeaveEditorContext { HWND owner; };
LRESULT CALLBACK LeaveEditorProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    auto* context = reinterpret_cast<LeaveEditorContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    switch (msg) {
    case WM_NCCREATE: SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams)); return TRUE;
    case WM_CREATE: {
        const int64_t start = ((std::time(nullptr) + 86399) / 86400) * 86400 + 8 * 3600;
        SetWindowTextW(hwnd, L"Urlaubsantrag einreichen");
        RoomEditorLabel(hwnd, L"Beginn (JJJJ-MM-TT SS:MM) *", 22, 20); RoomEditorLabel(hwnd, L"Ende (JJJJ-MM-TT SS:MM) *", 22, 68); RoomEditorLabel(hwnd, L"Grund *", 22, 116);
        CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", FormatBookingDateTime(start).c_str(), WS_CHILD | WS_VISIBLE, 22, 40, 390, 24, hwnd, reinterpret_cast<HMENU>(IDC_LEAVE_START), nullptr, nullptr);
        CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", FormatBookingDateTime(start + 8 * 3600).c_str(), WS_CHILD | WS_VISIBLE, 22, 88, 390, 24, hwnd, reinterpret_cast<HMENU>(IDC_LEAVE_END), nullptr, nullptr);
        CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", L"Erholungsurlaub", WS_CHILD | WS_VISIBLE, 22, 136, 390, 24, hwnd, reinterpret_cast<HMENU>(IDC_LEAVE_REASON), nullptr, nullptr);
        CreateWindowW(L"BUTTON", L"Antrag einreichen", WS_CHILD | WS_VISIBLE | BS_DEFPUSHBUTTON, 184, 190, 128, 30, hwnd, reinterpret_cast<HMENU>(IDC_LEAVE_SAVE), nullptr, nullptr); CreateWindowW(L"BUTTON", L"Abbrechen", WS_CHILD | WS_VISIBLE, 320, 190, 92, 30, hwnd, reinterpret_cast<HMENU>(IDC_LEAVE_CANCEL), nullptr, nullptr); return 0;
    }
    case WM_COMMAND:
        if (LOWORD(wParam) == IDC_LEAVE_CANCEL) { DestroyWindow(hwnd); return 0; }
        if (LOWORD(wParam) == IDC_LEAVE_SAVE && context && g_Database) { int64_t start=0,end=0; if (!ParseBookingDateTime(ControlText(hwnd,IDC_LEAVE_START),start) || !ParseBookingDateTime(ControlText(hwnd,IDC_LEAVE_END),end) || end<=start) { MessageBoxW(hwnd,L"Bitte einen gültigen Zeitraum im Format JJJJ-MM-TT SS:MM angeben.",L"Urlaubsantrag",MB_ICONERROR);return 0; } int64_t id=0; EnterpriseLeaveRequest leave{0,g_CurrentEmployeeId,start,end,ControlText(hwnd,IDC_LEAVE_REASON),L"pending",0,true};if(!g_Database->createLeaveRequest(leave,id,L"Ralf Krümmel")){MessageBoxW(hwnd,g_Database->error().c_str(),L"Urlaubsantrag",MB_ICONERROR);return 0;}leave.id=id;PublishLeaveChange(leave,"leave.requested");MessageBoxW(hwnd,L"Der Urlaubsantrag wurde eingereicht.",L"HR-Workflow",MB_ICONINFORMATION);DestroyWindow(hwnd);return 0; }
        break;
    case WM_CLOSE: DestroyWindow(hwnd); return 0;
    case WM_DESTROY: if(context&&context->owner){EnableWindow(context->owner,TRUE);SetForegroundWindow(context->owner);} delete context; return 0;
    }
    return DefWindowProcW(hwnd,msg,wParam,lParam);
}
void ShowLeaveEditor(HWND owner){auto* context=new LeaveEditorContext{owner};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraLeaveEditor",L"Urlaubsantrag",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,450,270,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}

struct LeaveManagerContext { HWND owner; std::vector<EnterpriseLeaveRequest> requests; };
void RefreshLeaveList(HWND hwnd,LeaveManagerContext* context){if(!context||!g_Database)return;context->requests=g_Database->leaveRequests(0);HWND list=GetDlgItem(hwnd,IDC_LEAVE_LIST);SendMessageW(list,LB_RESETCONTENT,0,0);for(const EnterpriseLeaveRequest& request:context->requests){std::wstring employee=L"Unbekannt";for(const EnterpriseEmployee& item:g_Database->employees())if(item.id==request.employeeId){employee=item.name;break;}const std::wstring status=request.status==L"pending"?L"Offen":request.status==L"approved"?L"Genehmigt":L"Abgelehnt";const std::wstring row=employee+L" | "+FormatBookingDateTime(request.startsAt)+L" bis "+FormatBookingDateTime(request.endsAt)+L" | "+status+L" | "+request.reason;SendMessageW(list,LB_ADDSTRING,0,reinterpret_cast<LPARAM>(row.c_str()));}}
LRESULT CALLBACK LeaveManagerProc(HWND hwnd,UINT msg,WPARAM wParam,LPARAM lParam){auto* context=reinterpret_cast<LeaveManagerContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));switch(msg){case WM_NCCREATE:SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams));return TRUE;case WM_CREATE:CreateWindowExW(WS_EX_CLIENTEDGE,L"LISTBOX",L"",WS_CHILD|WS_VISIBLE|WS_VSCROLL|LBS_NOTIFY,22,22,610,190,hwnd,reinterpret_cast<HMENU>(IDC_LEAVE_LIST),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Genehmigen",WS_CHILD|WS_VISIBLE,302,228,100,30,hwnd,reinterpret_cast<HMENU>(IDC_LEAVE_APPROVE),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Ablehnen",WS_CHILD|WS_VISIBLE,412,228,90,30,hwnd,reinterpret_cast<HMENU>(IDC_LEAVE_REJECT),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Schließen",WS_CHILD|WS_VISIBLE,542,228,90,30,hwnd,reinterpret_cast<HMENU>(IDC_LEAVE_CLOSE),nullptr,nullptr);context=reinterpret_cast<LeaveManagerContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));RefreshLeaveList(hwnd,context);return 0;case WM_COMMAND:if(LOWORD(wParam)==IDC_LEAVE_CLOSE){DestroyWindow(hwnd);return 0;}if(LOWORD(wParam)==IDC_LEAVE_APPROVE||LOWORD(wParam)==IDC_LEAVE_REJECT){const int selected=static_cast<int>(SendMessageW(GetDlgItem(hwnd,IDC_LEAVE_LIST),LB_GETCURSEL,0,0));if(!context||selected<0||static_cast<size_t>(selected)>=context->requests.size()){MessageBoxW(hwnd,L"Bitte zuerst einen Antrag auswählen.",L"Urlaubsfreigabe",MB_ICONINFORMATION);return 0;}const EnterpriseLeaveRequest& request=context->requests[static_cast<size_t>(selected)];if(!g_Database->decideLeaveRequest(request.id,LOWORD(wParam)==IDC_LEAVE_APPROVE,g_CurrentEmployeeId,L"Ralf Krümmel"))MessageBoxW(hwnd,g_Database->error().c_str(),L"Urlaubsfreigabe",MB_ICONERROR);else for(const EnterpriseLeaveRequest& leave:g_Database->leaveRequests(0,true))if(leave.id==request.id){PublishLeaveChange(leave,"leave.decided");break;}RefreshLeaveList(hwnd,context);return 0;}break;case WM_CLOSE:DestroyWindow(hwnd);return 0;case WM_DESTROY:if(context&&context->owner){EnableWindow(context->owner,TRUE);SetForegroundWindow(context->owner);}delete context;return 0;}return DefWindowProcW(hwnd,msg,wParam,lParam);}
void ShowLeaveManager(HWND owner){auto* context=new LeaveManagerContext{owner};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraLeaveManager",L"Urlaubsfreigaben",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,670,315,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}

struct ApprovalManagerContext { HWND owner; std::vector<EnterpriseTaskApproval> approvals; };
void RefreshApprovalList(HWND hwnd,ApprovalManagerContext* context){if(!context||!g_Database)return;context->approvals=g_Database->taskApprovals(g_CurrentEmployeeId);HWND list=GetDlgItem(hwnd,IDC_APPROVAL_LIST);SendMessageW(list,LB_RESETCONTENT,0,0);for(const EnterpriseTaskApproval& approval:context->approvals){std::wstring title=L"Unbekannte Aufgabe";for(const EnterpriseTask& task:g_Database->tasks())if(task.id==approval.taskId){title=task.title;break;}std::wstring requester=L"Unbekannt";for(const EnterpriseEmployee& employee:g_Database->employees())if(employee.id==approval.requestedBy){requester=employee.name;break;}const std::wstring row=title+L"  — beantragt von "+requester;SendMessageW(list,LB_ADDSTRING,0,reinterpret_cast<LPARAM>(row.c_str()));}}
LRESULT CALLBACK ApprovalManagerProc(HWND hwnd,UINT msg,WPARAM wParam,LPARAM lParam){auto* context=reinterpret_cast<ApprovalManagerContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));switch(msg){case WM_NCCREATE:SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams));return TRUE;case WM_CREATE:CreateWindowExW(WS_EX_CLIENTEDGE,L"LISTBOX",L"",WS_CHILD|WS_VISIBLE|WS_VSCROLL|LBS_NOTIFY,22,22,570,190,hwnd,reinterpret_cast<HMENU>(IDC_APPROVAL_LIST),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Genehmigen",WS_CHILD|WS_VISIBLE,262,228,105,30,hwnd,reinterpret_cast<HMENU>(IDC_APPROVAL_APPROVE),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Ablehnen",WS_CHILD|WS_VISIBLE,377,228,90,30,hwnd,reinterpret_cast<HMENU>(IDC_APPROVAL_REJECT),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Schließen",WS_CHILD|WS_VISIBLE,502,228,90,30,hwnd,reinterpret_cast<HMENU>(IDC_APPROVAL_CLOSE),nullptr,nullptr);context=reinterpret_cast<ApprovalManagerContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));RefreshApprovalList(hwnd,context);return 0;case WM_COMMAND:if(LOWORD(wParam)==IDC_APPROVAL_CLOSE){DestroyWindow(hwnd);return 0;}if(LOWORD(wParam)==IDC_APPROVAL_APPROVE||LOWORD(wParam)==IDC_APPROVAL_REJECT){const int selected=static_cast<int>(SendMessageW(GetDlgItem(hwnd,IDC_APPROVAL_LIST),LB_GETCURSEL,0,0));if(!context||selected<0||static_cast<size_t>(selected)>=context->approvals.size()){MessageBoxW(hwnd,L"Bitte zuerst eine Freigabe auswählen.",L"Freigaben",MB_ICONINFORMATION);return 0;}const EnterpriseTaskApproval& approval=context->approvals[static_cast<size_t>(selected)];if(!g_Database->decideTaskApproval(approval.id,LOWORD(wParam)==IDC_APPROVAL_APPROVE,g_CurrentEmployeeId,L"Ralf Krümmel"))MessageBoxW(hwnd,g_Database->error().c_str(),L"Freigaben",MB_ICONERROR);else for(const EnterpriseTaskApproval& current:g_Database->taskApprovals(0,true))if(current.id==approval.id){PublishTaskApprovalChange(current,"task.approval.decided");break;}RefreshApprovalList(hwnd,context);return 0;}break;case WM_CLOSE:DestroyWindow(hwnd);return 0;case WM_DESTROY:if(context&&context->owner){EnableWindow(context->owner,TRUE);SetForegroundWindow(context->owner);}delete context;return 0;}return DefWindowProcW(hwnd,msg,wParam,lParam);}
void ShowApprovalManager(HWND owner){auto* context=new ApprovalManagerContext{owner};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraApprovalManager",L"Meine Aufgabenfreigaben",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,630,315,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}

struct AssetEditorContext { HWND owner; EnterpriseFacilityAsset asset; bool editing=false; };
LRESULT CALLBACK AssetEditorProc(HWND hwnd,UINT msg,WPARAM wParam,LPARAM lParam){auto* context=reinterpret_cast<AssetEditorContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));switch(msg){case WM_NCCREATE:SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams));return TRUE;case WM_CREATE:{context=reinterpret_cast<AssetEditorContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));SetWindowTextW(hwnd,context&&context->editing?L"Facility-Asset bearbeiten":L"Facility-Asset anlegen");RoomEditorLabel(hwnd,L"Name *",22,20);RoomEditorLabel(hwnd,L"Kategorie *",22,68);RoomEditorLabel(hwnd,L"Standort *",22,116);RoomEditorLabel(hwnd,L"Status *",22,164);CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",context?context->asset.name.c_str():L"",WS_CHILD|WS_VISIBLE,22,40,390,24,hwnd,reinterpret_cast<HMENU>(IDC_ASSET_NAME),nullptr,nullptr);CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",context?context->asset.category.c_str():L"",WS_CHILD|WS_VISIBLE,22,88,390,24,hwnd,reinterpret_cast<HMENU>(IDC_ASSET_CATEGORY),nullptr,nullptr);CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",context?context->asset.location.c_str():L"",WS_CHILD|WS_VISIBLE,22,136,390,24,hwnd,reinterpret_cast<HMENU>(IDC_ASSET_LOCATION),nullptr,nullptr);HWND status=CreateWindowW(L"COMBOBOX",L"",WS_CHILD|WS_VISIBLE|CBS_DROPDOWNLIST,22,184,390,120,hwnd,reinterpret_cast<HMENU>(IDC_ASSET_STATUS),nullptr,nullptr);SendMessageW(status,CB_ADDSTRING,0,reinterpret_cast<LPARAM>(L"Betriebsbereit"));SendMessageW(status,CB_ADDSTRING,0,reinterpret_cast<LPARAM>(L"Wartung"));SendMessageW(status,CB_ADDSTRING,0,reinterpret_cast<LPARAM>(L"Außer Betrieb"));SendMessageW(status,CB_SETCURSEL,context&&context->asset.status==L"maintenance"?1:context&&context->asset.status==L"out_of_service"?2:0,0);CreateWindowW(L"BUTTON",L"Speichern",WS_CHILD|WS_VISIBLE|BS_DEFPUSHBUTTON,220,236,92,30,hwnd,reinterpret_cast<HMENU>(IDC_ASSET_SAVE),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Abbrechen",WS_CHILD|WS_VISIBLE,320,236,92,30,hwnd,reinterpret_cast<HMENU>(IDC_ASSET_CANCEL),nullptr,nullptr);return 0;}case WM_COMMAND:if(LOWORD(wParam)==IDC_ASSET_CANCEL){DestroyWindow(hwnd);return 0;}if(LOWORD(wParam)==IDC_ASSET_SAVE&&context&&g_Database){EnterpriseFacilityAsset asset=context->asset;asset.name=ControlText(hwnd,IDC_ASSET_NAME);asset.category=ControlText(hwnd,IDC_ASSET_CATEGORY);asset.location=ControlText(hwnd,IDC_ASSET_LOCATION);const int selected=static_cast<int>(SendMessageW(GetDlgItem(hwnd,IDC_ASSET_STATUS),CB_GETCURSEL,0,0));asset.status=selected==1?L"maintenance":selected==2?L"out_of_service":L"operational";int64_t id=0;const bool ok=context->editing?g_Database->updateFacilityAsset(asset,L"Ralf Krümmel"):g_Database->createFacilityAsset(asset,id,L"Ralf Krümmel");if(!ok){MessageBoxW(hwnd,g_Database->error().c_str(),L"Facility-Asset",MB_ICONERROR);return 0;}asset.id=context->editing?asset.id:id;PublishFacilityAssetChange(asset,context->editing?"facility.asset.updated":"facility.asset.created");InvalidateRect(context->owner,nullptr,FALSE);DestroyWindow(hwnd);return 0;}break;case WM_CLOSE:DestroyWindow(hwnd);return 0;case WM_DESTROY:if(context&&context->owner){EnableWindow(context->owner,TRUE);SetForegroundWindow(context->owner);}delete context;return 0;}return DefWindowProcW(hwnd,msg,wParam,lParam);}
void ShowAssetEditor(HWND owner,const EnterpriseFacilityAsset* asset=nullptr){auto* context=new AssetEditorContext{owner,asset?*asset:EnterpriseFacilityAsset{},asset!=nullptr};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraAssetEditor",L"Facility-Asset",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,450,315,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}

struct MaintenanceEditorContext { HWND owner; EnterpriseFacilityAsset asset; EnterpriseMaintenanceOrder order; bool editing=false; };
LRESULT CALLBACK MaintenanceEditorProc(HWND hwnd,UINT msg,WPARAM wParam,LPARAM lParam){auto* context=reinterpret_cast<MaintenanceEditorContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));switch(msg){case WM_NCCREATE:SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams));return TRUE;case WM_CREATE:{context=reinterpret_cast<MaintenanceEditorContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));SetWindowTextW(hwnd,context&&context->editing?L"Wartungsauftrag bearbeiten":L"Wartungsauftrag anlegen");RoomEditorLabel(hwnd,L"Auftrag *",22,20);RoomEditorLabel(hwnd,L"Fällig am (JJJJ-MM-TT SS:MM)",22,68);RoomEditorLabel(hwnd,L"Wiederholung (Tage, 0 = keine)",22,116);RoomEditorLabel(hwnd,L"Status",22,164);const std::wstring due=context&&context->order.dueAt>0?FormatBookingDateTime(context->order.dueAt):L"";CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",context?context->order.title.c_str():L"",WS_CHILD|WS_VISIBLE,22,40,390,24,hwnd,reinterpret_cast<HMENU>(IDC_MAINT_TITLE),nullptr,nullptr);CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",due.c_str(),WS_CHILD|WS_VISIBLE,22,88,390,24,hwnd,reinterpret_cast<HMENU>(IDC_MAINT_DUE),nullptr,nullptr);CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",std::to_wstring(context?context->order.recurrenceDays:0).c_str(),WS_CHILD|WS_VISIBLE|ES_NUMBER,22,136,390,24,hwnd,reinterpret_cast<HMENU>(IDC_MAINT_RECURRENCE),nullptr,nullptr);HWND status=CreateWindowW(L"COMBOBOX",L"",WS_CHILD|WS_VISIBLE|CBS_DROPDOWNLIST,22,184,390,120,hwnd,reinterpret_cast<HMENU>(IDC_MAINT_STATUS),nullptr,nullptr);SendMessageW(status,CB_ADDSTRING,0,reinterpret_cast<LPARAM>(L"Offen"));SendMessageW(status,CB_ADDSTRING,0,reinterpret_cast<LPARAM>(L"In Bearbeitung"));SendMessageW(status,CB_ADDSTRING,0,reinterpret_cast<LPARAM>(L"Abgeschlossen"));SendMessageW(status,CB_ADDSTRING,0,reinterpret_cast<LPARAM>(L"Storniert"));SendMessageW(status,CB_SETCURSEL,context&&context->order.status==L"in_progress"?1:context&&context->order.status==L"completed"?2:context&&context->order.status==L"cancelled"?3:0,0);CreateWindowW(L"BUTTON",L"Speichern",WS_CHILD|WS_VISIBLE|BS_DEFPUSHBUTTON,220,236,92,30,hwnd,reinterpret_cast<HMENU>(IDC_MAINT_SAVE),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Abbrechen",WS_CHILD|WS_VISIBLE,320,236,92,30,hwnd,reinterpret_cast<HMENU>(IDC_MAINT_CANCEL),nullptr,nullptr);return 0;}case WM_COMMAND:if(LOWORD(wParam)==IDC_MAINT_CANCEL){DestroyWindow(hwnd);return 0;}if(LOWORD(wParam)==IDC_MAINT_SAVE&&context&&g_Database){EnterpriseMaintenanceOrder order=context->order;order.assetId=context->asset.id;order.title=ControlText(hwnd,IDC_MAINT_TITLE);order.dueAt=0;const std::wstring due=ControlText(hwnd,IDC_MAINT_DUE);if(!due.empty()&&!ParseBookingDateTime(due,order.dueAt)){MessageBoxW(hwnd,L"Die Fälligkeit muss im Format JJJJ-MM-TT SS:MM angegeben werden.",L"Wartungsauftrag",MB_ICONERROR);return 0;}try{order.recurrenceDays=std::stoi(ControlText(hwnd,IDC_MAINT_RECURRENCE));}catch(...){order.recurrenceDays=-1;}const int selected=static_cast<int>(SendMessageW(GetDlgItem(hwnd,IDC_MAINT_STATUS),CB_GETCURSEL,0,0));order.status=selected==1?L"in_progress":selected==2?L"completed":selected==3?L"cancelled":L"open";int64_t id=0;const bool ok=context->editing?g_Database->updateMaintenanceOrder(order,L"Ralf Krümmel"):g_Database->createMaintenanceOrder(order,id,L"Ralf Krümmel");if(!ok){MessageBoxW(hwnd,g_Database->error().c_str(),L"Wartungsauftrag",MB_ICONERROR);return 0;}order.id=context->editing?order.id:id;PublishMaintenanceChange(context->asset,order,context->editing?"facility.maintenance.updated":"facility.maintenance.created");InvalidateRect(context->owner,nullptr,FALSE);DestroyWindow(hwnd);return 0;}break;case WM_CLOSE:DestroyWindow(hwnd);return 0;case WM_DESTROY:if(context&&context->owner){EnableWindow(context->owner,TRUE);SetForegroundWindow(context->owner);}delete context;return 0;}return DefWindowProcW(hwnd,msg,wParam,lParam);}
void ShowMaintenanceEditor(HWND owner,const EnterpriseFacilityAsset& asset,const EnterpriseMaintenanceOrder* order=nullptr){auto* context=new MaintenanceEditorContext{owner,asset,order?*order:EnterpriseMaintenanceOrder{},order!=nullptr};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraMaintenanceEditor",L"Wartungsauftrag",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,450,315,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}

struct MaintenanceManagerContext { HWND owner; EnterpriseFacilityAsset asset; std::vector<EnterpriseMaintenanceOrder> orders; };
void RefreshMaintenanceList(HWND hwnd,MaintenanceManagerContext* context){if(!context||!g_Database)return;context->orders=g_Database->maintenanceOrders(context->asset.id);HWND list=GetDlgItem(hwnd,IDC_MAINT_LIST);SendMessageW(list,LB_RESETCONTENT,0,0);for(const EnterpriseMaintenanceOrder& order:context->orders){const std::wstring status=order.status==L"in_progress"?L"In Bearbeitung":order.status==L"completed"?L"Abgeschlossen":order.status==L"cancelled"?L"Storniert":L"Offen";const std::wstring row=status+L" | "+order.title+(order.dueAt>0?L" | fällig "+FormatBookingDateTime(order.dueAt):L"")+ (order.recurrenceDays>0?L" | alle "+std::to_wstring(order.recurrenceDays)+L" Tage":L"");SendMessageW(list,LB_ADDSTRING,0,reinterpret_cast<LPARAM>(row.c_str()));}}
LRESULT CALLBACK MaintenanceManagerProc(HWND hwnd,UINT msg,WPARAM wParam,LPARAM lParam){auto* context=reinterpret_cast<MaintenanceManagerContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));switch(msg){case WM_NCCREATE:SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams));return TRUE;case WM_CREATE:{context=reinterpret_cast<MaintenanceManagerContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));const std::wstring title=L"Wartung: "+context->asset.name;SetWindowTextW(hwnd,title.c_str());CreateWindowExW(WS_EX_CLIENTEDGE,L"LISTBOX",L"",WS_CHILD|WS_VISIBLE|WS_VSCROLL|LBS_NOTIFY,22,22,650,190,hwnd,reinterpret_cast<HMENU>(IDC_MAINT_LIST),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Neu",WS_CHILD|WS_VISIBLE,22,228,80,30,hwnd,reinterpret_cast<HMENU>(IDC_MAINT_NEW),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Bearbeiten",WS_CHILD|WS_VISIBLE,112,228,95,30,hwnd,reinterpret_cast<HMENU>(IDC_MAINT_EDIT),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Status",WS_CHILD|WS_VISIBLE,217,228,80,30,hwnd,reinterpret_cast<HMENU>(IDC_MAINT_ADVANCE),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Archiv",WS_CHILD|WS_VISIBLE,307,228,80,30,hwnd,reinterpret_cast<HMENU>(IDC_MAINT_ARCHIVE),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Schließen",WS_CHILD|WS_VISIBLE,582,228,90,30,hwnd,reinterpret_cast<HMENU>(IDC_MAINT_CLOSE),nullptr,nullptr);RefreshMaintenanceList(hwnd,context);return 0;}case WM_ACTIVATE:if(LOWORD(wParam)!=WA_INACTIVE)RefreshMaintenanceList(hwnd,context);return 0;case WM_COMMAND:if(LOWORD(wParam)==IDC_MAINT_CLOSE){DestroyWindow(hwnd);return 0;}if(LOWORD(wParam)==IDC_MAINT_NEW){ShowMaintenanceEditor(hwnd,context->asset);return 0;}if(LOWORD(wParam)==IDC_MAINT_EDIT||LOWORD(wParam)==IDC_MAINT_ADVANCE||LOWORD(wParam)==IDC_MAINT_ARCHIVE||(LOWORD(wParam)==IDC_MAINT_LIST&&HIWORD(wParam)==LBN_DBLCLK)){const int selected=static_cast<int>(SendMessageW(GetDlgItem(hwnd,IDC_MAINT_LIST),LB_GETCURSEL,0,0));if(!context||selected<0||static_cast<size_t>(selected)>=context->orders.size()){MessageBoxW(hwnd,L"Bitte zuerst einen Wartungsauftrag auswählen.",L"Wartung",MB_ICONINFORMATION);return 0;}const EnterpriseMaintenanceOrder& order=context->orders[static_cast<size_t>(selected)];if(LOWORD(wParam)==IDC_MAINT_ARCHIVE){if(MessageBoxW(hwnd,L"Wartungsauftrag archivieren?",L"Wartung",MB_YESNO|MB_ICONWARNING)==IDYES){if(!g_Database->archiveMaintenanceOrder(order.id,L"Ralf Krümmel"))MessageBoxW(hwnd,g_Database->error().c_str(),L"Wartung",MB_ICONERROR);else{EnterpriseMaintenanceOrder archived=order;archived.active=false;PublishMaintenanceChange(context->asset,archived,"facility.maintenance.archived");}}RefreshMaintenanceList(hwnd,context);return 0;}if(LOWORD(wParam)==IDC_MAINT_ADVANCE){const std::wstring next=order.status==L"open"?L"in_progress":order.status==L"in_progress"?L"completed":order.status==L"completed"?L"open":L"open";if(!g_Database->updateMaintenanceOrderStatus(order.id,next,L"Ralf Krümmel"))MessageBoxW(hwnd,g_Database->error().c_str(),L"Wartung",MB_ICONERROR);else{EnterpriseMaintenanceOrder updated=order;updated.status=next;PublishMaintenanceChange(context->asset,updated,"facility.maintenance.status.changed");}RefreshMaintenanceList(hwnd,context);return 0;}ShowMaintenanceEditor(hwnd,context->asset,&order);return 0;}break;case WM_CLOSE:DestroyWindow(hwnd);return 0;case WM_DESTROY:if(context&&context->owner){EnableWindow(context->owner,TRUE);SetForegroundWindow(context->owner);}delete context;return 0;}return DefWindowProcW(hwnd,msg,wParam,lParam);}
void ShowMaintenanceManager(HWND owner,const EnterpriseFacilityAsset& asset){auto* context=new MaintenanceManagerContext{owner,asset};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraMaintenanceManager",L"Wartung",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,710,315,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}

struct AssetManagerContext { HWND owner; std::vector<EnterpriseFacilityAsset> assets; };
void RefreshAssetList(HWND hwnd,AssetManagerContext* context){if(!context||!g_Database)return;context->assets=g_Database->facilityAssets();HWND list=GetDlgItem(hwnd,IDC_ASSET_LIST);SendMessageW(list,LB_RESETCONTENT,0,0);for(const EnterpriseFacilityAsset& asset:context->assets){const std::wstring status=asset.status==L"maintenance"?L"Wartung":asset.status==L"out_of_service"?L"Außer Betrieb":L"Betriebsbereit";const std::wstring row=asset.name+L" | "+asset.category+L" | "+asset.location+L" | "+status;SendMessageW(list,LB_ADDSTRING,0,reinterpret_cast<LPARAM>(row.c_str()));}}
LRESULT CALLBACK AssetManagerProc(HWND hwnd,UINT msg,WPARAM wParam,LPARAM lParam){auto* context=reinterpret_cast<AssetManagerContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));switch(msg){case WM_NCCREATE:SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams));return TRUE;case WM_CREATE:CreateWindowExW(WS_EX_CLIENTEDGE,L"LISTBOX",L"",WS_CHILD|WS_VISIBLE|WS_VSCROLL|LBS_NOTIFY,22,22,570,190,hwnd,reinterpret_cast<HMENU>(IDC_ASSET_LIST),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Neu",WS_CHILD|WS_VISIBLE,22,228,80,30,hwnd,reinterpret_cast<HMENU>(IDC_ASSET_NEW),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Bearbeiten",WS_CHILD|WS_VISIBLE,112,228,95,30,hwnd,reinterpret_cast<HMENU>(IDC_ASSET_EDIT),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Wartung",WS_CHILD|WS_VISIBLE,217,228,95,30,hwnd,reinterpret_cast<HMENU>(IDC_ASSET_MAINTENANCE),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Archivieren",WS_CHILD|WS_VISIBLE,322,228,95,30,hwnd,reinterpret_cast<HMENU>(IDC_ASSET_ARCHIVE),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Schließen",WS_CHILD|WS_VISIBLE,502,228,90,30,hwnd,reinterpret_cast<HMENU>(IDC_ASSET_CLOSE),nullptr,nullptr);context=reinterpret_cast<AssetManagerContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));RefreshAssetList(hwnd,context);return 0;case WM_ACTIVATE:if(LOWORD(wParam)!=WA_INACTIVE)RefreshAssetList(hwnd,context);return 0;case WM_COMMAND:if(LOWORD(wParam)==IDC_ASSET_CLOSE){DestroyWindow(hwnd);return 0;}if(LOWORD(wParam)==IDC_ASSET_NEW){ShowAssetEditor(hwnd);return 0;}if(LOWORD(wParam)==IDC_ASSET_EDIT||LOWORD(wParam)==IDC_ASSET_MAINTENANCE||LOWORD(wParam)==IDC_ASSET_ARCHIVE||(LOWORD(wParam)==IDC_ASSET_LIST&&HIWORD(wParam)==LBN_DBLCLK)){const int selected=static_cast<int>(SendMessageW(GetDlgItem(hwnd,IDC_ASSET_LIST),LB_GETCURSEL,0,0));if(!context||selected<0||static_cast<size_t>(selected)>=context->assets.size()){MessageBoxW(hwnd,L"Bitte zuerst ein Asset auswählen.",L"Facility-Assets",MB_ICONINFORMATION);return 0;}const EnterpriseFacilityAsset& asset=context->assets[static_cast<size_t>(selected)];if(LOWORD(wParam)==IDC_ASSET_MAINTENANCE){ShowMaintenanceManager(hwnd,asset);return 0;}if(LOWORD(wParam)==IDC_ASSET_ARCHIVE){if(MessageBoxW(hwnd,L"Asset archivieren?",L"Facility-Assets",MB_YESNO|MB_ICONWARNING)==IDYES){if(!g_Database->archiveFacilityAsset(asset.id,L"Ralf Krümmel"))MessageBoxW(hwnd,g_Database->error().c_str(),L"Facility-Assets",MB_ICONERROR);else{EnterpriseFacilityAsset archived=asset;archived.active=false;PublishFacilityAssetChange(archived,"facility.asset.archived");}}RefreshAssetList(hwnd,context);return 0;}ShowAssetEditor(hwnd,&asset);return 0;}break;case WM_CLOSE:DestroyWindow(hwnd);return 0;case WM_DESTROY:if(context&&context->owner){EnableWindow(context->owner,TRUE);SetForegroundWindow(context->owner);}delete context;return 0;}return DefWindowProcW(hwnd,msg,wParam,lParam);}
void ShowAssetManager(HWND owner){auto* context=new AssetManagerContext{owner};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraAssetManager",L"Facility-Assets",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,630,315,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}

void ShowRoomHistory(HWND owner, const OfficeRoom& room) {
    if (!g_Database) return;
    std::wostringstream history;
    history << L"Raum: " << room.name << L"\n\n";
    const std::vector<EnterpriseBooking> entries = g_Database->bookingsForRoom(room.id, true);
    if (entries.empty()) history << L"Keine Buchungen oder Sperrzeiten vorhanden.";
    for (const EnterpriseBooking& entry : entries) {
        const std::wstring state = entry.status == L"confirmed" ? L"Gebucht" : entry.status == L"blocked" ? L"Sperrzeit" : L"Storniert";
        history << FormatBookingDateTime(entry.startUtc) << L" – " << FormatBookingDateTime(entry.endUtc)
                << L" | " << state << L" | " << entry.attendees << L" Teilnehmer | Vorgang #" << entry.id << L"\n";
    }
    MessageBoxW(owner, history.str().c_str(), L"Raumhistorie", MB_ICONINFORMATION);
}

struct BookingEditorContext { HWND owner; OfficeRoom room; EnterpriseBooking existing; bool reschedule = false; };
LRESULT CALLBACK BookingEditorProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    auto* context = reinterpret_cast<BookingEditorContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    switch (msg) {
    case WM_NCCREATE: SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams)); return TRUE;
    case WM_CREATE: {
        context = reinterpret_cast<BookingEditorContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA)); SetWindowTextW(hwnd, (L"Raum buchen: " + context->room.name).c_str());
        const int64_t roundedNow = ((std::time(nullptr) + 299) / 300) * 300;
        const int64_t initialStart = context->reschedule ? context->existing.startUtc : roundedNow;
        const int64_t initialEnd = context->reschedule ? context->existing.endUtc : roundedNow + 3600;
        RoomEditorLabel(hwnd, L"Beginn (JJJJ-MM-TT SS:MM) *", 22, 20); RoomEditorLabel(hwnd, L"Ende (JJJJ-MM-TT SS:MM) *", 22, 68); RoomEditorLabel(hwnd, L"Teilnehmer *", 22, 116);
        if (!context->reschedule) { RoomEditorLabel(hwnd, L"Serientermine (Anzahl, 1 = einzeln)", 22, 164); RoomEditorLabel(hwnd, L"Abstand in Tagen", 220, 164); }
        CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", FormatBookingDateTime(initialStart).c_str(), WS_CHILD | WS_VISIBLE | ES_AUTOHSCROLL, 22, 40, 390, 24, hwnd, reinterpret_cast<HMENU>(IDC_BOOK_DURATION), nullptr, nullptr);
        CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", FormatBookingDateTime(initialEnd).c_str(), WS_CHILD | WS_VISIBLE | ES_AUTOHSCROLL, 22, 88, 390, 24, hwnd, reinterpret_cast<HMENU>(IDC_BOOK_ATTENDEES), nullptr, nullptr);
        CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", std::to_wstring(context->reschedule ? context->existing.attendees : 1).c_str(), WS_CHILD | WS_VISIBLE | ES_NUMBER, 22, 136, 390, 24, hwnd, reinterpret_cast<HMENU>(IDC_BOOK_CANCEL_BOOKING), nullptr, nullptr);
        if (!context->reschedule) { CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", L"1", WS_CHILD | WS_VISIBLE | ES_NUMBER, 22, 184, 180, 24, hwnd, reinterpret_cast<HMENU>(IDC_BOOK_SERIES_OCCURRENCES), nullptr, nullptr); CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", L"7", WS_CHILD | WS_VISIBLE | ES_NUMBER, 220, 184, 192, 24, hwnd, reinterpret_cast<HMENU>(IDC_BOOK_SERIES_INTERVAL_DAYS), nullptr, nullptr); }
        const int actionY = context->reschedule ? 188 : 236;
        CreateWindowW(L"BUTTON", context->reschedule ? L"Umbuchen" : L"Buchen", WS_CHILD | WS_VISIBLE | BS_DEFPUSHBUTTON, 120, actionY, 92, 30, hwnd, reinterpret_cast<HMENU>(IDC_BOOK_SAVE), nullptr, nullptr);
        if (context->reschedule) CreateWindowW(L"BUTTON", L"Stornieren", WS_CHILD | WS_VISIBLE, 220, actionY, 92, 30, hwnd, reinterpret_cast<HMENU>(IDC_BOOK_CANCEL_BOOKING + 100), nullptr, nullptr);
        CreateWindowW(L"BUTTON", L"Abbrechen", WS_CHILD | WS_VISIBLE, 320, actionY, 92, 30, hwnd, reinterpret_cast<HMENU>(IDC_BOOK_CANCEL), nullptr, nullptr); return 0;
    }
    case WM_COMMAND:
        if (LOWORD(wParam) == IDC_BOOK_CANCEL) { DestroyWindow(hwnd); return 0; }
        if (LOWORD(wParam) == IDC_BOOK_CANCEL_BOOKING + 100 && context && context->reschedule && g_Database) {
            const std::string cancelPayload = "{\"bookingId\":" + std::to_string(context->existing.id) + "}";
            if (!g_SyncService.publish("booking.cancelled", cancelPayload)) { MessageBoxW(hwnd, L"Die zentrale Stornierung konnte nicht bestätigt werden. Die lokale Buchung bleibt unverändert.", L"Zentrale Buchungsstornierung", MB_ICONERROR); return 0; }
            if (!g_Database->cancelBooking(context->existing.id, L"Ralf Krümmel")) {
                const std::string restorePayload = "{\"bookingId\":" + std::to_string(context->existing.id) + ",\"roomId\":" + std::to_string(context->existing.roomId) + ",\"startsAt\":" + std::to_string(context->existing.startUtc) + ",\"endsAt\":" + std::to_string(context->existing.endUtc) + "}";
                g_SyncService.publish("booking.created", restorePayload);
                MessageBoxW(hwnd, g_Database->error().c_str(), L"Stornierung abgelehnt", MB_ICONERROR); return 0;
            }
            RefreshRoomsFromDatabase(); InvalidateRect(context->owner, nullptr, FALSE); DestroyWindow(hwnd); return 0;
        }
        if (LOWORD(wParam) == IDC_BOOK_SAVE && context && g_Database) {
            int attendees = 0, occurrences = 1, intervalDays = 7; int64_t start = 0, end = 0;
            try { attendees = std::stoi(ControlText(hwnd, IDC_BOOK_CANCEL_BOOKING)); } catch (...) {}
            if (!context->reschedule) { try { occurrences = std::stoi(ControlText(hwnd, IDC_BOOK_SERIES_OCCURRENCES)); intervalDays = std::stoi(ControlText(hwnd, IDC_BOOK_SERIES_INTERVAL_DAYS)); } catch (...) {} }
            if (!ParseBookingDateTime(ControlText(hwnd, IDC_BOOK_DURATION), start) || !ParseBookingDateTime(ControlText(hwnd, IDC_BOOK_ATTENDEES), end)) { MessageBoxW(hwnd, L"Bitte Beginn und Ende als JJJJ-MM-TT SS:MM eingeben.", L"Ungültiges Datum", MB_ICONERROR); return 0; }
            if (occurrences < 1 || occurrences > 104 || intervalDays < 1 || intervalDays > 366) { MessageBoxW(hwnd, L"Eine Serie muss 1 bis 104 Termine und einen Abstand von 1 bis 366 Tagen haben.", L"Ungültige Serie", MB_ICONERROR); return 0; }
            AuraBookingRequest request{context->room.id, start, end, g_CurrentEmployeeId, attendees};
            const int32_t ruleResult = aura_validate_booking(&request, context->room.capacityLimit);
            if (ruleResult != 0) {
                MessageBoxA(hwnd, aura_rule_message(ruleResult), "Buchung durch Regelwerk abgelehnt", MB_ICONERROR);
                return 0;
            }
            // A preflight alone leaves a race between two clients.  Hold a
            // short-lived central reservation for every slot until the local
            // SQLite transaction succeeds, then commit the reservation.
            std::vector<int64_t> reservationIds;
            int32_t centralResult = 0;
            for (int seriesIndex = 0; seriesIndex < occurrences && centralResult == 0; ++seriesIndex) {
                const int64_t offset = static_cast<int64_t>(seriesIndex) * intervalDays * 86400;
                const int64_t reservationId = NextCentralReservationId();
                centralResult = g_SyncService.reserveBooking(reservationId, context->reschedule ? context->existing.id : 0, context->room.id, start + offset, end + offset);
                if (centralResult == 0) reservationIds.push_back(reservationId);
            }
            if (centralResult != 0) {
                for (const int64_t reservationId : reservationIds) g_SyncService.releaseBooking(reservationId);
                const wchar_t* reason = centralResult == 5 ? L"Mindestens ein Serientermin kollidiert mit einer bereits zentral bestätigten Buchung." : L"Die zentrale Buchungsprüfung ist nicht erreichbar oder hat die Anfrage abgelehnt.";
                MessageBoxW(hwnd, reason, L"Zentrale Buchungsreservierung", MB_ICONERROR);
                return 0;
            }
            int64_t id = 0; std::vector<int64_t> seriesIds;
            const EnterpriseBooking booking{context->reschedule ? context->existing.id : 0, context->room.id, g_CurrentEmployeeId, start, end, attendees, L"confirmed"};
            const bool saved = context->reschedule ? g_Database->rescheduleBooking(booking, L"Ralf Krümmel") : occurrences > 1 ? g_Database->createRecurringBookings(booking, intervalDays, occurrences, seriesIds, L"Ralf Krümmel") : g_Database->createBooking(booking, id, L"Ralf Krümmel");
            if (!saved) { for (const int64_t reservationId : reservationIds) g_SyncService.releaseBooking(reservationId); MessageBoxW(hwnd, g_Database->error().c_str(), L"Buchung abgelehnt", MB_ICONERROR); return 0; }
            bool centralCommitted = true;
            size_t committedCount = 0;
            for (size_t index = 0; index < reservationIds.size(); ++index) {
                const int64_t bookingId = context->reschedule ? context->existing.id : occurrences > 1 ? seriesIds[index] : id;
                if (g_SyncService.commitBooking(reservationIds[index], bookingId, context->reschedule) != 0) { centralCommitted = false; break; }
                ++committedCount;
            }
            if (!centralCommitted) {
                // Compensate both durable sides.  This makes an interrupted
                // distributed commit visible as a failed booking, never as a
                // silently divergent local booking.
                for (size_t index = 0; index < committedCount; ++index) {
                    const int64_t bookingId = context->reschedule ? context->existing.id : occurrences > 1 ? seriesIds[index] : id;
                    g_SyncService.publish("booking.cancelled", "{\"bookingId\":" + std::to_string(bookingId) + "}");
                }
                for (size_t index = committedCount; index < reservationIds.size(); ++index) g_SyncService.releaseBooking(reservationIds[index]);
                if (context->reschedule) g_Database->rescheduleBooking(context->existing, L"Ralf Krümmel");
                else if (occurrences > 1) for (const int64_t bookingId : seriesIds) g_Database->cancelBooking(bookingId, L"Ralf Krümmel");
                else g_Database->cancelBooking(id, L"Ralf Krümmel");
                MessageBoxW(hwnd, L"Die zentrale Buchung konnte nicht verbindlich festgeschrieben werden. Die lokale Änderung wurde zurückgenommen.", L"Zentrale Buchungsreservierung", MB_ICONERROR);
                return 0;
            }
            if (occurrences > 1 && !context->reschedule) {
                for (int seriesIndex = 0; seriesIndex < occurrences; ++seriesIndex) { const int64_t offset = static_cast<int64_t>(seriesIndex) * intervalDays * 86400; g_SyncService.publish("booking.created", "{\"bookingId\":" + std::to_string(seriesIds[seriesIndex]) + ",\"roomId\":" + std::to_string(context->room.id) + ",\"startsAt\":" + std::to_string(start + offset) + ",\"endsAt\":" + std::to_string(end + offset) + "}"); }
            } else {
                const int64_t eventId = context->reschedule ? context->existing.id : id;
                g_SyncService.publish(context->reschedule ? "booking.rescheduled" : "booking.created", "{\"bookingId\":" + std::to_string(eventId) + ",\"roomId\":" + std::to_string(context->room.id) + ",\"startsAt\":" + std::to_string(start) + ",\"endsAt\":" + std::to_string(end) + "}");
            }
            RefreshRoomsFromDatabase(); InvalidateRect(context->owner, nullptr, FALSE); DestroyWindow(hwnd); return 0;
        }
        break;
    case WM_CLOSE: DestroyWindow(hwnd); return 0;
    case WM_DESTROY: if (context && context->owner) { EnableWindow(context->owner, TRUE); SetForegroundWindow(context->owner); } delete context; return 0;
    } return DefWindowProcW(hwnd, msg, wParam, lParam);
}
void ShowBookingEditor(HWND owner, const OfficeRoom& room, const EnterpriseBooking* existing = nullptr) { auto* context = new BookingEditorContext{owner, room, existing ? *existing : EnterpriseBooking{}, existing != nullptr}; EnableWindow(owner, FALSE); if (!CreateWindowExW(WS_EX_DLGMODALFRAME, L"AuraBookingEditor", L"Buchung", WS_CAPTION | WS_SYSMENU | WS_POPUP | WS_VISIBLE, CW_USEDEFAULT, CW_USEDEFAULT, 450, existing ? 270 : 330, owner, nullptr, GetModuleHandleW(nullptr), context)) { EnableWindow(owner, TRUE); delete context; } }

struct BlockEditorContext { HWND owner; OfficeRoom room; };
LRESULT CALLBACK BlockEditorProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    auto* context=reinterpret_cast<BlockEditorContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));
    switch(msg){
    case WM_NCCREATE:SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams));return TRUE;
    case WM_CREATE:{context=reinterpret_cast<BlockEditorContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));SetWindowTextW(hwnd,(L"Sperrzeit anlegen: "+context->room.name).c_str());const int64_t start=((std::time(nullptr)+299)/300)*300;RoomEditorLabel(hwnd,L"Beginn (JJJJ-MM-TT SS:MM) *",22,20);RoomEditorLabel(hwnd,L"Ende (JJJJ-MM-TT SS:MM) *",22,68);RoomEditorLabel(hwnd,L"Grund *",22,116);CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",FormatBookingDateTime(start).c_str(),WS_CHILD|WS_VISIBLE,22,40,390,24,hwnd,reinterpret_cast<HMENU>(IDC_BLOCK_START),nullptr,nullptr);CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",FormatBookingDateTime(start+3600).c_str(),WS_CHILD|WS_VISIBLE,22,88,390,24,hwnd,reinterpret_cast<HMENU>(IDC_BLOCK_END),nullptr,nullptr);CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",L"Wartungsfenster",WS_CHILD|WS_VISIBLE,22,136,390,24,hwnd,reinterpret_cast<HMENU>(IDC_BLOCK_REASON),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Sperrzeit speichern",WS_CHILD|WS_VISIBLE|BS_DEFPUSHBUTTON,190,190,122,30,hwnd,reinterpret_cast<HMENU>(IDC_BLOCK_SAVE),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Abbrechen",WS_CHILD|WS_VISIBLE,320,190,92,30,hwnd,reinterpret_cast<HMENU>(IDC_BLOCK_CANCEL),nullptr,nullptr);return 0;}
    case WM_COMMAND:if(LOWORD(wParam)==IDC_BLOCK_CANCEL){DestroyWindow(hwnd);return 0;}if(LOWORD(wParam)==IDC_BLOCK_SAVE&&context&&g_Database){int64_t start=0,end=0;if(!ParseBookingDateTime(ControlText(hwnd,IDC_BLOCK_START),start)||!ParseBookingDateTime(ControlText(hwnd,IDC_BLOCK_END),end)||end<=start){MessageBoxW(hwnd,L"Bitte gültige, aufsteigende Zeitangaben im Format JJJJ-MM-TT SS:MM eingeben.",L"Ungültige Sperrzeit",MB_ICONERROR);return 0;}const std::wstring reason=ControlText(hwnd,IDC_BLOCK_REASON);int64_t id=0;if(!g_Database->blockRoom(context->room.id,start,end,reason,id,L"Ralf Krümmel")){MessageBoxW(hwnd,g_Database->error().c_str(),L"Sperrzeit",MB_ICONERROR);return 0;}RefreshRoomsFromDatabase();InvalidateRect(context->owner,nullptr,FALSE);DestroyWindow(hwnd);return 0;}break;
    case WM_CLOSE:DestroyWindow(hwnd);return 0;
    case WM_DESTROY:if(context&&context->owner){EnableWindow(context->owner,TRUE);SetForegroundWindow(context->owner);}delete context;return 0;
    }return DefWindowProcW(hwnd,msg,wParam,lParam);
}
void ShowBlockEditor(HWND owner,const OfficeRoom& room){auto* context=new BlockEditorContext{owner,room};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraBlockEditor",L"Sperrzeit",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,450,270,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}

struct RoomSearchContext { HWND owner = nullptr; std::vector<EnterpriseRoom> results; };

void RefreshRoomSearchResults(HWND hwnd, RoomSearchContext* context) {
    if (!context || !g_Database) return;
    context->results = g_Database->searchRooms(ControlText(hwnd, IDC_ROOM_SEARCH_QUERY));
    HWND list = GetDlgItem(hwnd, IDC_ROOM_SEARCH_RESULTS);
    SendMessageW(list, LB_RESETCONTENT, 0, 0);
    for (const EnterpriseRoom& room : context->results) {
        const std::wstring row = room.name + L"  |  " + room.location + L"  |  " + std::to_wstring(room.capacity) + L" Plätze  |  " + room.equipment;
        SendMessageW(list, LB_ADDSTRING, 0, reinterpret_cast<LPARAM>(row.c_str()));
    }
}

LRESULT CALLBACK RoomSearchProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    auto* context = reinterpret_cast<RoomSearchContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    switch (msg) {
    case WM_NCCREATE:
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams)); return TRUE;
    case WM_CREATE:
        context = reinterpret_cast<RoomSearchContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
        SetWindowTextW(hwnd, L"Räume suchen und buchen");
        RoomEditorLabel(hwnd, L"Name, Standort oder Ausstattung", 22, 18);
        CreateWindowExW(WS_EX_CLIENTEDGE, L"EDIT", L"", WS_CHILD | WS_VISIBLE | ES_AUTOHSCROLL, 22, 38, 390, 24, hwnd, reinterpret_cast<HMENU>(IDC_ROOM_SEARCH_QUERY), nullptr, nullptr);
        CreateWindowW(L"BUTTON", L"Suchen", WS_CHILD | WS_VISIBLE | BS_DEFPUSHBUTTON, 320, 70, 92, 28, hwnd, reinterpret_cast<HMENU>(IDC_ROOM_SEARCH_EXECUTE), nullptr, nullptr);
        CreateWindowExW(WS_EX_CLIENTEDGE, L"LISTBOX", L"", WS_CHILD | WS_VISIBLE | WS_VSCROLL | LBS_NOTIFY, 22, 106, 390, 166, hwnd, reinterpret_cast<HMENU>(IDC_ROOM_SEARCH_RESULTS), nullptr, nullptr);
        CreateWindowW(L"BUTTON", L"Ausgewählten Raum buchen", WS_CHILD | WS_VISIBLE, 158, 286, 170, 30, hwnd, reinterpret_cast<HMENU>(IDC_ROOM_SEARCH_BOOK), nullptr, nullptr);
        CreateWindowW(L"BUTTON", L"Schließen", WS_CHILD | WS_VISIBLE, 336, 286, 76, 30, hwnd, reinterpret_cast<HMENU>(IDC_ROOM_SEARCH_CLOSE), nullptr, nullptr);
        RefreshRoomSearchResults(hwnd, context);
        SetFocus(GetDlgItem(hwnd, IDC_ROOM_SEARCH_QUERY));
        return 0;
    case WM_COMMAND:
        if (LOWORD(wParam) == IDC_ROOM_SEARCH_CLOSE) { DestroyWindow(hwnd); return 0; }
        if (LOWORD(wParam) == IDC_ROOM_SEARCH_EXECUTE || (LOWORD(wParam) == IDC_ROOM_SEARCH_QUERY && HIWORD(wParam) == EN_CHANGE)) { RefreshRoomSearchResults(hwnd, context); return 0; }
        if ((LOWORD(wParam) == IDC_ROOM_SEARCH_BOOK) || (LOWORD(wParam) == IDC_ROOM_SEARCH_RESULTS && HIWORD(wParam) == LBN_DBLCLK)) {
            const int selection = static_cast<int>(SendMessageW(GetDlgItem(hwnd, IDC_ROOM_SEARCH_RESULTS), LB_GETCURSEL, 0, 0));
            if (!context || selection < 0 || static_cast<size_t>(selection) >= context->results.size()) { MessageBoxW(hwnd, L"Bitte zuerst einen Raum auswählen.", L"Raumsuche", MB_ICONINFORMATION); return 0; }
            const EnterpriseRoom selected = context->results[static_cast<size_t>(selection)];
            OfficeRoom room; room.id = selected.id; room.name = selected.name; room.location = selected.location; room.equipment = selected.equipment; room.capacityLimit = selected.capacity; room.capacity = std::to_wstring(selected.capacity) + L" Personen • " + selected.location + L" • " + selected.equipment;
            HWND owner = context->owner;
            DestroyWindow(hwnd);
            ShowBookingEditor(owner, room);
            return 0;
        }
        break;
    case WM_CLOSE: DestroyWindow(hwnd); return 0;
    case WM_DESTROY:
        if (context && context->owner) { EnableWindow(context->owner, TRUE); SetForegroundWindow(context->owner); }
        delete context; SetWindowLongPtrW(hwnd, GWLP_USERDATA, 0); return 0;
    }
    return DefWindowProcW(hwnd, msg, wParam, lParam);
}

void ShowRoomSearch(HWND owner) {
    auto* context = new RoomSearchContext{}; context->owner = owner;
    EnableWindow(owner, FALSE);
    if (!CreateWindowExW(WS_EX_DLGMODALFRAME, L"AuraRoomSearch", L"Raumsuche", WS_CAPTION | WS_SYSMENU | WS_POPUP | WS_VISIBLE, CW_USEDEFAULT, CW_USEDEFAULT, 450, 365, owner, nullptr, GetModuleHandleW(nullptr), context)) { EnableWindow(owner, TRUE); delete context; }
}

struct EmployeeEditorContext { HWND owner; EnterpriseEmployee employee; bool editing = false; std::vector<EnterpriseRole> roles; std::vector<std::wstring> assignedRoles; };
LRESULT CALLBACK EmployeeEditorProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    auto* context = reinterpret_cast<EmployeeEditorContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    switch (msg) {
    case WM_NCCREATE: SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams)); return TRUE;
    case WM_CREATE: {
        context=reinterpret_cast<EmployeeEditorContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA)); SetWindowTextW(hwnd,context->editing?L"Mitarbeiter bearbeiten":L"Mitarbeiter anlegen"); if(g_Database){context->roles=g_Database->roles(); if(context->editing)context->assignedRoles=g_Database->rolesForEmployee(context->employee.id);}
        RoomEditorLabel(hwnd,L"Name *",22,20); RoomEditorLabel(hwnd,L"Stellenbezeichnung *",22,68); RoomEditorLabel(hwnd,L"Abteilung *",22,116); RoomEditorLabel(hwnd,L"Systemrolle",22,164);
        CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",context->employee.name.c_str(),WS_CHILD|WS_VISIBLE,22,40,390,24,hwnd,reinterpret_cast<HMENU>(IDC_EMP_NAME),nullptr,nullptr); CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",context->employee.role.c_str(),WS_CHILD|WS_VISIBLE,22,88,390,24,hwnd,reinterpret_cast<HMENU>(IDC_EMP_ROLE),nullptr,nullptr); CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",context->employee.department.c_str(),WS_CHILD|WS_VISIBLE,22,136,390,24,hwnd,reinterpret_cast<HMENU>(IDC_EMP_DEPT),nullptr,nullptr); HWND systemRole=CreateWindowW(L"COMBOBOX",L"",WS_CHILD|WS_VISIBLE|CBS_DROPDOWNLIST,22,184,390,120,hwnd,reinterpret_cast<HMENU>(IDC_EMP_SYSTEM_ROLE),nullptr,nullptr); for(const EnterpriseRole& role:context->roles)SendMessageW(systemRole,CB_ADDSTRING,0,reinterpret_cast<LPARAM>(role.name.c_str())); int selected=0; for(size_t index=0;index<context->roles.size();++index)if(!context->assignedRoles.empty()&&context->roles[index].code==context->assignedRoles.front())selected=static_cast<int>(index); SendMessageW(systemRole,CB_SETCURSEL,selected,0); CreateWindowW(L"BUTTON",L"Speichern",WS_CHILD|WS_VISIBLE,220,236,92,30,hwnd,reinterpret_cast<HMENU>(IDC_EMP_SAVE),nullptr,nullptr); CreateWindowW(L"BUTTON",L"Abbrechen",WS_CHILD|WS_VISIBLE,320,236,92,30,hwnd,reinterpret_cast<HMENU>(IDC_EMP_CANCEL),nullptr,nullptr); return 0;
    }
    case WM_COMMAND:
        if (LOWORD(wParam)==IDC_EMP_CANCEL) { DestroyWindow(hwnd); return 0; }
        if (LOWORD(wParam)==IDC_EMP_SAVE && context && g_Database) { EnterpriseEmployee employee=context->employee; employee.name=ControlText(hwnd,IDC_EMP_NAME); employee.role=ControlText(hwnd,IDC_EMP_ROLE); employee.department=ControlText(hwnd,IDC_EMP_DEPT); if(employee.presence.empty())employee.presence=L"office"; int64_t id=0; const bool ok=context->editing?g_Database->updateEmployee(employee,L"Ralf Krümmel"):g_Database->createEmployee(employee,id,L"Ralf Krümmel"); const int targetRole=static_cast<int>(SendMessageW(GetDlgItem(hwnd,IDC_EMP_SYSTEM_ROLE),CB_GETCURSEL,0,0)); const int64_t employeeId=context->editing?employee.id:id; if(!ok||targetRole<0||static_cast<size_t>(targetRole)>=context->roles.size()||!g_Database->assignRole(employeeId,context->roles[static_cast<size_t>(targetRole)].code,L"Ralf Krümmel")){MessageBoxW(hwnd,g_Database->error().c_str(),L"Mitarbeiter",MB_ICONERROR);return 0;} employee.id=employeeId; PublishEmployeeChange(employee,context->editing?"employee.updated":"employee.created"); RefreshTeamFromDatabase();InvalidateRect(context->owner,nullptr,FALSE);DestroyWindow(hwnd);return 0; } break;
    case WM_CLOSE: DestroyWindow(hwnd); return 0;
    case WM_DESTROY: if(context&&context->owner)EnableWindow(context->owner,TRUE); delete context; return 0;
    } return DefWindowProcW(hwnd,msg,wParam,lParam);
}
void ShowEmployeeEditor(HWND owner,const EnterpriseEmployee* employee=nullptr){auto* context=new EmployeeEditorContext{owner,employee?*employee:EnterpriseEmployee{},employee!=nullptr};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraEmployeeEditor",L"Mitarbeiter",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,450,315,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}

struct ProjectEditorContext { HWND owner; EnterpriseProject project; bool editing = false; };
LRESULT CALLBACK ProjectEditorProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    auto* context = reinterpret_cast<ProjectEditorContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    switch (msg) {
    case WM_NCCREATE: SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams)); return TRUE;
    case WM_CREATE:
        context=reinterpret_cast<ProjectEditorContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA)); SetWindowTextW(hwnd,context&&context->editing?L"Projekt bearbeiten":L"Projekt anlegen");
        RoomEditorLabel(hwnd,L"Projektname *",22,20); RoomEditorLabel(hwnd,L"Bereich *",22,68); RoomEditorLabel(hwnd,L"Beschreibung",22,116);
        CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",context?context->project.name.c_str():L"",WS_CHILD|WS_VISIBLE,22,40,390,24,hwnd,reinterpret_cast<HMENU>(IDC_PROJECT_NAME),nullptr,nullptr);
        CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",context?context->project.department.c_str():L"",WS_CHILD|WS_VISIBLE,22,88,390,24,hwnd,reinterpret_cast<HMENU>(IDC_PROJECT_DEPARTMENT),nullptr,nullptr);
        CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",context?context->project.description.c_str():L"",WS_CHILD|WS_VISIBLE|ES_MULTILINE,22,136,390,60,hwnd,reinterpret_cast<HMENU>(IDC_PROJECT_DESCRIPTION),nullptr,nullptr);
        CreateWindowW(L"BUTTON",L"Speichern",WS_CHILD|WS_VISIBLE|BS_DEFPUSHBUTTON,220,214,92,30,hwnd,reinterpret_cast<HMENU>(IDC_PROJECT_SAVE),nullptr,nullptr); CreateWindowW(L"BUTTON",L"Abbrechen",WS_CHILD|WS_VISIBLE,320,214,92,30,hwnd,reinterpret_cast<HMENU>(IDC_PROJECT_CANCEL),nullptr,nullptr); return 0;
    case WM_COMMAND:
        if (LOWORD(wParam)==IDC_PROJECT_CANCEL) { DestroyWindow(hwnd); return 0; }
        if (LOWORD(wParam)==IDC_PROJECT_SAVE && context && g_Database) { EnterpriseProject project=context->project; project.name=ControlText(hwnd,IDC_PROJECT_NAME); project.department=ControlText(hwnd,IDC_PROJECT_DEPARTMENT); project.description=ControlText(hwnd,IDC_PROJECT_DESCRIPTION); if(project.status.empty())project.status=L"planned"; if(project.ownerId==0)project.ownerId=g_CurrentEmployeeId; int64_t id=0; const bool ok=context->editing?g_Database->updateProject(project,L"Ralf Krümmel"):g_Database->createProject(project,id,L"Ralf Krümmel"); if(!ok){MessageBoxW(hwnd,g_Database->error().c_str(),L"Projekt",MB_ICONERROR);return 0;} if(!context->editing)project.id=id; PublishProjectChange(project,context->editing?"project.updated":"project.created"); InvalidateRect(context->owner,nullptr,FALSE);DestroyWindow(hwnd);return 0; } break;
    case WM_CLOSE: DestroyWindow(hwnd); return 0;
    case WM_DESTROY: if(context&&context->owner)EnableWindow(context->owner,TRUE); delete context; return 0;
    } return DefWindowProcW(hwnd,msg,wParam,lParam);
}
void ShowProjectEditor(HWND owner,const EnterpriseProject* project=nullptr){auto* context=new ProjectEditorContext{owner,project?*project:EnterpriseProject{},project!=nullptr};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraProjectEditor",L"Projekt",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,450,300,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}

struct OrganizationEditorContext { HWND owner; EnterpriseOrganizationUnit unit; bool editing=false; std::vector<EnterpriseEmployee> employees; };
LRESULT CALLBACK OrganizationEditorProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    auto* context=reinterpret_cast<OrganizationEditorContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));
    switch(msg) {
    case WM_NCCREATE: SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams)); return TRUE;
    case WM_CREATE: {
        context=reinterpret_cast<OrganizationEditorContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA)); if(g_Database)context->employees=g_Database->employees(); SetWindowTextW(hwnd,context&&context->editing?L"Organisationseinheit bearbeiten":L"Organisationseinheit anlegen");
        RoomEditorLabel(hwnd,L"Name *",22,20); RoomEditorLabel(hwnd,L"Kürzel / Code *",22,68); RoomEditorLabel(hwnd,L"Leitung",22,116);
        CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",context?context->unit.name.c_str():L"",WS_CHILD|WS_VISIBLE,22,40,390,24,hwnd,reinterpret_cast<HMENU>(IDC_ORG_NAME),nullptr,nullptr);
        CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",context?context->unit.code.c_str():L"",WS_CHILD|WS_VISIBLE,22,88,390,24,hwnd,reinterpret_cast<HMENU>(IDC_ORG_CODE),nullptr,nullptr);
        HWND manager=CreateWindowW(L"COMBOBOX",L"",WS_CHILD|WS_VISIBLE|CBS_DROPDOWNLIST,22,136,390,120,hwnd,reinterpret_cast<HMENU>(IDC_ORG_MANAGER),nullptr,nullptr); SendMessageW(manager,CB_ADDSTRING,0,reinterpret_cast<LPARAM>(L"Keine Leitung zugeordnet")); int selected=0; for(size_t i=0;i<context->employees.size();++i){const EnterpriseEmployee& employee=context->employees[i];const std::wstring label=employee.name+L" — "+employee.role;SendMessageW(manager,CB_ADDSTRING,0,reinterpret_cast<LPARAM>(label.c_str()));if(employee.id==context->unit.managerId)selected=static_cast<int>(i+1);} SendMessageW(manager,CB_SETCURSEL,selected,0);
        CreateWindowW(L"BUTTON",L"Speichern",WS_CHILD|WS_VISIBLE|BS_DEFPUSHBUTTON,220,190,92,30,hwnd,reinterpret_cast<HMENU>(IDC_ORG_SAVE),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Abbrechen",WS_CHILD|WS_VISIBLE,320,190,92,30,hwnd,reinterpret_cast<HMENU>(IDC_ORG_CANCEL),nullptr,nullptr);return 0;
    }
    case WM_COMMAND:
        if(LOWORD(wParam)==IDC_ORG_CANCEL){DestroyWindow(hwnd);return 0;}
        if(LOWORD(wParam)==IDC_ORG_SAVE&&context&&g_Database){EnterpriseOrganizationUnit unit=context->unit;unit.name=ControlText(hwnd,IDC_ORG_NAME);unit.code=ControlText(hwnd,IDC_ORG_CODE);const int selection=static_cast<int>(SendMessageW(GetDlgItem(hwnd,IDC_ORG_MANAGER),CB_GETCURSEL,0,0));unit.managerId=selection>0&&static_cast<size_t>(selection-1)<context->employees.size()?context->employees[static_cast<size_t>(selection-1)].id:0;int64_t id=0;const bool ok=context->editing?g_Database->updateOrganizationUnit(unit,L"Ralf Krümmel"):g_Database->createOrganizationUnit(unit,id,L"Ralf Krümmel");if(!ok){MessageBoxW(hwnd,g_Database->error().c_str(),L"Organisationseinheit",MB_ICONERROR);return 0;}unit.id=context->editing?unit.id:id;PublishOrganizationChange(unit,context->editing?"organization.updated":"organization.created");InvalidateRect(context->owner,nullptr,FALSE);DestroyWindow(hwnd);return 0;}break;
    case WM_CLOSE:DestroyWindow(hwnd);return 0;
    case WM_DESTROY:if(context&&context->owner){EnableWindow(context->owner,TRUE);SetForegroundWindow(context->owner);}delete context;return 0;
    }return DefWindowProcW(hwnd,msg,wParam,lParam);
}
void ShowOrganizationEditor(HWND owner,const EnterpriseOrganizationUnit* unit=nullptr){auto* context=new OrganizationEditorContext{owner,unit?*unit:EnterpriseOrganizationUnit{},unit!=nullptr};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraOrganizationEditor",L"Organisationseinheit",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,450,270,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}

struct OrganizationManagerContext { HWND owner; std::vector<EnterpriseOrganizationUnit> units; };
void RefreshOrganizationList(HWND hwnd,OrganizationManagerContext* context){if(!context||!g_Database)return;context->units=g_Database->organizationUnits();HWND list=GetDlgItem(hwnd,IDC_ORG_LIST);SendMessageW(list,LB_RESETCONTENT,0,0);for(const EnterpriseOrganizationUnit& unit:context->units){std::wstring manager=L"ohne Leitung";for(const EnterpriseEmployee& employee:g_Database->employees())if(employee.id==unit.managerId){manager=employee.name;break;}const std::wstring row=unit.name+L"  ["+unit.code+L"]  —  "+manager;SendMessageW(list,LB_ADDSTRING,0,reinterpret_cast<LPARAM>(row.c_str()));}}
LRESULT CALLBACK OrganizationManagerProc(HWND hwnd,UINT msg,WPARAM wParam,LPARAM lParam){auto* context=reinterpret_cast<OrganizationManagerContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));switch(msg){case WM_NCCREATE:SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams));return TRUE;case WM_CREATE:SetWindowTextW(hwnd,L"Organisationseinheiten");CreateWindowExW(WS_EX_CLIENTEDGE,L"LISTBOX",L"",WS_CHILD|WS_VISIBLE|WS_VSCROLL|LBS_NOTIFY,22,24,470,190,hwnd,reinterpret_cast<HMENU>(IDC_ORG_LIST),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Neu",WS_CHILD|WS_VISIBLE,22,230,80,30,hwnd,reinterpret_cast<HMENU>(IDC_ORG_NEW),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Bearbeiten",WS_CHILD|WS_VISIBLE,112,230,95,30,hwnd,reinterpret_cast<HMENU>(IDC_ORG_EDIT),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Archivieren",WS_CHILD|WS_VISIBLE,217,230,95,30,hwnd,reinterpret_cast<HMENU>(IDC_ORG_ARCHIVE),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Schließen",WS_CHILD|WS_VISIBLE,402,230,90,30,hwnd,reinterpret_cast<HMENU>(IDC_ORG_CLOSE),nullptr,nullptr);context=reinterpret_cast<OrganizationManagerContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));RefreshOrganizationList(hwnd,context);return 0;case WM_ACTIVATE:if(LOWORD(wParam)!=WA_INACTIVE)RefreshOrganizationList(hwnd,context);return 0;case WM_COMMAND:if(LOWORD(wParam)==IDC_ORG_CLOSE){DestroyWindow(hwnd);return 0;}if(LOWORD(wParam)==IDC_ORG_NEW){ShowOrganizationEditor(hwnd);return 0;}if(LOWORD(wParam)==IDC_ORG_EDIT||LOWORD(wParam)==IDC_ORG_ARCHIVE||(LOWORD(wParam)==IDC_ORG_LIST&&HIWORD(wParam)==LBN_DBLCLK)){const int selected=static_cast<int>(SendMessageW(GetDlgItem(hwnd,IDC_ORG_LIST),LB_GETCURSEL,0,0));if(!context||selected<0||static_cast<size_t>(selected)>=context->units.size()){MessageBoxW(hwnd,L"Bitte zuerst eine Organisationseinheit auswählen.",L"Organisation",MB_ICONINFORMATION);return 0;}const EnterpriseOrganizationUnit& unit=context->units[static_cast<size_t>(selected)];if(LOWORD(wParam)==IDC_ORG_ARCHIVE){if(MessageBoxW(hwnd,L"Organisationseinheit archivieren?",L"Organisation",MB_YESNO|MB_ICONWARNING)==IDYES&&g_Database->archiveOrganizationUnit(unit.id,L"Ralf Krümmel")){EnterpriseOrganizationUnit archived=unit;archived.active=false;PublishOrganizationChange(archived,"organization.archived");}RefreshOrganizationList(hwnd,context);return 0;}ShowOrganizationEditor(hwnd,&unit);return 0;}break;case WM_CLOSE:DestroyWindow(hwnd);return 0;case WM_DESTROY:if(context&&context->owner){EnableWindow(context->owner,TRUE);SetForegroundWindow(context->owner);}delete context;return 0;}return DefWindowProcW(hwnd,msg,wParam,lParam);}
void ShowOrganizationManager(HWND owner){auto* context=new OrganizationManagerContext{owner};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraOrganizationManager",L"Organisation",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,530,315,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}

void ApplyCurrentEmployee(const EnterpriseEmployee& employee) { g_CurrentEmployeeId=employee.id; if(g_Database)g_Database->setAuditActor(employee.name); g_State.userStatus=employee.presence==L"remote"?PresenceStatus::REMOTE:employee.presence==L"meeting"?PresenceStatus::MEETING:employee.presence==L"away"?PresenceStatus::AWAY:PresenceStatus::OFFICE; }
struct SessionPickerContext { HWND owner; std::vector<EnterpriseAccount> accounts; };
struct PasswordChangeContext { HWND loginWindow; HWND mainOwner; EnterpriseAccount account; std::wstring oldPassword; bool completed=false; };
LRESULT CALLBACK PasswordChangeProc(HWND hwnd,UINT msg,WPARAM wParam,LPARAM lParam){auto* context=reinterpret_cast<PasswordChangeContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));switch(msg){case WM_NCCREATE:SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams));return TRUE;case WM_CREATE:{context=reinterpret_cast<PasswordChangeContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));SetWindowTextW(hwnd,L"Passwortänderung erforderlich");CreateWindowW(L"STATIC",L"Dieses Konto benötigt vor der Nutzung ein neues Passwort.",WS_CHILD|WS_VISIBLE,22,18,390,22,hwnd,nullptr,nullptr,nullptr);CreateWindowW(L"STATIC",L"Neues Passwort (mindestens 12 Zeichen)",WS_CHILD|WS_VISIBLE,22,54,280,20,hwnd,nullptr,nullptr,nullptr);CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",L"",WS_CHILD|WS_VISIBLE|ES_PASSWORD|ES_AUTOHSCROLL,22,76,390,24,hwnd,reinterpret_cast<HMENU>(IDC_PASSWORD_NEW),nullptr,nullptr);CreateWindowW(L"STATIC",L"Passwort wiederholen",WS_CHILD|WS_VISIBLE,22,112,180,20,hwnd,nullptr,nullptr,nullptr);CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",L"",WS_CHILD|WS_VISIBLE|ES_PASSWORD|ES_AUTOHSCROLL,22,134,390,24,hwnd,reinterpret_cast<HMENU>(IDC_PASSWORD_CONFIRM),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Speichern",WS_CHILD|WS_VISIBLE|BS_DEFPUSHBUTTON,218,180,92,30,hwnd,reinterpret_cast<HMENU>(IDC_PASSWORD_SAVE),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Abbrechen",WS_CHILD|WS_VISIBLE,320,180,92,30,hwnd,reinterpret_cast<HMENU>(IDC_PASSWORD_CANCEL),nullptr,nullptr);return 0;}case WM_COMMAND:if(LOWORD(wParam)==IDC_PASSWORD_CANCEL){DestroyWindow(hwnd);return 0;}if(LOWORD(wParam)==IDC_PASSWORD_SAVE&&context&&g_Database){const std::wstring password=ControlText(hwnd,IDC_PASSWORD_NEW);if(password!=ControlText(hwnd,IDC_PASSWORD_CONFIRM)){MessageBoxW(hwnd,L"Die Passwörter stimmen nicht überein.",L"Passwortänderung",MB_ICONWARNING);return 0;}if(!g_Database->changePassword(context->account.employeeId,context->oldPassword,password,context->account.username)){MessageBoxW(hwnd,g_Database->error().c_str(),L"Passwortänderung",MB_ICONERROR);return 0;}for(const EnterpriseEmployee& employee:g_Database->employees())if(employee.id==context->account.employeeId){ApplyCurrentEmployee(employee);g_Authenticated=true;RefreshTeamFromDatabase();InvalidateRect(context->mainOwner,nullptr,FALSE);context->completed=true;DestroyWindow(context->loginWindow);DestroyWindow(hwnd);return 0;}MessageBoxW(hwnd,L"Zum Konto wurde kein aktiver Mitarbeiter gefunden.",L"Passwortänderung",MB_ICONERROR);return 0;}break;case WM_CLOSE:DestroyWindow(hwnd);return 0;case WM_DESTROY:if(context&&!context->completed&&context->loginWindow)EnableWindow(context->loginWindow,TRUE);delete context;return 0;}return DefWindowProcW(hwnd,msg,wParam,lParam);}
void ShowMandatoryPasswordChange(HWND loginWindow,HWND mainOwner,const EnterpriseAccount& account,const std::wstring& oldPassword){auto* context=new PasswordChangeContext{loginWindow,mainOwner,account,oldPassword};EnableWindow(loginWindow,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraPasswordChange",L"Passwortänderung",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,450,270,loginWindow,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(loginWindow,TRUE);delete context;}}
LRESULT CALLBACK SessionPickerProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    auto* context = reinterpret_cast<SessionPickerContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
    switch (msg) {
    case WM_NCCREATE:
        SetWindowLongPtrW(hwnd, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams)); return TRUE;
    case WM_CREATE: {
        context = reinterpret_cast<SessionPickerContext*>(GetWindowLongPtrW(hwnd, GWLP_USERDATA));
        if (g_Database) context->accounts = g_Database->accounts();
        SetWindowTextW(hwnd, L"Sicher anmelden");
        CreateWindowW(L"STATIC", L"Konto auswählen und Passwort eingeben.", WS_CHILD | WS_VISIBLE, 22, 18, 410, 22, hwnd, nullptr, nullptr, nullptr);
        HWND list = CreateWindowExW(WS_EX_CLIENTEDGE, L"LISTBOX", L"", WS_CHILD | WS_VISIBLE | WS_VSCROLL | LBS_NOTIFY, 22, 48, 410, 115, hwnd, reinterpret_cast<HMENU>(IDC_SESSION_LIST), nullptr, nullptr);
        int currentSelection = 0;
        for (size_t i = 0; i < context->accounts.size(); ++i) { const EnterpriseAccount& account = context->accounts[i]; const std::wstring label = account.username; SendMessageW(list, LB_ADDSTRING, 0, reinterpret_cast<LPARAM>(label.c_str())); if (account.employeeId == g_CurrentEmployeeId) currentSelection = static_cast<int>(i); }
        SendMessageW(list, LB_SETCURSEL, currentSelection, 0);
        CreateWindowW(L"STATIC",L"Passwort",WS_CHILD|WS_VISIBLE,22,174,100,20,hwnd,nullptr,nullptr,nullptr);CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",L"",WS_CHILD|WS_VISIBLE|ES_PASSWORD|ES_AUTOHSCROLL,22,195,410,24,hwnd,reinterpret_cast<HMENU>(IDC_SESSION_PASSWORD),nullptr,nullptr);
        CreateWindowW(L"BUTTON", L"Anmelden", WS_CHILD | WS_VISIBLE | BS_DEFPUSHBUTTON, 225, 235, 100, 30, hwnd, reinterpret_cast<HMENU>(IDC_SESSION_SELECT), nullptr, nullptr);
        CreateWindowW(L"BUTTON", L"Abbrechen", WS_CHILD | WS_VISIBLE, 335, 235, 97, 30, hwnd, reinterpret_cast<HMENU>(IDC_SESSION_CANCEL), nullptr, nullptr);
        return 0;
    }
    case WM_COMMAND:
        if (LOWORD(wParam) == IDC_SESSION_CANCEL) { DestroyWindow(hwnd); return 0; }
        if (LOWORD(wParam) == IDC_SESSION_SELECT || (LOWORD(wParam) == IDC_SESSION_LIST && HIWORD(wParam) == LBN_DBLCLK)) { const int selected = static_cast<int>(SendMessageW(GetDlgItem(hwnd, IDC_SESSION_LIST), LB_GETCURSEL, 0, 0)); if (!context || selected < 0 || static_cast<size_t>(selected) >= context->accounts.size()) { MessageBoxW(hwnd, L"Bitte ein Konto auswählen.", L"Anmeldung", MB_ICONINFORMATION); return 0; } const std::wstring password=ControlText(hwnd,IDC_SESSION_PASSWORD); EnterpriseAccount authenticated; if(!g_Database->authenticate(context->accounts[static_cast<size_t>(selected)].username,password,std::time(nullptr),authenticated)){MessageBoxW(hwnd,g_Database->error().c_str(),L"Anmeldung",MB_ICONERROR);SetWindowTextW(GetDlgItem(hwnd,IDC_SESSION_PASSWORD),L"");return 0;}if(authenticated.mustChangePassword){ShowMandatoryPasswordChange(hwnd,context->owner,authenticated,password);return 0;}for(const EnterpriseEmployee& employee:g_Database->employees())if(employee.id==authenticated.employeeId){ApplyCurrentEmployee(employee);g_Authenticated=true;RefreshTeamFromDatabase();InvalidateRect(context->owner,nullptr,FALSE);DestroyWindow(hwnd);return 0;}MessageBoxW(hwnd,L"Zum Konto wurde kein aktiver Mitarbeiter gefunden.",L"Anmeldung",MB_ICONERROR);return 0; }
        break;
    case WM_CLOSE: DestroyWindow(hwnd); return 0;
    case WM_DESTROY: if (context && context->owner) { EnableWindow(context->owner, TRUE); SetForegroundWindow(context->owner); } delete context; return 0;
    }
    return DefWindowProcW(hwnd, msg, wParam, lParam);
}
void ShowSessionPicker(HWND owner){auto* context=new SessionPickerContext{owner};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraSessionPicker",L"Anmeldung",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,470,325,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}

std::vector<std::wstring> ParsePermissions(const std::wstring& value){std::vector<std::wstring> result;std::wstringstream input(value);std::wstring item;while(std::getline(input,item,L',')){const size_t first=item.find_first_not_of(L" \t\r\n"),last=item.find_last_not_of(L" \t\r\n");if(first!=std::wstring::npos)result.push_back(item.substr(first,last-first+1));}return result;}
std::wstring JoinPermissions(const std::vector<std::wstring>& permissions){std::wstring value;for(const std::wstring& permission:permissions){if(!value.empty())value+=L", ";value+=permission;}return value;}
struct RoleEditorContext { HWND owner; EnterpriseRole role; bool editing=false; std::vector<std::wstring> permissions; };
LRESULT CALLBACK RoleEditorProc(HWND hwnd,UINT msg,WPARAM wParam,LPARAM lParam){auto* context=reinterpret_cast<RoleEditorContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));switch(msg){case WM_NCCREATE:SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams));return TRUE;case WM_CREATE:{context=reinterpret_cast<RoleEditorContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));if(g_Database&&context->editing)context->permissions=g_Database->permissionsForRole(context->role.id);SetWindowTextW(hwnd,context&&context->editing?L"Rolle bearbeiten":L"Rolle anlegen");RoomEditorLabel(hwnd,L"Code *",22,18);RoomEditorLabel(hwnd,L"Name *",22,66);RoomEditorLabel(hwnd,L"Beschreibung",22,114);RoomEditorLabel(hwnd,L"Berechtigungen * (kommagetrennt)",22,174);CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",context?context->role.code.c_str():L"",WS_CHILD|WS_VISIBLE,22,38,390,24,hwnd,reinterpret_cast<HMENU>(IDC_ROLE_CODE),nullptr,nullptr);CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",context?context->role.name.c_str():L"",WS_CHILD|WS_VISIBLE,22,86,390,24,hwnd,reinterpret_cast<HMENU>(IDC_ROLE_NAME),nullptr,nullptr);CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",context?context->role.description.c_str():L"",WS_CHILD|WS_VISIBLE,22,134,390,28,hwnd,reinterpret_cast<HMENU>(IDC_ROLE_DESCRIPTION),nullptr,nullptr);const std::wstring permissions=context?JoinPermissions(context->permissions):L"";CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",permissions.c_str(),WS_CHILD|WS_VISIBLE,22,194,390,28,hwnd,reinterpret_cast<HMENU>(IDC_ROLE_PERMISSIONS),nullptr,nullptr);CreateWindowW(L"STATIC",L"Beispiele: rooms.manage, tasks.manage, tickets.manage, employees.manage, admin.manage oder *",WS_CHILD|WS_VISIBLE,22,226,390,22,hwnd,nullptr,nullptr,nullptr);CreateWindowW(L"BUTTON",L"Speichern",WS_CHILD|WS_VISIBLE|BS_DEFPUSHBUTTON,220,260,92,30,hwnd,reinterpret_cast<HMENU>(IDC_ROLE_SAVE),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Abbrechen",WS_CHILD|WS_VISIBLE,320,260,92,30,hwnd,reinterpret_cast<HMENU>(IDC_ROLE_CANCEL),nullptr,nullptr);return 0;}case WM_COMMAND:if(LOWORD(wParam)==IDC_ROLE_CANCEL){DestroyWindow(hwnd);return 0;}if(LOWORD(wParam)==IDC_ROLE_SAVE&&context&&g_Database){EnterpriseRole role=context->role;role.code=ControlText(hwnd,IDC_ROLE_CODE);role.name=ControlText(hwnd,IDC_ROLE_NAME);role.description=ControlText(hwnd,IDC_ROLE_DESCRIPTION);const std::vector<std::wstring> permissions=ParsePermissions(ControlText(hwnd,IDC_ROLE_PERMISSIONS));int64_t id=0;const bool ok=context->editing?g_Database->updateRole(role,permissions,L"Ralf Krümmel"):g_Database->createRole(role,id,permissions,L"Ralf Krümmel");if(!ok){MessageBoxW(hwnd,g_Database->error().c_str(),L"Rolle",MB_ICONERROR);return 0;}InvalidateRect(context->owner,nullptr,FALSE);DestroyWindow(hwnd);return 0;}break;case WM_CLOSE:DestroyWindow(hwnd);return 0;case WM_DESTROY:if(context&&context->owner){EnableWindow(context->owner,TRUE);SetForegroundWindow(context->owner);}delete context;return 0;}return DefWindowProcW(hwnd,msg,wParam,lParam);}
void ShowRoleEditor(HWND owner,const EnterpriseRole* role=nullptr){auto* context=new RoleEditorContext{owner,role?*role:EnterpriseRole{},role!=nullptr};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraRoleEditor",L"Rolle",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,450,340,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}
struct RoleManagerContext { HWND owner; std::vector<EnterpriseRole> roles; };
void RefreshRoleList(HWND hwnd,RoleManagerContext* context){if(!context||!g_Database)return;context->roles=g_Database->roles();HWND list=GetDlgItem(hwnd,IDC_ROLE_LIST);SendMessageW(list,LB_RESETCONTENT,0,0);for(const EnterpriseRole& role:context->roles){const std::wstring row=role.name+L"  ["+role.code+L"]  —  "+JoinPermissions(g_Database->permissionsForRole(role.id));SendMessageW(list,LB_ADDSTRING,0,reinterpret_cast<LPARAM>(row.c_str()));}}
LRESULT CALLBACK RoleManagerProc(HWND hwnd,UINT msg,WPARAM wParam,LPARAM lParam){auto* context=reinterpret_cast<RoleManagerContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));switch(msg){case WM_NCCREATE:SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams));return TRUE;case WM_CREATE:CreateWindowExW(WS_EX_CLIENTEDGE,L"LISTBOX",L"",WS_CHILD|WS_VISIBLE|WS_VSCROLL|LBS_NOTIFY,22,22,570,190,hwnd,reinterpret_cast<HMENU>(IDC_ROLE_LIST),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Neu",WS_CHILD|WS_VISIBLE,22,228,80,30,hwnd,reinterpret_cast<HMENU>(IDC_ROLE_NEW),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Bearbeiten",WS_CHILD|WS_VISIBLE,112,228,95,30,hwnd,reinterpret_cast<HMENU>(IDC_ROLE_EDIT),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Archivieren",WS_CHILD|WS_VISIBLE,217,228,95,30,hwnd,reinterpret_cast<HMENU>(IDC_ROLE_ARCHIVE),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Schließen",WS_CHILD|WS_VISIBLE,502,228,90,30,hwnd,reinterpret_cast<HMENU>(IDC_ROLE_CLOSE),nullptr,nullptr);context=reinterpret_cast<RoleManagerContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));RefreshRoleList(hwnd,context);return 0;case WM_ACTIVATE:if(LOWORD(wParam)!=WA_INACTIVE)RefreshRoleList(hwnd,context);return 0;case WM_COMMAND:if(LOWORD(wParam)==IDC_ROLE_CLOSE){DestroyWindow(hwnd);return 0;}if(LOWORD(wParam)==IDC_ROLE_NEW){ShowRoleEditor(hwnd);return 0;}if(LOWORD(wParam)==IDC_ROLE_EDIT||LOWORD(wParam)==IDC_ROLE_ARCHIVE||(LOWORD(wParam)==IDC_ROLE_LIST&&HIWORD(wParam)==LBN_DBLCLK)){const int selected=static_cast<int>(SendMessageW(GetDlgItem(hwnd,IDC_ROLE_LIST),LB_GETCURSEL,0,0));if(!context||selected<0||static_cast<size_t>(selected)>=context->roles.size()){MessageBoxW(hwnd,L"Bitte zuerst eine Rolle auswählen.",L"Rollen",MB_ICONINFORMATION);return 0;}const EnterpriseRole& role=context->roles[static_cast<size_t>(selected)];if(LOWORD(wParam)==IDC_ROLE_ARCHIVE){if(MessageBoxW(hwnd,L"Rolle archivieren?",L"Rollen",MB_YESNO|MB_ICONWARNING)==IDYES&&!g_Database->archiveRole(role.id,L"Ralf Krümmel"))MessageBoxW(hwnd,g_Database->error().c_str(),L"Rollen",MB_ICONERROR);RefreshRoleList(hwnd,context);return 0;}ShowRoleEditor(hwnd,&role);return 0;}break;case WM_CLOSE:DestroyWindow(hwnd);return 0;case WM_DESTROY:if(context&&context->owner){EnableWindow(context->owner,TRUE);SetForegroundWindow(context->owner);}delete context;return 0;}return DefWindowProcW(hwnd,msg,wParam,lParam);}
void ShowRoleManager(HWND owner){auto* context=new RoleManagerContext{owner};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraRoleManager",L"Rollen & Berechtigungen",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,630,315,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}

struct AccountCreateContext { HWND owner; std::vector<EnterpriseEmployee> employees; };
LRESULT CALLBACK AccountCreateProc(HWND hwnd,UINT msg,WPARAM wParam,LPARAM lParam){auto* context=reinterpret_cast<AccountCreateContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));switch(msg){case WM_NCCREATE:SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams));return TRUE;case WM_CREATE:{context=reinterpret_cast<AccountCreateContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));SetWindowTextW(hwnd,L"Konto anlegen");CreateWindowW(L"STATIC",L"Mitarbeiter",WS_CHILD|WS_VISIBLE,22,18,120,20,hwnd,nullptr,nullptr,nullptr);HWND employees=CreateWindowW(L"COMBOBOX",L"",WS_CHILD|WS_VISIBLE|WS_VSCROLL|CBS_DROPDOWNLIST,22,40,390,180,hwnd,reinterpret_cast<HMENU>(IDC_ACCOUNT_EMPLOYEE),nullptr,nullptr);if(g_Database){const std::vector<EnterpriseAccount> accounts=g_Database->accounts(true);for(const EnterpriseEmployee& employee:g_Database->employees()){bool hasAccount=false;for(const EnterpriseAccount& account:accounts)if(account.employeeId==employee.id)hasAccount=true;if(!hasAccount){context->employees.push_back(employee);const std::wstring row=employee.name+L" — "+employee.department;SendMessageW(employees,CB_ADDSTRING,0,reinterpret_cast<LPARAM>(row.c_str()));}}}if(context&&!context->employees.empty())SendMessageW(employees,CB_SETCURSEL,0,0);CreateWindowW(L"STATIC",L"Benutzername",WS_CHILD|WS_VISIBLE,22,78,120,20,hwnd,nullptr,nullptr,nullptr);CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",L"",WS_CHILD|WS_VISIBLE|ES_AUTOHSCROLL,22,100,390,24,hwnd,reinterpret_cast<HMENU>(IDC_ACCOUNT_USERNAME),nullptr,nullptr);CreateWindowW(L"STATIC",L"Temporäres Passwort (mindestens 12 Zeichen)",WS_CHILD|WS_VISIBLE,22,136,310,20,hwnd,nullptr,nullptr,nullptr);CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",L"",WS_CHILD|WS_VISIBLE|ES_PASSWORD|ES_AUTOHSCROLL,22,158,390,24,hwnd,reinterpret_cast<HMENU>(IDC_ACCOUNT_PASSWORD),nullptr,nullptr);CreateWindowW(L"STATIC",L"Passwort wiederholen",WS_CHILD|WS_VISIBLE,22,194,180,20,hwnd,nullptr,nullptr,nullptr);CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",L"",WS_CHILD|WS_VISIBLE|ES_PASSWORD|ES_AUTOHSCROLL,22,216,390,24,hwnd,reinterpret_cast<HMENU>(IDC_ACCOUNT_CONFIRM),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Konto anlegen",WS_CHILD|WS_VISIBLE|BS_DEFPUSHBUTTON,208,262,104,30,hwnd,reinterpret_cast<HMENU>(IDC_ACCOUNT_SAVE),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Abbrechen",WS_CHILD|WS_VISIBLE,322,262,90,30,hwnd,reinterpret_cast<HMENU>(IDC_ACCOUNT_CANCEL),nullptr,nullptr);return 0;}case WM_COMMAND:if(LOWORD(wParam)==IDC_ACCOUNT_CANCEL){DestroyWindow(hwnd);return 0;}if(LOWORD(wParam)==IDC_ACCOUNT_SAVE&&context&&g_Database){const int selected=static_cast<int>(SendMessageW(GetDlgItem(hwnd,IDC_ACCOUNT_EMPLOYEE),CB_GETCURSEL,0,0));if(selected<0||static_cast<size_t>(selected)>=context->employees.size()){MessageBoxW(hwnd,L"Bitte einen Mitarbeiter ohne Konto auswählen.",L"Kontenverwaltung",MB_ICONWARNING);return 0;}const std::wstring password=ControlText(hwnd,IDC_ACCOUNT_PASSWORD);if(password!=ControlText(hwnd,IDC_ACCOUNT_CONFIRM)){MessageBoxW(hwnd,L"Die Passwörter stimmen nicht überein.",L"Kontenverwaltung",MB_ICONWARNING);return 0;}if(!g_Database->createOrResetAccount(context->employees[static_cast<size_t>(selected)].id,ControlText(hwnd,IDC_ACCOUNT_USERNAME),password,true,L"admin")){MessageBoxW(hwnd,g_Database->error().c_str(),L"Kontenverwaltung",MB_ICONERROR);return 0;}MessageBoxW(hwnd,L"Konto angelegt. Die Passwortänderung ist beim ersten Login verpflichtend.",L"Kontenverwaltung",MB_ICONINFORMATION);DestroyWindow(hwnd);return 0;}break;case WM_CLOSE:DestroyWindow(hwnd);return 0;case WM_DESTROY:if(context&&context->owner){EnableWindow(context->owner,TRUE);SetForegroundWindow(context->owner);}delete context;return 0;}return DefWindowProcW(hwnd,msg,wParam,lParam);}
void ShowAccountCreate(HWND owner){auto* context=new AccountCreateContext{owner};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraAccountCreate",L"Konto anlegen",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,450,350,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}
struct AccountResetContext { HWND owner; EnterpriseAccount account; };
LRESULT CALLBACK AccountResetProc(HWND hwnd,UINT msg,WPARAM wParam,LPARAM lParam){auto* context=reinterpret_cast<AccountResetContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));switch(msg){case WM_NCCREATE:SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams));return TRUE;case WM_CREATE:{context=reinterpret_cast<AccountResetContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));SetWindowTextW(hwnd,L"Zugang zurücksetzen");const std::wstring note=L"Temporäres Passwort für "+context->account.username+L" festlegen. Beim nächsten Login ist eine Änderung Pflicht.";CreateWindowW(L"STATIC",note.c_str(),WS_CHILD|WS_VISIBLE,22,18,410,34,hwnd,nullptr,nullptr,nullptr);CreateWindowW(L"STATIC",L"Temporäres Passwort (mindestens 12 Zeichen)",WS_CHILD|WS_VISIBLE,22,62,300,20,hwnd,nullptr,nullptr,nullptr);CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",L"",WS_CHILD|WS_VISIBLE|ES_PASSWORD|ES_AUTOHSCROLL,22,84,390,24,hwnd,reinterpret_cast<HMENU>(IDC_ACCOUNT_PASSWORD),nullptr,nullptr);CreateWindowW(L"STATIC",L"Passwort wiederholen",WS_CHILD|WS_VISIBLE,22,120,180,20,hwnd,nullptr,nullptr,nullptr);CreateWindowExW(WS_EX_CLIENTEDGE,L"EDIT",L"",WS_CHILD|WS_VISIBLE|ES_PASSWORD|ES_AUTOHSCROLL,22,142,390,24,hwnd,reinterpret_cast<HMENU>(IDC_ACCOUNT_CONFIRM),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Zurücksetzen",WS_CHILD|WS_VISIBLE|BS_DEFPUSHBUTTON,208,188,104,30,hwnd,reinterpret_cast<HMENU>(IDC_ACCOUNT_SAVE),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Abbrechen",WS_CHILD|WS_VISIBLE,322,188,90,30,hwnd,reinterpret_cast<HMENU>(IDC_ACCOUNT_CANCEL),nullptr,nullptr);return 0;}case WM_COMMAND:if(LOWORD(wParam)==IDC_ACCOUNT_CANCEL){DestroyWindow(hwnd);return 0;}if(LOWORD(wParam)==IDC_ACCOUNT_SAVE&&context&&g_Database){const std::wstring password=ControlText(hwnd,IDC_ACCOUNT_PASSWORD);if(password!=ControlText(hwnd,IDC_ACCOUNT_CONFIRM)){MessageBoxW(hwnd,L"Die Passwörter stimmen nicht überein.",L"Kontenverwaltung",MB_ICONWARNING);return 0;}if(!g_Database->createOrResetAccount(context->account.employeeId,context->account.username,password,true,L"admin")){MessageBoxW(hwnd,g_Database->error().c_str(),L"Kontenverwaltung",MB_ICONERROR);return 0;}MessageBoxW(hwnd,L"Zugang wurde zurückgesetzt. Die Passwortänderung ist beim nächsten Anmelden verpflichtend.",L"Kontenverwaltung",MB_ICONINFORMATION);DestroyWindow(hwnd);return 0;}break;case WM_CLOSE:DestroyWindow(hwnd);return 0;case WM_DESTROY:if(context&&context->owner){EnableWindow(context->owner,TRUE);SetForegroundWindow(context->owner);}delete context;return 0;}return DefWindowProcW(hwnd,msg,wParam,lParam);}
void ShowAccountReset(HWND owner,const EnterpriseAccount& account){auto* context=new AccountResetContext{owner,account};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraAccountReset",L"Zugang zurücksetzen",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,450,275,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}
struct AccountManagerContext { HWND owner; std::vector<EnterpriseAccount> accounts; };
void RefreshAccountList(HWND hwnd,AccountManagerContext* context){if(!context||!g_Database)return;context->accounts=g_Database->accounts(true);HWND list=GetDlgItem(hwnd,IDC_ACCOUNT_LIST);SendMessageW(list,LB_RESETCONTENT,0,0);for(const EnterpriseAccount& account:context->accounts){const std::wstring state=account.enabled?(account.lockedUntil>std::time(nullptr)?L"gesperrt":L"aktiv"):L"deaktiviert";const std::wstring row=account.username+L"  —  "+state+(account.mustChangePassword?L"  • Passwortwechsel offen":L"");SendMessageW(list,LB_ADDSTRING,0,reinterpret_cast<LPARAM>(row.c_str()));}}
LRESULT CALLBACK AccountManagerProc(HWND hwnd,UINT msg,WPARAM wParam,LPARAM lParam){auto* context=reinterpret_cast<AccountManagerContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));switch(msg){case WM_NCCREATE:SetWindowLongPtrW(hwnd,GWLP_USERDATA,reinterpret_cast<LONG_PTR>(reinterpret_cast<CREATESTRUCTW*>(lParam)->lpCreateParams));return TRUE;case WM_CREATE:CreateWindowExW(WS_EX_CLIENTEDGE,L"LISTBOX",L"",WS_CHILD|WS_VISIBLE|WS_VSCROLL|LBS_NOTIFY,22,22,570,190,hwnd,reinterpret_cast<HMENU>(IDC_ACCOUNT_LIST),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Neu",WS_CHILD|WS_VISIBLE,22,228,70,30,hwnd,reinterpret_cast<HMENU>(IDC_ACCOUNT_NEW),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Aktiv/Deaktiv",WS_CHILD|WS_VISIBLE,102,228,110,30,hwnd,reinterpret_cast<HMENU>(IDC_ACCOUNT_TOGGLE),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Zugang zurücksetzen",WS_CHILD|WS_VISIBLE,222,228,145,30,hwnd,reinterpret_cast<HMENU>(IDC_ACCOUNT_RESET),nullptr,nullptr);CreateWindowW(L"BUTTON",L"Schließen",WS_CHILD|WS_VISIBLE,502,228,90,30,hwnd,reinterpret_cast<HMENU>(IDC_ACCOUNT_CLOSE),nullptr,nullptr);context=reinterpret_cast<AccountManagerContext*>(GetWindowLongPtrW(hwnd,GWLP_USERDATA));RefreshAccountList(hwnd,context);return 0;case WM_ACTIVATE:if(LOWORD(wParam)!=WA_INACTIVE)RefreshAccountList(hwnd,context);return 0;case WM_COMMAND:if(LOWORD(wParam)==IDC_ACCOUNT_CLOSE){DestroyWindow(hwnd);return 0;}if(LOWORD(wParam)==IDC_ACCOUNT_NEW){ShowAccountCreate(hwnd);return 0;}if(LOWORD(wParam)==IDC_ACCOUNT_TOGGLE||LOWORD(wParam)==IDC_ACCOUNT_RESET){const int selected=static_cast<int>(SendMessageW(GetDlgItem(hwnd,IDC_ACCOUNT_LIST),LB_GETCURSEL,0,0));if(!context||selected<0||static_cast<size_t>(selected)>=context->accounts.size()){MessageBoxW(hwnd,L"Bitte zuerst ein Konto auswählen.",L"Kontenverwaltung",MB_ICONINFORMATION);return 0;}const EnterpriseAccount& account=context->accounts[static_cast<size_t>(selected)];if(LOWORD(wParam)==IDC_ACCOUNT_RESET){ShowAccountReset(hwnd,account);return 0;}if(account.employeeId==g_CurrentEmployeeId){MessageBoxW(hwnd,L"Das aktuell angemeldete Konto kann nicht deaktiviert werden.",L"Kontenverwaltung",MB_ICONWARNING);return 0;}if(!g_Database->setAccountEnabled(account.employeeId,!account.enabled,L"admin"))MessageBoxW(hwnd,g_Database->error().c_str(),L"Kontenverwaltung",MB_ICONERROR);RefreshAccountList(hwnd,context);return 0;}break;case WM_CLOSE:DestroyWindow(hwnd);return 0;case WM_DESTROY:if(context&&context->owner){EnableWindow(context->owner,TRUE);SetForegroundWindow(context->owner);}delete context;return 0;}return DefWindowProcW(hwnd,msg,wParam,lParam);}
void ShowAccountManager(HWND owner){auto* context=new AccountManagerContext{owner};EnableWindow(owner,FALSE);if(!CreateWindowExW(WS_EX_DLGMODALFRAME,L"AuraAccountManager",L"Kontenverwaltung",WS_CAPTION|WS_SYSMENU|WS_POPUP|WS_VISIBLE,CW_USEDEFAULT,CW_USEDEFAULT,630,315,owner,nullptr,GetModuleHandleW(nullptr),context)){EnableWindow(owner,TRUE);delete context;}}

// ============================================================================
// 5. ZEICHEN-PRIMITIVEN
// ============================================================================
void DrawRoundedRect(HDC hdc, const RECT& rc, COLORREF bg, COLORREF border, int radius = 10) {
    HPEN hPen = CreatePen(PS_SOLID, 1, border);
    HBRUSH hBrush = CreateSolidBrush(bg);
    HGDIOBJ oldPen = SelectObject(hdc, hPen);
    HGDIOBJ oldBrush = SelectObject(hdc, hBrush);

    RoundRect(hdc, rc.left, rc.top, rc.right, rc.bottom, radius, radius);

    SelectObject(hdc, oldPen);
    SelectObject(hdc, oldBrush);
    DeleteObject(hPen);
    DeleteObject(hBrush);
}

void DrawBadge(HDC hdc, int x, int y, const std::wstring& text, COLORREF bg, COLORREF fg) {
    RECT calcRc = { 0, 0, 0, 0 };
    SelectObject(hdc, g_Fonts.hSmall);
    DrawTextW(hdc, text.c_str(), -1, &calcRc, DT_CALCRECT | DT_SINGLELINE);

    int padX = 8, padY = 3;
    RECT badgeRc = { x, y, x + (calcRc.right - calcRc.left) + (padX * 2), y + (calcRc.bottom - calcRc.top) + (padY * 2) };

    DrawRoundedRect(hdc, badgeRc, bg, bg, 8);
    SetTextColor(hdc, fg);
    SetBkMode(hdc, TRANSPARENT);
    DrawTextW(hdc, text.c_str(), -1, &badgeRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
}

// ============================================================================
// 6. RENDERING-MODULE
// ============================================================================
void RenderSidebar(HDC hdc, const RECT& clientRc) {
    RECT sideRc = { 0, 0, 240, clientRc.bottom };
    HBRUSH hSideBrush = CreateSolidBrush(Theme::BG_SIDEBAR);
    FillRect(hdc, &sideRc, hSideBrush);
    DeleteObject(hSideBrush);

    HPEN hBorderPen = CreatePen(PS_SOLID, 1, Theme::BORDER);
    HGDIOBJ oldPen = SelectObject(hdc, hBorderPen);
    MoveToEx(hdc, 240, 0, nullptr);
    LineTo(hdc, 240, clientRc.bottom);
    SelectObject(hdc, oldPen);
    DeleteObject(hBorderPen);

    SetBkMode(hdc, TRANSPARENT);
    SelectObject(hdc, g_Fonts.hSubtitle);
    SetTextColor(hdc, Theme::TEXT_WHITE);
    RECT brandRc = { 24, 24, 220, 50 };
    DrawTextW(hdc, L"AURA WORKSPACE", -1, &brandRc, DT_SINGLELINE | DT_LEFT);

    SelectObject(hdc, g_Fonts.hSmall);
    SetTextColor(hdc, Theme::ACCENT);
    RECT subBrandRc = { 24, 46, 220, 65 };
    DrawTextW(hdc, L"ENTERPRISE CORE OS \u2022 v2.4.3", -1, &subBrandRc, DT_SINGLELINE | DT_LEFT);

    const wchar_t* navLabels[] = {
        L" \u25A3  Dashboard",
        L" \u25A4  R\u00e4ume & Kalender",
        L" \u2714  Aufgaben & Workflow",
        L" \u25CB  Team & Pr\u00e4senz",
        L" \u2709  Office Helpdesk",
        L" \u2699  Administration"
    };

    int startY = 90;
    for (int i = 0; i < 6; ++i) {
        RECT itemRc = { 14, startY + (i * 44), 226, startY + (i * 44) + 38 };
        bool isActive = (static_cast<int>(g_State.activeTab) == i);
        bool isHovered = (g_State.hoveredNavIndex == i);

        if (isActive) {
            DrawRoundedRect(hdc, itemRc, Theme::ACCENT_BG, Theme::ACCENT, 8);
        } else if (isHovered) {
            DrawRoundedRect(hdc, itemRc, Theme::BG_CARD, Theme::BORDER_MUTED, 8);
        }

        SelectObject(hdc, isActive ? g_Fonts.hBodyBold : g_Fonts.hBody);
        SetTextColor(hdc, isActive ? Theme::TEXT_WHITE : (isHovered ? Theme::TEXT_WHITE : Theme::TEXT_MUTED));
        
        RECT textRc = { itemRc.left + 12, itemRc.top, itemRc.right, itemRc.bottom };
        DrawTextW(hdc, navLabels[i], -1, &textRc, DT_SINGLELINE | DT_VCENTER);
    }

    RECT userRc = { 14, clientRc.bottom - 74, 226, clientRc.bottom - 16 };
    DrawRoundedRect(hdc, userRc, Theme::BG_CARD, Theme::BORDER, 10);
    std::wstring currentName = L"Kein Benutzer";
    std::wstring currentRole = L"Sitzung wählen";
    if (g_Database) for (const EnterpriseEmployee& employee : g_Database->employees()) if (employee.id == g_CurrentEmployeeId) { currentName = employee.name; currentRole = employee.role; break; }

    SelectObject(hdc, g_Fonts.hBodyBold);
    SetTextColor(hdc, Theme::TEXT_WHITE);
    RECT userNameRc = { userRc.left + 12, userRc.top + 8, userRc.right - 10, userRc.top + 26 };
    DrawTextW(hdc, currentName.c_str(), -1, &userNameRc, DT_SINGLELINE);

    SelectObject(hdc, g_Fonts.hSmall);
    SetTextColor(hdc, Theme::TEXT_MUTED);
    RECT userRoleRc = { userRc.left + 12, userRc.top + 28, userRc.right - 10, userRc.top + 45 };
    DrawTextW(hdc, currentRole.c_str(), -1, &userRoleRc, DT_SINGLELINE);

    HBRUSH hDot = CreateSolidBrush(Theme::STATUS_GREEN);
    HGDIOBJ oldB = SelectObject(hdc, hDot);
    SelectObject(hdc, GetStockObject(NULL_PEN));
    Ellipse(hdc, userRc.right - 22, userRc.top + 14, userRc.right - 12, userRc.top + 24);
    SelectObject(hdc, oldB);
    DeleteObject(hDot);
}

void RenderHeader(HDC hdc, const RECT& clientRc) {
    int startX = 240;
    RECT headerRc = { startX, 0, clientRc.right, 70 };

    HPEN hBorderPen = CreatePen(PS_SOLID, 1, Theme::BORDER);
    HGDIOBJ oldPen = SelectObject(hdc, hBorderPen);
    MoveToEx(hdc, startX, 70, nullptr);
    LineTo(hdc, clientRc.right, 70);
    SelectObject(hdc, oldPen);
    DeleteObject(hBorderPen);

    const wchar_t* tabTitles[] = {
        L"Office Leitstand \u2022 Zentrale \u00dcbersicht",
        L"Ressourcenverwaltung \u2022 Raumbuchung & Zonen",
        L"Aufgaben-Management \u2022 Aktuelle Arbeitsauftr\u00e4ge",
        L"Mitarbeiter-Verzeichnis & Status",
        L"Office-Helpdesk & Facility Support",
        L"Administration \u2022 Audit, Sicherung & Betrieb"
    };

    SelectObject(hdc, g_Fonts.hTitle);
    SetTextColor(hdc, Theme::TEXT_WHITE);
    RECT titleRc = { startX + 30, 20, startX + 600, 55 };
    DrawTextW(hdc, tabTitles[static_cast<int>(g_State.activeTab)], -1, &titleRc, DT_SINGLELINE | DT_VCENTER);

    RECT statusPill = { clientRc.right - 230, 18, clientRc.right - 30, 52 };
    DrawRoundedRect(hdc, statusPill, Theme::BG_CARD, Theme::BORDER, 16);

    const wchar_t* statusTexts[] = { L"\u25CF Im B\u00fcro", L"\u25CF Home-Office", L"\u25CF Im Meeting", L"\u25CF Pause" };
    COLORREF statusColors[] = { Theme::STATUS_GREEN, Theme::ACCENT, Theme::STATUS_YELLOW, Theme::TEXT_MUTED };

    int sIdx = static_cast<int>(g_State.userStatus);
    SelectObject(hdc, g_Fonts.hBodyBold);
    SetTextColor(hdc, statusColors[sIdx]);
    DrawTextW(hdc, statusTexts[sIdx], -1, &statusPill, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
}

void RenderDashboard(HDC hdc, const RECT& mainRc) {
    int x = mainRc.left + 30;
    int y = mainRc.top + 24;

    const int bookedRooms = static_cast<int>(std::count_if(g_State.rooms.begin(), g_State.rooms.end(), [](const OfficeRoom& room) { return room.isBooked; }));
    const int openTickets = static_cast<int>(std::count_if(g_State.tickets.begin(), g_State.tickets.end(), [](const HelpTicket& ticket) { return ticket.state != L"Gel\u00f6st" && ticket.state != L"Geschlossen"; }));
    const int openTasks = static_cast<int>(std::count_if(g_State.tasks.begin(), g_State.tasks.end(), [](const OfficeTask& task) { return !task.done; }));
    const std::wstring teamValue = std::to_wstring(g_State.team.size()) + L" aktiv";
    const std::wstring roomsValue = std::to_wstring(static_cast<int>(g_State.rooms.size()) - bookedRooms) + L" von " + std::to_wstring(g_State.rooms.size());
    const std::wstring ticketsValue = std::to_wstring(openTickets) + L" offen";
    const std::wstring tasksValue = std::to_wstring(openTasks) + L" offen";

    struct StatCard {
        const wchar_t* title;
        const wchar_t* value;
        const wchar_t* sub;
        COLORREF accent;
    };

    StatCard stats[4] = {
        { L"Aktive Mitarbeiter", teamValue.c_str(), L"Aus Organisationsdaten", Theme::STATUS_GREEN },
        { L"Freie Meetingr\u00e4ume", roomsValue.c_str(), L"Live aus Buchungsdaten", Theme::ACCENT },
        { L"Offene Tickets", ticketsValue.c_str(), L"Aus Helpdesk", Theme::STATUS_YELLOW },
        { L"Offene Aufgaben", tasksValue.c_str(), L"Aus Workflow", Theme::TEXT_SECONDARY }
    };

    int cardWidth = (mainRc.right - x - 30 - (3 * 16)) / 4;
    for (int i = 0; i < 4; ++i) {
        RECT cardRc = { x + (i * (cardWidth + 16)), y, x + (i * (cardWidth + 16)) + cardWidth, y + 104 };
        DrawRoundedRect(hdc, cardRc, Theme::BG_CARD, Theme::BORDER, 10);

        SelectObject(hdc, g_Fonts.hSmall);
        SetTextColor(hdc, Theme::TEXT_MUTED);
        RECT tRc = { cardRc.left + 16, cardRc.top + 14, cardRc.right - 16, cardRc.top + 30 };
        DrawTextW(hdc, stats[i].title, -1, &tRc, DT_SINGLELINE);

        SelectObject(hdc, g_Fonts.hStatBig);
        SetTextColor(hdc, Theme::TEXT_WHITE);
        RECT vRc = { cardRc.left + 16, cardRc.top + 34, cardRc.right - 16, cardRc.top + 68 };
        DrawTextW(hdc, stats[i].value, -1, &vRc, DT_SINGLELINE | DT_VCENTER);

        SelectObject(hdc, g_Fonts.hSmall);
        SetTextColor(hdc, stats[i].accent);
        RECT sRc = { cardRc.left + 16, cardRc.top + 72, cardRc.right - 16, cardRc.bottom - 10 };
        DrawTextW(hdc, stats[i].sub, -1, &sRc, DT_SINGLELINE);
    }

    int splitY = y + 124;
    int colWidth = (mainRc.right - x - 46) / 2;

    RECT leftPanel = { x, splitY, x + colWidth, mainRc.bottom - 24 };
    DrawRoundedRect(hdc, leftPanel, Theme::BG_CARD, Theme::BORDER, 12);

    SelectObject(hdc, g_Fonts.hSection);
    SetTextColor(hdc, Theme::TEXT_WHITE);
    RECT p1Title = { leftPanel.left + 20, leftPanel.top + 16, leftPanel.right - 20, leftPanel.top + 40 };
    DrawTextW(hdc, L"Heutiger Raum- & Terminplan", -1, &p1Title, DT_SINGLELINE);

    int mY = leftPanel.top + 50;
    for (size_t i = 0; i < g_State.meetings.size(); ++i) {
        RECT itemBox = { leftPanel.left + 16, mY, leftPanel.right - 16, mY + 68 };
        DrawRoundedRect(hdc, itemBox, Theme::BG_DARK, Theme::BORDER_MUTED, 8);

        SelectObject(hdc, g_Fonts.hSmall);
        SetTextColor(hdc, Theme::ACCENT);
        RECT timeRc = { itemBox.left + 14, itemBox.top + 10, itemBox.right - 14, itemBox.top + 26 };
        DrawTextW(hdc, g_State.meetings[i].time.c_str(), -1, &timeRc, DT_SINGLELINE);

        SelectObject(hdc, g_Fonts.hBodyBold);
        SetTextColor(hdc, Theme::TEXT_WHITE);
        RECT meetTitleRc = { itemBox.left + 14, itemBox.top + 27, itemBox.right - 14, itemBox.top + 46 };
        DrawTextW(hdc, g_State.meetings[i].title.c_str(), -1, &meetTitleRc, DT_SINGLELINE | DT_WORD_ELLIPSIS);

        SelectObject(hdc, g_Fonts.hSmall);
        SetTextColor(hdc, Theme::TEXT_MUTED);
        std::wstring meta = g_State.meetings[i].room + L" \u2022 Host: " + g_State.meetings[i].host;
        RECT metaRc = { itemBox.left + 14, itemBox.top + 46, itemBox.right - 14, itemBox.bottom - 6 };
        DrawTextW(hdc, meta.c_str(), -1, &metaRc, DT_SINGLELINE);

        mY += 76;
    }

    RECT rightPanel = { x + colWidth + 16, splitY, mainRc.right - 30, mainRc.bottom - 24 };
    DrawRoundedRect(hdc, rightPanel, Theme::BG_CARD, Theme::BORDER, 12);

    SelectObject(hdc, g_Fonts.hSection);
    SetTextColor(hdc, Theme::TEXT_WHITE);
    RECT p2Title = { rightPanel.left + 20, rightPanel.top + 16, rightPanel.right - 20, rightPanel.top + 40 };
    DrawTextW(hdc, L"Office Board & Quick Actions", -1, &p2Title, DT_SINGLELINE);

    RECT memoRc = { rightPanel.left + 16, rightPanel.top + 50, rightPanel.right - 16, rightPanel.top + 150 };
    DrawRoundedRect(hdc, memoRc, Theme::BG_DARK, Theme::BORDER_MUTED, 8);

    SelectObject(hdc, g_Fonts.hBodyBold);
    SetTextColor(hdc, Theme::STATUS_YELLOW);
    RECT memoHeader = { memoRc.left + 14, memoRc.top + 12, memoRc.right - 14, memoRc.top + 30 };
    DrawTextW(hdc, L"\u26A0 Facility-Hinweis: Wartungsfenster heute 18:00", -1, &memoHeader, DT_SINGLELINE);

    SelectObject(hdc, g_Fonts.hBody);
    SetTextColor(hdc, Theme::TEXT_SECONDARY);
    RECT memoBody = { memoRc.left + 14, memoRc.top + 36, memoRc.right - 14, memoRc.bottom - 10 };
    DrawTextW(hdc, L"Die L\u00fcftungszentrale im S\u00fcdfl\u00fcgel wird planm\u00e4\u00dfig gewartet. Arbeitspl\u00e4tze k\u00f6nnen regul\u00e4r genutzt werden. Bei Temperaturabweichungen bitte Ticket er\u00f6ffnen.", -1, &memoBody, DT_WORDBREAK);

    const std::vector<std::wstring> audit = g_Database ? g_Database->auditLog(1) : std::vector<std::wstring>{};
    SelectObject(hdc, g_Fonts.hSmall);
    SetTextColor(hdc, Theme::TEXT_MUTED);
    RECT auditRc = { rightPanel.left + 16, rightPanel.top + 152, rightPanel.right - 16, rightPanel.top + 168 };
    const std::wstring auditText = audit.empty() ? L"Audit: noch keine Änderungen" : L"Audit: " + audit.front();
    DrawTextW(hdc, auditText.c_str(), -1, &auditRc, DT_SINGLELINE | DT_WORD_ELLIPSIS);

    const wchar_t* actionLabels[] = {
        L"\u2795  Neuen Urlaubsantrag einreichen",
        L"\u2615  Kaffeebohnen Nachf\u00fcllung melden",
        L"\u2328  Neues Ticket an IT-Support senden"
    };

    int actY = rightPanel.top + 180;
    for (int i = 0; i < 3; ++i) {
        RECT actRc = { rightPanel.left + 16, actY, rightPanel.right - 16, actY + 44 };
        bool isHovered = (g_State.hoveredActionIndex == i);

        DrawRoundedRect(hdc, actRc, isHovered ? Theme::BG_CARD_HOVER : Theme::BG_DARK, isHovered ? Theme::ACCENT : Theme::BORDER_MUTED, 8);

        SelectObject(hdc, g_Fonts.hBodyBold);
        SetTextColor(hdc, isHovered ? Theme::ACCENT_HOVER : Theme::TEXT_WHITE);
        RECT actTextRc = { actRc.left + 16, actRc.top, actRc.right - 16, actRc.bottom };
        DrawTextW(hdc, actionLabels[i], -1, &actTextRc, DT_SINGLELINE | DT_VCENTER);

        actY += 52;
    }
}

void RenderRooms(HDC hdc, const RECT& mainRc) {
    int x = mainRc.left + 30;
    int y = mainRc.top + 24;

    SelectObject(hdc, g_Fonts.hSection);
    SetTextColor(hdc, Theme::TEXT_WHITE);
    RECT titleRc = { x, y, x + 360, y + 30 };
    DrawTextW(hdc, L"Aktive Räume & Ressourcen", -1, &titleRc, DT_SINGLELINE | DT_VCENTER);
    RECT searchRc = { mainRc.right - 530, y - 2, mainRc.right - 420, y + 32 };
    RECT importRc = { mainRc.right - 410, y - 2, mainRc.right - 300, y + 32 };
    RECT exportRc = { mainRc.right - 290, y - 2, mainRc.right - 180, y + 32 };
    RECT createRc = { mainRc.right - 170, y - 2, mainRc.right - 30, y + 32 };
    DrawRoundedRect(hdc, searchRc, Theme::BG_DARK, Theme::BORDER, 7);
    DrawRoundedRect(hdc, importRc, Theme::BG_DARK, Theme::BORDER, 7);
    DrawRoundedRect(hdc, exportRc, Theme::BG_DARK, Theme::BORDER, 7);
    DrawRoundedRect(hdc, createRc, Theme::ACCENT, Theme::ACCENT, 7);
    SelectObject(hdc, g_Fonts.hBodyBold);
    SetTextColor(hdc, Theme::TEXT_SECONDARY);
    DrawTextW(hdc, L"Räume suchen", -1, &searchRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
    DrawTextW(hdc, L"CSV importieren", -1, &importRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
    SetTextColor(hdc, Theme::TEXT_SECONDARY);
    DrawTextW(hdc, L"CSV exportieren", -1, &exportRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
    SetTextColor(hdc, Theme::TEXT_WHITE);
    DrawTextW(hdc, L"+ Raum anlegen", -1, &createRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
    y += 48;

    for (size_t i = 0; i < g_State.rooms.size(); ++i) {
        RECT roomCard = { x, y, mainRc.right - 30, y + 80 };
        DrawRoundedRect(hdc, roomCard, Theme::BG_CARD, Theme::BORDER, 10);

        SelectObject(hdc, g_Fonts.hSubtitle);
        SetTextColor(hdc, Theme::TEXT_WHITE);
        RECT nameRc = { roomCard.left + 20, roomCard.top + 16, roomCard.left + 450, roomCard.top + 40 };
        DrawTextW(hdc, g_State.rooms[i].name.c_str(), -1, &nameRc, DT_SINGLELINE);

        SelectObject(hdc, g_Fonts.hSmall);
        SetTextColor(hdc, Theme::TEXT_MUTED);
        RECT capRc = { roomCard.left + 20, roomCard.top + 44, roomCard.left + 450, roomCard.top + 64 };
        DrawTextW(hdc, g_State.rooms[i].capacity.c_str(), -1, &capRc, DT_SINGLELINE);

        bool booked = g_State.rooms[i].isBooked;
        DrawBadge(hdc, roomCard.right - 260, roomCard.top + 26, 
            booked ? g_State.rooms[i].until : L"\u2714 Sofort verf\u00fcgbar",
            booked ? RGB(60, 25, 30) : RGB(20, 50, 35),
            booked ? Theme::STATUS_RED : Theme::STATUS_GREEN);

        RECT blockRc = { roomCard.right - 445, roomCard.top + 22, roomCard.right - 350, roomCard.bottom - 22 };
        RECT archiveRc = { roomCard.right - 340, roomCard.top + 22, roomCard.right - 245, roomCard.bottom - 22 };
        RECT editRc = { roomCard.right - 235, roomCard.top + 22, roomCard.right - 130, roomCard.bottom - 22 };
        RECT btnRc = { roomCard.right - 120, roomCard.top + 22, roomCard.right - 20, roomCard.bottom - 22 };
        DrawRoundedRect(hdc, blockRc, Theme::BG_DARK, Theme::BORDER, 6);
        DrawRoundedRect(hdc, archiveRc, Theme::BG_DARK, Theme::BORDER, 6);
        DrawRoundedRect(hdc, editRc, Theme::BG_DARK, Theme::BORDER, 6);
        DrawRoundedRect(hdc, btnRc, booked ? Theme::BG_DARK : Theme::ACCENT, Theme::BORDER, 6);

        SelectObject(hdc, g_Fonts.hBodyBold);
        SetTextColor(hdc, Theme::STATUS_YELLOW);
        DrawTextW(hdc, L"Sperren", -1, &blockRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
        SetTextColor(hdc, Theme::TEXT_MUTED);
        DrawTextW(hdc, L"Archiv", -1, &archiveRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
        SetTextColor(hdc, Theme::TEXT_SECONDARY);
        DrawTextW(hdc, L"Bearbeiten", -1, &editRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
        SetTextColor(hdc, booked ? Theme::TEXT_MUTED : Theme::TEXT_WHITE);
        DrawTextW(hdc, g_State.rooms[i].blocked ? L"Gesperrt" : (booked ? L"Verwalten" : L"Buchen"), -1, &btnRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);

        y += 94;
    }
}

void RenderTasks(HDC hdc, const RECT& mainRc) {
    int x = mainRc.left + 30;
    int y = mainRc.top + 24;

    SelectObject(hdc, g_Fonts.hSection);
    SetTextColor(hdc, Theme::TEXT_WHITE);
    RECT listHeader = { x, y, x + 400, y + 24 };
    DrawTextW(hdc, L"Meine Vorg\u00e4nge (Klick: offen/erledigt)", -1, &listHeader, DT_SINGLELINE);
    RECT createRc = { mainRc.right - 170, y - 2, mainRc.right - 30, y + 32 };
    RECT projectRc = { mainRc.right - 290, y - 2, mainRc.right - 180, y + 32 };
    RECT approvalsRc = { mainRc.right - 410, y - 2, mainRc.right - 300, y + 32 };
    DrawRoundedRect(hdc, createRc, Theme::ACCENT, Theme::ACCENT, 7);
    DrawRoundedRect(hdc, projectRc, Theme::BG_DARK, Theme::BORDER, 7);
    DrawRoundedRect(hdc, approvalsRc, Theme::BG_DARK, Theme::BORDER, 7);
    SelectObject(hdc, g_Fonts.hBodyBold); SetTextColor(hdc, Theme::TEXT_WHITE);
    DrawTextW(hdc, L"+ Aufgabe anlegen", -1, &createRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
    SetTextColor(hdc, Theme::TEXT_SECONDARY); DrawTextW(hdc, L"+ Projekt", -1, &projectRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
    DrawTextW(hdc, L"Freigaben", -1, &approvalsRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);

    y += 48;
    if (g_Database) for (const EnterpriseProject& project : g_Database->projects()) {
        RECT projectCard = { x, y, mainRc.right - 30, y + 42 };
        DrawRoundedRect(hdc, projectCard, Theme::BG_DARK, Theme::BORDER, 7);
        SelectObject(hdc, g_Fonts.hBodyBold); SetTextColor(hdc, Theme::TEXT_WHITE);
        RECT projectName = { projectCard.left + 16, projectCard.top, projectCard.right - 340, projectCard.bottom };
        DrawTextW(hdc, project.name.c_str(), -1, &projectName, DT_SINGLELINE | DT_VCENTER);
        const std::wstring status = project.status == L"active" ? L"Aktiv" : project.status == L"planned" ? L"Geplant" : project.status == L"on_hold" ? L"Pausiert" : project.status == L"completed" ? L"Abgeschlossen" : L"Storniert";
        DrawBadge(hdc, projectCard.right - 330, projectCard.top + 11, project.department, Theme::BG_CARD, Theme::TEXT_SECONDARY);
        DrawBadge(hdc, projectCard.right - 205, projectCard.top + 11, status, Theme::BG_CARD, project.status == L"active" ? Theme::STATUS_GREEN : Theme::TEXT_MUTED);
        RECT editRc = { projectCard.right - 125, projectCard.top + 7, projectCard.right - 68, projectCard.bottom - 7 };
        RECT archiveRc = { projectCard.right - 61, projectCard.top + 7, projectCard.right - 7, projectCard.bottom - 7 };
        DrawRoundedRect(hdc, editRc, Theme::BG_CARD, Theme::BORDER, 5); DrawRoundedRect(hdc, archiveRc, Theme::BG_CARD, Theme::BORDER, 5);
        SelectObject(hdc, g_Fonts.hSmall); SetTextColor(hdc, Theme::TEXT_SECONDARY);
        DrawTextW(hdc, L"Bearbeiten", -1, &editRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
        DrawTextW(hdc, L"Archiv", -1, &archiveRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
        y += 48;
    }
    for (size_t i = 0; i < g_State.tasks.size(); ++i) {
        RECT taskCard = { x, y, mainRc.right - 30, y + 54 };
        DrawRoundedRect(hdc, taskCard, Theme::BG_CARD, Theme::BORDER, 8);

        RECT checkRc = { taskCard.left + 16, taskCard.top + 15, taskCard.left + 40, taskCard.top + 39 };
        DrawRoundedRect(hdc, checkRc, g_State.tasks[i].done ? Theme::ACCENT : Theme::BG_DARK, Theme::BORDER, 4);

        if (g_State.tasks[i].done) {
            SelectObject(hdc, g_Fonts.hBodyBold);
            SetTextColor(hdc, Theme::TEXT_WHITE);
            DrawTextW(hdc, L"\u2714", -1, &checkRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
        }

        SelectObject(hdc, g_Fonts.hBody);
        SetTextColor(hdc, g_State.tasks[i].done ? Theme::TEXT_MUTED : Theme::TEXT_WHITE);
        RECT textRc = { taskCard.left + 54, taskCard.top, taskCard.right - 545, taskCard.bottom };
        const std::wstring taskLabel = g_State.tasks[i].due.empty() ? g_State.tasks[i].text : g_State.tasks[i].text + L"  •  fällig " + g_State.tasks[i].due;
        DrawTextW(hdc, taskLabel.c_str(), -1, &textRc, DT_SINGLELINE | DT_VCENTER | DT_WORD_ELLIPSIS);

        DrawBadge(hdc, taskCard.right - 210, taskCard.top + 16, g_State.tasks[i].department, Theme::BG_DARK, Theme::TEXT_SECONDARY);

        COLORREF prioBg = (g_State.tasks[i].priority == L"Hoch") ? RGB(70, 20, 25) : Theme::BG_DARK;
        COLORREF prioFg = (g_State.tasks[i].priority == L"Hoch") ? Theme::STATUS_RED : Theme::TEXT_MUTED;
        DrawBadge(hdc, taskCard.right - 100, taskCard.top + 16, g_State.tasks[i].priority, prioBg, prioFg);
        RECT commentsRc = { taskCard.right - 540, taskCard.top + 13, taskCard.right - 465, taskCard.bottom - 13 };
        RECT editRc = { taskCard.right - 455, taskCard.top + 13, taskCard.right - 380, taskCard.bottom - 13 };
        RECT approvalRc = { taskCard.right - 370, taskCard.top + 13, taskCard.right - 295, taskCard.bottom - 13 };
        DrawRoundedRect(hdc, commentsRc, Theme::BG_DARK, Theme::BORDER, 6); DrawRoundedRect(hdc, editRc, Theme::BG_DARK, Theme::BORDER, 6);
        DrawRoundedRect(hdc, approvalRc, Theme::BG_DARK, Theme::BORDER, 6);
        SelectObject(hdc, g_Fonts.hSmall); SetTextColor(hdc, Theme::TEXT_SECONDARY);
        DrawTextW(hdc, L"Verlauf", -1, &commentsRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE); DrawTextW(hdc, L"Edit", -1, &editRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
        SetTextColor(hdc, Theme::ACCENT);
        DrawTextW(hdc, L"Freigabe", -1, &approvalRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
        RECT archiveRc = { taskCard.right - 285, taskCard.top + 13, taskCard.right - 220, taskCard.bottom - 13 };
        DrawRoundedRect(hdc, archiveRc, Theme::BG_DARK, Theme::BORDER, 6);
        SelectObject(hdc, g_Fonts.hSmall); SetTextColor(hdc, Theme::TEXT_MUTED);
        DrawTextW(hdc, L"Archiv", -1, &archiveRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);

        y += 62;
    }
}

void RenderTeam(HDC hdc, const RECT& mainRc) {
    int x = mainRc.left + 30;
    int y = mainRc.top + 24;

    RECT leaveRc = { mainRc.right - 470, y - 2, mainRc.right - 340, y + 32 };
    RECT organizationRc = { mainRc.right - 330, y - 2, mainRc.right - 200, y + 32 };
    RECT createRc = { mainRc.right - 190, y - 2, mainRc.right - 30, y + 32 };
    DrawRoundedRect(hdc, leaveRc, Theme::BG_DARK, Theme::BORDER, 7); DrawRoundedRect(hdc, organizationRc, Theme::BG_DARK, Theme::BORDER, 7);
    DrawRoundedRect(hdc, createRc, Theme::ACCENT, Theme::ACCENT, 7);
    SelectObject(hdc, g_Fonts.hBodyBold); SetTextColor(hdc, Theme::TEXT_SECONDARY);
    DrawTextW(hdc, L"Abwesenheiten", -1, &leaveRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE); DrawTextW(hdc, L"Organisation", -1, &organizationRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
    SetTextColor(hdc, Theme::TEXT_WHITE);
    DrawTextW(hdc, L"+ Mitarbeiter anlegen", -1, &createRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
    y += 48;

    for (size_t i = 0; i < g_State.team.size(); ++i) {
        RECT memberCard = { x, y, mainRc.right - 30, y + 64 };
        DrawRoundedRect(hdc, memberCard, Theme::BG_CARD, Theme::BORDER, 8);

        SelectObject(hdc, g_Fonts.hBodyBold);
        SetTextColor(hdc, Theme::TEXT_WHITE);
        RECT nameRc = { memberCard.left + 24, memberCard.top + 12, memberCard.left + 300, memberCard.top + 32 };
        DrawTextW(hdc, g_State.team[i].name.c_str(), -1, &nameRc, DT_SINGLELINE);

        SelectObject(hdc, g_Fonts.hSmall);
        SetTextColor(hdc, Theme::TEXT_MUTED);
        RECT roleRc = { memberCard.left + 24, memberCard.top + 34, memberCard.left + 300, memberCard.top + 54 };
        DrawTextW(hdc, g_State.team[i].role.c_str(), -1, &roleRc, DT_SINGLELINE);

        DrawBadge(hdc, memberCard.right - 220, memberCard.top + 20, g_State.team[i].status, Theme::BG_DARK, g_State.team[i].statusColor);
        RECT editRc = { memberCard.right - 135, memberCard.top + 17, memberCard.right - 75, memberCard.bottom - 17 };
        RECT archiveRc = { memberCard.right - 68, memberCard.top + 17, memberCard.right - 12, memberCard.bottom - 17 };
        DrawRoundedRect(hdc, editRc, Theme::BG_DARK, Theme::BORDER, 6); DrawRoundedRect(hdc, archiveRc, Theme::BG_DARK, Theme::BORDER, 6);
        SelectObject(hdc, g_Fonts.hSmall); SetTextColor(hdc, Theme::TEXT_SECONDARY);
        DrawTextW(hdc, L"Edit", -1, &editRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE); SetTextColor(hdc, Theme::TEXT_MUTED);
        DrawTextW(hdc, L"Archiv", -1, &archiveRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);

        y += 72;
    }
}

void RenderHelpdesk(HDC hdc, const RECT& mainRc) {
    int x = mainRc.left + 30;
    int y = mainRc.top + 24;

    SelectObject(hdc, g_Fonts.hSection);
    SetTextColor(hdc, Theme::TEXT_WHITE);
    RECT tHeader = { x, y, x + 400, y + 24 };
    DrawTextW(hdc, L"Facility & IT Ticket-Verlauf", -1, &tHeader, DT_SINGLELINE);
    RECT assetsRc = { mainRc.right - 290, y - 2, mainRc.right - 180, y + 32 };
    RECT createRc = { mainRc.right - 170, y - 2, mainRc.right - 30, y + 32 };
    DrawRoundedRect(hdc, assetsRc, Theme::BG_DARK, Theme::BORDER, 7);
    DrawRoundedRect(hdc, createRc, Theme::ACCENT, Theme::ACCENT, 7);
    SelectObject(hdc, g_Fonts.hBodyBold); SetTextColor(hdc, Theme::TEXT_SECONDARY); DrawTextW(hdc, L"Facility-Assets", -1, &assetsRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE); SetTextColor(hdc, Theme::TEXT_WHITE);
    DrawTextW(hdc, L"+ Ticket anlegen", -1, &createRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);

    y += 48;
    for (size_t i = 0; i < g_State.tickets.size(); ++i) {
        RECT ticketCard = { x, y, mainRc.right - 30, y + 68 };
        DrawRoundedRect(hdc, ticketCard, Theme::BG_CARD, Theme::BORDER, 8);

        SelectObject(hdc, g_Fonts.hBodyBold);
        SetTextColor(hdc, Theme::TEXT_WHITE);
        RECT tTitle = { ticketCard.left + 20, ticketCard.top + 14, ticketCard.right - 380, ticketCard.top + 34 };
        DrawTextW(hdc, g_State.tickets[i].title.c_str(), -1, &tTitle, DT_SINGLELINE);

        SelectObject(hdc, g_Fonts.hSmall);
        SetTextColor(hdc, Theme::TEXT_MUTED);
        RECT tTime = { ticketCard.left + 20, ticketCard.top + 38, ticketCard.right - 380, ticketCard.top + 56 };
        DrawTextW(hdc, g_State.tickets[i].timestamp.c_str(), -1, &tTime, DT_SINGLELINE);

        COLORREF stateColor = (g_State.tickets[i].state == L"Offen") ? Theme::STATUS_YELLOW :
                              (g_State.tickets[i].state == L"In Bearbeitung" ? Theme::ACCENT : Theme::STATUS_GREEN);

        DrawBadge(hdc, ticketCard.right - 350, ticketCard.top + 22, g_State.tickets[i].state, Theme::BG_DARK, stateColor);
        RECT commentsRc = { ticketCard.right - 245, ticketCard.top + 20, ticketCard.right - 160, ticketCard.bottom - 20 };
        RECT editRc = { ticketCard.right - 153, ticketCard.top + 20, ticketCard.right - 88, ticketCard.bottom - 20 };
        RECT archiveRc = { ticketCard.right - 78, ticketCard.top + 20, ticketCard.right - 16, ticketCard.bottom - 20 };
        DrawRoundedRect(hdc, commentsRc, Theme::BG_DARK, Theme::BORDER, 6); DrawRoundedRect(hdc, editRc, Theme::BG_DARK, Theme::BORDER, 6);
        DrawRoundedRect(hdc, archiveRc, Theme::BG_DARK, Theme::BORDER, 6);
        SelectObject(hdc, g_Fonts.hSmall); SetTextColor(hdc, Theme::TEXT_SECONDARY);
        DrawTextW(hdc, L"Verlauf", -1, &commentsRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE); DrawTextW(hdc, L"Edit", -1, &editRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);
        SetTextColor(hdc, Theme::TEXT_MUTED);
        DrawTextW(hdc, L"Archiv", -1, &archiveRc, DT_CENTER | DT_VCENTER | DT_SINGLELINE);

        y += 76;
    }
}

void RenderAdministration(HDC hdc, const RECT& mainRc) {
    const int x = mainRc.left + 30; int y = mainRc.top + 24;
    SelectObject(hdc, g_Fonts.hSection); SetTextColor(hdc, Theme::TEXT_WHITE);
    RECT title = {x,y,x+440,y+24}; DrawTextW(hdc,L"Betriebsverwaltung & revisionssicheres Audit",-1,&title,DT_SINGLELINE);
    RECT accounts = {mainRc.right-530,y-2,mainRc.right-410,y+32}; RECT roles = {mainRc.right-400,y-2,mainRc.right-280,y+32}; RECT backup = {mainRc.right-270,y-2,mainRc.right-150,y+32}; RECT restore = {mainRc.right-140,y-2,mainRc.right-30,y+32};
    DrawRoundedRect(hdc,accounts,Theme::BG_DARK,Theme::BORDER,7); DrawRoundedRect(hdc,roles,Theme::BG_DARK,Theme::BORDER,7); DrawRoundedRect(hdc,backup,Theme::ACCENT,Theme::ACCENT,7); DrawRoundedRect(hdc,restore,Theme::BG_DARK,Theme::BORDER,7);
    SelectObject(hdc,g_Fonts.hBodyBold); SetTextColor(hdc,Theme::TEXT_SECONDARY); DrawTextW(hdc,L"Konten",-1,&accounts,DT_CENTER|DT_VCENTER|DT_SINGLELINE); DrawTextW(hdc,L"Rollen",-1,&roles,DT_CENTER|DT_VCENTER|DT_SINGLELINE);
    SetTextColor(hdc,Theme::TEXT_WHITE); DrawTextW(hdc,L"Sicherung",-1,&backup,DT_CENTER|DT_VCENTER|DT_SINGLELINE);
    SetTextColor(hdc,Theme::TEXT_SECONDARY); DrawTextW(hdc,L"Wiederherstellen",-1,&restore,DT_CENTER|DT_VCENTER|DT_SINGLELINE);
    y += 54;
    RECT info = {x,y,mainRc.right-30,y+54}; DrawRoundedRect(hdc,info,Theme::BG_CARD,Theme::BORDER,8);
    SelectObject(hdc,g_Fonts.hSmall); SetTextColor(hdc,Theme::TEXT_MUTED); RECT ir={info.left+16,info.top+10,info.right-16,info.bottom-10};
    const std::wstring serviceState = L"Synchronisation: " + g_SyncService.status() + L"  •  Datenbasis: KR_BUILD\\aura-enterprise.db";
    DrawTextW(hdc,serviceState.c_str(),-1,&ir,DT_SINGLELINE|DT_VCENTER);
    y += 74; SelectObject(hdc,g_Fonts.hSection); SetTextColor(hdc,Theme::TEXT_WHITE); RECT auditTitle={x,y,x+300,y+22}; DrawTextW(hdc,L"Audit-Protokoll",-1,&auditTitle,DT_SINGLELINE); y+=32;
    const std::vector<std::wstring> audit = g_Database ? g_Database->auditLog(12) : std::vector<std::wstring>{};
    for (const std::wstring& entry : audit) { RECT row={x,y,mainRc.right-30,y+32}; DrawRoundedRect(hdc,row,Theme::BG_CARD,Theme::BORDER,6); SelectObject(hdc,g_Fonts.hSmall); SetTextColor(hdc,Theme::TEXT_SECONDARY); RECT text={row.left+12,row.top,row.right-12,row.bottom}; DrawTextW(hdc,entry.c_str(),-1,&text,DT_SINGLELINE|DT_VCENTER); y+=38; }
}

// ============================================================================
// 7. MAIN WINDOW PROCEDURE & EVENT ROUTING
// ============================================================================
LRESULT CALLBACK WndProc(HWND hwnd, UINT msg, WPARAM wParam, LPARAM lParam) {
    switch (msg) {
    case WM_CREATE:
        g_Fonts.Init();
        EnterpriseStore::Load();
        SetTimer(hwnd, 1, 1000, nullptr);
        return 0;

    case WM_ERASEBKGND:
        return 1;

    case WM_TIMER:
        { static ULONGLONG lastSync = 0; const ULONGLONG now = GetTickCount64();
          if (now - lastSync >= 15000) { lastSync = now; if (g_SyncService.synchronize()) { RefreshRoomsFromDatabase(); RefreshTasksFromDatabase(); RefreshTicketsFromDatabase(); RefreshTeamFromDatabase(); } }
        }
        InvalidateRect(hwnd, nullptr, FALSE);
        return 0;

    case WM_MOUSEMOVE: {
        int mouseX = GET_X_LPARAM(lParam);
        int mouseY = GET_Y_LPARAM(lParam);

        int oldNav = g_State.hoveredNavIndex;
        g_State.hoveredNavIndex = -1;
        if (mouseX >= 14 && mouseX <= 226 && mouseY >= 90 && mouseY < 90 + (6 * 44)) {
            g_State.hoveredNavIndex = (mouseY - 90) / 44;
        }

        int oldAct = g_State.hoveredActionIndex;
        g_State.hoveredActionIndex = -1;
        if (g_State.activeTab == NavTab::DASHBOARD) {
            RECT clientRc;
            GetClientRect(hwnd, &clientRc);
            int splitY = 24 + 104 + 20;
            int actY = splitY + 180;
            int rightLeft = 240 + 30 + ((clientRc.right - 270 - 46) / 2) + 16;
            if (mouseX >= rightLeft + 16 && mouseX <= clientRc.right - 46 && mouseY >= actY && mouseY < actY + (3 * 52)) {
                g_State.hoveredActionIndex = (mouseY - actY) / 52;
            }
        }

        if (oldNav != g_State.hoveredNavIndex || oldAct != g_State.hoveredActionIndex) {
            InvalidateRect(hwnd, nullptr, FALSE);
        }

        TRACKMOUSEEVENT tme = { sizeof(tme), TME_LEAVE, hwnd, 0 };
        TrackMouseEvent(&tme);
        return 0;
    }

    case WM_MOUSELEAVE:
        g_State.hoveredNavIndex = -1;
        g_State.hoveredActionIndex = -1;
        InvalidateRect(hwnd, nullptr, FALSE);
        return 0;

    case WM_LBUTTONDOWN: {
        int mouseX = GET_X_LPARAM(lParam);
        int mouseY = GET_Y_LPARAM(lParam);
        RECT clientRc;
        GetClientRect(hwnd, &clientRc);

        if (mouseX >= 14 && mouseX <= 226 && mouseY >= clientRc.bottom - 74 && mouseY <= clientRc.bottom - 16) {
            if (!RequireEnterprisePermission(hwnd, L"admin.manage")) return 0;
            ShowSessionPicker(hwnd);
            return 0;
        }

        if (mouseX >= 14 && mouseX <= 226 && mouseY >= 90 && mouseY < 90 + (6 * 44)) {
            int clickedTab = (mouseY - 90) / 44;
            if (clickedTab >= 0 && clickedTab < 6) {
                g_State.activeTab = static_cast<NavTab>(clickedTab);
                InvalidateRect(hwnd, nullptr, FALSE);
                return 0;
            }
        }

        if (mouseX >= clientRc.right - 230 && mouseX <= clientRc.right - 30 && mouseY >= 18 && mouseY <= 52) {
            int nextStatus = (static_cast<int>(g_State.userStatus) + 1) % 4;
            g_State.userStatus = static_cast<PresenceStatus>(nextStatus);
            const wchar_t* presenceCodes[] = { L"office", L"remote", L"meeting", L"away" };
            if (g_Database && !g_Database->updatePresence(g_CurrentEmployeeId, presenceCodes[nextStatus], L"Ralf Krümmel"))
                MessageBoxW(hwnd, g_Database->error().c_str(), L"Präsenzstatus konnte nicht gespeichert werden", MB_ICONERROR);
            else if (g_Database) for(const EnterpriseEmployee& employee:g_Database->employees(true)) if(employee.id==g_CurrentEmployeeId) { PublishEmployeeChange(employee,"employee.presence.changed"); break; }
            RefreshTeamFromDatabase();
            EnterpriseStore::Save();
            EnterpriseStore::Audit(L"presence.changed", L"personal-status");
            InvalidateRect(hwnd, nullptr, FALSE);
            return 0;
        }

        if (g_State.activeTab == NavTab::DASHBOARD) {
            const int splitY = 24 + 104 + 20;
            const int actionTop = splitY + 180;
            const int rightLeft = 240 + 30 + ((clientRc.right - 270 - 46) / 2) + 16;
            if (mouseX >= rightLeft + 16 && mouseX <= clientRc.right - 46 && mouseY >= actionTop && mouseY < actionTop + (3 * 52)) {
                const int action = (mouseY - actionTop) / 52;
                if (action == 0) {
                    ShowLeaveEditor(hwnd);
                } else {
                    ShowTicketEditor(hwnd);
                }
                return 0;
            }
        }

        if (g_State.activeTab == NavTab::TASKS) {
            const int createTop = 70 + 22;
            if (mouseX >= clientRc.right - 410 && mouseX <= clientRc.right - 300 && mouseY >= createTop && mouseY <= createTop + 34) {
                ShowApprovalManager(hwnd);
                return 0;
            }
            if (mouseX >= clientRc.right - 290 && mouseX <= clientRc.right - 180 && mouseY >= createTop && mouseY <= createTop + 34) { if(!RequireEnterprisePermission(hwnd,L"tasks.manage"))return 0; ShowProjectEditor(hwnd); return 0; }
            if (mouseX >= clientRc.right - 170 && mouseX <= clientRc.right - 30 && mouseY >= createTop && mouseY <= createTop + 34) { if(!RequireEnterprisePermission(hwnd,L"tasks.manage"))return 0; ShowTaskEditor(hwnd); return 0; }
            const std::vector<EnterpriseProject> projects = g_Database ? g_Database->projects() : std::vector<EnterpriseProject>{};
            int projectY = 70 + 24 + 48;
            for (const EnterpriseProject& project : projects) {
                if (mouseX >= clientRc.right - 155 && mouseX <= clientRc.right - 98 && mouseY >= projectY + 7 && mouseY <= projectY + 35) { if(!RequireEnterprisePermission(hwnd,L"tasks.manage"))return 0; ShowProjectEditor(hwnd, &project); return 0; }
                if (mouseX >= clientRc.right - 91 && mouseX <= clientRc.right - 37 && mouseY >= projectY + 7 && mouseY <= projectY + 35 && g_Database) {
                    if(!RequireEnterprisePermission(hwnd,L"tasks.manage"))return 0;
                    if (MessageBoxW(hwnd, L"Dieses Projekt archivieren? Zugeordnete Aufgaben bleiben historisch erhalten.", L"Projekt archivieren", MB_YESNO | MB_ICONWARNING) == IDYES) { if (!g_Database->archiveProject(project.id, L"Ralf Krümmel")) MessageBoxW(hwnd, g_Database->error().c_str(), L"Projekt", MB_ICONERROR); else { EnterpriseProject archived=project; archived.active=false; PublishProjectChange(archived,"project.archived"); } }
                    InvalidateRect(hwnd, nullptr, FALSE); return 0;
                }
                if (mouseX >= 270 && mouseX <= clientRc.right - 160 && mouseY >= projectY && mouseY <= projectY + 42 && g_Database) {
                    if(!RequireEnterprisePermission(hwnd,L"tasks.manage"))return 0;
                    EnterpriseProject updated = project;
                    updated.status = project.status == L"planned" ? L"active" : project.status == L"active" ? L"on_hold" : project.status == L"on_hold" ? L"completed" : L"planned";
                    if (!g_Database->updateProject(updated, L"Ralf Krümmel")) MessageBoxW(hwnd, g_Database->error().c_str(), L"Projektstatus", MB_ICONERROR); else PublishProjectChange(updated,"project.updated");
                    InvalidateRect(hwnd, nullptr, FALSE); return 0;
                }
                projectY += 48;
            }
            int y = projectY;
            for (size_t i = 0; i < g_State.tasks.size(); ++i) {
                if (mouseX >= clientRc.right - 540 && mouseX <= clientRc.right - 465 && mouseY >= y + 13 && mouseY <= y + 41 && g_Database) {
                    if(!RequireEnterprisePermission(hwnd,L"tasks.manage"))return 0;
                    for (const EnterpriseTask& task : g_Database->tasks()) if (task.id == g_State.tasks[i].id) { ShowTaskComments(hwnd, task); return 0; }
                    return 0;
                }
                if (mouseX >= clientRc.right - 455 && mouseX <= clientRc.right - 380 && mouseY >= y + 13 && mouseY <= y + 41 && g_Database) {
                    if(!RequireEnterprisePermission(hwnd,L"tasks.manage"))return 0;
                    for (const EnterpriseTask& task : g_Database->tasks()) if (task.id == g_State.tasks[i].id) { ShowTaskEditor(hwnd, &task); return 0; }
                    return 0;
                }
                if (mouseX >= clientRc.right - 285 && mouseX <= clientRc.right - 220 && mouseY >= y + 13 && mouseY <= y + 41) {
                    if(!RequireEnterprisePermission(hwnd,L"tasks.manage"))return 0;
                    if (g_Database && MessageBoxW(hwnd, L"Diesen Vorgang archivieren?", L"Aufgabe archivieren", MB_YESNO | MB_ICONWARNING) == IDYES) { if(g_Database->archiveTask(g_State.tasks[i].id, L"Ralf Krümmel")){ for(const EnterpriseTask& task:g_Database->tasks(true))if(task.id==g_State.tasks[i].id){PublishTaskChange(task,"task.archived");break;} } RefreshTasksFromDatabase(); InvalidateRect(hwnd, nullptr, FALSE); }
                    return 0;
                }
                if (mouseX >= clientRc.right - 370 && mouseX <= clientRc.right - 295 && mouseY >= y + 13 && mouseY <= y + 41 && g_Database) {
                    if(!RequireEnterprisePermission(hwnd,L"tasks.manage"))return 0;
                    int64_t approval = 0;
                    int64_t approver = 0; for (const EnterpriseEmployee& employee : g_Database->employees()) if (employee.id != g_CurrentEmployeeId) { approver = employee.id; break; }
                    if (!g_Database->createTaskApproval(g_State.tasks[i].id, g_CurrentEmployeeId, approver, approval, L"Ralf Krümmel")) MessageBoxW(hwnd, g_Database->error().c_str(), L"Freigabe", MB_ICONERROR);
                    else { for(const EnterpriseTaskApproval& current:g_Database->taskApprovals(0,true))if(current.id==approval){PublishTaskApprovalChange(current,"task.approval.requested");break;} MessageBoxW(hwnd, L"Freigabeanforderung wurde angelegt.", L"Workflow", MB_ICONINFORMATION); }
                    return 0;
                }
                if (mouseX >= 270 && mouseX <= clientRc.right - 30 && mouseY >= y && mouseY <= y + 54) {
                    if(!RequireEnterprisePermission(hwnd,L"tasks.manage"))return 0;
                    const bool nextDone = !g_State.tasks[i].done;
                    if (!RequireWorkflowTransition(hwnd, "task", g_State.tasks[i].done ? "done" : "open", nextDone ? "done" : "open")) return 0;
                    g_State.tasks[i].done = nextDone;
                    if (g_Database && !g_Database->updateTaskStatus(g_State.tasks[i].id, nextDone ? L"done" : L"open", L"Ralf Krümmel"))
                        MessageBoxW(hwnd, g_Database->error().c_str(), L"Aufgabe konnte nicht geändert werden", MB_ICONERROR);
                    else if(g_Database) for(const EnterpriseTask& task:g_Database->tasks())if(task.id==g_State.tasks[i].id){PublishTaskChange(task,"task.status.changed");break;}
                    RefreshTasksFromDatabase();
                    InvalidateRect(hwnd, nullptr, FALSE);
                    return 0;
                }
                y += 62;
            }
        }

        if (g_State.activeTab == NavTab::ROOMS) {
            const int createTop = 70 + 22;
            if (mouseX >= clientRc.right - 530 && mouseX <= clientRc.right - 420 && mouseY >= createTop && mouseY <= createTop + 34) {
                ShowRoomSearch(hwnd);
                return 0;
            }
            if (mouseX >= clientRc.right - 410 && mouseX <= clientRc.right - 300 && mouseY >= createTop && mouseY <= createTop + 34) {
                wchar_t selectedFile[MAX_PATH] = {};
                OPENFILENAMEW chooser = {}; chooser.lStructSize = sizeof(chooser); chooser.hwndOwner = hwnd; chooser.lpstrFile = selectedFile; chooser.nMaxFile = MAX_PATH; chooser.lpstrFilter = L"CSV-Dateien (*.csv)\0*.csv\0Alle Dateien\0*.*\0"; chooser.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST;
                if (GetOpenFileNameW(&chooser)) { int imported = 0; if (!g_Database || !g_Database->importRoomsCsv(selectedFile, imported, L"Ralf Krümmel")) MessageBoxW(hwnd, g_Database ? g_Database->error().c_str() : L"Datenbank nicht verfügbar", L"Import fehlgeschlagen", MB_ICONERROR); else { RefreshRoomsFromDatabase(); InvalidateRect(hwnd, nullptr, FALSE); MessageBoxW(hwnd, (L"Importiert: " + std::to_wstring(imported) + L" Räume").c_str(), L"Raumimport", MB_ICONINFORMATION); } }
                return 0;
            }
            if (mouseX >= clientRc.right - 290 && mouseX <= clientRc.right - 180 && mouseY >= createTop && mouseY <= createTop + 34) {
                if (!RequireEnterprisePermission(hwnd, L"rooms.manage")) return 0;
                const std::filesystem::path file = FindWorkspaceRoot() / L"KR_BUILD" / L"rooms-export.csv";
                if (!g_Database || !g_Database->exportRoomsCsv(file.wstring(), L"Ralf Krümmel")) MessageBoxW(hwnd, g_Database ? g_Database->error().c_str() : L"Datenbank nicht verfügbar", L"Export fehlgeschlagen", MB_ICONERROR);
                else MessageBoxW(hwnd, file.c_str(), L"Raumexport erstellt", MB_ICONINFORMATION);
                return 0;
            }
            if (mouseX >= clientRc.right - 170 && mouseX <= clientRc.right - 30 && mouseY >= createTop && mouseY <= createTop + 34) {
                if (!HasEnterprisePermission(L"rooms.manage")) { MessageBoxW(hwnd, L"Keine Berechtigung zur Raumverwaltung.", L"Zugriff verweigert", MB_ICONWARNING); return 0; }
                ShowRoomEditor(hwnd);
                return 0;
            }
            int y = 70 + 24 + 48;
            for (size_t i = 0; i < g_State.rooms.size(); ++i) {
                int blockLeft = clientRc.right - 445;
                int blockRight = clientRc.right - 350;
                int archiveLeft = clientRc.right - 340;
                int archiveRight = clientRc.right - 245;
                int editLeft = clientRc.right - 235;
                int editRight = clientRc.right - 130;
                int btnLeft = clientRc.right - 120;
                int btnRight = clientRc.right - 20;
                int btnTop = y + 22;
                int btnBottom = y + 80 - 22;

                if (mouseX >= blockLeft && mouseX <= blockRight && mouseY >= btnTop && mouseY <= btnBottom) {
                    if (!HasEnterprisePermission(L"rooms.manage")) { MessageBoxW(hwnd, L"Keine Berechtigung zur Raumverwaltung.", L"Zugriff verweigert", MB_ICONWARNING); return 0; }
                    ShowBlockEditor(hwnd, g_State.rooms[i]); return 0;
                }

                if (mouseX >= archiveLeft && mouseX <= archiveRight && mouseY >= btnTop && mouseY <= btnBottom) {
                    if (!HasEnterprisePermission(L"rooms.manage")) { MessageBoxW(hwnd, L"Keine Berechtigung zur Raumverwaltung.", L"Zugriff verweigert", MB_ICONWARNING); return 0; }
                    if (MessageBoxW(hwnd, L"Diesen Raum archivieren? Bereits vorhandene Buchungen bleiben historisch erhalten.", L"Raum archivieren", MB_YESNO | MB_ICONWARNING) == IDYES && g_Database) {
                        if (g_Database->archiveRoom(g_State.rooms[i].id, L"Ralf Krümmel")) for (const EnterpriseRoom& room : g_Database->rooms(true)) if (room.id == g_State.rooms[i].id) { PublishRoomChange(room, "room.archived"); break; }
                        RefreshRoomsFromDatabase();
                        InvalidateRect(hwnd, nullptr, FALSE);
                    }
                    return 0;
                }

                if (mouseX >= editLeft && mouseX <= editRight && mouseY >= btnTop && mouseY <= btnBottom) {
                    if (!HasEnterprisePermission(L"rooms.manage")) { MessageBoxW(hwnd, L"Keine Berechtigung zur Raumverwaltung.", L"Zugriff verweigert", MB_ICONWARNING); return 0; }
                    ShowRoomEditor(hwnd, &g_State.rooms[i]);
                    return 0;
                }

                if (mouseX >= btnLeft && mouseX <= btnRight && mouseY >= btnTop && mouseY <= btnBottom) {
                    if (!g_Database) return 0;
                    if (g_State.rooms[i].blocked) { MessageBoxW(hwnd, L"Dieser Raum ist wegen einer aktiven Wartungssperre nicht buchbar.", L"Raum gesperrt", MB_ICONWARNING); return 0; }
                    if (g_State.rooms[i].isBooked) {
                        EnterpriseBooking activeBooking;
                        if (g_Database->activeBookingForRoom(g_State.rooms[i].id, std::time(nullptr), activeBooking) && activeBooking.status == L"confirmed") {
                            ShowBookingEditor(hwnd, g_State.rooms[i], &activeBooking);
                            return 0;
                        }
                    } else {
                        ShowBookingEditor(hwnd, g_State.rooms[i]);
                        return 0;
                    }
                    MessageBoxW(hwnd, L"Diese Sperrzeit kann nur über die Raumverwaltung geändert werden.", L"Raum gesperrt", MB_ICONWARNING);
                    RefreshRoomsFromDatabase();
                    InvalidateRect(hwnd, nullptr, FALSE);
                    return 0;
                }
                if (mouseX >= 270 && mouseX < blockLeft && mouseY >= y && mouseY <= y + 80) { ShowRoomHistory(hwnd, g_State.rooms[i]); return 0; }
                y += 94;
            }
        }

        if (g_State.activeTab == NavTab::HELPDESK) {
            const int createTop = 70 + 22;
            if (mouseX >= clientRc.right - 290 && mouseX <= clientRc.right - 180 && mouseY >= createTop && mouseY <= createTop + 34) { if(!RequireEnterprisePermission(hwnd,L"tickets.manage"))return 0; ShowAssetManager(hwnd); return 0; }
            if (mouseX >= clientRc.right - 170 && mouseX <= clientRc.right - 30 && mouseY >= createTop && mouseY <= createTop + 34) { if(!RequireEnterprisePermission(hwnd,L"tickets.manage"))return 0; ShowTicketEditor(hwnd); return 0; }
            int y = 70 + 24 + 48;
            for (size_t i = 0; i < g_State.tickets.size(); ++i) {
                if (mouseX >= clientRc.right - 245 && mouseX <= clientRc.right - 160 && mouseY >= y + 20 && mouseY <= y + 48 && g_Database) {
                    if(!RequireEnterprisePermission(hwnd,L"tickets.manage"))return 0;
                    for (const EnterpriseTicket& ticket : g_Database->tickets()) if (ticket.id == g_State.tickets[i].id) { ShowTicketComments(hwnd, ticket); return 0; }
                    return 0;
                }
                if (mouseX >= clientRc.right - 153 && mouseX <= clientRc.right - 88 && mouseY >= y + 20 && mouseY <= y + 48 && g_Database) {
                    if(!RequireEnterprisePermission(hwnd,L"tickets.manage"))return 0;
                    for (const EnterpriseTicket& ticket : g_Database->tickets()) if (ticket.id == g_State.tickets[i].id) { ShowTicketEditor(hwnd, &ticket); return 0; }
                    return 0;
                }
                if (mouseX >= clientRc.right - 78 && mouseX <= clientRc.right - 16 && mouseY >= y + 20 && mouseY <= y + 48 && g_Database) {
                    if(!RequireEnterprisePermission(hwnd,L"tickets.manage"))return 0;
                    if (MessageBoxW(hwnd, L"Ticket archivieren? Der Verlauf bleibt im Audit erhalten.", L"Ticket archivieren", MB_YESNO | MB_ICONWARNING) == IDYES) { if(g_Database->archiveTicket(g_State.tickets[i].id, L"Ralf Krümmel")){for(const EnterpriseTicket& ticket:g_Database->tickets(true))if(ticket.id==g_State.tickets[i].id){PublishTicketChange(ticket,"ticket.archived");break;}} RefreshTicketsFromDatabase(); InvalidateRect(hwnd, nullptr, FALSE); }
                    return 0;
                }
                if (mouseX >= 270 && mouseX <= clientRc.right - 30 && mouseY >= y && mouseY <= y + 68 && g_Database) {
                    if(!RequireEnterprisePermission(hwnd,L"tickets.manage"))return 0;
                    const char* from = g_State.tickets[i].state == L"Offen" ? "open" : g_State.tickets[i].state == L"In Bearbeitung" ? "in_progress" : g_State.tickets[i].state == L"Gelöst" ? "resolved" : "closed";
                    const char* to = std::string(from) == "open" ? "in_progress" : std::string(from) == "in_progress" ? "resolved" : std::string(from) == "resolved" ? "closed" : "open";
                    if (!RequireWorkflowTransition(hwnd, "ticket", from, to)) return 0;
                    const std::wstring next = std::string(to) == "open" ? L"open" : std::string(to) == "in_progress" ? L"in_progress" : std::string(to) == "resolved" ? L"resolved" : L"closed";
                    if(g_Database->updateTicketStatus(g_State.tickets[i].id, next, L"Ralf Krümmel"))for(const EnterpriseTicket& ticket:g_Database->tickets())if(ticket.id==g_State.tickets[i].id){PublishTicketChange(ticket,"ticket.status.changed");break;} RefreshTicketsFromDatabase(); InvalidateRect(hwnd, nullptr, FALSE); return 0;
                }
                y += 76;
            }
        }

        if (g_State.activeTab == NavTab::TEAM) {
            const int createTop = 70 + 22;
            if (mouseX >= clientRc.right - 470 && mouseX <= clientRc.right - 340 && mouseY >= createTop && mouseY <= createTop + 34) { if(!RequireEnterprisePermission(hwnd,L"employees.manage"))return 0; ShowLeaveManager(hwnd); return 0; }
            if (mouseX >= clientRc.right - 330 && mouseX <= clientRc.right - 200 && mouseY >= createTop && mouseY <= createTop + 34) { if(!RequireEnterprisePermission(hwnd,L"employees.manage"))return 0; ShowOrganizationManager(hwnd); return 0; }
            if (mouseX >= clientRc.right - 190 && mouseX <= clientRc.right - 30 && mouseY >= createTop && mouseY <= createTop + 34) { if(!RequireEnterprisePermission(hwnd,L"employees.manage"))return 0; ShowEmployeeEditor(hwnd); return 0; }
            int y = 70 + 24 + 48;
            for (size_t i = 0; i < g_State.team.size(); ++i) {
                const int editLeft = clientRc.right - 135, editRight = clientRc.right - 75;
                const int archiveLeft = clientRc.right - 68, archiveRight = clientRc.right - 12;
                if (mouseY >= y && mouseY <= y + 64 && mouseX >= editLeft && mouseX <= editRight && g_Database) {
                    if(!RequireEnterprisePermission(hwnd,L"employees.manage"))return 0;
                    for (const EnterpriseEmployee& employee : g_Database->employees()) if (employee.id == g_State.team[i].id) { ShowEmployeeEditor(hwnd, &employee); return 0; }
                    return 0;
                }
                if (mouseY >= y && mouseY <= y + 64 && mouseX >= archiveLeft && mouseX <= archiveRight && g_Database) {
                    if(!RequireEnterprisePermission(hwnd,L"employees.manage"))return 0;
                    if (g_State.team[i].id == g_CurrentEmployeeId) { MessageBoxW(hwnd, L"Der angemeldete Benutzer kann nicht archiviert werden.", L"Mitarbeiterverwaltung", MB_ICONWARNING); return 0; }
                    if (MessageBoxW(hwnd, L"Mitarbeiter archivieren? Historie und Audit bleiben erhalten.", L"Mitarbeiter archivieren", MB_YESNO | MB_ICONWARNING) == IDYES) { if(g_Database->archiveEmployee(g_State.team[i].id, L"Ralf Krümmel")){for(const EnterpriseEmployee& employee:g_Database->employees(true))if(employee.id==g_State.team[i].id){PublishEmployeeChange(employee,"employee.archived");break;}} RefreshTeamFromDatabase(); InvalidateRect(hwnd, nullptr, FALSE); }
                    return 0;
                }
                if (mouseX >= 270 && mouseX <= clientRc.right - 30 && mouseY >= y && mouseY <= y + 64 && g_Database) {
                    const std::wstring current = g_State.team[i].status;
                    const std::wstring next = current == L"Im Büro" ? L"remote" : current == L"Home-Office" ? L"meeting" : current == L"Im Meeting" ? L"away" : L"office";
                    if (!g_Database->updatePresence(g_State.team[i].id, next, L"Ralf Krümmel")) MessageBoxW(hwnd, g_Database->error().c_str(), L"Präsenz konnte nicht geändert werden", MB_ICONERROR); else for(const EnterpriseEmployee& employee:g_Database->employees(true))if(employee.id==g_State.team[i].id){PublishEmployeeChange(employee,"employee.presence.changed");break;}
                    RefreshTeamFromDatabase(); InvalidateRect(hwnd, nullptr, FALSE); return 0;
                }
                y += 72;
            }
        }

        if (g_State.activeTab == NavTab::ADMIN) {
            const int top = 70 + 22;
            if (mouseX >= clientRc.right - 530 && mouseX <= clientRc.right - 410 && mouseY >= top && mouseY <= top + 34) { if(!RequireEnterprisePermission(hwnd,L"admin.manage"))return 0; ShowAccountManager(hwnd); return 0; }
            if (mouseX >= clientRc.right - 400 && mouseX <= clientRc.right - 280 && mouseY >= top && mouseY <= top + 34) { if(!RequireEnterprisePermission(hwnd,L"admin.manage"))return 0; ShowRoleManager(hwnd); return 0; }
            if (mouseX >= clientRc.right - 270 && mouseX <= clientRc.right - 150 && mouseY >= top && mouseY <= top + 34) {
                if(!RequireEnterprisePermission(hwnd,L"admin.manage"))return 0;
                const std::filesystem::path backup = FindWorkspaceRoot() / L"KR_BUILD" / L"backups" / (L"aura-enterprise-" + std::to_wstring(std::time(nullptr)) + L".db");
                std::filesystem::create_directories(backup.parent_path());
                if (!g_Database || !g_Database->backupTo(backup.wstring(), L"Ralf Krümmel")) MessageBoxW(hwnd,g_Database?g_Database->error().c_str():L"Datenbank nicht verfügbar",L"Sicherung fehlgeschlagen",MB_ICONERROR); else MessageBoxW(hwnd,backup.c_str(),L"Sicherung erstellt",MB_ICONINFORMATION);
                return 0;
            }
            if (mouseX >= clientRc.right - 140 && mouseX <= clientRc.right - 30 && mouseY >= top && mouseY <= top + 34) {
                if(!RequireEnterprisePermission(hwnd,L"admin.manage"))return 0;
                wchar_t selectedFile[MAX_PATH] = {}; OPENFILENAMEW chooser={}; chooser.lStructSize=sizeof(chooser); chooser.hwndOwner=hwnd; chooser.lpstrFile=selectedFile; chooser.nMaxFile=MAX_PATH; chooser.lpstrFilter=L"SQLite-Datenbank (*.db)\0*.db\0Alle Dateien\0*.*\0"; chooser.Flags=OFN_FILEMUSTEXIST|OFN_PATHMUSTEXIST;
                if(GetOpenFileNameW(&chooser) && MessageBoxW(hwnd,L"Die aktuelle Datenbasis wird durch die gewählte Sicherung ersetzt. Fortfahren?",L"Wiederherstellen",MB_YESNO|MB_ICONWARNING)==IDYES) { if(!g_Database || !g_Database->restoreFrom(selectedFile,L"Ralf Krümmel"))MessageBoxW(hwnd,g_Database?g_Database->error().c_str():L"Datenbank nicht verfügbar",L"Wiederherstellung fehlgeschlagen",MB_ICONERROR); else {RefreshRoomsFromDatabase();RefreshTasksFromDatabase();RefreshTicketsFromDatabase();RefreshTeamFromDatabase();InvalidateRect(hwnd,nullptr,FALSE);} }
                return 0;
            }
        }

        return 0;
    }

    case WM_PAINT: {
        PAINTSTRUCT ps;
        HDC hdc = BeginPaint(hwnd, &ps);

        RECT clientRc;
        GetClientRect(hwnd, &clientRc);
        int width = clientRc.right - clientRc.left;
        int height = clientRc.bottom - clientRc.top;

        HDC memDC = CreateCompatibleDC(hdc);
        HBITMAP memBitmap = CreateCompatibleBitmap(hdc, width, height);
        HGDIOBJ oldBitmap = SelectObject(memDC, memBitmap);

        HBRUSH hBgBrush = CreateSolidBrush(Theme::BG_DARK);
        FillRect(memDC, &clientRc, hBgBrush);
        DeleteObject(hBgBrush);

        RenderSidebar(memDC, clientRc);
        RenderHeader(memDC, clientRc);

        RECT mainArea = { 240, 70, clientRc.right, clientRc.bottom };
        switch (g_State.activeTab) {
        case NavTab::DASHBOARD: RenderDashboard(memDC, mainArea); break;
        case NavTab::ROOMS:     RenderRooms(memDC, mainArea);     break;
        case NavTab::TASKS:     RenderTasks(memDC, mainArea);     break;
        case NavTab::TEAM:      RenderTeam(memDC, mainArea);      break;
        case NavTab::HELPDESK:  RenderHelpdesk(memDC, mainArea);  break;
        case NavTab::ADMIN:     RenderAdministration(memDC, mainArea); break;
        }

        BitBlt(hdc, 0, 0, width, height, memDC, 0, 0, SRCCOPY);

        SelectObject(memDC, oldBitmap);
        DeleteObject(memBitmap);
        DeleteDC(memDC);

        EndPaint(hwnd, &ps);
        return 0;
    }

    case WM_DESTROY:
        g_Fonts.Cleanup();
        PostQuitMessage(0);
        return 0;
    }
    return DefWindowProcW(hwnd, msg, wParam, lParam);
}

// ============================================================================
// 8. UNIVERSELLE ENTRY-POINT IMPLEMENTIERUNG
// ============================================================================
int AppMain(HINSTANCE hInstance, int nCmdShow) {
    if (!DynGdi::Init()) {
        MessageBoxW(nullptr, L"Kritischer Systemfehler: GDI32-Schnittstelle konnte nicht initialisiert werden.", L"Aura Workspace", MB_ICONERROR);
        return 1;
    }

    SetProcessDPIAware();
    InitializeEnterpriseData();

    const wchar_t CLASS_NAME[] = L"AuraWorkspaceWindowClass";

    WNDCLASSEXW wc = {};
    wc.cbSize        = sizeof(WNDCLASSEXW);
    wc.style         = CS_HREDRAW | CS_VREDRAW;
    wc.lpfnWndProc   = WndProc;
    wc.hInstance     = hInstance;
    wc.hCursor       = LoadCursor(nullptr, IDC_ARROW);
    wc.hbrBackground = nullptr;
    wc.lpszClassName = CLASS_NAME;

    RegisterClassExW(&wc);

    WNDCLASSEXW roomEditorClass = {};
    roomEditorClass.cbSize = sizeof(roomEditorClass);
    roomEditorClass.lpfnWndProc = RoomEditorProc;
    roomEditorClass.hInstance = hInstance;
    roomEditorClass.hCursor = LoadCursor(nullptr, IDC_ARROW);
    roomEditorClass.hbrBackground = reinterpret_cast<HBRUSH>(COLOR_WINDOW + 1);
    roomEditorClass.lpszClassName = L"AuraRoomEditor";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = RoomSearchProc;
    roomEditorClass.lpszClassName = L"AuraRoomSearch";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = TaskEditorProc;
    roomEditorClass.lpszClassName = L"AuraTaskEditor";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = TaskDependencyProc;
    roomEditorClass.lpszClassName = L"AuraTaskDependencies";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = SubtaskManagerProc;
    roomEditorClass.lpszClassName = L"AuraSubtaskManager";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = TicketEditorProc;
    roomEditorClass.lpszClassName = L"AuraTicketEditor";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = TicketCommentProc;
    roomEditorClass.lpszClassName = L"AuraTicketComments";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = TaskCommentProc;
    roomEditorClass.lpszClassName = L"AuraTaskComments";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = AttachmentManagerProc;
    roomEditorClass.lpszClassName = L"AuraAttachmentManager";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = BookingEditorProc;
    roomEditorClass.lpszClassName = L"AuraBookingEditor";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = BlockEditorProc;
    roomEditorClass.lpszClassName = L"AuraBlockEditor";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = EmployeeEditorProc;
    roomEditorClass.lpszClassName = L"AuraEmployeeEditor";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = ProjectEditorProc;
    roomEditorClass.lpszClassName = L"AuraProjectEditor";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = OrganizationEditorProc;
    roomEditorClass.lpszClassName = L"AuraOrganizationEditor";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = OrganizationManagerProc;
    roomEditorClass.lpszClassName = L"AuraOrganizationManager";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = SessionPickerProc;
    roomEditorClass.lpszClassName = L"AuraSessionPicker";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = PasswordChangeProc;
    roomEditorClass.lpszClassName = L"AuraPasswordChange";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = RoleEditorProc;
    roomEditorClass.lpszClassName = L"AuraRoleEditor";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = RoleManagerProc;
    roomEditorClass.lpszClassName = L"AuraRoleManager";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = AccountResetProc;
    roomEditorClass.lpszClassName = L"AuraAccountReset";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = AccountCreateProc;
    roomEditorClass.lpszClassName = L"AuraAccountCreate";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = AccountManagerProc;
    roomEditorClass.lpszClassName = L"AuraAccountManager";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = LeaveEditorProc;
    roomEditorClass.lpszClassName = L"AuraLeaveEditor";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = LeaveManagerProc;
    roomEditorClass.lpszClassName = L"AuraLeaveManager";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = ApprovalManagerProc;
    roomEditorClass.lpszClassName = L"AuraApprovalManager";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = AssetEditorProc;
    roomEditorClass.lpszClassName = L"AuraAssetEditor";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = MaintenanceEditorProc;
    roomEditorClass.lpszClassName = L"AuraMaintenanceEditor";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = MaintenanceManagerProc;
    roomEditorClass.lpszClassName = L"AuraMaintenanceManager";
    RegisterClassExW(&roomEditorClass);
    roomEditorClass.lpfnWndProc = AssetManagerProc;
    roomEditorClass.lpszClassName = L"AuraAssetManager";
    RegisterClassExW(&roomEditorClass);

    HWND hwnd = CreateWindowExW(
        0,
        CLASS_NAME,
        L"Aura Workspace \u2022 Native Modern Office OS",
        WS_OVERLAPPEDWINDOW,
        CW_USEDEFAULT, CW_USEDEFAULT, 1280, 820,
        nullptr,
        nullptr,
        hInstance,
        nullptr
    );

    if (!hwnd) return 0;

    ShowWindow(hwnd, nCmdShow);
    UpdateWindow(hwnd);
    ShowSessionPicker(hwnd);

    MSG msg = {};
    while (GetMessageW(&msg, nullptr, 0, 0)) {
        TranslateMessage(&msg);
        DispatchMessageW(&msg);
    }

    return static_cast<int>(msg.wParam);
}

// 1. MinGW Standard GUI-Einstiegspunkt (ohne -municode)
int WINAPI WinMain(HINSTANCE hInstance, HINSTANCE, LPSTR, int nCmdShow) {
    return AppMain(hInstance, nCmdShow);
}

// 2. Unicode GUI-Einstiegspunkt (mit -municode)
int WINAPI wWinMain(HINSTANCE hInstance, HINSTANCE, PWSTR, int nCmdShow) {
    return AppMain(hInstance, nCmdShow);
}

// 3. Konsole / Default CRT-Einstiegspunkt (ohne -mwindows)
int main() {
    return AppMain(GetModuleHandleW(nullptr), SW_SHOWDEFAULT);
}
