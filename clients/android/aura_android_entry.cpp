// ARM64 entry point consumed by the KR Android runner as libuser.so.
// Network identity and all business authorization stay with AURA Central.
#include <cstdio>

extern "C" int main(int, char**) {
    std::puts("AURA Workspace Enterprise Android");
    std::puts("Central protocol client is packaged. Configure AURA_SYNC_URL and AURA_SYNC_TOKEN in the managed device profile.");
    std::puts("Functions: leave requests, rooms, tasks, projects, tickets and cursor synchronization.");
    return 0;
}
