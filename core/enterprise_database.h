#pragma once

#include <cstdint>
#include <string>
#include <tuple>
#include <vector>

struct EnterpriseRoom {
    int64_t id = 0;
    std::wstring name;
    std::wstring location;
    int capacity = 0;
    std::wstring equipment;
    bool active = true;
};

struct EnterpriseBooking {
    int64_t id = 0;
    int64_t roomId = 0;
    int64_t userId = 0;
    int64_t startUtc = 0;
    int64_t endUtc = 0;
    int attendees = 0;
    std::wstring status;
};

struct EnterpriseTask {
    int64_t id = 0;
    std::wstring title;
    std::wstring department;
    std::wstring priority;
    std::wstring status;
    int64_t assigneeId = 0;
    bool active = true;
    int64_t dueAt = 0;
    int recurrenceDays = 0;
};

struct EnterpriseProject {
    int64_t id = 0;
    std::wstring name;
    std::wstring department;
    std::wstring description;
    std::wstring status;
    int64_t ownerId = 0;
    bool active = true;
};

struct EnterpriseTicket {
    int64_t id = 0;
    std::wstring title;
    std::wstring category;
    std::wstring priority;
    std::wstring status;
    int64_t assigneeId = 0;
    bool active = true;
};

struct EnterpriseComment {
    int64_t id = 0;
    std::wstring subjectType;
    int64_t subjectId = 0;
    int64_t authorId = 0;
    std::wstring body;
    std::wstring createdAt;
};

struct EnterpriseFacilityAsset {
    int64_t id = 0;
    std::wstring name;
    std::wstring category;
    std::wstring location;
    std::wstring status;
    bool active = true;
};

struct EnterpriseMaintenanceOrder {
    int64_t id = 0;
    int64_t assetId = 0;
    std::wstring title;
    std::wstring status;
    int64_t dueAt = 0;
    int recurrenceDays = 0;
    bool active = true;
};

struct EnterpriseEmployee {
    int64_t id = 0;
    std::wstring name;
    std::wstring role;
    std::wstring department;
    std::wstring presence;
    bool active = true;
};

struct EnterpriseRole {
    int64_t id = 0;
    std::wstring code;
    std::wstring name;
    std::wstring description;
    bool active = true;
};

struct EnterpriseOrganizationUnit {
    int64_t id = 0;
    std::wstring name;
    std::wstring code;
    int64_t managerId = 0;
    bool active = true;
};

struct EnterpriseLeaveRequest {
    int64_t id = 0;
    int64_t employeeId = 0;
    int64_t startsAt = 0;
    int64_t endsAt = 0;
    std::wstring reason;
    std::wstring status;
    int64_t decidedBy = 0;
    bool active = true;
};

struct EnterpriseTaskApproval {
    int64_t id = 0;
    int64_t taskId = 0;
    int64_t requestedBy = 0;
    int64_t approverId = 0;
    std::wstring status;
    int64_t decidedBy = 0;
    bool active = true;
};

struct EnterpriseTaskDependency {
    int64_t taskId = 0;
    int64_t dependsOnTaskId = 0;
};

struct EnterpriseSubtask {
    int64_t id = 0;
    int64_t taskId = 0;
    std::wstring title;
    std::wstring status;
    int64_t assigneeId = 0;
    int64_t dueAt = 0;
    bool active = true;
};

struct EnterpriseAttachment {
    int64_t id = 0;
    std::wstring subjectType;
    int64_t subjectId = 0;
    std::wstring originalName;
    std::wstring storedPath;
    int64_t sizeBytes = 0;
    std::wstring sha256;
    int64_t uploadedBy = 0;
    std::wstring createdAt;
    bool active = true;
};

struct EnterpriseAccount {
    int64_t employeeId = 0;
    std::wstring username;
    bool enabled = true;
    int failedAttempts = 0;
    int64_t lockedUntil = 0;
    bool mustChangePassword = false;
};

class EnterpriseDatabase {
public:
    explicit EnterpriseDatabase(const std::wstring& databasePath);
    ~EnterpriseDatabase();
    EnterpriseDatabase(const EnterpriseDatabase&) = delete;
    EnterpriseDatabase& operator=(const EnterpriseDatabase&) = delete;

    bool ready() const;
    const std::wstring& error() const;
    void setAuditActor(const std::wstring& actor);
    std::vector<EnterpriseRoom> rooms(bool includeArchived = false);
    std::vector<EnterpriseRoom> searchRooms(const std::wstring& query, bool includeArchived = false);
    bool createRoom(const EnterpriseRoom& room, int64_t& newId, const std::wstring& actor);
    bool updateRoom(const EnterpriseRoom& room, const std::wstring& actor);
    bool archiveRoom(int64_t roomId, const std::wstring& actor);
    // Applies a central room projection using a stable remote-to-local mapping.
    // This never guesses by room name, so remote renames remain deterministic.
    bool applySyncedRoom(const std::wstring& externalId, const EnterpriseRoom& room, const std::wstring& actor);
    // Preserve a remote booking's identity separately from its local row ID.
    bool applySyncedBooking(const std::wstring& clientId, int64_t remoteId, const std::wstring& roomExternalId, const EnterpriseBooking& booking, const std::wstring& reason, const std::wstring& actor, bool deferOverlapCheck = false);
    bool applySyncedBookingSnapshot(const std::wstring& snapshotJson, const std::wstring& ownClientId, bool& changed);
    bool cancelSyncedBooking(const std::wstring& clientId, int64_t remoteId, const std::wstring& actor);
    bool createBooking(const EnterpriseBooking& booking, int64_t& newId, const std::wstring& actor, int64_t actingEmployee = 0);
    bool createRecurringBookings(const EnterpriseBooking& firstBooking, int intervalDays, int occurrences, std::vector<int64_t>& newIds, const std::wstring& actor, int64_t actingEmployee = 0);
    bool rescheduleBooking(const EnterpriseBooking& booking, const std::wstring& actor, int64_t actingEmployee = 0);
    bool blockRoom(int64_t roomId, int64_t startUtc, int64_t endUtc, const std::wstring& reason, int64_t& newId, const std::wstring& actor, int64_t actingEmployee = 0);
    bool cancelBooking(int64_t bookingId, const std::wstring& actor, int64_t actingEmployee = 0);
    bool activeBookingForRoom(int64_t roomId, int64_t nowUtc, EnterpriseBooking& booking);
    std::vector<EnterpriseBooking> bookingsForRoom(int64_t roomId, bool includeCancelled = true);
    std::vector<EnterpriseTask> tasks(bool includeArchived = false);
    std::vector<EnterpriseProject> projects(bool includeArchived = false);
    bool createProject(const EnterpriseProject& project, int64_t& newId, const std::wstring& actor);
    bool updateProject(const EnterpriseProject& project, const std::wstring& actor);
    bool changeProjectStatus(int64_t projectId, const std::wstring& expected, const std::wstring& status, const std::wstring& actor);
    bool archiveProject(int64_t projectId, const std::wstring& actor);
    bool applySyncedProject(const std::wstring& externalId, const EnterpriseProject& project, const std::wstring& actor);
    bool applySyncedProjectAssignment(const std::wstring& projectExternalId, const std::wstring& taskExternalId, bool present, const std::wstring& actor);
    bool applySyncedProjectAssignmentSnapshot(const std::wstring& snapshotJson, bool& changed);
    bool configureDesktopProjectAssignmentOutbox(const std::wstring& clientId);
    bool nextDesktopProjectAssignmentOperation(std::wstring& operationId, int64_t& projectId, int64_t& taskId, bool& present, bool& expected, std::wstring& wireJson);
    bool freezeDesktopProjectAssignmentOperation(const std::wstring& operationId, const std::wstring& serverUrl, int64_t serverActor, const std::wstring& wireJson);
    bool acknowledgeDesktopProjectAssignmentOperation(const std::wstring& operationId, const std::wstring& serverUrl, int64_t serverActor, const std::wstring& wireJson, const std::wstring& actor);
    bool assignTaskToProject(int64_t taskId, int64_t projectId, const std::wstring& actor);
    bool setTaskProject(int64_t taskId, int64_t projectId, const std::wstring& actor);
    std::vector<EnterpriseTask> tasksForProject(int64_t projectId, bool includeArchived = false);
    bool createTask(const EnterpriseTask& task, int64_t& newId, const std::wstring& actor, int64_t projectId = 0);
    bool updateTask(const EnterpriseTask& task, const std::wstring& actor, int64_t expectedProject = -1, int64_t projectId = -1);
    bool updateTaskStatus(int64_t taskId, const std::wstring& status, const std::wstring& actor, int64_t* repeatedTask = nullptr);
    bool archiveTask(int64_t taskId, const std::wstring& actor);
    bool applySyncedTask(const std::wstring& externalId, const EnterpriseTask& task, const std::wstring& actor, bool schedulePresent = false);
    bool applySyncedTaskRecurrence(const std::wstring& sourceExternalId, const std::wstring& successorExternalId, const std::wstring& actor);
    bool applySyncedTaskRecurrenceSnapshot(const std::wstring& snapshotJson, bool& changed);
    bool desktopTaskRecurrenceLinks(const std::wstring& clientId, const std::wstring& serverUrl, int64_t serverActor, std::vector<std::tuple<int64_t,std::wstring,std::wstring>>& links);
    bool applySyncedMaintenanceRecurrence(const std::wstring& sourceExternalId, const std::wstring& successorExternalId, const std::wstring& actor);
    bool applySyncedMaintenanceRecurrenceSnapshot(const std::wstring& snapshotJson, bool& changed);
    bool desktopMaintenanceRecurrenceLinks(const std::wstring& clientId, const std::wstring& serverUrl, int64_t serverActor, std::vector<std::tuple<int64_t,std::wstring,std::wstring>>& links);
    std::vector<EnterpriseTaskDependency> taskDependencies(int64_t taskId);
    bool addTaskDependency(int64_t taskId, int64_t dependsOnTaskId, const std::wstring& actor);
    bool removeTaskDependency(int64_t taskId, int64_t dependsOnTaskId, const std::wstring& actor);
    bool applySyncedTaskDependency(const std::wstring& taskExternalId, const std::wstring& dependencyExternalId, bool present, const std::wstring& actor);
    bool configureDesktopDependencyOutbox(const std::wstring& clientId);
    bool nextDesktopDependencyOperation(std::wstring& operationId, int64_t& taskId, int64_t& dependencyId, bool& present, bool& expected, std::wstring& wireJson);
    bool freezeDesktopDependencyOperation(const std::wstring& operationId, const std::wstring& serverUrl, int64_t serverActor, const std::wstring& wireJson);
    bool acknowledgeDesktopDependencyOperation(const std::wstring& operationId, const std::wstring& serverUrl, int64_t serverActor, const std::wstring& wireJson, const std::wstring& actor);
    std::vector<EnterpriseSubtask> subtasks(int64_t taskId, bool includeArchived = false);
    bool createSubtask(const EnterpriseSubtask& subtask, int64_t& newId, const std::wstring& actor);
    bool updateSubtask(const EnterpriseSubtask& subtask, const std::wstring& actor, const EnterpriseSubtask* expected = nullptr);
    bool updateSubtaskStatus(int64_t subtaskId, const std::wstring& status, const std::wstring& actor);
    bool archiveSubtask(int64_t subtaskId, const std::wstring& actor, const EnterpriseSubtask* expected = nullptr);
    bool configureDesktopSubtaskOutbox(const std::wstring& clientId);
    bool nextDesktopSubtaskOperation(std::wstring& operationId, std::wstring& action, std::wstring& externalId, std::wstring& valueJson, std::wstring& expectedJson, std::wstring& wireJson);
    bool freezeDesktopSubtaskOperation(const std::wstring& operationId, const std::wstring& serverUrl, int64_t serverActor, const std::wstring& wireJson);
    bool acknowledgeDesktopSubtaskOperation(const std::wstring& operationId, const std::wstring& serverUrl, int64_t serverActor, const std::wstring& wireJson, const std::wstring& actor);
    bool applySyncedSubtask(const std::wstring& externalId, const std::wstring& taskExternalId, const std::wstring& assigneeExternalId, const EnterpriseSubtask& subtask, const std::wstring& actor);
    std::vector<EnterpriseAttachment> attachmentsFor(const std::wstring& subjectType, int64_t subjectId, bool includeArchived = false);
    bool createAttachment(const EnterpriseAttachment& attachment, int64_t& newId, const std::wstring& actor);
    bool archiveAttachment(int64_t attachmentId, const std::wstring& actor, int64_t actingEmployeeId = 0);
    bool restoreAttachment(int64_t attachmentId, const std::wstring& actor);
    // Imports attachment metadata after its content-addressed binary object has
    // been accepted by Central. The device-local path is deliberately supplied
    // by the client and is never replicated between workspaces.
    bool applySyncedAttachment(const std::wstring& externalId, const std::wstring& subjectExternalId, const std::wstring& uploaderExternalId, const EnterpriseAttachment& attachment, const std::wstring& actor, bool* wasChanged = nullptr);
    bool createOrResetAccount(int64_t employeeId, const std::wstring& username, const std::wstring& password, bool mustChangePassword, const std::wstring& actor);
    bool authenticate(const std::wstring& username, const std::wstring& password, int64_t nowUtc, EnterpriseAccount& account);
    bool changePassword(int64_t employeeId, const std::wstring& oldPassword, const std::wstring& newPassword, const std::wstring& actor);
    std::vector<EnterpriseAccount> accounts(bool includeDisabled = false);
    bool setAccountEnabled(int64_t employeeId, bool enabled, const std::wstring& actor);
    bool createTaskApproval(int64_t taskId, int64_t requestedBy, int64_t approverId, int64_t& approvalId, const std::wstring& actor);
    bool decideTaskApproval(int64_t approvalId, bool approved, int64_t decidedBy, const std::wstring& actor);
    bool applySyncedTaskApproval(const std::wstring& externalId, const std::wstring& taskExternalId, const std::wstring& requesterExternalId, const std::wstring& approverExternalId, const std::wstring& decidedByExternalId, const EnterpriseTaskApproval& approval, const std::wstring& actor);
    std::vector<EnterpriseTaskApproval> taskApprovals(int64_t approverId = 0, bool includeDecided = false);
    std::vector<EnterpriseTicket> tickets(bool includeArchived = false);
    bool createTicket(const EnterpriseTicket& ticket, int64_t& newId, const std::wstring& actor);
    bool updateTicket(const EnterpriseTicket& ticket, const std::wstring& actor);
    bool updateTicketStatus(int64_t ticketId, const std::wstring& status, const std::wstring& actor);
    bool archiveTicket(int64_t ticketId, const std::wstring& actor);
    // Applies a central ticket projection using the stable remote-to-local mapping.
    bool applySyncedTicket(const std::wstring& externalId, const EnterpriseTicket& ticket, const std::wstring& actor);
    std::vector<EnterpriseComment> commentsFor(const std::wstring& subjectType, int64_t subjectId);
    bool createComment(const EnterpriseComment& comment, int64_t& newId, const std::wstring& actor);
    bool applySyncedComment(const std::wstring& externalId, const std::wstring& subjectExternalId, const std::wstring& authorExternalId, const EnterpriseComment& comment, const std::wstring& actor);
    std::vector<EnterpriseFacilityAsset> facilityAssets(bool includeArchived = false);
    bool createFacilityAsset(const EnterpriseFacilityAsset& asset, int64_t& newId, const std::wstring& actor);
    bool updateFacilityAsset(const EnterpriseFacilityAsset& asset, const std::wstring& actor);
    bool archiveFacilityAsset(int64_t assetId, const std::wstring& actor);
    // Upserts a centrally identified asset without relying on local SQLite IDs.
    bool applySyncedFacilityAsset(const std::wstring& externalId, const EnterpriseFacilityAsset& asset, const std::wstring& actor);
    std::vector<EnterpriseMaintenanceOrder> maintenanceOrders(int64_t assetId = 0, bool includeArchived = false);
    bool createMaintenanceOrder(const EnterpriseMaintenanceOrder& order, int64_t& newId, const std::wstring& actor);
    bool updateMaintenanceOrder(const EnterpriseMaintenanceOrder& order, const std::wstring& actor);
    bool updateMaintenanceOrderStatus(int64_t orderId, const std::wstring& status, const std::wstring& actor);
    bool archiveMaintenanceOrder(int64_t orderId, const std::wstring& actor);
    bool applySyncedMaintenanceOrder(const std::wstring& externalId, const std::wstring& assetExternalId, const EnterpriseMaintenanceOrder& order, const std::wstring& actor);
    std::vector<EnterpriseEmployee> employees(bool includeArchived = false);
    bool createEmployee(const EnterpriseEmployee& employee, int64_t& newId, const std::wstring& actor);
    bool saveEmployeeWithRole(const EnterpriseEmployee& employee, const std::wstring& roleCode, const std::wstring& actor);
    bool updateEmployee(const EnterpriseEmployee& employee, const std::wstring& actor);
    bool updatePresence(int64_t employeeId, const std::wstring& presence, const std::wstring& actor, int64_t actingEmployeeId = 0);
    bool archiveEmployee(int64_t employeeId, const std::wstring& actor);
    bool applySyncedEmployee(const std::wstring& externalId, const EnterpriseEmployee& employee, const std::wstring& actor);
    // Resolves a remote stable object identity to this client's SQLite key.
    int64_t syncedLocalId(const std::wstring& objectType, const std::wstring& externalId);
    std::wstring syncedExternalId(const std::wstring& objectType, int64_t localId);
    bool ensureSyncedExternalId(const std::wstring& objectType, int64_t localId, const std::wstring& externalId);
    std::vector<EnterpriseLeaveRequest> leaveRequests(int64_t employeeId = 0, bool includeClosed = false);
    bool createLeaveRequest(const EnterpriseLeaveRequest& request, int64_t& newId, const std::wstring& actor);
    bool decideLeaveRequest(int64_t requestId, bool approved, int64_t decidedBy, const std::wstring& actor, const EnterpriseLeaveRequest* expected = nullptr);
    bool applySyncedLeaveRequest(const std::wstring& externalId, const EnterpriseLeaveRequest& request, const std::wstring& actor);
    std::vector<EnterpriseOrganizationUnit> organizationUnits(bool includeArchived = false);
    bool createOrganizationUnit(const EnterpriseOrganizationUnit& unit, int64_t& newId, const std::wstring& actor);
    bool updateOrganizationUnit(const EnterpriseOrganizationUnit& unit, const std::wstring& actor);
    bool archiveOrganizationUnit(int64_t unitId, const std::wstring& actor);
    bool applySyncedOrganizationUnit(const std::wstring& externalId, const EnterpriseOrganizationUnit& unit, const std::wstring& actor);
    bool assignRole(int64_t employeeId, const std::wstring& roleCode, const std::wstring& actor);
    std::vector<EnterpriseRole> roles(bool includeInactive = false);
    bool createRole(const EnterpriseRole& role, int64_t& newId, const std::vector<std::wstring>& permissions, const std::wstring& actor);
    bool updateRole(const EnterpriseRole& role, const std::vector<std::wstring>& permissions, const std::wstring& actor);
    bool archiveRole(int64_t roleId, const std::wstring& actor);
    std::vector<std::wstring> permissionsForRole(int64_t roleId);
    std::vector<std::wstring> rolesForEmployee(int64_t employeeId);
    bool hasPermission(int64_t employeeId, const std::wstring& permission);
    bool backupTo(const std::wstring& destinationPath, const std::wstring& actor, bool recordAudit = true);
    bool recordBackup(const std::wstring& destinationPath, const std::wstring& actor);
    bool restoreFrom(const std::wstring& sourcePath, const std::wstring& actor, bool recordAudit = true);
    bool exportRoomsCsv(const std::wstring& destinationPath, const std::wstring& actor, bool recordAudit = true);
    bool recordRoomExport(const std::wstring& destinationPath, const std::wstring& actor);
    bool importRoomsCsv(const std::wstring& sourcePath, int& importedCount, const std::wstring& actor);
    std::vector<std::wstring> auditLog(int limit = 100);

private:
    struct sqlite3* db_ = nullptr;
    std::wstring error_;
    std::wstring auditActor_;
    bool migrate();
    bool execute(const char* sql);
    void setError(const char* context);
    bool audit(const std::wstring& actor, const std::wstring& action, const std::wstring& subject);
};
