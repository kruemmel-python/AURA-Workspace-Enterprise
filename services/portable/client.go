// Package portable is the platform-neutral AURA API adapter.  It contains no
// Win32, SQLite, or cgo dependencies and can therefore be embedded by Linux
// and Android front ends as well as command-line integration tooling.
package portable

import (
	"bytes"
	"context"
	sha256pkg "crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"
)

type Client struct {
	BaseURL, Token string
	HTTP           *http.Client
}

type EventHistoryEntry struct {
	ID    int64  `json:"id"`
	Topic string `json:"topic"`
	At    string `json:"at"`
}

func (client Client) EventHistory(ctx context.Context) ([]EventHistoryEntry, error) {
	var entries []EventHistoryEntry
	err := client.request(ctx, http.MethodGet, "/api/v1/admin/event-history", nil, &entries)
	return entries, err
}

// DefaultHTTPClient preserves certificate verification.  If the deployment
// uses a private enterprise CA, AURA_SYNC_CA_CERT may point at its PEM file.
func DefaultHTTPClient(timeout time.Duration) (*http.Client, error) {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if caPath := strings.TrimSpace(os.Getenv("AURA_SYNC_CA_CERT")); caPath != "" {
		pem, err := os.ReadFile(caPath)
		if err != nil {
			return nil, fmt.Errorf("read AURA_SYNC_CA_CERT: %w", err)
		}
		roots, err := x509.SystemCertPool()
		if err != nil || roots == nil {
			roots = x509.NewCertPool()
		}
		if !roots.AppendCertsFromPEM(pem) {
			return nil, fmt.Errorf("AURA_SYNC_CA_CERT contains no PEM certificate")
		}
		transport.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: roots}
	}
	return &http.Client{Timeout: timeout, Transport: transport}, nil
}

type LeaveRequest struct {
	IdentityVerified       *bool  `json:"identityVerified,omitempty"`
	IdentityOriginVerified *bool  `json:"identityOriginVerified,omitempty"`
	EmployeeExternalID     string `json:"employeeExternalId,omitempty"`
	DeciderExternalID      string `json:"deciderExternalId,omitempty"`
	ID                     string `json:"id"`
	EmployeeID             int64  `json:"employeeId"`
	StartsAt               int64  `json:"startsAt"`
	EndsAt                 int64  `json:"endsAt"`
	Reason                 string `json:"reason"`
	Status                 string `json:"status"`
	DecidedBy              int64  `json:"decidedBy,omitempty"`
}

// LeaveSyncOperation is an idempotent request or decision, attributed to the token identity.
type LeaveSyncOperation struct {
	ClientID           string        `json:"clientId"`
	OperationID        string        `json:"operationId"`
	Action             string        `json:"action"`
	Leave              LeaveRequest  `json:"leave"`
	Expected           *LeaveRequest `json:"expected"`
	EmployeeExternalID string        `json:"employeeExternalId,omitempty"`
	DeciderExternalID  string        `json:"deciderExternalId,omitempty"`
}

type LeaveSyncAbandonment struct {
	Operation LeaveSyncOperation `json:"operation"`
	Remote    LeaveRequest       `json:"remote"`
}

func (client Client) AbandonLeaveSync(ctx context.Context, value LeaveSyncAbandonment) error {
	capabilities, err := client.Capabilities(ctx)
	if err != nil {
		return err
	}
	if capabilities.LeaveRequestReconciliation != 1 {
		return fmt.Errorf("server does not support leave request reconciliation")
	}
	return client.request(ctx, http.MethodPost, "/api/v1/leave-sync-abandon", value, nil)
}

func (client Client) SyncLeave(ctx context.Context, operation LeaveSyncOperation) error {
	// IdentityVerified is server-provided projection metadata, not an input.
	operation.Leave.IdentityVerified = nil
	operation.Leave.IdentityOriginVerified = nil
	operation.Leave.EmployeeExternalID = ""
	operation.Leave.DeciderExternalID = ""
	if operation.Expected != nil {
		expected := *operation.Expected
		expected.IdentityVerified = nil
		expected.IdentityOriginVerified = nil
		expected.EmployeeExternalID = ""
		expected.DeciderExternalID = ""
		operation.Expected = &expected
	}
	return client.request(ctx, http.MethodPost, "/api/v1/leave-sync", operation, nil)
}

type Task struct {
	ID             string `json:"id"`
	Title          string `json:"title"`
	Department     string `json:"department"`
	Priority       string `json:"priority"`
	Status         string `json:"status"`
	AssigneeID     int64  `json:"assigneeId"`
	CreatedBy      int64  `json:"createdBy"`
	DueAt          int64  `json:"dueAt,omitempty"`
	RecurrenceDays int    `json:"recurrenceDays,omitempty"`
}
type TaskSyncState struct {
	ID             string `json:"id"`
	Title          string `json:"title"`
	Department     string `json:"department"`
	Priority       string `json:"priority"`
	Status         string `json:"status"`
	AssigneeID     int64  `json:"assigneeId"`
	CreatedBy      int64  `json:"createdBy"`
	Active         bool   `json:"active"`
	DueAt          int64  `json:"dueAt"`
	RecurrenceDays int    `json:"recurrenceDays"`
}
type TaskSyncOperation struct {
	ClientID              string         `json:"clientId"`
	OperationID           string         `json:"operationId"`
	Action                string         `json:"action"`
	Task                  TaskSyncState  `json:"task"`
	Expected              *TaskSyncState `json:"expected"`
	RecurrenceSuccessorID string         `json:"recurrenceSuccessorId,omitempty"`
}
type TaskSyncAbandonment struct {
	Operation TaskSyncOperation `json:"operation"`
	Remote    TaskSyncState     `json:"remote"`
}

func (client Client) AbandonTaskSync(ctx context.Context, value TaskSyncAbandonment) error {
	capabilities, err := client.Capabilities(ctx)
	if err != nil {
		return err
	}
	if capabilities.TaskIdentityReconciliation != 1 {
		return fmt.Errorf("server does not support task identity reconciliation")
	}
	return client.request(ctx, http.MethodPost, "/api/v1/task-sync-abandon", value, nil)
}

type TaskRecurrenceReservation struct {
	ClientID    string            `json:"clientId"`
	OperationID string            `json:"operationId"`
	Original    TaskSyncOperation `json:"original"`
	SuccessorID string            `json:"successorId"`
}

func (client Client) ReserveTaskRecurrence(ctx context.Context, value TaskRecurrenceReservation) error {
	return client.request(ctx, http.MethodPost, "/api/v1/task-recurrence-reserve", value, nil)
}

type TaskRecurrenceLink struct {
	ClientID    string `json:"clientId"`
	OperationID string `json:"operationId"`
	SourceID    string `json:"sourceId"`
	SuccessorID string `json:"successorId"`
}

func (client Client) LinkTaskRecurrence(ctx context.Context, value TaskRecurrenceLink) error {
	return client.request(ctx, http.MethodPost, "/api/v1/task-recurrence-link", value, nil)
}
func (client Client) TaskRecurrenceLinks(ctx context.Context) ([]TaskRecurrenceLink, error) {
	var links []TaskRecurrenceLink
	err := client.request(ctx, http.MethodGet, "/api/v1/task-recurrence-link", nil, &links)
	return links, err
}

func (client Client) SyncTask(ctx context.Context, operation TaskSyncOperation) error {
	return client.request(ctx, http.MethodPost, "/api/v1/task-sync", operation, nil)
}
func (client Client) TasksForSync(ctx context.Context) ([]TaskSyncState, error) {
	var result []TaskSyncState
	err := client.request(ctx, http.MethodGet, "/api/v1/tasks?includeArchived=true", nil, &result)
	return result, err
}

type Ticket struct {
	ID         string `json:"id"`
	Title      string `json:"title"`
	Category   string `json:"category"`
	Priority   string `json:"priority"`
	Status     string `json:"status"`
	AssigneeID int64  `json:"assigneeId"`
	CreatedBy  int64  `json:"createdBy"`
	Active     bool   `json:"active"`
}
type Room struct {
	ID           string `json:"id"`
	Name         string `json:"name"`
	Location     string `json:"location"`
	Capacity     int    `json:"capacity"`
	Equipment    string `json:"equipment"`
	Active       bool   `json:"active"`
	Occupied     *bool  `json:"occupied,omitempty"`
	Availability string `json:"availability,omitempty"`
}
type Project struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	Department  string `json:"department"`
	Description string `json:"description"`
	Status      string `json:"status"`
	OwnerID     int64  `json:"ownerId"`
	Active      bool   `json:"active"`
}
type TicketSyncOperation struct {
	ClientID    string  `json:"clientId"`
	OperationID string  `json:"operationId"`
	Action      string  `json:"action"`
	Ticket      Ticket  `json:"ticket"`
	Expected    *Ticket `json:"expected"`
}

func (client Client) SyncTicket(ctx context.Context, v TicketSyncOperation) error {
	return client.request(ctx, http.MethodPost, "/api/v1/ticket-sync", v, nil)
}
func (client Client) TicketsForSync(ctx context.Context) ([]Ticket, error) {
	var result []Ticket
	err := client.request(ctx, http.MethodGet, "/api/v1/ticket-sync", nil, &result)
	return result, err
}

type ProjectSyncOperation struct {
	ClientID    string   `json:"clientId"`
	OperationID string   `json:"operationId"`
	Action      string   `json:"action"`
	Project     Project  `json:"project"`
	Expected    *Project `json:"expected"`
}

func (client Client) SyncProject(ctx context.Context, v ProjectSyncOperation) error {
	return client.request(ctx, http.MethodPost, "/api/v1/project-sync", v, nil)
}
func (client Client) ProjectsForSync(ctx context.Context) ([]Project, error) {
	var result []Project
	err := client.request(ctx, http.MethodGet, "/api/v1/project-sync", nil, &result)
	return result, err
}

type FacilityAsset struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Category string `json:"category"`
	Location string `json:"location"`
	Status   string `json:"status"`
	Active   bool   `json:"active"`
}

func (client Client) LinkMaintenanceRecurrence(ctx context.Context, value TaskRecurrenceLink) error {
	return client.request(ctx, http.MethodPost, "/api/v1/maintenance-recurrence-link", value, nil)
}
func (client Client) MaintenanceRecurrenceLinks(ctx context.Context) ([]TaskRecurrenceLink, error) {
	var result []TaskRecurrenceLink
	err := client.request(ctx, http.MethodGet, "/api/v1/maintenance-recurrence-link", nil, &result)
	return result, err
}

type MaintenanceSyncOperation struct {
	ClientID              string            `json:"clientId"`
	OperationID           string            `json:"operationId"`
	Action                string            `json:"action"`
	MaintenanceOrder      MaintenanceOrder  `json:"maintenanceOrder"`
	Expected              *MaintenanceOrder `json:"expected"`
	RecurrenceSuccessorID string            `json:"recurrenceSuccessorId,omitempty"`
}

func (client Client) SyncMaintenance(ctx context.Context, v MaintenanceSyncOperation) error {
	return client.request(ctx, http.MethodPost, "/api/v1/maintenance-sync", v, nil)
}
func (client Client) MaintenanceForSync(ctx context.Context) ([]MaintenanceOrder, error) {
	var result []MaintenanceOrder
	err := client.request(ctx, http.MethodGet, "/api/v1/maintenance-sync", nil, &result)
	return result, err
}

type FacilityAssetSyncOperation struct {
	ClientID      string         `json:"clientId"`
	OperationID   string         `json:"operationId"`
	Action        string         `json:"action"`
	FacilityAsset FacilityAsset  `json:"facilityAsset"`
	Expected      *FacilityAsset `json:"expected"`
}

func (client Client) SyncFacilityAsset(ctx context.Context, v FacilityAssetSyncOperation) error {
	return client.request(ctx, http.MethodPost, "/api/v1/facility-asset-sync", v, nil)
}
func (client Client) FacilityAssetsForSync(ctx context.Context) ([]FacilityAsset, error) {
	var result []FacilityAsset
	err := client.request(ctx, http.MethodGet, "/api/v1/facility-asset-sync", nil, &result)
	return result, err
}

type MaintenanceOrder struct {
	ID             string `json:"id"`
	AssetID        string `json:"assetId"`
	Title          string `json:"title"`
	Status         string `json:"status"`
	DueAt          int64  `json:"dueAt"`
	RecurrenceDays int    `json:"recurrenceDays"`
	Active         bool   `json:"active"`
}

func maintenancePath(asset, id string) string {
	q := url.Values{"assetId": {asset}}
	if id != "" {
		q.Set("id", id)
	}
	return "/api/v1/maintenance?" + q.Encode()
}
func (client Client) Maintenance(ctx context.Context, asset string) ([]MaintenanceOrder, error) {
	var result []MaintenanceOrder
	err := client.request(ctx, http.MethodGet, maintenancePath(asset, ""), nil, &result)
	return result, err
}
func (client Client) SaveMaintenance(ctx context.Context, v MaintenanceOrder, create bool) (MaintenanceOrder, error) {
	method := http.MethodPut
	if create {
		method = http.MethodPost
	}
	var result MaintenanceOrder
	err := client.request(ctx, method, maintenancePath(v.AssetID, v.ID), v, &result)
	return result, err
}
func (client Client) MaintenanceStatus(ctx context.Context, asset, id, status string) error {
	return client.request(ctx, http.MethodPatch, maintenancePath(asset, id), map[string]string{"status": status}, nil)
}
func (client Client) ArchiveMaintenance(ctx context.Context, asset, id string) error {
	return client.request(ctx, http.MethodDelete, maintenancePath(asset, id), nil, nil)
}

type ServerCapabilities struct {
	PersonalPresenceSync             int `json:"personalPresenceSync"`
	CanonicalRoomBooking             int `json:"canonicalRoomBooking"`
	BookingSync                      int `json:"bookingSync"`
	LegacyBookingReconciliation      int `json:"legacyBookingReconciliation"`
	BookingBlockSync                 int `json:"bookingBlockSync"`
	RoomSync                         int `json:"roomSync"`
	RoomIdentityReconciliation       int `json:"roomIdentityReconciliation"`
	EmployeeSync                     int `json:"employeeSync"`
	EmployeeIdentityReconciliation   int `json:"employeeIdentityReconciliation"`
	LeaveSync                        int `json:"leaveSync"`
	LeaveRequestReconciliation       int `json:"leaveRequestReconciliation"`
	LeaveIdentityVerified            int `json:"leaveIdentityVerified"`
	LeaveIdentityBinding             int `json:"leaveIdentityBinding"`
	LeaveSnapshotIdentityBinding     int `json:"leaveSnapshotIdentityBinding"`
	LeaveDecisionCompare             int `json:"leaveDecisionCompare"`
	TaskSync                         int `json:"taskSync"`
	TaskIdentityReconciliation       int `json:"taskIdentityReconciliation"`
	TaskRecurrenceLink               int `json:"taskRecurrenceLink"`
	TaskRecurrenceReservation        int `json:"taskRecurrenceReservation"`
	TaskRecurrencePreflight          int `json:"taskRecurrencePreflight"`
	SubtaskSync                      int `json:"subtaskSync"`
	SubtaskIdentityReconciliation    int `json:"subtaskIdentityReconciliation"`
	CentralSubtaskCompare            int `json:"centralSubtaskCompare"`
	CentralSubtaskArchiveCompare     int `json:"centralSubtaskArchiveCompare"`
	DependencySync                   int `json:"dependencySync"`
	ProjectSync                      int `json:"projectSync"`
	ProjectAssignmentSync            int `json:"projectAssignmentSync"`
	TaskProjectCreate                int `json:"taskProjectCreate"`
	TaskProjectUpdate                int `json:"taskProjectUpdate"`
	TaskCommentSync                  int `json:"taskCommentSync"`
	PersonalTaskCommentSync          int `json:"personalTaskCommentSync"`
	PersonalCommentPull              int `json:"personalCommentPull"`
	TicketSync                       int `json:"ticketSync"`
	TicketCommentSync                int `json:"ticketCommentSync"`
	PersonalTicketCommentSync        int `json:"personalTicketCommentSync"`
	AttachmentSync                   int `json:"attachmentSync"`
	PersonalAttachmentSync           int `json:"personalAttachmentSync"`
	PersonalAttachmentArchive        int `json:"personalAttachmentArchive"`
	PersonalAttachmentPull           int `json:"personalAttachmentPull"`
	PersonalAttachmentStates         int `json:"personalAttachmentStates"`
	AttachmentRestoreSync            int `json:"attachmentRestoreSync"`
	OrganizationSync                 int `json:"organizationSync"`
	FacilityAssetSync                int `json:"facilityAssetSync"`
	MaintenanceSync                  int `json:"maintenanceSync"`
	MaintenanceRecurrenceLink        int `json:"maintenanceRecurrenceLink"`
	MaintenanceRecurrenceReservation int `json:"maintenanceRecurrenceReservation"`
	ApprovalSync                     int `json:"approvalSync"`
}

func (client Client) Capabilities(ctx context.Context) (ServerCapabilities, error) {
	var v ServerCapabilities
	err := client.request(ctx, http.MethodGet, "/api/v1/capabilities", nil, &v)
	return v, err
}

type SyncIdentity struct {
	EmployeeID         int64 `json:"employeeId"`
	CanManageRooms     bool  `json:"canManageRooms"`
	CanBookRooms       bool  `json:"canBookRooms"`
	CanManageEmployees bool  `json:"canManageEmployees"`
	CanManageTasks     bool  `json:"canManageTasks"`
	CanReadTasks       bool  `json:"canReadTasks"`
	CanManageTickets   bool  `json:"canManageTickets"`
	CanReadTickets     bool  `json:"canReadTickets"`
	CanCollaborate     bool  `json:"canCollaborate"`
	CanManageFacility  bool  `json:"canManageFacility"`
	CanManageProjects  bool  `json:"canManageProjects"`
	CanRequestLeave    bool  `json:"canRequestLeave"`
	CanApproveLeave    bool  `json:"canApproveLeave"`
}

func (client Client) Identity(ctx context.Context) (SyncIdentity, error) {
	var v SyncIdentity
	err := client.request(ctx, http.MethodGet, "/api/v1/identity", nil, &v)
	return v, err
}

// RoomSyncOperation carries an immutable operation ID and the server state on
// which an edit was based. A 409 response requires explicit reconciliation.
type RoomSyncOperation struct {
	ClientID    string `json:"clientId"`
	OperationID string `json:"operationId"`
	Action      string `json:"action"`
	Room        Room   `json:"room"`
	Expected    *Room  `json:"expected"`
}

func (client Client) SyncRoom(ctx context.Context, operation RoomSyncOperation) error {
	operation.Room.Occupied = nil
	operation.Room.Availability = ""
	if operation.Expected != nil {
		expected := *operation.Expected
		expected.Occupied = nil
		expected.Availability = ""
		operation.Expected = &expected
	}
	return client.request(ctx, http.MethodPost, "/api/v1/room-sync", operation, nil)
}

type RoomSyncAbandonment struct {
	Operation RoomSyncOperation `json:"operation"`
	Remote    Room              `json:"remote"`
}

func (client Client) AbandonRoomSync(ctx context.Context, value RoomSyncAbandonment) error {
	value.Operation.Room.Occupied = nil
	value.Operation.Room.Availability = ""
	if value.Operation.Expected != nil {
		expected := *value.Operation.Expected
		expected.Occupied = nil
		expected.Availability = ""
		value.Operation.Expected = &expected
	}
	value.Remote.Occupied = nil
	value.Remote.Availability = ""
	return client.request(ctx, http.MethodPost, "/api/v1/room-sync-abandon", value, nil)
}

type RoomBooking struct {
	ClientID  string `json:"clientId"`
	BookingID int64  `json:"bookingId"`
	RoomID    int64  `json:"roomId"`
	RoomKey   string `json:"roomKey"`
	UserID    int64  `json:"userId"`
	Attendees int    `json:"attendees"`
	Status    string `json:"status"`
	Reason    string `json:"reason"`
	StartsAt  int64  `json:"startsAt"`
	EndsAt    int64  `json:"endsAt"`
}
type LegacyBookingReconciliation struct {
	Expected           RoomBooking `json:"expected"`
	EmployeeExternalID string      `json:"employeeExternalId"`
	Attendees          int         `json:"attendees"`
}

func (client Client) ReconcileLegacyBooking(ctx context.Context, value LegacyBookingReconciliation) (RoomBooking, error) {
	var result RoomBooking
	err := client.request(ctx, http.MethodPost, "/api/v1/legacy-booking-reconcile", value, &result)
	return result, err
}

type BookingSyncOperation struct {
	ClientID    string       `json:"clientId"`
	OperationID string       `json:"operationId"`
	Action      string       `json:"action"`
	Booking     RoomBooking  `json:"booking"`
	Expected    *RoomBooking `json:"expected"`
}

func (client Client) SyncBooking(ctx context.Context, operation BookingSyncOperation) error {
	return client.request(ctx, http.MethodPost, "/api/v1/booking-sync", operation, nil)
}
func (client Client) AbandonBookingSync(ctx context.Context, operation BookingSyncOperation) error {
	return client.request(ctx, http.MethodPost, "/api/v1/booking-sync-abandon", operation, nil)
}
func (client Client) BookingsForSync(ctx context.Context) ([]RoomBooking, error) {
	var values []RoomBooking
	err := client.request(ctx, http.MethodGet, "/api/v1/booking-sync", nil, &values)
	return values, err
}

type RoomBookingInput struct {
	ClientID     string `json:"clientId"`
	BookingID    int64  `json:"bookingId"`
	StartsAt     int64  `json:"startsAt"`
	EndsAt       int64  `json:"endsAt"`
	Attendees    int    `json:"attendees"`
	Occurrences  int    `json:"occurrences"`
	IntervalDays int    `json:"intervalDays"`
	Blocked      bool   `json:"blocked"`
	Reason       string `json:"reason"`
}

func (client Client) RoomBookings(ctx context.Context, room string) ([]RoomBooking, error) {
	var v []RoomBooking
	err := client.request(ctx, http.MethodGet, "/api/v1/room-bookings?roomKey="+url.QueryEscape(room), nil, &v)
	return v, err
}
func (client Client) ChangeRoomBookings(ctx context.Context, method, room string, v RoomBookingInput) error {
	return client.request(ctx, method, "/api/v1/room-bookings?roomKey="+url.QueryEscape(room), v, nil)
}

type ApprovalSyncRecord struct {
	ID          string `json:"id"`
	TaskID      string `json:"taskId"`
	RequestedBy string `json:"requestedBy"`
	ApproverID  string `json:"approverId"`
	Status      string `json:"status"`
	DecidedBy   string `json:"decidedBy"`
	Active      bool   `json:"active"`
}
type ApprovalSyncOperation struct {
	ClientID    string              `json:"clientId"`
	OperationID string              `json:"operationId"`
	Action      string              `json:"action"`
	Approval    ApprovalSyncRecord  `json:"approval"`
	Expected    *ApprovalSyncRecord `json:"expected"`
}

func (client Client) ApprovalsForSync(ctx context.Context) ([]ApprovalSyncRecord, error) {
	var result []ApprovalSyncRecord
	err := client.request(ctx, http.MethodGet, "/api/v1/approval-sync", nil, &result)
	return result, err
}
func (client Client) SyncApproval(ctx context.Context, v ApprovalSyncOperation) error {
	return client.request(ctx, http.MethodPost, "/api/v1/approval-sync", v, nil)
}

type Approval struct {
	ID          string `json:"id"`
	TaskID      string `json:"taskId"`
	TaskTitle   string `json:"taskTitle"`
	RequesterID string `json:"requesterId"`
	ApproverID  string `json:"approverId"`
	Status      string `json:"status"`
	DecidedBy   string `json:"decidedBy"`
}

func (client Client) Approvals(ctx context.Context) ([]Approval, error) {
	var v []Approval
	err := client.request(ctx, http.MethodGet, "/api/v1/task-approvals", nil, &v)
	return v, err
}
func (client Client) RequestApproval(ctx context.Context, task string, approver int64) error {
	return client.request(ctx, http.MethodPost, "/api/v1/task-approvals", map[string]any{"taskId": task, "approverId": approver}, nil)
}
func (client Client) DecideApproval(ctx context.Context, id, status string) error {
	return client.request(ctx, http.MethodPatch, "/api/v1/task-approvals?id="+url.QueryEscape(id), map[string]string{"status": status}, nil)
}

type Organization struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	Code      string `json:"code"`
	ManagerID int64  `json:"managerId"`
	Active    bool   `json:"active"`
}

type OrganizationSyncOperation struct {
	ClientID     string        `json:"clientId"`
	OperationID  string        `json:"operationId"`
	Action       string        `json:"action"`
	Organization Organization  `json:"organization"`
	Expected     *Organization `json:"expected"`
}

func (client Client) SyncOrganization(ctx context.Context, v OrganizationSyncOperation) error {
	return client.request(ctx, http.MethodPost, "/api/v1/organization-sync", v, nil)
}
func (client Client) OrganizationsForSync(ctx context.Context) ([]Organization, error) {
	var result []Organization
	err := client.request(ctx, http.MethodGet, "/api/v1/organization-sync", nil, &result)
	return result, err
}

func (client Client) Organizations(ctx context.Context) ([]Organization, error) {
	var result []Organization
	err := client.request(ctx, http.MethodGet, "/api/v1/organizations", nil, &result)
	return result, err
}
func (client Client) SaveOrganization(ctx context.Context, v Organization, create bool) (Organization, error) {
	method := http.MethodPut
	path := "/api/v1/organizations?id=" + url.QueryEscape(v.ID)
	if create {
		method = http.MethodPost
		path = "/api/v1/organizations"
	}
	var result Organization
	err := client.request(ctx, method, path, v, &result)
	return result, err
}
func (client Client) ArchiveOrganization(ctx context.Context, id string) error {
	return client.request(ctx, http.MethodDelete, "/api/v1/organizations?id="+url.QueryEscape(id), nil, nil)
}

type Employee struct {
	AccountEmployeeID int64  `json:"accountEmployeeId,omitempty"`
	ID                string `json:"id"`
	Name              string `json:"name"`
	Role              string `json:"role"`
	Department        string `json:"department"`
	Presence          string `json:"presence"`
	Active            bool   `json:"active"`
}

type EmployeeSyncOperation struct {
	ClientID    string    `json:"clientId"`
	OperationID string    `json:"operationId"`
	Action      string    `json:"action"`
	Employee    Employee  `json:"employee"`
	Expected    *Employee `json:"expected"`
}

func (client Client) SyncEmployee(ctx context.Context, operation EmployeeSyncOperation) error {
	return client.request(ctx, http.MethodPost, "/api/v1/employee-sync", operation, nil)
}

type EmployeeSyncAbandonment struct {
	Operation EmployeeSyncOperation `json:"operation"`
	Remote    Employee              `json:"remote"`
}

func (client Client) AbandonEmployeeSync(ctx context.Context, value EmployeeSyncAbandonment) error {
	return client.request(ctx, http.MethodPost, "/api/v1/employee-sync-abandon", value, nil)
}
func (client Client) EmployeesForSync(ctx context.Context) ([]Employee, error) {
	var result []Employee
	err := client.request(ctx, http.MethodGet, "/api/v1/employees?includeArchived=true", nil, &result)
	return result, err
}

type EmployeeAccount struct {
	EmployeeID       int64  `json:"employeeId"`
	LinkedEmployeeID string `json:"linkedEmployeeId"`
	LinkedName       string `json:"linkedName"`
}

func (client Client) EmployeeAccounts(ctx context.Context) ([]EmployeeAccount, error) {
	var result []EmployeeAccount
	err := client.request(ctx, http.MethodGet, "/api/v1/employee-accounts", nil, &result)
	return result, err
}
func (client Client) LinkEmployeeAccount(ctx context.Context, id string, account int64) error {
	return client.request(ctx, http.MethodPut, "/api/v1/employee-account-link?id="+url.QueryEscape(id), map[string]int64{"accountEmployeeId": account}, nil)
}

// OwnPresence reads or updates only the identity associated with this token.
func (client Client) OwnPresence(ctx context.Context) (string, error) {
	var result struct {
		Presence string `json:"presence"`
	}
	err := client.request(ctx, http.MethodGet, "/api/v1/my-presence", nil, &result)
	return result.Presence, err
}
func (client Client) SetOwnPresence(ctx context.Context, expected, presence string) (string, error) {
	var result struct {
		Presence string `json:"presence"`
	}
	err := client.request(ctx, http.MethodPatch, "/api/v1/my-presence", map[string]string{"expected": expected, "presence": presence}, &result)
	return result.Presence, err
}

type OwnPresenceInfo struct {
	Presence   string `json:"presence"`
	EmployeeID string `json:"employeeId"`
	Name       string `json:"name,omitempty"`
	Role       string `json:"role,omitempty"`
}

func (client Client) OwnPresenceDetails(ctx context.Context) (OwnPresenceInfo, error) {
	var result OwnPresenceInfo
	err := client.request(ctx, http.MethodGet, "/api/v1/my-presence", nil, &result)
	return result, err
}
func (client Client) SetOwnPresenceChecked(ctx context.Context, id, expected, presence string) (OwnPresenceInfo, error) {
	var result OwnPresenceInfo
	err := client.request(ctx, http.MethodPatch, "/api/v1/my-presence", map[string]string{"employeeId": id, "expected": expected, "presence": presence}, &result)
	return result, err
}

func (client Client) Employees(ctx context.Context) ([]Employee, error) {
	var result []Employee
	err := client.request(ctx, http.MethodGet, "/api/v1/employees", nil, &result)
	return result, err
}
func (client Client) SaveEmployee(ctx context.Context, v Employee, create bool) (Employee, error) {
	method := http.MethodPut
	path := "/api/v1/employees?id=" + url.QueryEscape(v.ID)
	if create {
		method = http.MethodPost
		path = "/api/v1/employees"
	}
	var result Employee
	err := client.request(ctx, method, path, v, &result)
	return result, err
}
func (client Client) ArchiveEmployee(ctx context.Context, id string) error {
	return client.request(ctx, http.MethodDelete, "/api/v1/employees?id="+url.QueryEscape(id), nil, nil)
}

type SubjectAttachment struct {
	UploaderExternalID string `json:"uploaderExternalId,omitempty"`
	ImportedBy         int64  `json:"importedBy,omitempty"`
	ID                 string `json:"id"`
	SubjectType        string `json:"subjectType"`
	SubjectID          string `json:"subjectId"`
	OriginalName       string `json:"originalName"`
	SizeBytes          int64  `json:"sizeBytes"`
	SHA256             string `json:"sha256"`
	UploadedBy         string `json:"uploadedBy"`
	Active             bool   `json:"active"`
}

type AttachmentSyncOperation struct {
	ClientID        string             `json:"clientId"`
	OperationID     string             `json:"operationId"`
	Action          string             `json:"action"`
	Attachment      SubjectAttachment  `json:"attachment"`
	Expected        *SubjectAttachment `json:"expected"`
	ActorExternalID string             `json:"actorExternalId,omitempty"`
}

func (client Client) AttachmentsForSync(ctx context.Context, kind string) ([]SubjectAttachment, error) {
	var result []SubjectAttachment
	err := client.request(ctx, http.MethodGet, "/api/v1/attachment-sync?subjectType="+url.QueryEscape(kind), nil, &result)
	return result, err
}
func (client Client) SyncAttachment(ctx context.Context, kind string, v AttachmentSyncOperation) error {
	v.Attachment.UploaderExternalID = ""
	if v.Expected != nil {
		expected := *v.Expected
		expected.UploaderExternalID = ""
		v.Expected = &expected
	}
	return client.request(ctx, http.MethodPost, "/api/v1/attachment-sync?subjectType="+url.QueryEscape(kind), v, nil)
}

func (client Client) SyncPersonalAttachment(ctx context.Context, kind string, v AttachmentSyncOperation) error {
	v.Attachment.UploaderExternalID = ""
	if v.Expected != nil {
		expected := *v.Expected
		expected.UploaderExternalID = ""
		v.Expected = &expected
	}
	return client.request(ctx, http.MethodPost, "/api/v1/personal-attachment-sync?subjectType="+url.QueryEscape(kind), v, nil)
}

func attachmentPath(kind, id, attachmentID, name string) string {
	q := url.Values{"subjectType": {kind}, "subjectId": {id}}
	if attachmentID != "" {
		q.Set("id", attachmentID)
	}
	if name != "" {
		q.Set("name", name)
	}
	return "/api/v1/subject-attachments?" + q.Encode()
}
func (client Client) SubjectAttachments(ctx context.Context, kind, id string) ([]SubjectAttachment, error) {
	var result []SubjectAttachment
	err := client.request(ctx, http.MethodGet, attachmentPath(kind, id, "", ""), nil, &result)
	return result, err
}
func (client Client) AllSubjectAttachments(ctx context.Context, kind, id string) ([]SubjectAttachment, error) {
	var result []SubjectAttachment
	err := client.request(ctx, http.MethodGet, attachmentPath(kind, id, "", "")+"&includeArchived=1", nil, &result)
	return result, err
}
func (client Client) RestoreSubjectAttachment(ctx context.Context, kind, id string, expected SubjectAttachment) error {
	expected.UploaderExternalID = ""
	return client.request(ctx, http.MethodPost, attachmentPath(kind, id, expected.ID, ""), expected, nil)
}
func (client Client) ArchiveSubjectAttachment(ctx context.Context, kind, id, attachmentID string) error {
	return client.request(ctx, http.MethodDelete, attachmentPath(kind, id, attachmentID, ""), nil, nil)
}
func (client Client) subjectAttachmentTransfer(ctx context.Context, method, path string, body io.Reader) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, method, strings.TrimRight(client.BaseURL, "/")+path, body)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+client.Token)
	c := client.HTTP
	if c == nil {
		c, err = DefaultHTTPClient(60 * time.Second)
		if err != nil {
			return nil, err
		}
	}
	resp, err := c.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return nil, fmt.Errorf("attachment: %s: %s", resp.Status, strings.TrimSpace(string(b)))
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, 100*1024*1024+1))
	if err != nil {
		return nil, err
	}
	if len(data) > 100*1024*1024 {
		return nil, fmt.Errorf("attachment too large")
	}
	return data, nil
}
func (client Client) UploadSubjectAttachment(ctx context.Context, kind, id, name string, body io.Reader) (SubjectAttachment, error) {
	var v SubjectAttachment
	data, err := client.subjectAttachmentTransfer(ctx, http.MethodPut, attachmentPath(kind, id, "", name), body)
	if err == nil {
		err = json.Unmarshal(data, &v)
	}
	return v, err
}
func (client Client) DownloadSubjectAttachment(ctx context.Context, kind, id, attachmentID, digest string, size int64) ([]byte, error) {
	data, err := client.subjectAttachmentTransfer(ctx, http.MethodGet, attachmentPath(kind, id, attachmentID, ""), nil)
	if err != nil {
		return nil, err
	}
	sum := sha256pkg.Sum256(data)
	if hex.EncodeToString(sum[:]) != digest || int64(len(data)) != size {
		return nil, fmt.Errorf("attachment integrity mismatch")
	}
	return data, nil
}

func personalAttachmentPath(kind, id, attachmentID, viewer string) string {
	q := url.Values{"subjectType": {kind}, "subjectId": {id}, "viewerEmployeeId": {viewer}}
	if attachmentID != "" {
		q.Set("id", attachmentID)
	}
	return "/api/v1/personal-subject-attachments?" + q.Encode()
}
func (client Client) PersonalSubjectAttachments(ctx context.Context, kind, id, viewer string) ([]SubjectAttachment, error) {
	var result []SubjectAttachment
	err := client.request(ctx, http.MethodGet, personalAttachmentPath(kind, id, "", viewer), nil, &result)
	return result, err
}

type PersonalAttachmentState struct {
	ID                 string `json:"id"`
	Active             bool   `json:"active"`
	UploaderExternalID string `json:"uploaderExternalId"`
}

func (client Client) PersonalAttachmentStates(ctx context.Context, kind, id, viewer string, known []SubjectAttachment) ([]PersonalAttachmentState, error) {
	for i := range known {
		known[i].UploaderExternalID = ""
	}
	var result []PersonalAttachmentState
	err := client.request(ctx, http.MethodPost, "/api/v1/personal-attachment-states", map[string]any{"subjectType": kind, "subjectId": id, "viewerEmployeeId": viewer, "known": known}, &result)
	return result, err
}
func (client Client) DownloadPersonalSubjectAttachment(ctx context.Context, kind, id, attachmentID, viewer, digest string, size int64) ([]byte, error) {
	data, err := client.subjectAttachmentTransfer(ctx, http.MethodGet, personalAttachmentPath(kind, id, attachmentID, viewer), nil)
	if err != nil {
		return nil, err
	}
	sum := sha256pkg.Sum256(data)
	if hex.EncodeToString(sum[:]) != digest || int64(len(data)) != size {
		return nil, fmt.Errorf("attachment integrity mismatch")
	}
	return data, nil
}

type Comment struct {
	ImportedBy       int64  `json:"importedBy,omitempty"`
	AuthorExternalID string `json:"authorExternalId,omitempty"`
	ID               string `json:"id"`
	SubjectType      string `json:"subjectType"`
	SubjectID        string `json:"subjectId"`
	AuthorID         string `json:"authorId"`
	Body             string `json:"body"`
	CreatedAt        string `json:"createdAt"`
}

type TicketCommentSyncOperation struct {
	ClientID    string  `json:"clientId"`
	OperationID string  `json:"operationId"`
	Comment     Comment `json:"comment"`
}

func (client Client) TicketCommentsForSync(ctx context.Context) ([]Comment, error) {
	var result []Comment
	err := client.request(ctx, http.MethodGet, "/api/v1/ticket-comment-sync", nil, &result)
	return result, err
}
func (client Client) SyncTicketComment(ctx context.Context, v TicketCommentSyncOperation) error {
	v.Comment.AuthorExternalID = ""
	return client.request(ctx, http.MethodPost, "/api/v1/ticket-comment-sync", v, nil)
}

func (client Client) SyncPersonalTicketComment(ctx context.Context, v TicketCommentSyncOperation) error {
	v.Comment.AuthorExternalID = ""
	return client.request(ctx, http.MethodPost, "/api/v1/personal-ticket-comment-sync", v, nil)
}

type TaskCommentSyncOperation struct {
	ClientID    string  `json:"clientId"`
	OperationID string  `json:"operationId"`
	Comment     Comment `json:"comment"`
}

func (client Client) TaskCommentsForSync(ctx context.Context) ([]Comment, error) {
	var result []Comment
	err := client.request(ctx, http.MethodGet, "/api/v1/task-comment-sync", nil, &result)
	return result, err
}
func (client Client) SyncTaskComment(ctx context.Context, v TaskCommentSyncOperation) error {
	v.Comment.AuthorExternalID = ""
	return client.request(ctx, http.MethodPost, "/api/v1/task-comment-sync", v, nil)
}

func (client Client) SyncPersonalTaskComment(ctx context.Context, v TaskCommentSyncOperation) error {
	v.Comment.AuthorExternalID = ""
	return client.request(ctx, http.MethodPost, "/api/v1/personal-task-comment-sync", v, nil)
}

func (client Client) Comments(ctx context.Context, kind, id string) ([]Comment, error) {
	var result []Comment
	err := client.request(ctx, http.MethodGet, "/api/v1/comments?"+url.Values{"subjectType": {kind}, "subjectId": {id}}.Encode(), nil, &result)
	return result, err
}
func (client Client) PersonalComments(ctx context.Context, kind, id, viewer string) ([]Comment, error) {
	var result []Comment
	err := client.request(ctx, http.MethodGet, "/api/v1/personal-comments?"+url.Values{"subjectType": {kind}, "subjectId": {id}, "viewerEmployeeId": {viewer}}.Encode(), nil, &result)
	return result, err
}
func (client Client) AddComment(ctx context.Context, kind, id, body string) (Comment, error) {
	var result Comment
	err := client.request(ctx, http.MethodPost, "/api/v1/comments?"+url.Values{"subjectType": {kind}, "subjectId": {id}}.Encode(), map[string]string{"body": body}, &result)
	return result, err
}

type Change struct {
	Topic   string `json:"topic"`
	Payload string `json:"payload"`
	At      string `json:"at"`
}
type ChangeFeed struct {
	Events     []Change `json:"events"`
	NextCursor int      `json:"nextCursor"`
}

func (client Client) request(ctx context.Context, method, path string, input any, output any) error {
	if strings.TrimSpace(client.BaseURL) == "" || strings.TrimSpace(client.Token) == "" {
		return fmt.Errorf("base URL and token are required")
	}
	var body io.Reader
	if input != nil {
		encoded, err := json.Marshal(input)
		if err != nil {
			return err
		}
		body = bytes.NewReader(encoded)
	}
	request, err := http.NewRequestWithContext(ctx, method, strings.TrimRight(client.BaseURL, "/")+path, body)
	if err != nil {
		return err
	}
	request.Header.Set("Authorization", "Bearer "+client.Token)
	request.Header.Set("Accept", "application/json")
	if input != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	httpClient := client.HTTP
	if httpClient == nil {
		var clientErr error
		httpClient, clientErr = DefaultHTTPClient(15 * time.Second)
		if clientErr != nil {
			return clientErr
		}
	}
	response, err := httpClient.Do(request)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		limited, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
		return fmt.Errorf("AURA API %s %s: %s: %s", method, path, response.Status, strings.TrimSpace(string(limited)))
	}
	if output != nil && response.StatusCode != http.StatusNoContent {
		return json.NewDecoder(response.Body).Decode(output)
	}
	return nil
}

func (client Client) CreateLeave(ctx context.Context, startsAt, endsAt int64, reason string) (LeaveRequest, error) {
	var result LeaveRequest
	err := client.request(ctx, http.MethodPost, "/api/v1/leave-requests", map[string]any{"startsAt": startsAt, "endsAt": endsAt, "reason": reason}, &result)
	return result, err
}

type LegacyLeaveReconciliation struct {
	Expected           LeaveRequest `json:"expected"`
	EmployeeExternalID string       `json:"employeeExternalId"`
	DeciderExternalID  string       `json:"deciderExternalId"`
}

func (client Client) ReconcileLegacyLeave(ctx context.Context, value LegacyLeaveReconciliation) (LeaveRequest, error) {
	value.Expected.IdentityVerified = nil
	value.Expected.IdentityOriginVerified = nil
	value.Expected.EmployeeExternalID = ""
	value.Expected.DeciderExternalID = ""
	var result LeaveRequest
	err := client.request(ctx, http.MethodPost, "/api/v1/legacy-leave-reconcile", value, &result)
	return result, err
}
func (client Client) DecideLeave(ctx context.Context, id, decision string) (LeaveRequest, error) {
	var result LeaveRequest
	err := client.request(ctx, http.MethodPost, "/api/v1/leave-requests/"+id+"/decision", map[string]string{"decision": decision}, &result)
	return result, err
}
func (client Client) DecideLeaveCompared(ctx context.Context, expected LeaveRequest, decision string) (LeaveRequest, error) {
	var result LeaveRequest
	capabilities, err := client.Capabilities(ctx)
	if err != nil {
		return result, err
	}
	if capabilities.LeaveDecisionCompare != 1 {
		return result, fmt.Errorf("server does not support safe leave decision comparison")
	}
	employee := expected.EmployeeExternalID
	expected.IdentityVerified = nil
	expected.IdentityOriginVerified = nil
	expected.EmployeeExternalID = ""
	expected.DeciderExternalID = ""
	err = client.request(ctx, http.MethodPost, "/api/v1/leave-requests/"+url.PathEscape(expected.ID)+"/decision", map[string]any{"decision": decision, "expected": expected, "expectedEmployeeExternalId": employee}, &result)
	return result, err
}
func (client Client) Leaves(ctx context.Context) ([]LeaveRequest, error) {
	var result []LeaveRequest
	err := client.request(ctx, http.MethodGet, "/api/v1/leave-requests", nil, &result)
	return result, err
}
func (client Client) Tasks(ctx context.Context) ([]Task, error) {
	var result []Task
	err := client.request(ctx, http.MethodGet, "/api/v1/tasks", nil, &result)
	return result, err
}

type Subtask struct {
	ID         string `json:"id"`
	TaskID     string `json:"taskId"`
	Title      string `json:"title"`
	Status     string `json:"status"`
	AssigneeID int64  `json:"assigneeId"`
	DueAt      int64  `json:"dueAt"`
	Active     bool   `json:"active"`
}

func (client Client) Subtasks(ctx context.Context, taskID string) ([]Subtask, error) {
	var result []Subtask
	err := client.request(ctx, http.MethodGet, "/api/v1/task-subtasks?taskId="+url.QueryEscape(taskID), nil, &result)
	return result, err
}
func (client Client) SaveSubtask(ctx context.Context, sub Subtask, create bool) (Subtask, error) {
	method := http.MethodPut
	path := "/api/v1/task-subtasks?taskId=" + url.QueryEscape(sub.TaskID) + "&id=" + url.QueryEscape(sub.ID)
	if create {
		method = http.MethodPost
	}
	input := map[string]any{"title": sub.Title, "status": sub.Status, "assigneeId": sub.AssigneeID, "dueAt": sub.DueAt}
	var result Subtask
	err := client.request(ctx, method, path, input, &result)
	return result, err
}
func (client Client) SaveSubtaskCompared(ctx context.Context, sub, expected Subtask) (Subtask, error) {
	var result Subtask
	capabilities, err := client.Capabilities(ctx)
	if err != nil {
		return result, err
	}
	if capabilities.CentralSubtaskCompare != 1 {
		return result, fmt.Errorf("server does not support safe subtask comparison")
	}
	input := map[string]any{"title": sub.Title, "status": sub.Status, "assigneeId": sub.AssigneeID, "dueAt": sub.DueAt, "expected": expected}
	err = client.request(ctx, http.MethodPut, "/api/v1/task-subtasks?taskId="+url.QueryEscape(sub.TaskID)+"&id="+url.QueryEscape(sub.ID), input, &result)
	return result, err
}
func (client Client) ArchiveSubtask(ctx context.Context, taskID, id string) error {
	return client.request(ctx, http.MethodDelete, "/api/v1/task-subtasks?taskId="+url.QueryEscape(taskID)+"&id="+url.QueryEscape(id), nil, nil)
}

func (client Client) ArchiveSubtaskCompared(ctx context.Context, expected Subtask) error {
	capabilities, err := client.Capabilities(ctx)
	if err != nil {
		return err
	}
	if capabilities.CentralSubtaskArchiveCompare != 1 {
		return fmt.Errorf("server does not support safe subtask archive comparison")
	}
	return client.request(ctx, http.MethodDelete, "/api/v1/task-subtasks?taskId="+url.QueryEscape(expected.TaskID)+"&id="+url.QueryEscape(expected.ID), map[string]any{"expected": expected}, nil)
}

type SubtaskSyncOperation struct {
	ClientID    string   `json:"clientId"`
	OperationID string   `json:"operationId"`
	Action      string   `json:"action"`
	Subtask     Subtask  `json:"subtask"`
	Expected    *Subtask `json:"expected"`
}

type SubtaskSyncAbandonment struct {
	Operation SubtaskSyncOperation `json:"operation"`
	Remote    Subtask              `json:"remote"`
}

func (client Client) AbandonSubtaskSync(ctx context.Context, value SubtaskSyncAbandonment) error {
	capabilities, err := client.Capabilities(ctx)
	if err != nil {
		return err
	}
	if capabilities.SubtaskIdentityReconciliation != 1 {
		return fmt.Errorf("server does not support subtask identity reconciliation")
	}
	return client.request(ctx, http.MethodPost, "/api/v1/subtask-sync-abandon", value, nil)
}

func (client Client) SyncSubtask(ctx context.Context, operation SubtaskSyncOperation) error {
	return client.request(ctx, http.MethodPost, "/api/v1/subtask-sync", operation, nil)
}
func (client Client) SubtasksForSync(ctx context.Context) ([]Subtask, error) {
	var result []Subtask
	err := client.request(ctx, http.MethodGet, "/api/v1/subtask-sync", nil, &result)
	return result, err
}

type ProjectAssignmentSyncEdge struct {
	ProjectID string `json:"projectId"`
	TaskID    string `json:"taskId"`
}
type ProjectAssignmentSyncOperation struct {
	ClientID    string `json:"clientId"`
	OperationID string `json:"operationId"`
	ProjectID   string `json:"projectId"`
	TaskID      string `json:"taskId"`
	Present     bool   `json:"present"`
	Expected    bool   `json:"expected"`
}

func (client Client) SyncProjectAssignment(ctx context.Context, v ProjectAssignmentSyncOperation) error {
	return client.request(ctx, http.MethodPost, "/api/v1/project-assignment-sync", v, nil)
}
func (client Client) ProjectAssignmentsForSync(ctx context.Context) ([]ProjectAssignmentSyncEdge, error) {
	var result []ProjectAssignmentSyncEdge
	err := client.request(ctx, http.MethodGet, "/api/v1/project-assignment-sync", nil, &result)
	return result, err
}

type DependencySyncEdge struct {
	TaskID       string `json:"taskId"`
	DependencyID string `json:"dependencyId"`
}
type DependencySyncOperation struct {
	ClientID     string `json:"clientId"`
	OperationID  string `json:"operationId"`
	TaskID       string `json:"taskId"`
	DependencyID string `json:"dependencyId"`
	Present      bool   `json:"present"`
	Expected     bool   `json:"expected"`
}

func (client Client) SyncDependency(ctx context.Context, v DependencySyncOperation) error {
	return client.request(ctx, http.MethodPost, "/api/v1/dependency-sync", v, nil)
}
func (client Client) DependenciesForSync(ctx context.Context) ([]DependencySyncEdge, error) {
	var result []DependencySyncEdge
	err := client.request(ctx, http.MethodGet, "/api/v1/dependency-sync", nil, &result)
	return result, err
}

type TaskDependency struct {
	ID    string `json:"id"`
	Title string `json:"title"`
}

func (client Client) TaskDependencies(ctx context.Context, taskID string) ([]TaskDependency, error) {
	var result []TaskDependency
	err := client.request(ctx, http.MethodGet, "/api/v1/task-dependencies?taskId="+url.QueryEscape(taskID), nil, &result)
	return result, err
}
func (client Client) ChangeTaskDependency(ctx context.Context, taskID, dependencyID string, add bool) error {
	method := http.MethodDelete
	if add {
		method = http.MethodPost
	}
	return client.request(ctx, method, "/api/v1/task-dependencies?taskId="+url.QueryEscape(taskID)+"&dependencyId="+url.QueryEscape(dependencyID), nil, nil)
}
func (client Client) CreateTask(ctx context.Context, title, department, priority string, assigneeID int64) (Task, error) {
	var result Task
	err := client.request(ctx, http.MethodPost, "/api/v1/tasks", map[string]any{"title": title, "department": department, "priority": priority, "assigneeId": assigneeID}, &result)
	return result, err
}
func (client Client) CreateScheduledTask(ctx context.Context, title, department, priority string, assigneeID, dueAt int64, recurrenceDays int) (Task, error) {
	var result Task
	err := client.request(ctx, http.MethodPost, "/api/v1/tasks", map[string]any{"title": title, "department": department, "priority": priority, "assigneeId": assigneeID, "dueAt": dueAt, "recurrenceDays": recurrenceDays}, &result)
	return result, err
}
func (client Client) CreateScheduledTaskInProject(ctx context.Context, title, department, priority string, assigneeID, dueAt int64, recurrenceDays int, projectID string) (Task, error) {
	var result Task
	capabilities, err := client.Capabilities(ctx)
	if err != nil {
		return result, err
	}
	if capabilities.TaskProjectCreate != 1 {
		return result, fmt.Errorf("server does not support creating tasks with a project")
	}
	err = client.request(ctx, http.MethodPost, "/api/v1/tasks", map[string]any{"title": title, "department": department, "priority": priority, "assigneeId": assigneeID, "dueAt": dueAt, "recurrenceDays": recurrenceDays, "projectId": projectID}, &result)
	return result, err
}
func (client Client) UpdateScheduledTask(ctx context.Context, v Task, dueAt *int64, recurrenceDays *int) (Task, error) {
	payload := map[string]any{"title": v.Title, "department": v.Department, "priority": v.Priority, "assigneeId": v.AssigneeID}
	if dueAt != nil {
		payload["dueAt"] = *dueAt
	}
	if recurrenceDays != nil {
		payload["recurrenceDays"] = *recurrenceDays
	}
	var result Task
	err := client.request(ctx, http.MethodPut, "/api/v1/tasks/"+url.PathEscape(v.ID), payload, &result)
	return result, err
}
func (client Client) UpdateScheduledTaskProject(ctx context.Context, v Task, dueAt *int64, recurrenceDays *int, expectedProject, projectID string) (Task, error) {
	var result Task
	capabilities, err := client.Capabilities(ctx)
	if err != nil {
		return result, err
	}
	if capabilities.TaskProjectUpdate != 1 {
		return result, fmt.Errorf("server does not support editing tasks with a project")
	}
	payload := map[string]any{"title": v.Title, "department": v.Department, "priority": v.Priority, "assigneeId": v.AssigneeID, "expectedProjectId": expectedProject, "projectId": projectID}
	if dueAt != nil {
		payload["dueAt"] = *dueAt
	}
	if recurrenceDays != nil {
		payload["recurrenceDays"] = *recurrenceDays
	}
	err = client.request(ctx, http.MethodPut, "/api/v1/tasks/"+url.PathEscape(v.ID), payload, &result)
	return result, err
}
func (client Client) UpdateTask(ctx context.Context, v Task) (Task, error) {
	var result Task
	err := client.request(ctx, http.MethodPut, "/api/v1/tasks/"+url.PathEscape(v.ID), v, &result)
	return result, err
}
func (client Client) ArchiveTask(ctx context.Context, id string) error {
	return client.request(ctx, http.MethodPost, "/api/v1/tasks/"+url.PathEscape(id)+"/archive", nil, nil)
}
func (client Client) SetTaskStatus(ctx context.Context, id, status string) (Task, error) {
	var result Task
	err := client.request(ctx, http.MethodPost, "/api/v1/tasks/"+id+"/status", map[string]string{"status": status}, &result)
	return result, err
}
func (client Client) Projects(ctx context.Context) ([]Project, error) {
	var result []Project
	err := client.request(ctx, http.MethodGet, "/api/v1/projects", nil, &result)
	return result, err
}
func (client Client) CreateProject(ctx context.Context, name, department, description, status string, ownerID int64) (Project, error) {
	var result Project
	err := client.request(ctx, http.MethodPost, "/api/v1/projects", map[string]any{"name": name, "department": department, "description": description, "status": status, "ownerId": ownerID}, &result)
	return result, err
}
func (client Client) UpdateProject(ctx context.Context, project Project) (Project, error) {
	var result Project
	err := client.request(ctx, http.MethodPut, "/api/v1/projects/"+project.ID, map[string]any{"name": project.Name, "department": project.Department, "description": project.Description, "status": project.Status, "ownerId": project.OwnerID}, &result)
	return result, err
}
func (client Client) ChangeProjectStatus(ctx context.Context, id, expected, status string) (Project, error) {
	var result Project
	err := client.request(ctx, http.MethodPost, "/api/v1/project-status", map[string]string{"id": id, "expected": expected, "status": status}, &result)
	return result, err
}
func (client Client) ArchiveProject(ctx context.Context, id string) error {
	return client.request(ctx, http.MethodPost, "/api/v1/projects/"+id+"/archive", nil, nil)
}
func (client Client) Tickets(ctx context.Context) ([]Ticket, error) {
	var result []Ticket
	err := client.request(ctx, http.MethodGet, "/api/v1/tickets", nil, &result)
	return result, err
}
func (client Client) CreateTicket(ctx context.Context, title, category, priority string, assigneeID int64) (Ticket, error) {
	var result Ticket
	err := client.request(ctx, http.MethodPost, "/api/v1/tickets", map[string]any{"title": title, "category": category, "priority": priority, "assigneeId": assigneeID}, &result)
	return result, err
}
func (client Client) UpdateTicket(ctx context.Context, v Ticket) (Ticket, error) {
	var result Ticket
	err := client.request(ctx, http.MethodPut, "/api/v1/tickets/"+url.PathEscape(v.ID), v, &result)
	return result, err
}
func (client Client) ArchiveTicket(ctx context.Context, id string) error {
	return client.request(ctx, http.MethodPost, "/api/v1/tickets/"+url.PathEscape(id)+"/archive", nil, nil)
}
func (client Client) SetTicketStatus(ctx context.Context, id, status string) (Ticket, error) {
	var result Ticket
	err := client.request(ctx, http.MethodPost, "/api/v1/tickets/"+id+"/status", map[string]string{"status": status}, &result)
	return result, err
}
func (client Client) Rooms(ctx context.Context, includeArchived bool) ([]Room, error) {
	path := "/api/v1/rooms"
	if includeArchived {
		path += "?includeArchived=true"
	}
	var result []Room
	err := client.request(ctx, http.MethodGet, path, nil, &result)
	return result, err
}
func (client Client) RoomsWithAvailability(ctx context.Context) ([]Room, error) {
	var result []Room
	err := client.request(ctx, http.MethodGet, "/api/v1/rooms?availability=true", nil, &result)
	return result, err
}
func (client Client) CreateRoom(ctx context.Context, name, location string, capacity int, equipment string) (Room, error) {
	var result Room
	err := client.request(ctx, http.MethodPost, "/api/v1/rooms", map[string]any{"name": name, "location": location, "capacity": capacity, "equipment": equipment}, &result)
	return result, err
}
func (client Client) UpdateRoom(ctx context.Context, room Room) (Room, error) {
	var result Room
	err := client.request(ctx, http.MethodPut, "/api/v1/rooms/"+room.ID, map[string]any{"name": room.Name, "location": room.Location, "capacity": room.Capacity, "equipment": room.Equipment}, &result)
	return result, err
}
func (client Client) ArchiveRoom(ctx context.Context, id string) error {
	return client.request(ctx, http.MethodPost, "/api/v1/rooms/"+id+"/archive", nil, nil)
}
func (client Client) Changes(ctx context.Context, cursor int) (ChangeFeed, error) {
	var result ChangeFeed
	err := client.request(ctx, http.MethodGet, fmt.Sprintf("/api/v1/sync/changes?cursor=%d", cursor), nil, &result)
	return result, err
}

// PublishEvent exposes the same permission-checked enterprise event contract
// used by native clients. It is intentionally used for entities whose central
// REST resource is not yet required by the portable read model.
func (client Client) PublishEvent(ctx context.Context, topic string, payload any) error {
	encoded, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	return client.request(ctx, http.MethodPost, "/events", map[string]string{"topic": topic, "payload": string(encoded)}, nil)
}

// UploadAttachment transfers a content-addressed binary object to AURA Central.
// The returned SHA-256 is safe to persist in attachment metadata and to use for download.
func (client Client) UploadAttachment(ctx context.Context, content io.Reader) (string, error) {
	data, err := io.ReadAll(io.LimitReader(content, 100*1024*1024+1))
	if err != nil {
		return "", err
	}
	if len(data) > 100*1024*1024 {
		return "", fmt.Errorf("attachment exceeds 100 MB")
	}
	sum := sha256pkg.Sum256(data)
	digest := hex.EncodeToString(sum[:])
	request, err := http.NewRequestWithContext(ctx, http.MethodPut, strings.TrimRight(client.BaseURL, "/")+"/api/v1/attachments/"+digest, bytes.NewReader(data))
	if err != nil {
		return "", err
	}
	request.Header.Set("Authorization", "Bearer "+client.Token)
	request.Header.Set("X-Aura-SHA256", digest)
	request.Header.Set("Content-Type", "application/octet-stream")
	httpClient := client.HTTP
	if httpClient == nil {
		var clientErr error
		httpClient, clientErr = DefaultHTTPClient(15 * time.Second)
		if clientErr != nil {
			return "", clientErr
		}
	}
	response, err := httpClient.Do(request)
	if err != nil {
		return "", err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusCreated && response.StatusCode != http.StatusNoContent {
		return "", fmt.Errorf("attachment upload failed: %s", response.Status)
	}
	return digest, nil
}

func (client Client) UploadPersonalAttachment(ctx context.Context, kind, subject, viewer string, content io.Reader) (string, error) {
	data, err := io.ReadAll(io.LimitReader(content, 100*1024*1024+1))
	if err != nil {
		return "", err
	}
	if len(data) > 100*1024*1024 {
		return "", fmt.Errorf("attachment exceeds 100 MB")
	}
	sum := sha256pkg.Sum256(data)
	digest := hex.EncodeToString(sum[:])
	request, err := http.NewRequestWithContext(ctx, http.MethodPut, strings.TrimRight(client.BaseURL, "/")+"/api/v1/personal-attachment-objects/"+digest+"?"+url.Values{"subjectType": {kind}, "subjectId": {subject}, "viewerEmployeeId": {viewer}}.Encode(), bytes.NewReader(data))
	if err != nil {
		return "", err
	}
	request.Header.Set("Authorization", "Bearer "+client.Token)
	request.Header.Set("X-Aura-SHA256", digest)
	request.Header.Set("Content-Type", "application/octet-stream")
	httpClient := client.HTTP
	if httpClient == nil {
		var clientErr error
		httpClient, clientErr = DefaultHTTPClient(15 * time.Second)
		if clientErr != nil {
			return "", clientErr
		}
	}
	response, err := httpClient.Do(request)
	if err != nil {
		return "", err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusCreated && response.StatusCode != http.StatusNoContent {
		return "", fmt.Errorf("attachment upload failed: %s", response.Status)
	}
	return digest, nil
}

func (client Client) DownloadAttachment(ctx context.Context, sha256 string) ([]byte, error) {
	if len(sha256) != 64 {
		return nil, fmt.Errorf("invalid attachment SHA-256")
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(client.BaseURL, "/")+"/api/v1/attachments/"+sha256, nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Authorization", "Bearer "+client.Token)
	httpClient := client.HTTP
	if httpClient == nil {
		var clientErr error
		httpClient, clientErr = DefaultHTTPClient(15 * time.Second)
		if clientErr != nil {
			return nil, clientErr
		}
	}
	response, err := httpClient.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("attachment download failed: %s", response.Status)
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, 100*1024*1024+1))
	if err != nil {
		return nil, err
	}
	if len(data) > 100*1024*1024 {
		return nil, fmt.Errorf("attachment exceeds 100 MB")
	}
	sum := sha256pkg.Sum256(data)
	if hex.EncodeToString(sum[:]) != sha256 {
		return nil, fmt.Errorf("attachment integrity mismatch")
	}
	return data, nil
}
func (client Client) CreateFacilityAsset(ctx context.Context, asset FacilityAsset) error {
	asset.Active = true
	return client.PublishEvent(ctx, "facility.asset.created", asset)
}
func (client Client) UpdateFacilityAsset(ctx context.Context, asset FacilityAsset) error {
	asset.Active = true
	return client.PublishEvent(ctx, "facility.asset.updated", asset)
}
func (client Client) ArchiveFacilityAsset(ctx context.Context, asset FacilityAsset) error {
	asset.Active = false
	return client.PublishEvent(ctx, "facility.asset.archived", asset)
}
func (client Client) FacilityAssets(ctx context.Context, includeArchived bool) ([]FacilityAsset, error) {
	path := "/api/v1/facility-assets"
	if includeArchived {
		path += "?includeArchived=true"
	}
	var result []FacilityAsset
	err := client.request(ctx, http.MethodGet, path, nil, &result)
	return result, err
}
