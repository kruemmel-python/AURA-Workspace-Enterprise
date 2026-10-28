// AURA Workspace Enterprise portable command client.
// It is UI-neutral so Linux and Android adapters use the same central protocol.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"aura/services/portable"
)

func fail(err error) { fmt.Fprintln(os.Stderr, "AURA:", err); os.Exit(1) }
func emit(value any) {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		fail(err)
	}
	fmt.Println(string(data))
}

func main() {
	url := flag.String("url", os.Getenv("AURA_SYNC_URL"), "AURA Central URL")
	token := flag.String("token", os.Getenv("AURA_SYNC_TOKEN"), "Bearer token")
	action := flag.String("action", "sync", "sync|rooms|tasks|projects|tickets|leave|facility-assets|create-leave|leave-decision|create-task|task-status|create-ticket|ticket-status|create-room|update-room|archive-room|create-project|update-project|archive-project|create-facility-asset|update-facility-asset|archive-facility-asset|create-comment|request-task-approval|decide-task-approval|upload-attachment|download-attachment|publish-event")
	cursor := flag.Int("cursor", 0, "cursor for sync")
	roomKey := flag.String("room-key", "", "central room identity")
	bookingClient := flag.String("booking-client", "", "booking source identity")
	bookingID := flag.Int64("booking-id", 0, "booking id")
	attendees := flag.Int("attendees", 1, "booking attendee count")
	occurrences := flag.Int("occurrences", 1, "booking series count")
	interval := flag.Int("interval", 7, "booking interval in days")
	includeArchived := flag.Bool("include-archived", false, "include archived rooms or facility assets (requires management permission)")
	starts := flag.Int64("starts-at", 0, "leave start as Unix seconds")
	ends := flag.Int64("ends-at", 0, "leave end as Unix seconds")
	reason := flag.String("reason", "", "leave reason")
	title := flag.String("title", "", "task or ticket title")
	department := flag.String("department", "", "department")
	role := flag.String("role", "", "employee job title")
	code := flag.String("code", "", "organization code")
	managerID := flag.Int64("manager-id", 0, "organization manager identity")
	accountID := flag.Int64("account-id", 0, "server account employee identity")
	presence := flag.String("presence", "office", "employee presence")
	category := flag.String("category", "", "ticket category")
	priority := flag.String("priority", "Mittel", "Niedrig|Mittel|Hoch|Kritisch")
	status := flag.String("status", "", "workflow status")
	expectedStatus := flag.String("expected-status", "", "previous project status")
	id := flag.String("id", "", "central object id")
	projectID := flag.String("project-id", "", "central project identity")
	expectedProjectID := flag.String("expected-project-id", "", "previous central project identity")
	assignee := flag.Int64("assignee-id", 0, "central assignee employee id")
	name := flag.String("name", "", "room or project name")
	location := flag.String("location", "", "room location")
	assetCategory := flag.String("asset-category", "", "facility asset category")
	assetStatus := flag.String("asset-status", "operational", "operational|maintenance|out_of_service")
	assetID := flag.String("asset-id", "", "facility asset id")
	due := flag.Int64("due", 0, "maintenance due time")
	recurrence := flag.Int("recurrence", 0, "maintenance recurrence days")
	capacity := flag.Int("capacity", 0, "room capacity")
	equipment := flag.String("equipment", "", "room equipment")
	description := flag.String("description", "", "project description")
	owner := flag.Int64("owner-id", 0, "central project owner id")
	topic := flag.String("topic", "", "enterprise event topic")
	payload := flag.String("payload", "", "JSON event payload")
	subjectType := flag.String("subject-type", "", "task|ticket")
	subjectID := flag.String("subject-id", "", "stable central task or ticket id")
	taskID := flag.String("task-id", "", "task identifier")
	dependencyID := flag.String("dependency-id", "", "dependency identifier")
	authorID := flag.String("author-id", "", "stable central employee id")
	body := flag.String("body", "", "comment body")
	requesterID := flag.String("requested-by", "", "stable central requester employee id")
	approverID := flag.String("approver-id", "", "stable central approver employee id")
	decisionBy := flag.String("decided-by", "", "stable central deciding employee id")
	filePath := flag.String("file", "", "source or destination file path for attachment actions")
	digest := flag.String("sha256", "", "expected attachment digest")
	size := flag.Int64("size", 0, "expected attachment size")
	flag.Parse()
	if strings.TrimSpace(*url) == "" || strings.TrimSpace(*token) == "" {
		fail(fmt.Errorf("-url/-token (or AURA_SYNC_URL/AURA_SYNC_TOKEN) are required"))
	}
	client := portable.Client{BaseURL: *url, Token: *token}
	ctx := context.Background()
	switch *action {
	case "central-event-history":
		value, err := client.EventHistory(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "sync":
		value, err := client.Changes(ctx, *cursor)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "central-approvals":
		value, err := client.Approvals(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "central-approval-request":
		approver, err := strconv.ParseInt(*approverID, 10, 64)
		if err != nil {
			fail(err)
		}
		if err = client.RequestApproval(ctx, *subjectID, approver); err != nil {
			fail(err)
		}
		emit(map[string]bool{"requested": true})
	case "central-approval-decision":
		if err := client.DecideApproval(ctx, *id, *status); err != nil {
			fail(err)
		}
		emit(map[string]bool{"decided": true})
	case "central-organizations":
		value, err := client.Organizations(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "central-organization-create", "central-organization-update":
		value, err := client.SaveOrganization(ctx, portable.Organization{ID: *id, Name: *name, Code: *code, ManagerID: *managerID}, *action == "central-organization-create")
		if err != nil {
			fail(err)
		}
		emit(value)
	case "central-organization-archive":
		if err := client.ArchiveOrganization(ctx, *id); err != nil {
			fail(err)
		}
		emit(map[string]bool{"archived": true})
	case "my-presence-details":
		value, err := client.OwnPresenceDetails(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "set-my-presence-checked":
		value, err := client.SetOwnPresenceChecked(ctx, *id, *status, *presence)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "my-presence":
		value, err := client.OwnPresenceDetails(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "set-my-presence":
		value, err := client.SetOwnPresence(ctx, *status, *presence)
		if err != nil {
			fail(err)
		}
		emit(map[string]string{"presence": value})
	case "employee-accounts":
		value, err := client.EmployeeAccounts(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "employee-account-link":
		if err := client.LinkEmployeeAccount(ctx, *id, *accountID); err != nil {
			fail(err)
		}
		emit(map[string]bool{"updated": true})
	case "employees":
		value, err := client.Employees(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "sync-employees":
		value, err := client.EmployeesForSync(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "sync-employee-event":
		var value portable.EmployeeSyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.SyncEmployee(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "abandon-employee-event":
		var value portable.EmployeeSyncAbandonment
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid abandonment payload: %w", err))
		}
		if err := client.AbandonEmployeeSync(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "central-employee-create", "central-employee-update":
		value, err := client.SaveEmployee(ctx, portable.Employee{ID: *id, Name: *name, Role: *role, Department: *department, Presence: *presence}, *action == "central-employee-create")
		if err != nil {
			fail(err)
		}
		emit(value)
	case "central-employee-archive":
		if err := client.ArchiveEmployee(ctx, *id); err != nil {
			fail(err)
		}
		emit(map[string]bool{"archived": true})
	case "capabilities":
		value, err := client.Capabilities(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "identity":
		value, err := client.Identity(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "abandon-leave-event":
		var value portable.LeaveSyncAbandonment
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid abandonment payload: %w", err))
		}
		if err := client.AbandonLeaveSync(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "sync-leave-event":
		var value portable.LeaveSyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.SyncLeave(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "sync-tickets":
		value, err := client.TicketsForSync(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "sync-ticket-event":
		var value portable.TicketSyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.SyncTicket(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "sync-projects":
		value, err := client.ProjectsForSync(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "sync-project-event":
		var value portable.ProjectSyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.SyncProject(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "sync-approvals":
		value, err := client.ApprovalsForSync(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "sync-approval-event":
		var value portable.ApprovalSyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.SyncApproval(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "maintenance-recurrence-link":
		var value portable.TaskRecurrenceLink
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(err)
		}
		if err := client.LinkMaintenanceRecurrence(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "maintenance-recurrence-links":
		value, err := client.MaintenanceRecurrenceLinks(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "sync-maintenance":
		value, err := client.MaintenanceForSync(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "sync-maintenance-event":
		var value portable.MaintenanceSyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.SyncMaintenance(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "sync-facility-assets":
		value, err := client.FacilityAssetsForSync(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "sync-facility-asset-event":
		var value portable.FacilityAssetSyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.SyncFacilityAsset(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "sync-organizations":
		value, err := client.OrganizationsForSync(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "sync-organization-event":
		var value portable.OrganizationSyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.SyncOrganization(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "sync-attachments":
		value, err := client.AttachmentsForSync(ctx, *subjectType)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "sync-attachment-event":
		var value portable.AttachmentSyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.SyncAttachment(ctx, *subjectType, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "sync-personal-attachment-event":
		var value portable.AttachmentSyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.SyncPersonalAttachment(ctx, *subjectType, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "sync-ticket-comments":
		value, err := client.TicketCommentsForSync(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "sync-ticket-comment-event":
		var value portable.TicketCommentSyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.SyncTicketComment(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "sync-personal-ticket-comment-event":
		var value portable.TicketCommentSyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.SyncPersonalTicketComment(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "sync-task-comments":
		value, err := client.TaskCommentsForSync(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "sync-task-comment-event":
		var value portable.TaskCommentSyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.SyncTaskComment(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "sync-personal-task-comment-event":
		var value portable.TaskCommentSyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.SyncPersonalTaskComment(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "sync-project-assignments":
		value, err := client.ProjectAssignmentsForSync(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "task-recurrence-reserve":
		var value portable.TaskRecurrenceReservation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(err)
		}
		if err := client.ReserveTaskRecurrence(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "task-recurrence-link":
		var value portable.TaskRecurrenceLink
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid recurrence link: %w", err))
		}
		if err := client.LinkTaskRecurrence(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "task-recurrence-links":
		value, err := client.TaskRecurrenceLinks(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "sync-project-assignment-event":
		var value portable.ProjectAssignmentSyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.SyncProjectAssignment(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "sync-dependencies":
		value, err := client.DependenciesForSync(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "sync-dependency-event":
		var value portable.DependencySyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.SyncDependency(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "legacy-booking-reconcile":
		var value portable.LegacyBookingReconciliation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid reconciliation payload: %w", err))
		}
		result, err := client.ReconcileLegacyBooking(ctx, value)
		if err != nil {
			fail(err)
		}
		emit(result)
	case "sync-bookings":
		value, err := client.BookingsForSync(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "abandon-booking-event":
		var value portable.BookingSyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.AbandonBookingSync(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "sync-booking-event":
		var value portable.BookingSyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.SyncBooking(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "sync-subtasks":
		value, err := client.SubtasksForSync(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "sync-subtask-event":
		var value portable.SubtaskSyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.SyncSubtask(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "sync-tasks":
		value, err := client.TasksForSync(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "sync-task-event":
		var value portable.TaskSyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.SyncTask(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "abandon-task-event":
		var value portable.TaskSyncAbandonment
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid abandonment payload: %w", err))
		}
		if err := client.AbandonTaskSync(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "abandon-subtask-event":
		var value portable.SubtaskSyncAbandonment
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid abandonment payload: %w", err))
		}
		if err := client.AbandonSubtaskSync(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "sync-room-event":
		var value portable.RoomSyncOperation
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid sync payload: %w", err))
		}
		if err := client.SyncRoom(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "abandon-room-event":
		var value portable.RoomSyncAbandonment
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("invalid abandonment payload: %w", err))
		}
		if err := client.AbandonRoomSync(ctx, value); err != nil {
			fail(err)
		}
		emit(map[string]bool{"accepted": true})
	case "central-bookings":
		value, err := client.RoomBookings(ctx, *roomKey)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "central-book", "central-reschedule", "central-cancel-booking", "central-block-room":
		method := "POST"
		if *action == "central-reschedule" {
			method = "PATCH"
		}
		if *action == "central-cancel-booking" {
			method = "DELETE"
		}
		input := portable.RoomBookingInput{ClientID: *bookingClient, BookingID: *bookingID, StartsAt: *starts, EndsAt: *ends, Attendees: *attendees, Occurrences: *occurrences, IntervalDays: *interval, Blocked: *action == "central-block-room", Reason: *reason}
		if err := client.ChangeRoomBookings(ctx, method, *roomKey, input); err != nil {
			fail(err)
		}
		emit(map[string]bool{"updated": true})
	case "rooms-availability":
		value, err := client.RoomsWithAvailability(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "rooms":
		value, err := client.Rooms(ctx, *includeArchived)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "tasks":
		value, err := client.Tasks(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "projects":
		value, err := client.Projects(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "tickets":
		value, err := client.Tickets(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "legacy-leave-reconcile":
		var input portable.LegacyLeaveReconciliation
		if err := json.Unmarshal([]byte(*payload), &input); err != nil {
			fail(fmt.Errorf("invalid reconciliation payload: %w", err))
		}
		value, err := client.ReconcileLegacyLeave(ctx, input)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "leave":
		value, err := client.Leaves(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "central-maintenance":
		value, err := client.Maintenance(ctx, *assetID)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "central-maintenance-create", "central-maintenance-update":
		value, err := client.SaveMaintenance(ctx, portable.MaintenanceOrder{ID: *id, AssetID: *assetID, Title: *title, DueAt: *due, RecurrenceDays: *recurrence}, *action == "central-maintenance-create")
		if err != nil {
			fail(err)
		}
		emit(value)
	case "central-maintenance-status":
		if err := client.MaintenanceStatus(ctx, *assetID, *id, *status); err != nil {
			fail(err)
		}
		emit(map[string]bool{"updated": true})
	case "central-maintenance-archive":
		if err := client.ArchiveMaintenance(ctx, *assetID, *id); err != nil {
			fail(err)
		}
		emit(map[string]bool{"archived": true})
	case "facility-assets":
		value, err := client.FacilityAssets(ctx, *includeArchived)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "create-leave":
		if *starts <= 0 || *ends <= *starts || strings.TrimSpace(*reason) == "" {
			fail(fmt.Errorf("-starts-at, -ends-at and -reason are required; starts-at=" + strconv.FormatInt(*starts, 10)))
		}
		value, err := client.CreateLeave(ctx, *starts, *ends, *reason)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "leave-decision":
		if *id == "" || (*status != "approved" && *status != "rejected") {
			fail(fmt.Errorf("-id and -status approved|rejected are required"))
		}
		var value portable.LeaveRequest
		var err error
		if *payload != "" {
			var expected portable.LeaveRequest
			if json.Unmarshal([]byte(*payload), &expected) != nil || expected.ID != *id {
				fail(fmt.Errorf("invalid expected leave request"))
			}
			value, err = client.DecideLeaveCompared(ctx, expected, *status)
		} else {
			value, err = client.DecideLeave(ctx, *id, *status)
		}
		if err != nil {
			fail(err)
		}
		emit(value)
	case "subtasks":
		value, err := client.Subtasks(ctx, *taskID)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "subtask-save":
		sub := portable.Subtask{ID: *id, TaskID: *taskID, Title: *title, Status: *status, AssigneeID: *assignee, DueAt: *due}
		var value portable.Subtask
		var err error
		if *payload != "" {
			var expected portable.Subtask
			if json.Unmarshal([]byte(*payload), &expected) != nil || expected.ID != sub.ID || expected.TaskID != sub.TaskID {
				fail(fmt.Errorf("invalid expected subtask"))
			}
			value, err = client.SaveSubtaskCompared(ctx, sub, expected)
		} else {
			value, err = client.SaveSubtask(ctx, sub, *id == "" || *id == "0")
		}
		if err != nil {
			fail(err)
		}
		emit(value)
	case "subtask-archive":
		var err error
		if *payload != "" {
			var expected portable.Subtask
			if json.Unmarshal([]byte(*payload), &expected) != nil || expected.ID != *id || expected.TaskID != *taskID {
				fail(fmt.Errorf("invalid expected subtask"))
			}
			err = client.ArchiveSubtaskCompared(ctx, expected)
		} else {
			err = client.ArchiveSubtask(ctx, *taskID, *id)
		}
		if err != nil {
			fail(err)
		}
		emit(map[string]bool{"archived": true})
	case "dependencies":
		value, err := client.TaskDependencies(ctx, *taskID)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "dependency-add", "dependency-remove":
		if err := client.ChangeTaskDependency(ctx, *taskID, *dependencyID, *action == "dependency-add"); err != nil {
			fail(err)
		}
		emit(map[string]bool{"updated": true})
	case "create-task":
		if strings.TrimSpace(*title) == "" || strings.TrimSpace(*department) == "" || *assignee <= 0 {
			fail(fmt.Errorf("-title, -department and -assignee-id are required"))
		}
		var value portable.Task
		var err error
		if *projectID != "" {
			value, err = client.CreateScheduledTaskInProject(ctx, *title, *department, *priority, *assignee, *due, *recurrence, *projectID)
		} else {
			value, err = client.CreateScheduledTask(ctx, *title, *department, *priority, *assignee, *due, *recurrence)
		}
		if err != nil {
			fail(err)
		}
		emit(value)
	case "update-task":
		var taskDue *int64
		var taskRecurrence *int
		projectSpecified := false
		flag.Visit(func(f *flag.Flag) {
			if f.Name == "due" {
				taskDue = due
			}
			if f.Name == "recurrence" {
				taskRecurrence = recurrence
			}
			if f.Name == "expected-project-id" {
				projectSpecified = true
			}
		})
		var value portable.Task
		var err error
		if projectSpecified {
			value, err = client.UpdateScheduledTaskProject(ctx, portable.Task{ID: *id, Title: *title, Department: *department, Priority: *priority, AssigneeID: *assignee}, taskDue, taskRecurrence, *expectedProjectID, *projectID)
		} else {
			value, err = client.UpdateScheduledTask(ctx, portable.Task{ID: *id, Title: *title, Department: *department, Priority: *priority, AssigneeID: *assignee}, taskDue, taskRecurrence)
		}
		if err != nil {
			fail(err)
		}
		emit(value)
	case "archive-task":
		if err := client.ArchiveTask(ctx, *id); err != nil {
			fail(err)
		}
		emit(map[string]bool{"archived": true})
	case "task-status":
		if *id == "" || *status == "" {
			fail(fmt.Errorf("-id and -status are required"))
		}
		value, err := client.SetTaskStatus(ctx, *id, *status)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "create-ticket":
		if strings.TrimSpace(*title) == "" || strings.TrimSpace(*category) == "" || *assignee <= 0 {
			fail(fmt.Errorf("-title, -category and -assignee-id are required"))
		}
		value, err := client.CreateTicket(ctx, *title, *category, *priority, *assignee)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "update-ticket":
		value, err := client.UpdateTicket(ctx, portable.Ticket{ID: *id, Title: *title, Category: *category, Priority: *priority, AssigneeID: *assignee})
		if err != nil {
			fail(err)
		}
		emit(value)
	case "archive-ticket":
		if err := client.ArchiveTicket(ctx, *id); err != nil {
			fail(err)
		}
		emit(map[string]bool{"archived": true})
	case "ticket-status":
		if *id == "" || *status == "" {
			fail(fmt.Errorf("-id and -status are required"))
		}
		value, err := client.SetTicketStatus(ctx, *id, *status)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "create-room":
		if strings.TrimSpace(*name) == "" || strings.TrimSpace(*location) == "" || *capacity <= 0 {
			fail(fmt.Errorf("-name, -location and positive -capacity are required"))
		}
		value, err := client.CreateRoom(ctx, *name, *location, *capacity, *equipment)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "update-room":
		if *id == "" || strings.TrimSpace(*name) == "" || strings.TrimSpace(*location) == "" || *capacity <= 0 {
			fail(fmt.Errorf("-id, -name, -location and positive -capacity are required"))
		}
		value, err := client.UpdateRoom(ctx, portable.Room{ID: *id, Name: *name, Location: *location, Capacity: *capacity, Equipment: *equipment})
		if err != nil {
			fail(err)
		}
		emit(value)
	case "archive-room":
		if *id == "" {
			fail(fmt.Errorf("-id is required"))
		}
		if err := client.ArchiveRoom(ctx, *id); err != nil {
			fail(err)
		}
		fmt.Println(`{"archived":true}`)
	case "create-project":
		if strings.TrimSpace(*name) == "" || strings.TrimSpace(*department) == "" || *owner <= 0 {
			fail(fmt.Errorf("-name, -department and -owner-id are required"))
		}
		projectStatus := *status
		if projectStatus == "" {
			projectStatus = "planned"
		}
		value, err := client.CreateProject(ctx, *name, *department, *description, projectStatus, *owner)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "project-status":
		value, err := client.ChangeProjectStatus(ctx, *id, *expectedStatus, *status)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "update-project":
		if *id == "" || strings.TrimSpace(*name) == "" || strings.TrimSpace(*department) == "" || *owner <= 0 || *status == "" {
			fail(fmt.Errorf("-id, -name, -department, -status and -owner-id are required"))
		}
		value, err := client.UpdateProject(ctx, portable.Project{ID: *id, Name: *name, Department: *department, Description: *description, Status: *status, OwnerID: *owner})
		if err != nil {
			fail(err)
		}
		emit(value)
	case "archive-project":
		if *id == "" {
			fail(fmt.Errorf("-id is required"))
		}
		if err := client.ArchiveProject(ctx, *id); err != nil {
			fail(err)
		}
		fmt.Println(`{"archived":true}`)
	case "create-facility-asset", "update-facility-asset", "archive-facility-asset":
		assetID := *id
		if assetID == "" && *action == "create-facility-asset" {
			assetID = "portable:" + strconv.FormatInt(time.Now().UnixNano(), 10)
		}
		if assetID == "" || strings.TrimSpace(*name) == "" || strings.TrimSpace(*assetCategory) == "" || strings.TrimSpace(*location) == "" {
			fail(fmt.Errorf("-id (for update/archive), -name, -asset-category and -location are required"))
		}
		asset := portable.FacilityAsset{ID: assetID, Name: *name, Category: *assetCategory, Location: *location, Status: *assetStatus}
		var err error
		if *action == "create-facility-asset" {
			err = client.CreateFacilityAsset(ctx, asset)
		} else if *action == "update-facility-asset" {
			err = client.UpdateFacilityAsset(ctx, asset)
		} else {
			err = client.ArchiveFacilityAsset(ctx, asset)
		}
		if err != nil {
			fail(err)
		}
		emit(asset)
	case "comments":
		value, err := client.Comments(ctx, *subjectType, *subjectID)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "personal-comments":
		value, err := client.PersonalComments(ctx, *subjectType, *subjectID, *authorID)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "add-comment":
		value, err := client.AddComment(ctx, *subjectType, *subjectID, *body)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "create-comment":
		if (*subjectType != "task" && *subjectType != "ticket") || strings.TrimSpace(*subjectID) == "" || strings.TrimSpace(*authorID) == "" || strings.TrimSpace(*body) == "" {
			fail(fmt.Errorf("-subject-type task|ticket, -subject-id, -author-id and -body are required"))
		}
		commentID := "portable:comment:" + strconv.FormatInt(time.Now().UnixNano(), 10)
		value := map[string]string{"id": commentID, "subjectType": *subjectType, "subjectId": *subjectID, "authorId": *authorID, "body": *body}
		if err := client.PublishEvent(ctx, "comment.created", value); err != nil {
			fail(err)
		}
		emit(value)
	case "request-task-approval":
		if strings.TrimSpace(*subjectID) == "" || strings.TrimSpace(*requesterID) == "" || strings.TrimSpace(*approverID) == "" || *requesterID == *approverID {
			fail(fmt.Errorf("-subject-id, -requested-by and a different -approver-id are required"))
		}
		approvalID := "portable:approval:" + strconv.FormatInt(time.Now().UnixNano(), 10)
		value := map[string]any{"id": approvalID, "taskId": *subjectID, "requestedBy": *requesterID, "approverId": *approverID, "status": "pending", "decidedBy": "", "active": true}
		if err := client.PublishEvent(ctx, "task.approval.requested", value); err != nil {
			fail(err)
		}
		emit(value)
	case "decide-task-approval":
		if strings.TrimSpace(*id) == "" || strings.TrimSpace(*subjectID) == "" || strings.TrimSpace(*requesterID) == "" || strings.TrimSpace(*approverID) == "" || strings.TrimSpace(*decisionBy) == "" || *decisionBy != *approverID || (*status != "approved" && *status != "rejected") {
			fail(fmt.Errorf("-id, -subject-id, -requested-by, -approver-id, matching -decided-by and -status approved|rejected are required"))
		}
		value := map[string]any{"id": *id, "taskId": *subjectID, "requestedBy": *requesterID, "approverId": *approverID, "status": *status, "decidedBy": *decisionBy, "active": true}
		if err := client.PublishEvent(ctx, "task.approval.decided", value); err != nil {
			fail(err)
		}
		emit(value)
	case "subject-attachments":
		value, err := client.SubjectAttachments(ctx, *subjectType, *subjectID)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "personal-attachment-states":
		var known []portable.SubjectAttachment
		if err := json.Unmarshal([]byte(*payload), &known); err != nil || known == nil {
			fail(fmt.Errorf("invalid known attachments"))
		}
		value, err := client.PersonalAttachmentStates(ctx, *subjectType, *subjectID, *authorID, known)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "personal-subject-attachments":
		value, err := client.PersonalSubjectAttachments(ctx, *subjectType, *subjectID, *authorID)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "subject-attachments-all":
		value, err := client.AllSubjectAttachments(ctx, *subjectType, *subjectID)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "subject-attachment-restore":
		var expected portable.SubjectAttachment
		if err := json.Unmarshal([]byte(*payload), &expected); err != nil || expected.ID == "" || expected.SubjectType != *subjectType || expected.SubjectID != *subjectID || expected.Active {
			fail(fmt.Errorf("invalid archived attachment"))
		}
		if err := client.RestoreSubjectAttachment(ctx, *subjectType, *subjectID, expected); err != nil {
			fail(err)
		}
		emit(map[string]bool{"restored": true})
	case "subject-attachment-upload":
		file, err := os.Open(*filePath)
		if err != nil {
			fail(err)
		}
		defer file.Close()
		value, err := client.UploadSubjectAttachment(ctx, *subjectType, *subjectID, filepath.Base(*filePath), file)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "subject-attachment-download":
		data, err := client.DownloadSubjectAttachment(ctx, *subjectType, *subjectID, *id, *digest, *size)
		if err != nil {
			fail(err)
		}
		f, err := os.OpenFile(*filePath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if err != nil {
			fail(err)
		}
		_, err = f.Write(data)
		closeErr := f.Close()
		if err == nil {
			err = closeErr
		}
		if err != nil {
			os.Remove(*filePath)
			fail(err)
		}
		emit(map[string]bool{"downloaded": true})
	case "personal-subject-attachment-download":
		data, err := client.DownloadPersonalSubjectAttachment(ctx, *subjectType, *subjectID, *id, *authorID, *digest, *size)
		if err != nil {
			fail(err)
		}
		f, err := os.OpenFile(*filePath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
		if err != nil {
			fail(err)
		}
		_, err = f.Write(data)
		closeErr := f.Close()
		if err == nil {
			err = closeErr
		}
		if err != nil {
			os.Remove(*filePath)
			fail(err)
		}
		emit(map[string]bool{"downloaded": true})
	case "subject-attachment-archive":
		if err := client.ArchiveSubjectAttachment(ctx, *subjectType, *subjectID, *id); err != nil {
			fail(err)
		}
		emit(map[string]bool{"archived": true})
	case "upload-attachment":
		if strings.TrimSpace(*filePath) == "" {
			fail(fmt.Errorf("-file is required"))
		}
		file, err := os.Open(*filePath)
		if err != nil {
			fail(err)
		}
		defer file.Close()
		digest, err := client.UploadAttachment(ctx, file)
		if err != nil {
			fail(err)
		}
		emit(map[string]string{"sha256": digest})
	case "upload-personal-attachment":
		if strings.TrimSpace(*filePath) == "" || strings.TrimSpace(*authorID) == "" {
			fail(fmt.Errorf("-file and -author-id are required"))
		}
		file, err := os.Open(*filePath)
		if err != nil {
			fail(err)
		}
		defer file.Close()
		digest, err := client.UploadPersonalAttachment(ctx, *subjectType, *subjectID, *authorID, file)
		if err != nil {
			fail(err)
		}
		emit(map[string]string{"sha256": digest})
	case "download-attachment":
		if strings.TrimSpace(*id) == "" || strings.TrimSpace(*filePath) == "" {
			fail(fmt.Errorf("-id SHA-256 and destination -file are required"))
		}
		data, err := client.DownloadAttachment(ctx, *id)
		if err != nil {
			fail(err)
		}
		if err := os.WriteFile(*filePath, data, 0600); err != nil {
			fail(err)
		}
		emit(map[string]any{"downloaded": true, "bytes": len(data), "sha256": *id})
	case "publish-event":
		if strings.TrimSpace(*topic) == "" || strings.TrimSpace(*payload) == "" {
			fail(fmt.Errorf("-topic and -payload JSON are required"))
		}
		var value any
		if err := json.Unmarshal([]byte(*payload), &value); err != nil {
			fail(fmt.Errorf("-payload must be JSON: %w", err))
		}
		if err := client.PublishEvent(ctx, *topic, value); err != nil {
			fail(err)
		}
		emit(map[string]any{"published": true, "topic": *topic})
	default:
		fail(fmt.Errorf("unknown action %q", *action))
	}
}
