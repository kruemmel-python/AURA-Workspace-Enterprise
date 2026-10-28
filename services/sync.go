package main

/*
#include <stdlib.h>
*/
import "C"

import (
	"bufio"
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
	"unsafe"
)

var eventLog = struct {
	sync.RWMutex
	latest string
}{latest: "Synchronisation bereit"}
var centralMirror *centralStore

// Serializes this process's desktop outbox writes and replay. Server journals
// use their own persistence and are not covered by this mutex.
var desktopOutboxMutex sync.Mutex

func setDesktopSyncStatus(code int) {
	message := "Synchronisation abgeschlossen"
	switch code {
	case 0:
		if os.Getenv("AURA_SYNC_URL") == "" {
			message = "Kein Server konfiguriert"
		}
	case 1:
		message = "Synchronisation angehalten: Datei fehlt oder Integrität stimmt nicht"
	case 2, 4:
		message = "Synchronisation angehalten: Warteschlange nicht verfügbar"
	case 3, 9:
		message = "Synchronisation angehalten: ungültige Daten"
	case 5:
		message = "Synchronisation angehalten: Konflikt mit Serverstand"
	case 6:
		message = "Synchronisation angehalten: Zugangsdaten oder Clientkennung fehlen"
	case 7:
		message = "Synchronisation wartet auf Serververbindung"
	default:
		message = "Synchronisation angehalten: Server hat Übertragung abgelehnt"
	}
	eventLog.Lock()
	eventLog.latest = message
	eventLog.Unlock()
}

// centralHTTPClient keeps TLS verification enabled.  A private company CA can
// be supplied explicitly through AURA_SYNC_CA_CERT; public certificates keep
// using the operating-system trust store without additional configuration.
func centralHTTPClient(timeout time.Duration) (*http.Client, error) {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if caPath := strings.TrimSpace(os.Getenv("AURA_SYNC_CA_CERT")); caPath != "" {
		certificate, err := os.ReadFile(caPath)
		if err != nil {
			return nil, fmt.Errorf("read AURA_SYNC_CA_CERT: %w", err)
		}
		roots, err := x509.SystemCertPool()
		if err != nil || roots == nil {
			roots = x509.NewCertPool()
		}
		if !roots.AppendCertsFromPEM(certificate) {
			return nil, fmt.Errorf("AURA_SYNC_CA_CERT contains no PEM certificate")
		}
		transport.TLSClientConfig = &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: roots}
	}
	return &http.Client{Timeout: timeout, Transport: transport}, nil
}

type syncEvent struct {
	Topic       string `json:"topic"`
	Payload     string `json:"payload"`
	At          string `json:"at"`
	ClientID    string `json:"clientId,omitempty"`
	OperationID string `json:"operationId,omitempty"`
}

type desktopAttachmentUpload struct {
	Path     string `json:"path"`
	Metadata string `json:"metadata"`
}

type bookingPayload struct {
	ClientID  string `json:"clientId,omitempty"`
	BookingID int64  `json:"bookingId"`
	RoomID    int64  `json:"roomId"`
	RoomKey   string `json:"roomKey,omitempty"`
	UserID    int64  `json:"userId,omitempty"`
	Attendees int    `json:"attendees,omitempty"`
	Status    string `json:"status,omitempty"`
	Reason    string `json:"reason,omitempty"`
	StartsAt  int64  `json:"startsAt"`
	EndsAt    int64  `json:"endsAt"`
}

type bookingIdentityReconciliation struct {
	Booking      bookingPayload `json:"booking"`
	Original     bookingPayload `json:"original"`
	EmployeeKey  string         `json:"employeeExternalId"`
	ReconciledBy int64          `json:"reconciledBy"`
}

type bookingCheck struct {
	ClientID  string `json:"clientId,omitempty"`
	BookingID int64  `json:"bookingId"`
	RoomID    int64  `json:"roomId"`
	RoomKey   string `json:"roomKey,omitempty"`
	StartsAt  int64  `json:"startsAt"`
	EndsAt    int64  `json:"endsAt"`
}
type serverIdentity struct {
	EmployeeID  int64    `json:"employeeId"`
	Permissions []string `json:"permissions"`
}
type leaveRequest struct {
	ID         string `json:"id"`
	EmployeeID int64  `json:"employeeId"`
	StartsAt   int64  `json:"startsAt"`
	EndsAt     int64  `json:"endsAt"`
	Reason     string `json:"reason"`
	Status     string `json:"status"`
	DecidedBy  int64  `json:"decidedBy,omitempty"`
}

// The marker is written only by authenticated account-based leave endpoints.
type leaveSyncOperation struct {
	ClientID           string        `json:"clientId"`
	OperationID        string        `json:"operationId"`
	Action             string        `json:"action"`
	Leave              leaveRequest  `json:"leave"`
	Expected           *leaveRequest `json:"expected"`
	EmployeeExternalID string        `json:"employeeExternalId,omitempty"`
	DeciderExternalID  string        `json:"deciderExternalId,omitempty"`
}
type leaveAccountEvent struct {
	leaveRequest
	IdentityKind       string `json:"identityKind"`
	EmployeeExternalID string `json:"employeeExternalId,omitempty"`
	DeciderExternalID  string `json:"deciderExternalId,omitempty"`
}

type leaveReconciliationEvent struct {
	leaveRequest
	IdentityKind       string       `json:"identityKind"`
	Original           leaveRequest `json:"original"`
	ReconciledBy       int64        `json:"reconciledBy"`
	EmployeeExternalID string       `json:"employeeExternalId"`
	DeciderExternalID  string       `json:"deciderExternalId"`
}

func encodeLeaveEvent(leave leaveRequest, verified bool) []byte {
	if verified {
		payload, _ := json.Marshal(leaveAccountEvent{leaveRequest: leave, IdentityKind: "server-account-v1"})
		return payload
	}
	payload, _ := json.Marshal(leave)
	return payload
}

func encodeBoundLeaveEvent(leave leaveRequest, employee, decider string) []byte {
	payload, _ := json.Marshal(leaveAccountEvent{leaveRequest: leave, IdentityKind: "server-account-v1", EmployeeExternalID: employee, DeciderExternalID: decider})
	return payload
}

type centralTask struct {
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
type taskSyncOperation struct {
	ClientID              string       `json:"clientId"`
	OperationID           string       `json:"operationId"`
	Action                string       `json:"action"`
	Task                  centralTask  `json:"task"`
	Expected              *centralTask `json:"expected"`
	RecurrenceSuccessorID string       `json:"recurrenceSuccessorId,omitempty"`
}
type centralSubtask struct {
	ID         string `json:"id"`
	TaskID     string `json:"taskId"`
	Title      string `json:"title"`
	Status     string `json:"status"`
	AssigneeID int64  `json:"assigneeId"`
	DueAt      int64  `json:"dueAt"`
	Active     bool   `json:"active"`
}
type subtaskSyncOperation struct {
	ClientID                   string          `json:"clientId"`
	OperationID                string          `json:"operationId"`
	Action                     string          `json:"action"`
	Subtask                    centralSubtask  `json:"subtask"`
	Expected                   *centralSubtask `json:"expected"`
	AssigneeEmployeeID         *string         `json:"assigneeEmployeeId,omitempty"`
	ExpectedAssigneeEmployeeID *string         `json:"expectedAssigneeEmployeeId,omitempty"`
}
type centralTicket struct {
	ID         string `json:"id"`
	Title      string `json:"title"`
	Category   string `json:"category"`
	Priority   string `json:"priority"`
	Status     string `json:"status"`
	AssigneeID int64  `json:"assigneeId"`
	CreatedBy  int64  `json:"createdBy"`
	Active     bool   `json:"active"`
}
type centralRoom struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	Location  string `json:"location"`
	Capacity  int    `json:"capacity"`
	Equipment string `json:"equipment"`
	Active    bool   `json:"active"`
}
type roomSyncOperation struct {
	ClientID    string       `json:"clientId"`
	OperationID string       `json:"operationId"`
	Action      string       `json:"action"`
	Room        centralRoom  `json:"room"`
	Expected    *centralRoom `json:"expected"`
}

type centralProject struct {
	ID          string `json:"id"`
	Name        string `json:"name"`
	Department  string `json:"department"`
	Description string `json:"description"`
	Status      string `json:"status"`
	OwnerID     int64  `json:"ownerId"`
	Active      bool   `json:"active"`
}
type centralEmployee struct {
	AccountEmployeeID int64  `json:"accountEmployeeId,omitempty"`
	ID                string `json:"id"`
	Name              string `json:"name"`
	Role              string `json:"role"`
	Department        string `json:"department"`
	Presence          string `json:"presence"`
	Active            bool   `json:"active"`
}
type employeeSyncOperation struct {
	ClientID    string           `json:"clientId"`
	OperationID string           `json:"operationId"`
	Action      string           `json:"action"`
	Employee    centralEmployee  `json:"employee"`
	Expected    *centralEmployee `json:"expected"`
}

type centralOrganization struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	Code      string `json:"code"`
	ManagerID int64  `json:"managerId"`
	Active    bool   `json:"active"`
}
type centralFacilityAsset struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Category string `json:"category"`
	Location string `json:"location"`
	Status   string `json:"status"`
	Active   bool   `json:"active"`
}
type centralMaintenanceOrder struct {
	ID             string `json:"id"`
	AssetID        string `json:"assetId"`
	Title          string `json:"title"`
	Status         string `json:"status"`
	DueAt          int64  `json:"dueAt"`
	RecurrenceDays int    `json:"recurrenceDays"`
	Active         bool   `json:"active"`
}
type centralTaskApproval struct {
	ID          string `json:"id"`
	TaskID      string `json:"taskId"`
	RequestedBy string `json:"requestedBy"`
	ApproverID  string `json:"approverId"`
	Status      string `json:"status"`
	DecidedBy   string `json:"decidedBy"`
	Active      bool   `json:"active"`
}
type centralComment struct {
	ImportedBy  int64  `json:"importedBy,omitempty"`
	ID          string `json:"id"`
	SubjectType string `json:"subjectType"`
	SubjectID   string `json:"subjectId"`
	AuthorID    string `json:"authorId"`
	Body        string `json:"body"`
	CreatedAt   string `json:"createdAt,omitempty"`
}
type centralAttachment struct {
	ImportedBy   int64  `json:"importedBy,omitempty"`
	ID           string `json:"id"`
	SubjectType  string `json:"subjectType"`
	SubjectID    string `json:"subjectId"`
	OriginalName string `json:"originalName"`
	SizeBytes    int64  `json:"sizeBytes"`
	SHA256       string `json:"sha256"`
	UploadedBy   string `json:"uploadedBy"`
	Active       bool   `json:"active"`
}

func allows(identity serverIdentity, permission string) bool {
	for _, value := range identity.Permissions {
		if value == "*" || value == permission {
			return true
		}
	}
	return false
}
func validTaskTransition(from, to string) bool {
	return (from == "open" && (to == "in_progress" || to == "done" || to == "cancelled")) || (from == "in_progress" && (to == "done" || to == "cancelled" || to == "open")) || (from == "done" && (to == "open" || to == "in_progress")) || (from == "cancelled" && to == "open")
}
func validTaskStatus(status string) bool {
	return status == "open" || status == "in_progress" || status == "done" || status == "cancelled"
}
func validTicketTransition(from, to string) bool {
	return (from == "open" && (to == "in_progress" || to == "closed")) || (from == "in_progress" && (to == "resolved" || to == "closed" || to == "open")) || (from == "resolved" && (to == "closed" || to == "in_progress")) || (from == "closed" && to == "open")
}
func validTicketStatus(status string) bool {
	return status == "open" || status == "in_progress" || status == "resolved" || status == "closed"
}
func validProjectStatus(status string) bool {
	return status == "planned" || status == "active" || status == "on_hold" || status == "completed" || status == "cancelled"
}
func eventPermission(topic string) string {
	switch topic {
	case "booking.created", "booking.rescheduled", "booking.cancelled":
		return "rooms.book"
	case "room.created", "room.updated", "room.archived":
		return "rooms.manage"
	case "task.created", "task.updated", "task.status.changed", "task.archived":
		return "tasks.manage"
	case "task.approval.requested", "task.approval.decided":
		return "tasks.manage"
	case "comment.created":
		return "collaboration.comment"
	case "ticket.created", "ticket.updated", "ticket.status.changed", "ticket.archived":
		return "tickets.manage"
	case "project.created", "project.updated", "project.archived":
		return "projects.manage"
	case "employee.created", "employee.updated", "employee.presence.changed", "employee.archived":
		return "employees.manage"
	case "organization.created", "organization.updated", "organization.archived":
		return "employees.manage"
	case "leave.requested":
		return "leave.request"
	case "leave.decided":
		return "leave.approve"
	case "facility.asset.created", "facility.asset.updated", "facility.asset.archived", "facility.maintenance.created", "facility.maintenance.updated", "facility.maintenance.status.changed", "facility.maintenance.archived":
		return "facility.manage"
	default:
		return "sync.events.write"
	}
}
func configuredIdentities() (map[string]serverIdentity, error) {
	raw := os.Getenv("AURA_SYNC_IDENTITIES")
	if raw == "" {
		return nil, nil
	}
	identities := map[string]serverIdentity{}
	if err := json.Unmarshal([]byte(raw), &identities); err != nil || len(identities) == 0 {
		return nil, fmt.Errorf("AURA_SYNC_IDENTITIES ist ungültig")
	}
	for value, identity := range identities {
		if strings.TrimSpace(value) == "" || identity.EmployeeID <= 0 || len(identity.Permissions) == 0 {
			return nil, fmt.Errorf("AURA_SYNC_IDENTITIES enthält eine ungültige Identität")
		}
	}
	return identities, nil
}
func identityFor(token, presented string) (serverIdentity, bool) {
	if !strings.HasPrefix(presented, "Bearer ") {
		return serverIdentity{}, false
	}
	presented = strings.TrimPrefix(presented, "Bearer ")
	if os.Getenv("AURA_SYNC_IDENTITIES") != "" {
		identities, err := configuredIdentities()
		if err != nil {
			return serverIdentity{}, false
		}
		identity, found := identities[presented]
		if found {
			return identity, true
		}
		return managedIdentity(presented)
	}
	if presented != token {
		return managedIdentity(presented)
	}
	return serverIdentity{EmployeeID: 1, Permissions: []string{"*"}}, true
}

// bookingReservation is a short-lived central lock held while a client writes
// its local transaction.  ReservationID is client-generated and independent
// from the local SQLite booking ID, which is allocated only on commit.
type bookingReservation struct {
	ClientID      string `json:"clientId,omitempty"`
	ReservationID int64  `json:"reservationId"`
	BookingID     int64  `json:"bookingId"`
	RoomID        int64  `json:"roomId"`
	RoomKey       string `json:"roomKey,omitempty"`
	StartsAt      int64  `json:"startsAt"`
	EndsAt        int64  `json:"endsAt"`
	ExpiresAt     int64  `json:"expiresAt,omitempty"`
	EmployeeKey   string `json:"employeeKey,omitempty"`
	Attendees     int    `json:"attendees,omitempty"`
	ActorID       int64  `json:"-"`
}

type bookingCommit struct {
	ClientID      string `json:"clientId,omitempty"`
	ReservationID int64  `json:"reservationId"`
	BookingID     int64  `json:"bookingId"`
	Topic         string `json:"topic"`
}

type bookingRegistry struct {
	sync.Mutex
	active       map[string]bookingPayload
	reservations map[string]bookingReservation
}

func bookingKey(clientID string, bookingID int64) string {
	return clientID + ":" + strconv.FormatInt(bookingID, 10)
}
func reservationKey(clientID string, reservationID int64) string {
	return clientID + ":" + strconv.FormatInt(reservationID, 10)
}
func clientInstanceID() string { return strings.TrimSpace(os.Getenv("AURA_SYNC_CLIENT_ID")) }

func (registry *bookingRegistry) purgeExpiredReservations(now int64) {
	for id, reservation := range registry.reservations {
		if reservation.ExpiresAt <= now {
			delete(registry.reservations, id)
		}
	}
}

// Legacy records have no shared room identity. Conservatively treat them as
// conflicting with a canonical booking at overlapping times until migrated.
func sameBookingRoom(leftKey string, leftID int64, rightKey string, rightID int64) bool {
	if leftKey != "" && rightKey != "" {
		return leftKey == rightKey
	}
	if leftKey == "" && rightKey == "" {
		return leftID == rightID
	}
	return true
}
func (registry *bookingRegistry) conflicts(clientID string, bookingID, roomID, startsAt, endsAt, reservationID int64, roomKey string) bool {
	for existingID, existing := range registry.active {
		if existingID != bookingKey(clientID, bookingID) && sameBookingRoom(existing.RoomKey, existing.RoomID, roomKey, roomID) && existing.StartsAt < endsAt && existing.EndsAt > startsAt {
			return true
		}
	}
	for existingID, existing := range registry.reservations {
		if existingID != reservationKey(clientID, reservationID) && sameBookingRoom(existing.RoomKey, existing.RoomID, roomKey, roomID) && existing.StartsAt < endsAt && existing.EndsAt > startsAt {
			return true
		}
	}
	return false
}

func persistEvent(journal string, event syncEvent) error {
	encoded, err := json.Marshal(event)
	if err != nil {
		return err
	}
	// The central database is committed first.  The NDJSON copy is deliberately
	// secondary: a failed mirror write must not make the authoritative server
	// forget an accepted enterprise event.
	authoritative := centralMirror != nil
	if authoritative {
		if err := centralMirror.append(event); err != nil {
			return err
		}
	}
	file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		if authoritative {
			return nil
		}
		return err
	}
	_, err = file.Write(append(encoded, '\n'))
	closeErr := file.Close()
	if err != nil {
		if authoritative {
			return nil
		}
		return err
	}
	if closeErr != nil {
		if authoritative {
			return nil
		}
		return closeErr
	}
	return nil
}

// persistRelatedEvents uses one SQLite transaction. The journal is a secondary mirror.
func persistRelatedEvents(journal string, events []syncEvent) error {
	if len(events) == 1 {
		return persistEvent(journal, events[0])
	}
	if centralMirror == nil {
		return fmt.Errorf("central database required for recurrence")
	}
	if err := centralMirror.appendMany(events); err != nil {
		return err
	}
	file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		return nil
	}
	defer file.Close()
	for _, e := range events {
		if b, err := json.Marshal(e); err == nil {
			_, _ = file.Write(append(b, '\n'))
		}
	}
	return nil
}

// changesSince exposes the durable event journal as a monotonic cursor feed.
// Clients persist only the returned nextCursor locally and may replay safely.
func changesSince(journal string, cursor int) ([]syncEvent, int, error) {
	if cursor < 0 {
		return nil, cursor, fmt.Errorf("invalid cursor")
	}
	file, err := os.Open(journal)
	if os.IsNotExist(err) {
		return []syncEvent{}, cursor, nil
	}
	if err != nil {
		return nil, cursor, err
	}
	defer file.Close()
	events := []syncEvent{}
	index := 0
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		index++
		if index <= cursor {
			continue
		}
		var event syncEvent
		if json.Unmarshal(scanner.Bytes(), &event) == nil {
			events = append(events, event)
		}
	}
	return events, index, scanner.Err()
}

func authoritativeEvents(journal string) ([]syncEvent, error) {
	if centralMirror != nil {
		events, _, err := centralMirror.eventsAfter(0)
		return events, err
	}
	events, _, err := changesSince(journal, 0)
	return events, err
}

func publishEvent(event syncEvent) int {
	desktopOutboxMutex.Lock()
	defer desktopOutboxMutex.Unlock()
	journal := os.Getenv("AURA_SYNC_JOURNAL")
	if journal == "" {
		return 2
	}
	if event.Topic == "attachment.archived" {
		client := clientInstanceID()
		if client == "" {
			return 6
		}
		var operation [16]byte
		if _, err := rand.Read(operation[:]); err != nil {
			return 4
		}
		event.ClientID = client
		event.OperationID = hex.EncodeToString(operation[:])
	}
	if event.Topic == "booking.created" || event.Topic == "booking.rescheduled" || event.Topic == "booking.cancelled" {
		var booking bookingPayload
		if json.Unmarshal([]byte(event.Payload), &booking) != nil {
			return 3
		}
		if booking.ClientID == "" {
			booking.ClientID = clientInstanceID()
		}
		if booking.ClientID == "" {
			return 6
		}
		encoded, err := json.Marshal(booking)
		if err != nil {
			return 3
		}
		event.Payload = string(encoded)
	}
	if endpoint := os.Getenv("AURA_SYNC_URL"); endpoint != "" {
		token := os.Getenv("AURA_SYNC_TOKEN")
		if token == "" {
			// Non-booking desktop events describe changes already committed to
			// the local workspace. Preserve them for a later authenticated
			// replay. Booking operations have separate reservation semantics.
			if event.Topic != "booking.created" && event.Topic != "booking.rescheduled" && event.Topic != "booking.cancelled" {
				if persistEvent(journal, event) != nil {
					return 4
				}
			}
			return 6
		}
		if event.Topic == "desktop.attachment.upload" {
			if persistEvent(journal, event) != nil {
				return 4
			}
			return flushOutboxLocked()
		}
		// Do not deliver a newer event while older changes are waiting. The
		// event is appended before replay so a failed request remains durable.
		if info, err := os.Stat(journal); err == nil && info.Size() > 0 {
			if persistEvent(journal, event) != nil {
				return 4
			}
			return flushOutboxLocked()
		} else if err != nil && !os.IsNotExist(err) {
			return 4
		}
		if code := sendCentralEvent(endpoint, token, event); code != 0 {
			if persistEvent(journal, event) != nil {
				return 4
			}
			return code
		}
		return 0
	}
	if err := persistEvent(journal, event); err != nil {
		return 4
	}
	return 0
}

func sendCentralEvent(endpoint, token string, event syncEvent) int {
	if event.Topic == "desktop.attachment.upload" {
		var pending desktopAttachmentUpload
		var attachment centralAttachment
		if json.Unmarshal([]byte(event.Payload), &pending) != nil || pending.Path == "" || json.Unmarshal([]byte(pending.Metadata), &attachment) != nil || len(attachment.SHA256) != 64 || attachment.SizeBytes < 0 || !attachment.Active {
			return 3
		}
		if code := uploadAttachmentFileExpected(pending.Path, attachment.SHA256, attachment.SizeBytes); code != 0 {
			return code
		}
		return sendCentralEvent(endpoint, token, syncEvent{Topic: "attachment.created", Payload: pending.Metadata, At: event.At})
	}
	body, err := json.Marshal(event)
	if err != nil {
		return 3
	}
	path := "/events"
	if event.Topic == "attachment.archived" && event.OperationID != "" {
		path = "/api/v1/desktop-attachment-events"
	}
	if event.Topic == "desktop.leave.requested" || event.Topic == "desktop.leave.decided" {
		path = "/api/v1/desktop-leave-events"
	}
	request, err := http.NewRequest(http.MethodPost, strings.TrimRight(endpoint, "/")+path, strings.NewReader(string(body)))
	if err != nil {
		return 7
	}
	request.Header.Set("Authorization", "Bearer "+token)
	request.Header.Set("Content-Type", "application/json")
	client, clientErr := centralHTTPClient(5 * time.Second)
	if clientErr != nil {
		return 7
	}
	response, err := client.Do(request)
	if err != nil {
		return 7
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusConflict {
		return 5
	}
	if response.StatusCode != http.StatusAccepted {
		return 8
	}
	return 0
}

// A direct cancellation is sent before the desktop changes its local booking.
// Failure therefore must not add a delayed cancellation to the replay queue.
// Without a configured server the normal offline workflow is retained.
func cancelDesktopBooking(payload string) int {
	var booking bookingPayload
	if json.Unmarshal([]byte(payload), &booking) != nil || booking.BookingID <= 0 {
		return 3
	}
	if booking.ClientID == "" {
		booking.ClientID = clientInstanceID()
	}
	if booking.ClientID == "" {
		return 6
	}
	encoded, err := json.Marshal(booking)
	if err != nil {
		return 3
	}
	event := syncEvent{Topic: "booking.cancelled", Payload: string(encoded), At: time.Now().UTC().Format(time.RFC3339Nano)}
	endpoint := os.Getenv("AURA_SYNC_URL")
	if endpoint == "" {
		return publishEvent(event)
	}
	desktopOutboxMutex.Lock()
	defer desktopOutboxMutex.Unlock()
	token := os.Getenv("AURA_SYNC_TOKEN")
	if token == "" {
		return 6
	}
	// A queued earlier creation/reschedule must not arrive after this
	// cancellation. A replay error leaves the cancellation unsent.
	if code := flushOutboxLocked(); code != 0 {
		return code
	}
	return sendCentralEvent(endpoint, token, event)
}

// uploadAttachmentFile copies a content-addressed object to Central without
// introducing a second local cache.  The caller already owns the durable
// workspace copy; a failed upload can therefore be retried by the user
// without losing the original evidence file.
func uploadAttachmentFile(path string) int {
	return uploadAttachmentFileExpected(path, "", -1)
}

func uploadAttachmentFileExpected(path, expected string, expectedSize int64) int {
	endpoint, token := os.Getenv("AURA_SYNC_URL"), os.Getenv("AURA_SYNC_TOKEN")
	if endpoint == "" {
		return 0 // offline operation: the workspace object remains authoritative locally
	}
	if token == "" {
		return 6
	}
	file, err := os.Open(path)
	if err != nil {
		return 1
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil || info.Size() < 0 || info.Size() > 100<<20 {
		return 1
	}
	if expectedSize >= 0 && info.Size() != expectedSize {
		return 1
	}
	hash := sha256.New()
	if _, err = io.Copy(hash, file); err != nil {
		return 1
	}
	digest := hex.EncodeToString(hash.Sum(nil))
	if expected != "" && digest != expected {
		return 1
	}
	if _, err = file.Seek(0, io.SeekStart); err != nil {
		return 1
	}
	request, err := http.NewRequest(http.MethodPut, strings.TrimRight(endpoint, "/")+"/api/v1/attachments/"+digest, file)
	if err != nil {
		return 7
	}
	request.Header.Set("Authorization", "Bearer "+token)
	request.Header.Set("X-Aura-SHA256", digest)
	request.Header.Set("Content-Type", "application/octet-stream")
	client, clientErr := centralHTTPClient(30 * time.Second)
	if clientErr != nil {
		return 7
	}
	response, err := client.Do(request)
	if err != nil {
		return 7
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusCreated && response.StatusCode != http.StatusNoContent {
		return 8
	}
	return 0
}

// downloadAttachmentFile restores a verified central object into a supplied
// workspace path. It writes a sibling temporary file and only replaces the
// target after the response hash and size have been checked.
func downloadAttachmentFile(digest, destination string) int {
	endpoint, token := os.Getenv("AURA_SYNC_URL"), os.Getenv("AURA_SYNC_TOKEN")
	if endpoint == "" || token == "" || len(digest) != 64 {
		return 6
	}
	if _, err := hex.DecodeString(digest); err != nil {
		return 1
	}
	request, err := http.NewRequest(http.MethodGet, strings.TrimRight(endpoint, "/")+"/api/v1/attachments/"+digest, nil)
	if err != nil {
		return 7
	}
	request.Header.Set("Authorization", "Bearer "+token)
	client, clientErr := centralHTTPClient(30 * time.Second)
	if clientErr != nil {
		return 7
	}
	response, err := client.Do(request)
	if err != nil {
		return 7
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return 8
	}
	if err := os.MkdirAll(filepath.Dir(destination), 0700); err != nil {
		return 1
	}
	temp, err := os.CreateTemp(filepath.Dir(destination), ".download-*")
	if err != nil {
		return 1
	}
	tempName := temp.Name()
	defer os.Remove(tempName)
	hash := sha256.New()
	limited := io.LimitReader(response.Body, (100<<20)+1)
	written, copyErr := io.Copy(io.MultiWriter(temp, hash), limited)
	closeErr := temp.Close()
	if copyErr != nil || closeErr != nil || written > 100<<20 || hex.EncodeToString(hash.Sum(nil)) != digest {
		return 1
	}
	if err := os.Rename(tempName, destination); err != nil {
		return 1
	}
	return 0
}

// fetchChanges obtains the server's monotonic change feed.  The caller owns
// the cursor and only advances it after it has durably stored the returned
// batch in its local cache.
func fetchChanges(cursor int) ([]syncEvent, int, int) {
	endpoint, token := os.Getenv("AURA_SYNC_URL"), os.Getenv("AURA_SYNC_TOKEN")
	if endpoint == "" {
		return nil, cursor, 2
	}
	if token == "" {
		return nil, cursor, 6
	}
	request, err := http.NewRequest(http.MethodGet, strings.TrimRight(endpoint, "/")+"/api/v1/sync/changes?desktop=1&cursor="+strconv.Itoa(cursor), nil)
	if err != nil {
		return nil, cursor, 7
	}
	request.Header.Set("Authorization", "Bearer "+token)
	client, clientErr := centralHTTPClient(5 * time.Second)
	if clientErr != nil {
		return nil, cursor, 7
	}
	response, err := client.Do(request)
	if err != nil {
		return nil, cursor, 7
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, cursor, 8
	}
	var feed struct {
		Events     []syncEvent `json:"events"`
		NextCursor int         `json:"nextCursor"`
	}
	if json.NewDecoder(http.MaxBytesReader(nil, response.Body, 8<<20)).Decode(&feed) != nil || feed.NextCursor < cursor {
		return nil, cursor, 9
	}
	return feed.Events, feed.NextCursor, 0
}

// desktopBookingSnapshot returns the booking state and the feed cursor sampled
// by the server while holding its booking registry lock.
func desktopBookingSnapshot() ([]bookingPayload, int64, int) {
	endpoint, token := os.Getenv("AURA_SYNC_URL"), os.Getenv("AURA_SYNC_TOKEN")
	if endpoint == "" {
		return nil, 0, 2
	}
	if token == "" {
		return nil, 0, 6
	}
	request, err := http.NewRequest(http.MethodGet, strings.TrimRight(endpoint, "/")+"/api/v1/desktop-booking-snapshot", nil)
	if err != nil {
		return nil, 0, 7
	}
	request.Header.Set("Authorization", "Bearer "+token)
	client, err := centralHTTPClient(5 * time.Second)
	if err != nil {
		return nil, 0, 7
	}
	response, err := client.Do(request)
	if err != nil {
		return nil, 0, 7
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, 0, 8
	}
	var snapshot struct {
		Cursor   int64            `json:"cursor"`
		Bookings []bookingPayload `json:"bookings"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(nil, response.Body, 8<<20))
	if decoder.Decode(&snapshot) != nil || snapshot.Cursor < 0 || snapshot.Bookings == nil || len(snapshot.Bookings) > 100000 {
		return nil, 0, 9
	}
	var trailing any
	if decoder.Decode(&trailing) != io.EOF {
		return nil, 0, 9
	}
	return snapshot.Bookings, snapshot.Cursor, 0
}

func desktopProjectAssignmentSnapshot() ([]map[string]string, int64, int) {
	endpoint, token := os.Getenv("AURA_SYNC_URL"), os.Getenv("AURA_SYNC_TOKEN")
	if endpoint == "" {
		return nil, 0, 2
	}
	if token == "" {
		return nil, 0, 6
	}
	request, err := http.NewRequest(http.MethodGet, strings.TrimRight(endpoint, "/")+"/api/v1/desktop-project-assignment-snapshot", nil)
	if err != nil {
		return nil, 0, 7
	}
	request.Header.Set("Authorization", "Bearer "+token)
	client, err := centralHTTPClient(5 * time.Second)
	if err != nil {
		return nil, 0, 7
	}
	response, err := client.Do(request)
	if err != nil {
		return nil, 0, 7
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, 0, 8
	}
	var snapshot struct {
		Cursor      int64               `json:"cursor"`
		Assignments []map[string]string `json:"assignments"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(nil, response.Body, 8<<20))
	if decoder.Decode(&snapshot) != nil || snapshot.Cursor < 0 || snapshot.Assignments == nil || len(snapshot.Assignments) > 100000 {
		return nil, 0, 9
	}
	var trailing any
	if decoder.Decode(&trailing) != io.EOF {
		return nil, 0, 9
	}
	return snapshot.Assignments, snapshot.Cursor, 0
}

func desktopTaskRecurrenceSnapshot() ([]map[string]string, int64, int) {
	endpoint, token := os.Getenv("AURA_SYNC_URL"), os.Getenv("AURA_SYNC_TOKEN")
	if endpoint == "" {
		return nil, 0, 2
	}
	if token == "" {
		return nil, 0, 6
	}
	request, err := http.NewRequest(http.MethodGet, strings.TrimRight(endpoint, "/")+"/api/v1/desktop-task-recurrence-snapshot", nil)
	if err != nil {
		return nil, 0, 7
	}
	request.Header.Set("Authorization", "Bearer "+token)
	client, err := centralHTTPClient(5 * time.Second)
	if err != nil {
		return nil, 0, 7
	}
	response, err := client.Do(request)
	if err != nil {
		return nil, 0, 7
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, 0, 8
	}
	var snapshot struct {
		Cursor int64               `json:"cursor"`
		Links  []map[string]string `json:"links"`
	}
	decoder := json.NewDecoder(io.LimitReader(response.Body, 8<<20))
	if decoder.Decode(&snapshot) != nil || snapshot.Cursor < 0 || snapshot.Links == nil || len(snapshot.Links) > 100000 {
		return nil, 0, 9
	}
	var trailing any
	if decoder.Decode(&trailing) != io.EOF {
		return nil, 0, 9
	}
	return snapshot.Links, snapshot.Cursor, 0
}

func desktopMaintenanceRecurrenceSnapshot() ([]map[string]string, int64, int) {
	endpoint, token := os.Getenv("AURA_SYNC_URL"), os.Getenv("AURA_SYNC_TOKEN")
	if endpoint == "" {
		return nil, 0, 2
	}
	if token == "" {
		return nil, 0, 6
	}
	request, err := http.NewRequest(http.MethodGet, strings.TrimRight(endpoint, "/")+"/api/v1/desktop-maintenance-recurrence-snapshot", nil)
	if err != nil {
		return nil, 0, 7
	}
	request.Header.Set("Authorization", "Bearer "+token)
	client, err := centralHTTPClient(5 * time.Second)
	if err != nil {
		return nil, 0, 7
	}
	response, err := client.Do(request)
	if err != nil {
		return nil, 0, 7
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, 0, 8
	}
	var snapshot struct {
		Cursor int64               `json:"cursor"`
		Links  []map[string]string `json:"links"`
	}
	decoder := json.NewDecoder(io.LimitReader(response.Body, 8<<20))
	if decoder.Decode(&snapshot) != nil || snapshot.Cursor < 0 || snapshot.Links == nil || len(snapshot.Links) > 100000 {
		return nil, 0, 9
	}
	var trailing any
	if decoder.Decode(&trailing) != io.EOF {
		return nil, 0, 9
	}
	return snapshot.Links, snapshot.Cursor, 0
}

func desktopAttachmentSnapshot() ([]centralAttachment, int64, int) {
	endpoint, token, clientID := os.Getenv("AURA_SYNC_URL"), os.Getenv("AURA_SYNC_TOKEN"), clientInstanceID()
	if endpoint == "" {
		return nil, 0, 2
	}
	if token == "" || clientID == "" {
		return nil, 0, 6
	}
	request, err := http.NewRequest(http.MethodGet, strings.TrimRight(endpoint, "/")+"/api/v1/desktop-attachment-snapshot?clientId="+url.QueryEscape(clientID), nil)
	if err != nil {
		return nil, 0, 7
	}
	request.Header.Set("Authorization", "Bearer "+token)
	client, err := centralHTTPClient(5 * time.Second)
	if err != nil {
		return nil, 0, 7
	}
	response, err := client.Do(request)
	if err != nil {
		return nil, 0, 7
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusNotFound {
		return nil, 0, 11
	}
	if response.StatusCode == http.StatusForbidden {
		return nil, 0, 12
	}
	if response.StatusCode != http.StatusOK {
		return nil, 0, 8
	}
	var snapshot struct {
		Cursor      int64               `json:"cursor"`
		Attachments []centralAttachment `json:"attachments"`
	}
	decoder := json.NewDecoder(http.MaxBytesReader(nil, response.Body, 8<<20))
	if decoder.Decode(&snapshot) != nil || snapshot.Cursor < 0 || snapshot.Attachments == nil || len(snapshot.Attachments) > 100000 {
		return nil, 0, 9
	}
	var trailing any
	if decoder.Decode(&trailing) != io.EOF {
		return nil, 0, 9
	}
	return snapshot.Attachments, snapshot.Cursor, 0
}

// flushOutbox replays events in write order and leaves the failed event plus
// every following event intact.  This preserves causal order across an
// interrupted reconnect.
func flushOutbox() int {
	desktopOutboxMutex.Lock()
	defer desktopOutboxMutex.Unlock()
	return flushOutboxLocked()
}

func flushOutboxLocked() int {
	journal, endpoint, token := os.Getenv("AURA_SYNC_JOURNAL"), os.Getenv("AURA_SYNC_URL"), os.Getenv("AURA_SYNC_TOKEN")
	if journal == "" {
		return 2
	}
	if endpoint == "" {
		return 0
	}
	if token == "" {
		return 6
	}
	content, err := os.ReadFile(journal)
	if os.IsNotExist(err) {
		return 0
	}
	if err != nil {
		return 4
	}
	lines := strings.Split(strings.TrimSuffix(string(content), "\n"), "\n")
	remaining := []string{}
	result := 0
	for index, line := range lines {
		if strings.TrimSpace(line) == "" {
			continue
		}
		var event syncEvent
		if json.Unmarshal([]byte(line), &event) != nil {
			remaining = append(remaining, lines[index:]...)
			result = 3
			break
		}
		if code := sendCentralEvent(endpoint, token, event); code != 0 {
			remaining = append(remaining, lines[index:]...)
			result = code
			break
		}
	}
	value := ""
	if len(remaining) > 0 {
		value = strings.Join(remaining, "\n") + "\n"
	}
	temp, err := os.CreateTemp(filepath.Dir(journal), ".aura-outbox-*")
	if err != nil {
		return 4
	}
	tempName := temp.Name()
	defer os.Remove(tempName)
	_, err = io.WriteString(temp, value)
	if err == nil {
		err = temp.Sync()
	}
	if closeErr := temp.Close(); err == nil {
		err = closeErr
	}
	if err != nil || os.Rename(tempName, journal) != nil {
		return 4
	}
	return result
}

type desktopAttachmentReview struct {
	ServerURL  string            `json:"serverUrl"`
	Event      syncEvent         `json:"event"`
	Expected   centralAttachment `json:"expected"`
	Projection syncEvent         `json:"projection"`
}

func desktopAttachmentProjection(expected centralAttachment, event syncEvent) (syncEvent, error) {
	var original centralAttachment
	if json.Unmarshal([]byte(event.Payload), &original) != nil || original.UploadedBy == "" {
		return syncEvent{}, fmt.Errorf("invalid original attachment")
	}
	expected.UploadedBy = original.UploadedBy
	payload, err := json.Marshal(expected)
	if err != nil {
		return syncEvent{}, err
	}
	topic := "attachment.archived"
	if expected.Active {
		topic = "attachment.restored"
	}
	return syncEvent{Topic: topic, Payload: string(payload)}, nil
}

func desktopArchiveHead(content []byte) (syncEvent, []byte, error) {
	remaining := content
	for len(remaining) > 0 {
		line := remaining
		if index := bytes.IndexByte(remaining, '\n'); index >= 0 {
			line = remaining[:index]
			remaining = remaining[index+1:]
		} else {
			remaining = nil
		}
		if len(bytes.TrimSpace(line)) == 0 {
			continue
		}
		var event syncEvent
		var attachment centralAttachment
		if json.Unmarshal(line, &event) != nil || event.Topic != "attachment.archived" || event.OperationID == "" || event.ClientID == "" || event.ClientID != clientInstanceID() || json.Unmarshal([]byte(event.Payload), &attachment) != nil || attachment.ID == "" || (attachment.SubjectType != "task" && attachment.SubjectType != "ticket") || attachment.SubjectID == "" {
			return syncEvent{}, nil, fmt.Errorf("first queued event is not a supported attachment archive")
		}
		return event, remaining, nil
	}
	return syncEvent{}, nil, fmt.Errorf("outbox is empty")
}

func inspectDesktopAttachmentArchive() (desktopAttachmentReview, int) {
	journal, endpoint, token := os.Getenv("AURA_SYNC_JOURNAL"), strings.TrimRight(os.Getenv("AURA_SYNC_URL"), "/"), os.Getenv("AURA_SYNC_TOKEN")
	if journal == "" || endpoint == "" || token == "" {
		return desktopAttachmentReview{}, 6
	}
	desktopOutboxMutex.Lock()
	content, err := os.ReadFile(journal)
	event, _, parseErr := desktopArchiveHead(content)
	desktopOutboxMutex.Unlock()
	if err != nil {
		return desktopAttachmentReview{}, 4
	}
	if parseErr != nil {
		return desktopAttachmentReview{}, 3
	}
	var attachment centralAttachment
	_ = json.Unmarshal([]byte(event.Payload), &attachment)
	query := url.Values{"subjectType": {attachment.SubjectType}, "subjectId": {attachment.SubjectID}, "includeArchived": {"1"}}
	request, err := http.NewRequest(http.MethodGet, endpoint+"/api/v1/subject-attachments?"+query.Encode(), nil)
	if err != nil {
		return desktopAttachmentReview{}, 7
	}
	request.Header.Set("Authorization", "Bearer "+token)
	client, err := centralHTTPClient(5 * time.Second)
	if err != nil {
		return desktopAttachmentReview{}, 7
	}
	response, err := client.Do(request)
	if err != nil {
		return desktopAttachmentReview{}, 7
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return desktopAttachmentReview{}, 8
	}
	var records []centralAttachment
	decoder := json.NewDecoder(http.MaxBytesReader(nil, response.Body, 8<<20))
	if decoder.Decode(&records) != nil || records == nil {
		return desktopAttachmentReview{}, 9
	}
	var trailing any
	if decoder.Decode(&trailing) != io.EOF {
		return desktopAttachmentReview{}, 9
	}
	for _, record := range records {
		if record.ID == attachment.ID {
			projection, err := desktopAttachmentProjection(record, event)
			if err != nil {
				return desktopAttachmentReview{}, 3
			}
			return desktopAttachmentReview{endpoint, event, record, projection}, 0
		}
	}
	return desktopAttachmentReview{}, 5
}

func abandonDesktopAttachmentArchive(review desktopAttachmentReview, finalize bool) int {
	desktopOutboxMutex.Lock()
	defer desktopOutboxMutex.Unlock()
	journal, endpoint, token := os.Getenv("AURA_SYNC_JOURNAL"), strings.TrimRight(os.Getenv("AURA_SYNC_URL"), "/"), os.Getenv("AURA_SYNC_TOKEN")
	if journal == "" || endpoint == "" || token == "" || endpoint != review.ServerURL {
		return 6
	}
	content, err := os.ReadFile(journal)
	if err != nil {
		return 4
	}
	event, remainder, err := desktopArchiveHead(content)
	if err != nil || event != review.Event {
		return 5
	}
	projection, err := desktopAttachmentProjection(review.Expected, event)
	if err != nil || projection != review.Projection {
		return 5
	}
	var subject centralAttachment
	_ = json.Unmarshal([]byte(event.Payload), &subject)
	for _, line := range bytes.Split(remainder, []byte{'\n'}) {
		if len(bytes.TrimSpace(line)) == 0 {
			continue
		}
		var next syncEvent
		if json.Unmarshal(line, &next) != nil {
			return 3
		}
		payload := next.Payload
		if next.Topic == "desktop.attachment.upload" {
			var upload desktopAttachmentUpload
			if json.Unmarshal([]byte(payload), &upload) != nil {
				return 3
			}
			payload = upload.Metadata
		}
		if strings.HasPrefix(next.Topic, "attachment.") || next.Topic == "desktop.attachment.upload" {
			var other centralAttachment
			if json.Unmarshal([]byte(payload), &other) != nil || other.ID == "" {
				return 3
			}
			if other.ID == subject.ID {
				return 5
			}
		}
	}
	body, err := json.Marshal(struct {
		Event    syncEvent         `json:"event"`
		Expected centralAttachment `json:"expected"`
	}{event, review.Expected})
	if err != nil {
		return 3
	}
	request, err := http.NewRequest(http.MethodPost, endpoint+"/api/v1/desktop-attachment-abandon", bytes.NewReader(body))
	if err != nil {
		return 7
	}
	request.Header.Set("Authorization", "Bearer "+token)
	request.Header.Set("Content-Type", "application/json")
	client, err := centralHTTPClient(5 * time.Second)
	if err != nil {
		return 7
	}
	response, err := client.Do(request)
	if err != nil {
		return 7
	}
	response.Body.Close()
	if response.StatusCode == http.StatusConflict {
		return 5
	}
	if response.StatusCode != http.StatusAccepted {
		return 8
	}
	if !finalize {
		return 0
	}
	temp, err := os.CreateTemp(filepath.Dir(journal), ".aura-outbox-*")
	if err != nil {
		return 4
	}
	name := temp.Name()
	defer os.Remove(name)
	_, err = temp.Write(remainder)
	if err == nil {
		err = temp.Sync()
	}
	if closeErr := temp.Close(); err == nil {
		err = closeErr
	}
	if err != nil || os.Rename(name, journal) != nil {
		return 4
	}
	return 0
}

// preflightBooking asks the central authority before a local transaction is committed.
// Offline clients retain their durable outbox workflow; connected clients fail closed.
func preflightBooking(bookingID, roomID, startsAt, endsAt int64) int {
	endpoint := os.Getenv("AURA_SYNC_URL")
	if endpoint == "" {
		return 0
	}
	token := os.Getenv("AURA_SYNC_TOKEN")
	if token == "" {
		return 6
	}
	clientID := clientInstanceID()
	if clientID == "" {
		return 6
	}
	body, err := json.Marshal(bookingCheck{ClientID: clientID, BookingID: bookingID, RoomID: roomID, StartsAt: startsAt, EndsAt: endsAt})
	if err != nil {
		return 3
	}
	request, err := http.NewRequest(http.MethodPost, strings.TrimRight(endpoint, "/")+"/bookings/check", strings.NewReader(string(body)))
	if err != nil {
		return 7
	}
	request.Header.Set("Authorization", "Bearer "+token)
	request.Header.Set("Content-Type", "application/json")
	client, clientErr := centralHTTPClient(5 * time.Second)
	if clientErr != nil {
		return 7
	}
	response, err := client.Do(request)
	if err != nil {
		return 7
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusNoContent {
		return 0
	}
	if response.StatusCode == http.StatusConflict {
		return 5
	}
	return 8
}

func centralBookingRequest(path string, body any, accepted ...int) int {
	endpoint := os.Getenv("AURA_SYNC_URL")
	if endpoint == "" {
		return 0
	}
	token := os.Getenv("AURA_SYNC_TOKEN")
	if token == "" {
		return 6
	}
	encoded, err := json.Marshal(body)
	if err != nil {
		return 3
	}
	request, err := http.NewRequest(http.MethodPost, strings.TrimRight(endpoint, "/")+path, strings.NewReader(string(encoded)))
	if err != nil {
		return 7
	}
	request.Header.Set("Authorization", "Bearer "+token)
	request.Header.Set("Content-Type", "application/json")
	client, clientErr := centralHTTPClient(5 * time.Second)
	if clientErr != nil {
		return 7
	}
	response, err := client.Do(request)
	if err != nil {
		return 7
	}
	defer response.Body.Close()
	for _, status := range accepted {
		if response.StatusCode == status {
			return 0
		}
	}
	if response.StatusCode == http.StatusConflict {
		return 5
	}
	return 8
}

func reserveBooking(reservationID, bookingID, roomID, startsAt, endsAt int64) int {
	if reservationID >= 0 || roomID <= 0 || endsAt <= startsAt {
		return 1
	}
	clientID := clientInstanceID()
	if clientID == "" {
		return 6
	}
	return centralBookingRequest("/bookings/reserve", bookingReservation{ClientID: clientID, ReservationID: reservationID, BookingID: bookingID, RoomID: roomID, StartsAt: startsAt, EndsAt: endsAt}, http.StatusNoContent)
}

// Verify the server supports the field: old servers silently ignore unknown JSON.
func reserveBookingWithRoomKey(reservationID, bookingID, roomID, startsAt, endsAt int64, roomKey string) int {
	return reserveBookingWithDetails(reservationID, bookingID, roomID, startsAt, endsAt, roomKey, "", 0)
}

func reserveBookingWithDetails(reservationID, bookingID, roomID, startsAt, endsAt int64, roomKey, employeeKey string, attendees int) int {
	endpoint := strings.TrimRight(strings.TrimSpace(os.Getenv("AURA_SYNC_URL")), "/")
	if endpoint == "" {
		return 0
	}
	token := os.Getenv("AURA_SYNC_TOKEN")
	clientID := clientInstanceID()
	if token == "" || clientID == "" {
		return 6
	}
	if reservationID >= 0 || roomID <= 0 || endsAt <= startsAt || strings.TrimSpace(roomKey) == "" || (employeeKey == "" && attendees != 0) || (employeeKey != "" && attendees <= 0) {
		return 1
	}
	client, err := centralHTTPClient(5 * time.Second)
	if err != nil {
		return 7
	}
	req, err := http.NewRequest(http.MethodGet, endpoint+"/api/v1/capabilities", nil)
	if err != nil {
		return 7
	}
	req.Header.Set("Authorization", "Bearer "+token)
	response, err := client.Do(req)
	if err != nil {
		return 7
	}
	defer response.Body.Close()
	var capabilities struct {
		CanonicalRoomBooking  int `json:"canonicalRoomBooking"`
		DesktopBookingDetails int `json:"desktopBookingDetails"`
	}
	if response.StatusCode != 200 || json.NewDecoder(io.LimitReader(response.Body, 4096)).Decode(&capabilities) != nil || capabilities.CanonicalRoomBooking != 1 || (employeeKey != "" && capabilities.DesktopBookingDetails != 1) {
		return 8
	}
	return centralBookingRequest("/bookings/reserve", bookingReservation{ClientID: clientID, ReservationID: reservationID, BookingID: bookingID, RoomID: roomID, RoomKey: roomKey, StartsAt: startsAt, EndsAt: endsAt, EmployeeKey: employeeKey, Attendees: attendees}, http.StatusNoContent)
}

func commitBooking(reservationID, bookingID int64, reschedule bool) int {
	if reservationID >= 0 || bookingID <= 0 {
		return 1
	}
	topic := "booking.created"
	if reschedule {
		topic = "booking.rescheduled"
	}
	clientID := clientInstanceID()
	if clientID == "" {
		return 6
	}
	return centralBookingRequest("/bookings/commit", bookingCommit{ClientID: clientID, ReservationID: reservationID, BookingID: bookingID, Topic: topic}, http.StatusAccepted)
}

func releaseBooking(reservationID int64) int {
	if reservationID >= 0 {
		return 1
	}
	clientID := clientInstanceID()
	if clientID == "" {
		return 6
	}
	return centralBookingRequest("/bookings/release", struct {
		ClientID      string `json:"clientId"`
		ReservationID int64  `json:"reservationId"`
	}{clientID, reservationID}, http.StatusNoContent, http.StatusNotFound)
}

func syncHandler(journal string, token string) http.Handler {
	attachmentRoot := journal + ".attachments"
	_ = os.MkdirAll(attachmentRoot, 0700)
	registry := bookingRegistry{active: make(map[string]bookingPayload), reservations: make(map[string]bookingReservation)}
	bookingHistory := make(map[string]bookingPayload)
	bookingReconciliations := make(map[string]bookingIdentityReconciliation)
	leaves := make(map[string]leaveRequest)
	leaveAccounts := make(map[string]bool)
	leaveOriginalEmployees := make(map[string]string)
	leaveOriginalDeciders := make(map[string]string)
	leaveReconciled := make(map[string]bool)
	leaveReconciliations := make(map[string]leaveReconciliationEvent)
	tasks := make(map[string]centralTask)
	// Recurrences created by the central status endpoint are persisted as an
	// atomic, adjacent status/creation pair with one timestamp.
	taskSuccessors := make(map[string]string)
	type recurrenceReservationOwner struct {
		ClientID string
		ActorID  int64
	}
	taskReservationOwners := make(map[string]recurrenceReservationOwner)
	dependencies := make(map[string]map[string]bool)
	projectAssignments := make(map[string]map[string]bool)
	subtasks := make(map[string]centralSubtask)
	tickets := make(map[string]centralTicket)
	rooms := make(map[string]centralRoom)
	projects := make(map[string]centralProject)
	employees := make(map[string]centralEmployee)
	organizations := make(map[string]centralOrganization)
	assets := make(map[string]centralFacilityAsset)
	maintenanceOrders := make(map[string]centralMaintenanceOrder)
	maintenanceSuccessors := make(map[string]string)
	maintenanceReservationOwners := make(map[string]recurrenceReservationOwner)
	approvals := make(map[string]centralTaskApproval)
	// Resolve Windows employee object IDs through explicit active account links.
	approvalAccount := func(id string) (string, bool) {
		if n, err := strconv.ParseInt(id, 10, 64); err == nil && n > 0 && strconv.FormatInt(n, 10) == id {
			return id, true
		}
		e, ok := employees[id]
		if !ok || !e.Active || e.AccountEmployeeID <= 0 {
			return "", false
		}
		for otherID, other := range employees {
			if otherID != id && other.Active && other.AccountEmployeeID == e.AccountEmployeeID {
				return "", false
			}
		}
		return strconv.FormatInt(e.AccountEmployeeID, 10), true
	}
	projectApproval := func(a centralTaskApproval) (centralTaskApproval, bool) {
		requester, ok := approvalAccount(a.RequestedBy)
		if !ok {
			return a, false
		}
		approver, ok := approvalAccount(a.ApproverID)
		if !ok || requester == approver {
			return a, false
		}
		decider := ""
		if a.DecidedBy != "" {
			decider, ok = approvalAccount(a.DecidedBy)
			if !ok {
				return a, false
			}
		}
		a.RequestedBy, a.ApproverID, a.DecidedBy = requester, approver, decider
		return a, true
	}
	approvalEmployee := func(account string) string {
		var found string
		for id, e := range employees {
			if e.Active && strconv.FormatInt(e.AccountEmployeeID, 10) == account {
				if found != "" {
					return ""
				}
				found = id
			}
		}
		return found
	}
	comments := make(map[string]centralComment)
	attachments := make(map[string]centralAttachment)
	attachmentRestored := make(map[string]bool)
	if previous, err := authoritativeEvents(journal); err == nil {
		for index, prior := range previous {
			if prior.Topic == "central.task_recurrence.reserved" {
				var reservation struct {
					SourceID    string `json:"sourceId"`
					SuccessorID string `json:"successorId"`
					ClientID    string `json:"clientId"`
					ActorID     int64  `json:"actorId"`
				}
				if json.Unmarshal([]byte(prior.Payload), &reservation) == nil && reservation.SourceID != "" && reservation.SuccessorID != "" && reservation.ClientID != "" && reservation.ActorID > 0 {
					taskSuccessors[reservation.SourceID] = reservation.SuccessorID
					taskReservationOwners[reservation.SuccessorID] = recurrenceReservationOwner{reservation.ClientID, reservation.ActorID}
				}
			}
			if prior.Topic == "central.task_recurrence.claimed" {
				var claim struct {
					SourceID    string `json:"sourceId"`
					SuccessorID string `json:"successorId"`
					ClientID    string `json:"clientId"`
					ActorID     int64  `json:"actorId"`
				}
				if json.Unmarshal([]byte(prior.Payload), &claim) == nil && claim.SourceID != "" && claim.SuccessorID != "" && claim.ClientID != "" && claim.ActorID > 0 && taskSuccessors[claim.SourceID] == claim.SuccessorID {
					taskReservationOwners[claim.SuccessorID] = recurrenceReservationOwner{claim.ClientID, claim.ActorID}
				}
			}
			if prior.Topic == "central.task_recurrence.linked" {
				var link struct {
					SourceID    string `json:"sourceId"`
					SuccessorID string `json:"successorId"`
				}
				if json.Unmarshal([]byte(prior.Payload), &link) == nil && link.SourceID != "" && link.SuccessorID != "" {
					taskSuccessors[link.SourceID] = link.SuccessorID
				}
			}
			if prior.Topic == "task.status.changed" && index+1 < len(previous) && previous[index+1].Topic == "task.created" && prior.At == previous[index+1].At {
				var source, successor centralTask
				if json.Unmarshal([]byte(prior.Payload), &source) == nil && json.Unmarshal([]byte(previous[index+1].Payload), &successor) == nil && source.ID != "" && successor.ID != "" && source.ID != successor.ID && source.Status == "done" && successor.Status == "open" && source.RecurrenceDays > 0 && successor.RecurrenceDays == source.RecurrenceDays && source.Title == successor.Title && source.Department == successor.Department && source.Priority == successor.Priority && source.AssigneeID == successor.AssigneeID && source.CreatedBy == successor.CreatedBy {
					base := source.DueAt
					if base == 0 {
						if at, err := time.Parse(time.RFC3339Nano, prior.At); err == nil {
							base = at.Unix()
						}
					}
					if base > 0 && base <= int64(^uint64(0)>>1)-int64(source.RecurrenceDays)*86400 && successor.DueAt == base+int64(source.RecurrenceDays)*86400 {
						taskSuccessors[source.ID] = successor.ID
					}
				}
			}
			if prior.Topic == "central.maintenance_recurrence.linked" {
				var link struct {
					SourceID    string `json:"sourceId"`
					SuccessorID string `json:"successorId"`
				}
				if json.Unmarshal([]byte(prior.Payload), &link) == nil && link.SourceID != "" && link.SuccessorID != "" && link.SourceID != link.SuccessorID {
					maintenanceSuccessors[link.SourceID] = link.SuccessorID
				}
			}
			// Older servers persisted central completions and successors as an atomic adjacent pair.
			if prior.Topic == "facility.maintenance.status.changed" && index+1 < len(previous) && previous[index+1].Topic == "facility.maintenance.created" && prior.At == previous[index+1].At {
				var source, next centralMaintenanceOrder
				if json.Unmarshal([]byte(prior.Payload), &source) == nil && json.Unmarshal([]byte(previous[index+1].Payload), &next) == nil && source.ID != "" && next.ID != "" && source.ID != next.ID && source.Status == "completed" && next.Status == "open" && source.RecurrenceDays > 0 && source.RecurrenceDays <= 3660 && source.RecurrenceDays == next.RecurrenceDays && source.AssetID == next.AssetID && source.Title == next.Title {
					base := source.DueAt
					if base == 0 {
						if at, err := time.Parse(time.RFC3339Nano, prior.At); err == nil {
							base = at.Unix()
						}
					}
					delta := int64(source.RecurrenceDays) * 86400
					if base > 0 && base <= int64(^uint64(0)>>1)-delta && (next.DueAt == base+delta || (source.DueAt == 0 && base > 1 && next.DueAt == base+delta-1)) {
						maintenanceSuccessors[source.ID] = next.ID
					}
				}
			}
			var booking bookingPayload
			if (prior.Topic == "booking.created" || prior.Topic == "booking.rescheduled") && json.Unmarshal([]byte(prior.Payload), &booking) == nil && booking.BookingID > 0 && booking.EndsAt > booking.StartsAt {
				registry.active[bookingKey(booking.ClientID, booking.BookingID)] = booking
				bookingHistory[bookingKey(booking.ClientID, booking.BookingID)] = booking
			}
			if prior.Topic == "booking.cancelled" && json.Unmarshal([]byte(prior.Payload), &booking) == nil {
				key := bookingKey(booking.ClientID, booking.BookingID)
				if prior, ok := bookingHistory[key]; ok {
					prior.Status = "cancelled"
					bookingHistory[key] = prior
				}
				delete(registry.active, key)
			}
			if prior.Topic == "central.booking.identity.reconciled" {
				var resolved bookingIdentityReconciliation
				if json.Unmarshal([]byte(prior.Payload), &resolved) == nil && resolved.Booking.ClientID != "" && resolved.Booking.BookingID > 0 {
					key := bookingKey(resolved.Booking.ClientID, resolved.Booking.BookingID)
					bookingHistory[key] = resolved.Booking
					bookingReconciliations[key] = resolved
					if resolved.Booking.Status == "cancelled" {
						delete(registry.active, key)
					} else {
						registry.active[key] = resolved.Booking
					}
				}
			}
			var leave leaveRequest
			if (prior.Topic == "leave.requested" || prior.Topic == "leave.decided" || prior.Topic == "leave.identity.reconciled") && json.Unmarshal([]byte(prior.Payload), &leave) == nil && leave.ID != "" {
				leaves[leave.ID] = leave
				var marked leaveAccountEvent
				leaveAccounts[leave.ID] = json.Unmarshal([]byte(prior.Payload), &marked) == nil && marked.IdentityKind == "server-account-v1"
				if marked.EmployeeExternalID != "" {
					leaveOriginalEmployees[leave.ID] = marked.EmployeeExternalID
				}
				if marked.DeciderExternalID != "" {
					leaveOriginalDeciders[leave.ID] = marked.DeciderExternalID
				}
				if prior.Topic == "leave.identity.reconciled" && leaveAccounts[leave.ID] {
					var resolution leaveReconciliationEvent
					if json.Unmarshal([]byte(prior.Payload), &resolution) == nil {
						leaveReconciled[leave.ID] = true
						leaveReconciliations[leave.ID] = resolution
					}
				}
			}
			var task centralTask
			if prior.Topic == "task.created" || prior.Topic == "task.updated" {
				var reservation struct {
					ID          string `json:"id"`
					SuccessorID string `json:"recurrenceSuccessorId"`
					ClientID    string `json:"recurrenceReservationClientId"`
					ActorID     int64  `json:"recurrenceReservationActorId"`
				}
				if json.Unmarshal([]byte(prior.Payload), &reservation) == nil && reservation.ID != "" && reservation.SuccessorID != "" {
					taskSuccessors[reservation.ID] = reservation.SuccessorID
					if reservation.ClientID != "" && reservation.ActorID > 0 {
						taskReservationOwners[reservation.SuccessorID] = recurrenceReservationOwner{reservation.ClientID, reservation.ActorID}
					}
				}
			}
			if (prior.Topic == "task.created" || prior.Topic == "task.updated" || prior.Topic == "task.status.changed" || prior.Topic == "task.archived") && json.Unmarshal([]byte(prior.Payload), &task) == nil && task.ID != "" {
				if prior.Topic != "task.archived" && !task.Active {
					task.Active = true // Compatibility with event records written before the active flag existed.
				}
				tasks[task.ID] = task
			}
			if prior.Topic == "central.project_assignment.added" || prior.Topic == "central.project_assignment.removed" {
				var edge struct {
					ProjectID string `json:"projectId"`
					TaskID    string `json:"taskId"`
				}
				if json.Unmarshal([]byte(prior.Payload), &edge) == nil && edge.ProjectID != "" && edge.TaskID != "" {
					if projectAssignments[edge.ProjectID] == nil {
						projectAssignments[edge.ProjectID] = make(map[string]bool)
					}
					if prior.Topic == "central.project_assignment.added" {
						projectAssignments[edge.ProjectID][edge.TaskID] = true
					} else {
						delete(projectAssignments[edge.ProjectID], edge.TaskID)
					}
				}
			}
			if prior.Topic == "central.task_dependency.added" || prior.Topic == "central.task_dependency.removed" {
				var dep struct {
					TaskID       string `json:"taskId"`
					DependencyID string `json:"dependencyId"`
				}
				if json.Unmarshal([]byte(prior.Payload), &dep) == nil && dep.TaskID != "" && dep.DependencyID != "" {
					if dependencies[dep.TaskID] == nil {
						dependencies[dep.TaskID] = make(map[string]bool)
					}
					if prior.Topic == "central.task_dependency.added" {
						dependencies[dep.TaskID][dep.DependencyID] = true
					} else {
						delete(dependencies[dep.TaskID], dep.DependencyID)
					}
				}
			}
			if prior.Topic == "central.task_subtask.saved" || prior.Topic == "central.task_subtask.archived" {
				var sub centralSubtask
				if json.Unmarshal([]byte(prior.Payload), &sub) == nil && sub.ID != "" && sub.TaskID != "" {
					subtasks[sub.ID] = sub
				}
			}
			var ticket centralTicket
			if (prior.Topic == "ticket.created" || prior.Topic == "ticket.updated" || prior.Topic == "ticket.status.changed" || prior.Topic == "ticket.archived") && json.Unmarshal([]byte(prior.Payload), &ticket) == nil && ticket.ID != "" {
				if prior.Topic != "ticket.archived" && !ticket.Active {
					ticket.Active = true
				}
				tickets[ticket.ID] = ticket
			}
			var room centralRoom
			if (prior.Topic == "room.created" || prior.Topic == "room.updated" || prior.Topic == "room.archived") && json.Unmarshal([]byte(prior.Payload), &room) == nil && room.ID != "" {
				rooms[room.ID] = room
			}
			var project centralProject
			if (prior.Topic == "project.created" || prior.Topic == "project.updated" || prior.Topic == "project.archived") && json.Unmarshal([]byte(prior.Payload), &project) == nil && project.ID != "" {
				projects[project.ID] = project
			}
			var employee centralEmployee
			if (prior.Topic == "employee.created" || prior.Topic == "employee.updated" || prior.Topic == "employee.presence.changed" || prior.Topic == "employee.archived") && json.Unmarshal([]byte(prior.Payload), &employee) == nil && employee.ID != "" {
				employees[employee.ID] = employee
			}
			var organization centralOrganization
			if (prior.Topic == "organization.created" || prior.Topic == "organization.updated" || prior.Topic == "organization.archived") && json.Unmarshal([]byte(prior.Payload), &organization) == nil && organization.ID != "" {
				organizations[organization.ID] = organization
			}
			var asset centralFacilityAsset
			if (prior.Topic == "facility.asset.created" || prior.Topic == "facility.asset.updated" || prior.Topic == "facility.asset.archived") && json.Unmarshal([]byte(prior.Payload), &asset) == nil && asset.ID != "" {
				assets[asset.ID] = asset
			}
			var maintenance centralMaintenanceOrder
			if (prior.Topic == "facility.maintenance.created" || prior.Topic == "facility.maintenance.updated" || prior.Topic == "facility.maintenance.status.changed" || prior.Topic == "facility.maintenance.archived") && json.Unmarshal([]byte(prior.Payload), &maintenance) == nil && maintenance.ID != "" {
				maintenanceOrders[maintenance.ID] = maintenance
				var reservation struct {
					SuccessorID string `json:"recurrenceSuccessorId"`
					ClientID    string `json:"recurrenceReservationClientId"`
					ActorID     int64  `json:"recurrenceReservationActorId"`
				}
				if json.Unmarshal([]byte(prior.Payload), &reservation) == nil && reservation.SuccessorID != "" && reservation.ClientID != "" && reservation.ActorID > 0 {
					maintenanceSuccessors[maintenance.ID] = reservation.SuccessorID
					maintenanceReservationOwners[reservation.SuccessorID] = recurrenceReservationOwner{reservation.ClientID, reservation.ActorID}
				}
			}
			var approval centralTaskApproval
			if (prior.Topic == "task.approval.requested" || prior.Topic == "task.approval.decided") && json.Unmarshal([]byte(prior.Payload), &approval) == nil && approval.ID != "" {
				approvals[approval.ID] = approval
			}
			var attachment centralAttachment
			if (prior.Topic == "attachment.created" || prior.Topic == "attachment.archived" || prior.Topic == "attachment.restored") && json.Unmarshal([]byte(prior.Payload), &attachment) == nil && attachment.ID != "" {
				if prior.Topic == "attachment.archived" {
					attachment.Active = false
				}
				attachments[attachment.ID] = attachment
				if prior.Topic == "attachment.restored" {
					attachmentRestored[attachment.ID] = true
				}
			}
			var comment centralComment
			if prior.Topic == "comment.created" && json.Unmarshal([]byte(prior.Payload), &comment) == nil && comment.ID != "" {
				if comment.CreatedAt == "" {
					comment.CreatedAt = prior.At
				}
				comments[comment.ID] = comment
			}
		}
	}
	return http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		writer.Header().Set("X-Content-Type-Options", "nosniff")
		writer.Header().Set("Cache-Control", "no-store")
		if request.URL.Path == "/health" && request.Method == http.MethodGet {
			writer.Header().Set("Content-Type", "application/json")
			_, _ = writer.Write([]byte(`{"status":"ok"}`))
			return
		}
		identity, authenticated := identityFor(token, request.Header.Get("Authorization"))
		if token == "" || !authenticated {
			http.Error(writer, "unauthorized", http.StatusUnauthorized)
			return
		}
		if request.URL.Path == "/api/v1/admin/backup" && request.Method == http.MethodPost {
			if !allows(identity, "admin.manage") {
				http.Error(writer, "forbidden", http.StatusForbidden)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", http.StatusServiceUnavailable)
				return
			}
			if request.ContentLength > 0 {
				http.Error(writer, "body not supported", http.StatusBadRequest)
				return
			}
			// The location is selected by the administrator operating the server,
			// not by any client-supplied path.
			directory := os.Getenv("AURA_CENTRAL_BACKUP_DIR")
			if directory == "" {
				http.Error(writer, "AURA_CENTRAL_BACKUP_DIR is not configured", http.StatusServiceUnavailable)
				return
			}
			name, err := centralMirror.snapshot(directory)
			if err != nil {
				http.Error(writer, "central backup failed", http.StatusInternalServerError)
				return
			}
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(map[string]string{"filename": name, "status": "created"})
			return
		}
		if managedAdminHandler(writer, request, identity) {
			return
		}
		// Administrative account inventory is intentionally read-only: active
		// server credentials are configured through AURA_SYNC_IDENTITIES.
		// Never expose bearer-token map keys in an API response.
		if request.URL.Path == "/api/v1/admin/accounts" && request.Method == http.MethodGet {
			if !allows(identity, "admin.manage") {
				http.Error(writer, "forbidden", http.StatusForbidden)
				return
			}
			identities, err := configuredIdentities()
			if err != nil {
				http.Error(writer, "identity configuration invalid", 500)
				return
			}
			accounts := []map[string]any{}
			if identities == nil {
				accounts = append(accounts, map[string]any{"employeeId": int64(1), "permissions": []string{"*"}, "source": "legacy-server-token"})
			} else {
				seen := map[int64]bool{}
				for _, account := range identities {
					if seen[account.EmployeeID] {
						continue
					}
					seen[account.EmployeeID] = true
					accounts = append(accounts, map[string]any{"employeeId": account.EmployeeID, "permissions": account.Permissions, "source": "server-configuration"})
				}
				sort.Slice(accounts, func(i, j int) bool { return accounts[i]["employeeId"].(int64) < accounts[j]["employeeId"].(int64) })
			}
			accounts = append(accounts, managedAccountsForInventory()...)
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(accounts)
			return
		}
		if request.URL.Path == "/api/v1/admin/permissions" && request.Method == http.MethodGet {
			if !allows(identity, "admin.manage") {
				http.Error(writer, "forbidden", http.StatusForbidden)
				return
			}
			identities, err := configuredIdentities()
			if err != nil {
				http.Error(writer, "identity configuration invalid", 500)
				return
			}
			permissions := map[string]bool{}
			if identities == nil {
				permissions["*"] = true
			} else {
				for _, account := range identities {
					for _, name := range account.Permissions {
						permissions[name] = true
					}
				}
			}
			items := []string{}
			for name := range permissions {
				items = append(items, name)
			}
			sort.Strings(items)
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(items)
			return
		}
		if request.URL.Path == "/api/v1/admin/event-history" && request.Method == http.MethodGet {
			if !allows(identity, "audit.read") {
				http.Error(writer, "forbidden", http.StatusForbidden)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", http.StatusServiceUnavailable)
				return
			}
			entries, err := centralMirror.eventHistory()
			if err != nil {
				http.Error(writer, "persistence failure", http.StatusInternalServerError)
				return
			}
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(entries)
			return
		}
		// Fence a desktop archive that was never accepted before discarding it
		// locally. The reviewed central state must still match exactly.
		if request.URL.Path == "/api/v1/desktop-attachment-abandon" && request.Method == http.MethodPost {
			if centralMirror == nil {
				http.Error(writer, "central database required", http.StatusServiceUnavailable)
				return
			}
			defer request.Body.Close()
			var input struct {
				Event    syncEvent         `json:"event"`
				Expected centralAttachment `json:"expected"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil {
				http.Error(writer, "invalid request", http.StatusBadRequest)
				return
			}
			var extra any
			if decoder.Decode(&extra) != io.EOF {
				http.Error(writer, "invalid request", http.StatusBadRequest)
				return
			}
			op := input.Event
			var attachment centralAttachment
			if op.Topic != "attachment.archived" || strings.TrimSpace(op.ClientID) == "" || len(op.ClientID) > 128 || strings.TrimSpace(op.OperationID) == "" || len(op.OperationID) > 128 || json.Unmarshal([]byte(op.Payload), &attachment) != nil || attachment.ID == "" || (attachment.SubjectType != "task" && attachment.SubjectType != "ticket") || attachment.Active || attachment.ImportedBy != 0 {
				http.Error(writer, "invalid attachment operation", http.StatusBadRequest)
				return
			}
			if !allows(identity, "sync.events.write") && !allows(identity, attachment.SubjectType+"s.manage") {
				http.Error(writer, "forbidden", http.StatusForbidden)
				return
			}
			canonical, _ := json.Marshal(struct{ Topic, Payload string }{op.Topic, op.Payload})
			sum := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(sum[:])
			abandoned := "abandoned:" + fingerprint
			client := "desktop-attachment:" + op.ClientID
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, client, op.OperationID, fingerprint) {
			case 1:
				http.Error(writer, "operation already accepted", http.StatusConflict)
				return
			case -1:
				http.Error(writer, "receipt storage failure", http.StatusInternalServerError)
				return
			}
			switch centralMirror.roomReceipt(identity.EmployeeID, client, op.OperationID, abandoned) {
			case 1:
				current, found := attachments[attachment.ID]
				if !found || current != input.Expected {
					http.Error(writer, "attachment review changed", http.StatusConflict)
					return
				}
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", http.StatusConflict)
				return
			case -1:
				http.Error(writer, "receipt storage failure", http.StatusInternalServerError)
				return
			}
			switch centralMirror.anyReceipt(client, op.OperationID) {
			case 1:
				http.Error(writer, "operation belongs to another account", http.StatusConflict)
				return
			case -1:
				http.Error(writer, "receipt storage failure", http.StatusInternalServerError)
				return
			}
			current, found := attachments[attachment.ID]
			candidate := attachment
			candidate.Active = current.Active
			candidate.ImportedBy = current.ImportedBy
			if !found || current != input.Expected || candidate != current {
				http.Error(writer, "attachment review changed", http.StatusConflict)
				return
			}
			payload, _ := json.Marshal(map[string]any{"clientId": op.ClientID, "operationId": op.OperationID, "actorId": identity.EmployeeID, "attachmentId": attachment.ID, "original": attachment, "expected": current})
			decision := syncEvent{Topic: "central.attachment_sync.abandoned", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, client, op.OperationID, abandoned, decision) != nil {
				http.Error(writer, "persistence failure", http.StatusInternalServerError)
				return
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		// Fence a never-accepted create before a client maps its local room to
		// a different existing room. The original operation can never arrive late.
		if request.URL.Path == "/api/v1/room-sync-abandon" && request.Method == http.MethodPost {
			if !allows(identity, "rooms.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			var input struct {
				Operation roomSyncOperation `json:"operation"`
				Remote    centralRoom       `json:"remote"`
			}
			defer request.Body.Close()
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil {
				http.Error(writer, "invalid request", 400)
				return
			}
			op := input.Operation
			if strings.TrimSpace(op.ClientID) == "" || len(op.ClientID) > 128 || strings.TrimSpace(op.OperationID) == "" || len(op.OperationID) > 128 || op.Action != "create" || op.Expected != nil || op.Room.ID == "" || op.Room.Name == "" || op.Room.Location == "" || op.Room.Capacity <= 0 || !op.Room.Active || input.Remote.ID == "" || input.Remote.ID == op.Room.ID {
				http.Error(writer, "invalid room operation", 400)
				return
			}
			canonical, _ := json.Marshal(op)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			abandoned := "abandoned:" + fingerprint
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, op.ClientID, op.OperationID, fingerprint) {
			case 1:
				http.Error(writer, "operation already accepted", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			switch centralMirror.roomReceipt(identity.EmployeeID, op.ClientID, op.OperationID, abandoned) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			remote, exists := rooms[input.Remote.ID]
			_, ownExists := rooms[op.Room.ID]
			if ownExists || !exists || remote != input.Remote || !strings.EqualFold(remote.Name, op.Room.Name) {
				http.Error(writer, "room conflict changed", 409)
				return
			}
			payload, _ := json.Marshal(map[string]any{"clientId": op.ClientID, "operationId": op.OperationID, "actorId": identity.EmployeeID, "original": op.Room, "remote": remote})
			event := syncEvent{Topic: "central.room_sync.abandoned", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, op.ClientID, op.OperationID, abandoned, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		if request.URL.Path == "/api/v1/room-sync" && request.Method == http.MethodPost {
			if !allows(identity, "rooms.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			defer request.Body.Close()
			var input roomSyncOperation
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ClientID) == "" || len(input.ClientID) > 128 || strings.TrimSpace(input.OperationID) == "" || len(input.OperationID) > 128 || strings.TrimSpace(input.Room.ID) == "" || len(input.Room.ID) > 256 || strings.TrimSpace(input.Room.Name) == "" || strings.TrimSpace(input.Room.Location) == "" || input.Room.Capacity <= 0 || (input.Action != "create" && input.Action != "update" && input.Action != "archive") || (input.Action == "archive" && input.Room.Active) || (input.Action == "create" && !input.Room.Active) {
				http.Error(writer, "invalid room sync request", 400)
				return
			}
			canonical, _ := json.Marshal(input)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			existing, found := rooms[input.Room.ID]
			if input.Action == "create" {
				if found || input.Expected != nil {
					http.Error(writer, "room exists or unexpected base", 409)
					return
				}
			} else if !found || input.Expected == nil || *input.Expected != existing || (input.Action == "archive" && !existing.Active) {
				http.Error(writer, "room changed on server", 409)
				return
			}
			for id, v := range rooms {
				if id != input.Room.ID && strings.EqualFold(v.Name, input.Room.Name) {
					http.Error(writer, "room name already exists", 409)
					return
				}
			}
			topic := "room." + map[string]string{"create": "created", "update": "updated", "archive": "archived"}[input.Action]
			payload, _ := json.Marshal(input.Room)
			event := syncEvent{Topic: topic, Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			rooms[input.Room.ID] = input.Room
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(event); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		// Local task sync persists exactly the state supplied by the client.
		// Recurring successors are separate local operations, preventing duplicates.
		// Metadata and acknowledgement are committed atomically; object bytes are
		// uploaded separately and verified before accepting a create operation.
		if request.URL.Path == "/api/v1/attachment-sync" || request.URL.Path == "/api/v1/personal-attachment-sync" {
			personal := request.URL.Path == "/api/v1/personal-attachment-sync"
			kind := request.URL.Query().Get("subjectType")
			if kind != "task" && kind != "ticket" {
				http.Error(writer, "invalid subject type", 400)
				return
			}
			permission := "tasks.manage"
			if kind == "ticket" {
				permission = "tickets.manage"
			}
			readPermission := "tasks.read"
			if kind == "ticket" {
				readPermission = "tickets.read"
			}
			if (!personal && (!allows(identity, permission) || !allows(identity, "employees.manage"))) || (personal && (!allows(identity, "collaboration.comment") || (!allows(identity, readPermission) && !allows(identity, permission)))) {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			if request.Method == http.MethodGet {
				if personal {
					http.Error(writer, "method not allowed", 405)
					return
				}
				registry.Lock()
				defer registry.Unlock()
				result := []centralAttachment{}
				for _, a := range attachments {
					if a.SubjectType == kind {
						result = append(result, a)
					}
				}
				sort.Slice(result, func(i, j int) bool { return result[i].ID < result[j].ID })
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method != http.MethodPost {
				http.Error(writer, "method not allowed", 405)
				return
			}
			defer request.Body.Close()
			var input struct {
				ClientID        string             `json:"clientId"`
				OperationID     string             `json:"operationId"`
				Action          string             `json:"action"`
				Attachment      centralAttachment  `json:"attachment"`
				Expected        *centralAttachment `json:"expected"`
				ActorExternalID string             `json:"actorExternalId,omitempty"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ClientID) == "" || len(input.ClientID) > 128 || strings.TrimSpace(input.OperationID) == "" || len(input.OperationID) > 128 || strings.TrimSpace(input.Attachment.ID) == "" || len(input.Attachment.ID) > 256 || input.Attachment.SubjectType != kind || strings.TrimSpace(input.Attachment.SubjectID) == "" || len(input.Attachment.SubjectID) > 256 || strings.TrimSpace(input.Attachment.OriginalName) == "" || len(input.Attachment.OriginalName) > 255 || filepath.Base(input.Attachment.OriginalName) != input.Attachment.OriginalName || strings.ContainsAny(input.Attachment.OriginalName, "\\\x00") || input.Attachment.SizeBytes < 0 || input.Attachment.SizeBytes > 100*1024*1024 || len(input.Attachment.SHA256) != 64 || strings.ToLower(input.Attachment.SHA256) != input.Attachment.SHA256 || strings.TrimSpace(input.Attachment.UploadedBy) == "" || len(input.Attachment.UploadedBy) > 256 || (input.Action != "create" && input.Action != "archive" && input.Action != "restore") || (input.Action == "create" && (!input.Attachment.Active || input.Attachment.ImportedBy != 0)) || (input.Action == "archive" && input.Attachment.Active) || (input.Action == "restore" && !input.Attachment.Active) {
				http.Error(writer, "invalid attachment sync request", 400)
				return
			}
			if _, err := hex.DecodeString(input.Attachment.SHA256); err != nil {
				http.Error(writer, "invalid attachment hash", 400)
				return
			}
			if personal && ((input.Action != "create" && input.Action != "archive") || (input.Action == "create" && !strings.HasPrefix(input.Attachment.ID, input.ClientID+":attachment:")) || (input.Action == "archive" && (input.ActorExternalID == "" || len(input.ActorExternalID) > 256))) {
				http.Error(writer, "invalid personal attachment operation", 403)
				return
			}
			canonical, _ := json.Marshal(input)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			if kind == "task" {
				task, ok := tasks[input.Attachment.SubjectID]
				if !ok {
					http.Error(writer, "task missing", 409)
					return
				}
				if personal && (!task.Active || (!allows(identity, "tasks.manage") && task.AssigneeID != identity.EmployeeID && task.CreatedBy != identity.EmployeeID)) {
					http.Error(writer, "task not accessible", 403)
					return
				}
			} else {
				ticket, ok := tickets[input.Attachment.SubjectID]
				if !ok {
					http.Error(writer, "ticket missing", 409)
					return
				}
				if personal && (!ticket.Active || (!allows(identity, "tickets.manage") && ticket.AssigneeID != identity.EmployeeID && ticket.CreatedBy != identity.EmployeeID)) {
					http.Error(writer, "ticket not accessible", 403)
					return
				}
			}
			if personal && ((input.Action == "create" && input.Attachment.UploadedBy != approvalEmployee(strconv.FormatInt(identity.EmployeeID, 10))) || (input.Action == "archive" && input.ActorExternalID != approvalEmployee(strconv.FormatInt(identity.EmployeeID, 10)))) {
				http.Error(writer, "attachment author not accessible", 403)
				return
			}
			existing, found := attachments[input.Attachment.ID]
			if input.Action == "create" {
				if found || input.Expected != nil {
					http.Error(writer, "attachment exists or unexpected base", 409)
					return
				}
				author, parseErr := strconv.ParseInt(input.Attachment.UploadedBy, 10, 64)
				numeric := parseErr == nil && author > 0 && strconv.FormatInt(author, 10) == input.Attachment.UploadedBy
				count := 0
				for _, e := range employees {
					if (numeric && e.AccountEmployeeID == author) || (!numeric && e.ID == input.Attachment.UploadedBy && e.AccountEmployeeID > 0) {
						count++
					}
				}
				if count != 1 {
					http.Error(writer, "uploader has no unique employee mapping", 409)
					return
				}
			}
			if input.Action == "create" || input.Action == "restore" {
				path := filepath.Join(attachmentRoot, input.Attachment.SHA256)
				info, err := os.Lstat(path)
				if err != nil || !info.Mode().IsRegular() || info.Size() != input.Attachment.SizeBytes {
					http.Error(writer, "attachment object missing or invalid", 409)
					return
				}
				f, err := os.Open(path)
				if err != nil {
					http.Error(writer, "attachment object unavailable", 409)
					return
				}
				h := sha256.New()
				n, copyErr := io.Copy(h, io.LimitReader(f, 100*1024*1024+1))
				closeErr := f.Close()
				if copyErr != nil || closeErr != nil || n != input.Attachment.SizeBytes || hex.EncodeToString(h.Sum(nil)) != input.Attachment.SHA256 {
					http.Error(writer, "attachment object integrity mismatch", 409)
					return
				}
			}
			if input.Action == "create" {
				input.Attachment.ImportedBy = identity.EmployeeID
			} else {
				if !found || input.Expected == nil || *input.Expected != existing || existing.Active == input.Attachment.Active {
					http.Error(writer, "attachment changed on server", 409)
					return
				}
				next := existing
				next.Active = input.Action == "restore"
				if input.Attachment != next {
					http.Error(writer, "attachment metadata cannot be changed", 409)
					return
				}
			}
			topic := "attachment.created"
			if input.Action == "archive" {
				topic = "attachment.archived"
			} else if input.Action == "restore" {
				topic = "attachment.restored"
			}
			payload, _ := json.Marshal(input.Attachment)
			if personal && input.Action == "archive" {
				payload, _ = json.Marshal(struct {
					centralAttachment
					ArchiveActorID         int64  `json:"archiveActorId"`
					ArchiveActorExternalID string `json:"archiveActorExternalId"`
				}{input.Attachment, identity.EmployeeID, input.ActorExternalID})
			}
			event := syncEvent{Topic: topic, Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			attachments[input.Attachment.ID] = input.Attachment
			if input.Action == "restore" {
				attachmentRestored[input.Attachment.ID] = true
			}
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(event); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		if request.URL.Path == "/api/v1/ticket-sync" {
			if !allows(identity, "tickets.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			if request.Method == http.MethodGet {
				registry.Lock()
				defer registry.Unlock()
				result := []centralTicket{}
				for _, ticket := range tickets {
					result = append(result, ticket)
				}
				sort.Slice(result, func(i, j int) bool { return result[i].ID < result[j].ID })
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method != http.MethodPost {
				http.Error(writer, "method not allowed", 405)
				return
			}
			defer request.Body.Close()
			var input struct {
				ClientID    string         `json:"clientId"`
				OperationID string         `json:"operationId"`
				Action      string         `json:"action"`
				Ticket      centralTicket  `json:"ticket"`
				Expected    *centralTicket `json:"expected"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ClientID) == "" || len(input.ClientID) > 128 || strings.TrimSpace(input.OperationID) == "" || len(input.OperationID) > 128 || strings.TrimSpace(input.Ticket.ID) == "" || len(input.Ticket.ID) > 256 || strings.TrimSpace(input.Ticket.Title) == "" || strings.TrimSpace(input.Ticket.Category) == "" || (input.Ticket.Priority != "Niedrig" && input.Ticket.Priority != "Mittel" && input.Ticket.Priority != "Hoch" && input.Ticket.Priority != "Kritisch") || !validTicketStatus(input.Ticket.Status) || input.Ticket.AssigneeID <= 0 || input.Ticket.CreatedBy <= 0 || (input.Action != "create" && input.Action != "update" && input.Action != "archive") || (input.Action == "create" && !input.Ticket.Active) || (input.Action == "archive" && input.Ticket.Active) {
				http.Error(writer, "invalid ticket sync request", 400)
				return
			}
			canonical, _ := json.Marshal(input)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			existing, found := tickets[input.Ticket.ID]
			if input.Action == "create" {
				if found || input.Expected != nil {
					http.Error(writer, "ticket exists or unexpected base", 409)
					return
				}
			} else if !found || input.Expected == nil || *input.Expected != existing || (input.Action == "archive" && !existing.Active) {
				http.Error(writer, "ticket changed on server", 409)
				return
			}
			if input.Action == "create" && input.Ticket.CreatedBy != identity.EmployeeID {
				http.Error(writer, "creator identity mismatch", 403)
				return
			}
			if input.Action != "create" && input.Ticket.CreatedBy != existing.CreatedBy {
				http.Error(writer, "ticket creator cannot be changed", 409)
				return
			}
			topic := "ticket." + map[string]string{"create": "created", "update": "updated", "archive": "archived"}[input.Action]
			payload, _ := json.Marshal(input.Ticket)
			event := syncEvent{Topic: topic, Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			tickets[input.Ticket.ID] = input.Ticket
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(event); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		// Each operation is submitted by its actual requester or decision maker.
		if request.URL.Path == "/api/v1/approval-sync" {
			if !allows(identity, "tasks.read") && !allows(identity, "tasks.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			actor := strconv.FormatInt(identity.EmployeeID, 10)
			if request.Method == http.MethodGet {
				registry.Lock()
				defer registry.Unlock()
				result := []centralTaskApproval{}
				for _, a := range approvals {
					projected, mapped := projectApproval(a)
					if mapped && projected.Active && (projected.RequestedBy == actor || projected.ApproverID == actor) {
						result = append(result, projected)
					}
				}
				sort.Slice(result, func(i, j int) bool { return result[i].ID < result[j].ID })
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method != http.MethodPost {
				http.Error(writer, "method not allowed", 405)
				return
			}
			defer request.Body.Close()
			var input struct {
				ClientID    string               `json:"clientId"`
				OperationID string               `json:"operationId"`
				Action      string               `json:"action"`
				Approval    centralTaskApproval  `json:"approval"`
				Expected    *centralTaskApproval `json:"expected"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ClientID) == "" || len(input.ClientID) > 128 || strings.TrimSpace(input.OperationID) == "" || len(input.OperationID) > 128 || strings.TrimSpace(input.Approval.ID) == "" || len(input.Approval.ID) > 256 || strings.TrimSpace(input.Approval.TaskID) == "" || len(input.Approval.TaskID) > 256 || !input.Approval.Active || (input.Action != "request" && input.Action != "decide") {
				http.Error(writer, "invalid approval sync request", 400)
				return
			}
			requester, err1 := strconv.ParseInt(input.Approval.RequestedBy, 10, 64)
			approver, err2 := strconv.ParseInt(input.Approval.ApproverID, 10, 64)
			if err1 != nil || err2 != nil || requester <= 0 || approver <= 0 || requester == approver || strconv.FormatInt(requester, 10) != input.Approval.RequestedBy || strconv.FormatInt(approver, 10) != input.Approval.ApproverID {
				http.Error(writer, "invalid approval identities", 400)
				return
			}
			if input.Action == "request" {
				if !allows(identity, "tasks.manage") || input.Approval.RequestedBy != actor {
					http.Error(writer, "requester identity mismatch", 403)
					return
				}
				if input.Approval.Status != "pending" || input.Approval.DecidedBy != "" || input.Expected != nil {
					http.Error(writer, "invalid approval request state", 400)
					return
				}
				identities, err := configuredIdentities()
				if err != nil {
					http.Error(writer, "identity configuration invalid", 500)
					return
				}
				allowed := identities == nil && approver == 1
				for _, candidate := range identities {
					if candidate.EmployeeID == approver && (allows(candidate, "tasks.read") || allows(candidate, "tasks.manage")) {
						allowed = true
						break
					}
				}
				if !allowed {
					http.Error(writer, "approver account unavailable or lacks task access", 400)
					return
				}
			} else {
				if input.Approval.ApproverID != actor || input.Approval.DecidedBy != actor {
					http.Error(writer, "decision identity mismatch", 403)
					return
				}
				if input.Approval.Status != "approved" && input.Approval.Status != "rejected" {
					http.Error(writer, "invalid decision", 400)
					return
				}
			}
			canonical, _ := json.Marshal(input)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			task, found := tasks[input.Approval.TaskID]
			if !found || !task.Active {
				http.Error(writer, "task not active", 409)
				return
			}
			existing, found := approvals[input.Approval.ID]
			if input.Action == "request" {
				if found {
					http.Error(writer, "approval exists", 409)
					return
				}
			} else {
				projected, mapped := projectApproval(existing)
				if !found || !mapped || input.Expected == nil || *input.Expected != projected || existing.Status != "pending" || !existing.Active {
					http.Error(writer, "approval changed on server", 409)
					return
				}
				if projected.ID != input.Approval.ID || projected.TaskID != input.Approval.TaskID || projected.RequestedBy != input.Approval.RequestedBy || projected.ApproverID != input.Approval.ApproverID {
					http.Error(writer, "approval identity cannot change", 409)
					return
				}
			}
			stored := input.Approval
			if input.Action == "decide" {
				stored.RequestedBy, stored.ApproverID, stored.DecidedBy = existing.RequestedBy, existing.ApproverID, existing.ApproverID
			} else if requesterID, approverID := approvalEmployee(input.Approval.RequestedBy), approvalEmployee(input.Approval.ApproverID); requesterID != "" && approverID != "" {
				stored.RequestedBy, stored.ApproverID = requesterID, approverID
			}
			topic := "task.approval.requested"
			if input.Action == "decide" {
				topic = "task.approval.decided"
			}
			payload, _ := json.Marshal(stored)
			event := syncEvent{Topic: topic, Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			approvals[input.Approval.ID] = stored
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(event); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		if request.URL.Path == "/api/v1/maintenance-sync" {
			if !allows(identity, "facility.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			if request.Method == http.MethodGet {
				registry.Lock()
				defer registry.Unlock()
				result := []centralMaintenanceOrder{}
				for _, asset := range maintenanceOrders {
					result = append(result, asset)
				}
				sort.Slice(result, func(i, j int) bool { return result[i].ID < result[j].ID })
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method != http.MethodPost {
				http.Error(writer, "method not allowed", 405)
				return
			}
			defer request.Body.Close()
			var input struct {
				ClientID              string                   `json:"clientId"`
				OperationID           string                   `json:"operationId"`
				Action                string                   `json:"action"`
				MaintenanceOrder      centralMaintenanceOrder  `json:"maintenanceOrder"`
				Expected              *centralMaintenanceOrder `json:"expected"`
				RecurrenceSuccessorID string                   `json:"recurrenceSuccessorId,omitempty"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ClientID) == "" || len(input.ClientID) > 128 || strings.TrimSpace(input.OperationID) == "" || len(input.OperationID) > 128 || strings.TrimSpace(input.MaintenanceOrder.ID) == "" || len(input.MaintenanceOrder.ID) > 256 || strings.TrimSpace(input.MaintenanceOrder.Title) == "" || strings.TrimSpace(input.MaintenanceOrder.AssetID) == "" || len(input.MaintenanceOrder.AssetID) > 256 || input.MaintenanceOrder.DueAt < 0 || input.MaintenanceOrder.RecurrenceDays < 0 || input.MaintenanceOrder.RecurrenceDays > 3660 || (input.MaintenanceOrder.Status != "open" && input.MaintenanceOrder.Status != "in_progress" && input.MaintenanceOrder.Status != "completed" && input.MaintenanceOrder.Status != "cancelled") || (input.Action != "create" && input.Action != "update" && input.Action != "archive") || (input.Action == "create" && !input.MaintenanceOrder.Active) || (input.Action == "archive" && input.MaintenanceOrder.Active) {
				http.Error(writer, "invalid maintenance order sync request", 400)
				return
			}
			canonical, _ := json.Marshal(input)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			existing, found := maintenanceOrders[input.MaintenanceOrder.ID]
			if input.Action == "create" {
				if owner, bound := maintenanceReservationOwners[input.MaintenanceOrder.ID]; bound {
					if owner.ClientID != input.ClientID || owner.ActorID != identity.EmployeeID {
						http.Error(writer, "recurrence successor reserved for another client", 409)
						return
					}
					for parent, child := range maintenanceSuccessors {
						if child == input.MaintenanceOrder.ID {
							source, ok := maintenanceOrders[parent]
							if !ok || source.AssetID != input.MaintenanceOrder.AssetID {
								http.Error(writer, "recurrence successor asset differs", 409)
								return
							}
						}
					}
				}
			}
			if input.RecurrenceSuccessorID != "" {
				child := input.RecurrenceSuccessorID
				if len(child) > 256 || strings.TrimSpace(child) != child || child == input.MaintenanceOrder.ID || input.Action == "archive" || !input.MaintenanceOrder.Active || input.MaintenanceOrder.Status != "completed" || input.MaintenanceOrder.RecurrenceDays <= 0 {
					http.Error(writer, "invalid recurrence reservation", 400)
					return
				}
				if existing := maintenanceSuccessors[input.MaintenanceOrder.ID]; existing != "" && existing != child {
					http.Error(writer, "different recurrence successor", 409)
					return
				}
				if other, exists := maintenanceOrders[child]; exists {
					if maintenanceSuccessors[input.MaintenanceOrder.ID] != child || other.AssetID != input.MaintenanceOrder.AssetID {
						http.Error(writer, "successor already exists", 409)
						return
					}
				} else {
					if !strings.HasPrefix(child, input.ClientID+":maintenance_order:") {
						http.Error(writer, "successor identity belongs to another client", 409)
						return
					}
					if owner, bound := maintenanceReservationOwners[child]; bound && (owner.ClientID != input.ClientID || owner.ActorID != identity.EmployeeID) {
						http.Error(writer, "recurrence reservation belongs to another client", 409)
						return
					}
				}
				for source, successor := range maintenanceSuccessors {
					if source != input.MaintenanceOrder.ID && successor == child {
						http.Error(writer, "successor already reserved", 409)
						return
					}
				}
				seen := map[string]bool{}
				for cursor := child; cursor != ""; cursor = maintenanceSuccessors[cursor] {
					if cursor == input.MaintenanceOrder.ID || seen[cursor] {
						http.Error(writer, "recurrence cycle", 409)
						return
					}
					seen[cursor] = true
				}
			}
			if child := maintenanceSuccessors[input.MaintenanceOrder.ID]; child != "" {
				if _, exists := maintenanceOrders[child]; !exists {
					if found && input.MaintenanceOrder.AssetID != existing.AssetID {
						http.Error(writer, "cannot change reserved maintenance asset", 409)
						return
					}
					if input.Action != "archive" && input.MaintenanceOrder.Status == "completed" {
						owner := maintenanceReservationOwners[child]
						if input.RecurrenceSuccessorID != child || owner.ClientID != input.ClientID || owner.ActorID != identity.EmployeeID {
							http.Error(writer, "recurrence successor is pending", 409)
							return
						}
					}
				}
			}
			if input.Action == "create" {
				if found || input.Expected != nil {
					http.Error(writer, "maintenance order exists or unexpected base", 409)
					return
				}
			} else if !found || input.Expected == nil || *input.Expected != existing || (input.Action == "archive" && !existing.Active) {
				http.Error(writer, "maintenance order changed on server", 409)
				return
			}
			if _, exists := assets[input.MaintenanceOrder.AssetID]; !exists {
				http.Error(writer, "facility asset missing", 409)
				return
			}
			topic := "facility.maintenance." + map[string]string{"create": "created", "update": "updated", "archive": "archived"}[input.Action]
			reservationClient := ""
			var reservationActor int64
			if input.RecurrenceSuccessorID != "" {
				if _, exists := maintenanceOrders[input.RecurrenceSuccessorID]; !exists {
					reservationClient = input.ClientID
					reservationActor = identity.EmployeeID
				}
			}
			payload, _ := json.Marshal(struct {
				centralMaintenanceOrder
				RecurrenceSuccessorID string `json:"recurrenceSuccessorId,omitempty"`
				ReservationClient     string `json:"recurrenceReservationClientId,omitempty"`
				ReservationActor      int64  `json:"recurrenceReservationActorId,omitempty"`
			}{input.MaintenanceOrder, input.RecurrenceSuccessorID, reservationClient, reservationActor})
			event := syncEvent{Topic: topic, Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			maintenanceOrders[input.MaintenanceOrder.ID] = input.MaintenanceOrder
			if input.RecurrenceSuccessorID != "" {
				maintenanceSuccessors[input.MaintenanceOrder.ID] = input.RecurrenceSuccessorID
				if reservationClient != "" {
					maintenanceReservationOwners[input.RecurrenceSuccessorID] = recurrenceReservationOwner{reservationClient, reservationActor}
				}
			}
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(event); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		if request.URL.Path == "/api/v1/facility-asset-sync" {
			if !allows(identity, "facility.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			if request.Method == http.MethodGet {
				registry.Lock()
				defer registry.Unlock()
				result := []centralFacilityAsset{}
				for _, asset := range assets {
					result = append(result, asset)
				}
				sort.Slice(result, func(i, j int) bool { return result[i].ID < result[j].ID })
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method != http.MethodPost {
				http.Error(writer, "method not allowed", 405)
				return
			}
			defer request.Body.Close()
			var input struct {
				ClientID      string                `json:"clientId"`
				OperationID   string                `json:"operationId"`
				Action        string                `json:"action"`
				FacilityAsset centralFacilityAsset  `json:"facilityAsset"`
				Expected      *centralFacilityAsset `json:"expected"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ClientID) == "" || len(input.ClientID) > 128 || strings.TrimSpace(input.OperationID) == "" || len(input.OperationID) > 128 || strings.TrimSpace(input.FacilityAsset.ID) == "" || len(input.FacilityAsset.ID) > 256 || strings.TrimSpace(input.FacilityAsset.Name) == "" || strings.TrimSpace(input.FacilityAsset.Category) == "" || strings.TrimSpace(input.FacilityAsset.Location) == "" || (input.FacilityAsset.Status != "operational" && input.FacilityAsset.Status != "maintenance" && input.FacilityAsset.Status != "out_of_service") || (input.Action != "create" && input.Action != "update" && input.Action != "archive") || (input.Action == "create" && !input.FacilityAsset.Active) || (input.Action == "archive" && input.FacilityAsset.Active) {
				http.Error(writer, "invalid facility-asset sync request", 400)
				return
			}
			canonical, _ := json.Marshal(input)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			existing, found := assets[input.FacilityAsset.ID]
			if input.Action == "create" {
				if found || input.Expected != nil {
					http.Error(writer, "facility-asset exists or unexpected base", 409)
					return
				}
			} else if !found || input.Expected == nil || *input.Expected != existing || (input.Action == "archive" && !existing.Active) {
				http.Error(writer, "facility-asset changed on server", 409)
				return
			}
			for id, v := range assets {
				if id != input.FacilityAsset.ID && strings.EqualFold(v.Name, input.FacilityAsset.Name) {
					http.Error(writer, "facility-asset name already exists", 409)
					return
				}
			}
			topic := "facility.asset." + map[string]string{"create": "created", "update": "updated", "archive": "archived"}[input.Action]
			payload, _ := json.Marshal(input.FacilityAsset)
			event := syncEvent{Topic: topic, Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			assets[input.FacilityAsset.ID] = input.FacilityAsset
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(event); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		if request.URL.Path == "/api/v1/organization-sync" {
			if !allows(identity, "employees.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			if request.Method == http.MethodGet {
				registry.Lock()
				defer registry.Unlock()
				result := []centralOrganization{}
				for _, organization := range organizations {
					result = append(result, organization)
				}
				sort.Slice(result, func(i, j int) bool { return result[i].ID < result[j].ID })
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method != http.MethodPost {
				http.Error(writer, "method not allowed", 405)
				return
			}
			defer request.Body.Close()
			var input struct {
				ClientID     string               `json:"clientId"`
				OperationID  string               `json:"operationId"`
				Action       string               `json:"action"`
				Organization centralOrganization  `json:"organization"`
				Expected     *centralOrganization `json:"expected"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ClientID) == "" || len(input.ClientID) > 128 || strings.TrimSpace(input.OperationID) == "" || len(input.OperationID) > 128 || strings.TrimSpace(input.Organization.ID) == "" || len(input.Organization.ID) > 256 || strings.TrimSpace(input.Organization.Name) == "" || strings.TrimSpace(input.Organization.Code) == "" || input.Organization.ManagerID < 0 || (input.Action != "create" && input.Action != "update" && input.Action != "archive") || (input.Action == "create" && !input.Organization.Active) || (input.Action == "archive" && input.Organization.Active) {
				http.Error(writer, "invalid organization sync request", 400)
				return
			}
			canonical, _ := json.Marshal(input)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			existing, found := organizations[input.Organization.ID]
			if input.Action == "create" {
				if found || input.Expected != nil {
					http.Error(writer, "organization exists or unexpected base", 409)
					return
				}
			} else if !found || input.Expected == nil || *input.Expected != existing || (input.Action == "archive" && !existing.Active) {
				http.Error(writer, "organization changed on server", 409)
				return
			}
			for id, v := range organizations {
				if id != input.Organization.ID && (strings.EqualFold(v.Name, input.Organization.Name) || strings.EqualFold(v.Code, input.Organization.Code)) {
					http.Error(writer, "organization name already exists", 409)
					return
				}
			}
			topic := "organization." + map[string]string{"create": "created", "update": "updated", "archive": "archived"}[input.Action]
			payload, _ := json.Marshal(input.Organization)
			event := syncEvent{Topic: topic, Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			organizations[input.Organization.ID] = input.Organization
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(event); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		if request.URL.Path == "/api/v1/project-sync" {
			if !allows(identity, "projects.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			if request.Method == http.MethodGet {
				registry.Lock()
				defer registry.Unlock()
				result := []centralProject{}
				for _, project := range projects {
					result = append(result, project)
				}
				sort.Slice(result, func(i, j int) bool { return result[i].ID < result[j].ID })
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method != http.MethodPost {
				http.Error(writer, "method not allowed", 405)
				return
			}
			defer request.Body.Close()
			var input struct {
				ClientID    string          `json:"clientId"`
				OperationID string          `json:"operationId"`
				Action      string          `json:"action"`
				Project     centralProject  `json:"project"`
				Expected    *centralProject `json:"expected"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ClientID) == "" || len(input.ClientID) > 128 || strings.TrimSpace(input.OperationID) == "" || len(input.OperationID) > 128 || strings.TrimSpace(input.Project.ID) == "" || len(input.Project.ID) > 256 || strings.TrimSpace(input.Project.Name) == "" || strings.TrimSpace(input.Project.Department) == "" || !validProjectStatus(input.Project.Status) || input.Project.OwnerID <= 0 || (input.Action != "create" && input.Action != "update" && input.Action != "archive") || (input.Action == "create" && !input.Project.Active) || (input.Action == "archive" && input.Project.Active) {
				http.Error(writer, "invalid project sync request", 400)
				return
			}
			canonical, _ := json.Marshal(input)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			existing, found := projects[input.Project.ID]
			if input.Action == "create" {
				if found || input.Expected != nil {
					http.Error(writer, "project exists or unexpected base", 409)
					return
				}
			} else if !found || input.Expected == nil || *input.Expected != existing || (input.Action == "archive" && !existing.Active) {
				http.Error(writer, "project changed on server", 409)
				return
			}
			for id, v := range projects {
				if id != input.Project.ID && strings.EqualFold(v.Name, input.Project.Name) {
					http.Error(writer, "project name already exists", 409)
					return
				}
			}
			topic := "project." + map[string]string{"create": "created", "update": "updated", "archive": "archived"}[input.Action]
			payload, _ := json.Marshal(input.Project)
			event := syncEvent{Topic: topic, Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			projects[input.Project.ID] = input.Project
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(event); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		// Administrative import of immutable local ticket comments. The server records
		// the importer separately from the original, mapped author account.
		if request.URL.Path == "/api/v1/ticket-comment-sync" || request.URL.Path == "/api/v1/personal-ticket-comment-sync" {
			personal := request.URL.Path == "/api/v1/personal-ticket-comment-sync"
			if (!personal && (!allows(identity, "tickets.manage") || !allows(identity, "employees.manage"))) || (personal && (!allows(identity, "collaboration.comment") || (!allows(identity, "tickets.read") && !allows(identity, "tickets.manage")))) {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			if request.Method == http.MethodGet {
				if personal {
					http.Error(writer, "method not allowed", 405)
					return
				}
				registry.Lock()
				defer registry.Unlock()
				result := []centralComment{}
				for _, c := range comments {
					if c.SubjectType == "ticket" {
						result = append(result, c)
					}
				}
				sort.Slice(result, func(i, j int) bool { return result[i].ID < result[j].ID })
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method != http.MethodPost {
				http.Error(writer, "method not allowed", 405)
				return
			}
			defer request.Body.Close()
			var input struct {
				ClientID    string         `json:"clientId"`
				OperationID string         `json:"operationId"`
				Comment     centralComment `json:"comment"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ClientID) == "" || len(input.ClientID) > 128 || strings.TrimSpace(input.OperationID) == "" || len(input.OperationID) > 128 || strings.TrimSpace(input.Comment.ID) == "" || len(input.Comment.ID) > 256 || input.Comment.SubjectType != "ticket" || strings.TrimSpace(input.Comment.SubjectID) == "" || len(input.Comment.SubjectID) > 256 || len(strings.TrimSpace(input.Comment.Body)) == 0 || len(input.Comment.Body) > 16000 || input.Comment.ImportedBy != 0 {
				http.Error(writer, "invalid ticket comment sync request", 400)
				return
			}
			if strings.TrimSpace(input.Comment.AuthorID) == "" || len(input.Comment.AuthorID) > 256 {
				http.Error(writer, "invalid author identity", 400)
				return
			}
			if _, err := time.Parse(time.RFC3339Nano, input.Comment.CreatedAt); err != nil {
				http.Error(writer, "invalid comment timestamp", 400)
				return
			}
			canonical, _ := json.Marshal(input)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			ticket, ok := tickets[input.Comment.SubjectID]
			if !ok {
				http.Error(writer, "ticket missing", 409)
				return
			}
			if personal && (!ticket.Active || !strings.HasPrefix(input.Comment.ID, input.ClientID+":comment:") || (!allows(identity, "tickets.manage") && ticket.AssigneeID != identity.EmployeeID && ticket.CreatedBy != identity.EmployeeID) || input.Comment.AuthorID != approvalEmployee(strconv.FormatInt(identity.EmployeeID, 10))) {
				http.Error(writer, "ticket or comment author not accessible", 403)
				return
			}
			count := 0
			author, parseErr := strconv.ParseInt(input.Comment.AuthorID, 10, 64)
			numeric := parseErr == nil && author > 0 && strconv.FormatInt(author, 10) == input.Comment.AuthorID
			for _, e := range employees {
				if (numeric && e.AccountEmployeeID == author) || (!numeric && e.ID == input.Comment.AuthorID && e.AccountEmployeeID > 0) {
					count++
				}
			}
			if count != 1 {
				http.Error(writer, "author account has no unique employee mapping", 409)
				return
			}
			if _, ok := comments[input.Comment.ID]; ok {
				http.Error(writer, "comment identity already exists", 409)
				return
			}
			input.Comment.ImportedBy = identity.EmployeeID
			payload, _ := json.Marshal(input.Comment)
			event := syncEvent{Topic: "comment.created", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			comments[input.Comment.ID] = input.Comment
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(event); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		if request.URL.Path == "/api/v1/task-comment-sync" || request.URL.Path == "/api/v1/personal-task-comment-sync" {
			personal := request.URL.Path == "/api/v1/personal-task-comment-sync"
			if (!personal && (!allows(identity, "tasks.manage") || !allows(identity, "employees.manage"))) || (personal && (!allows(identity, "collaboration.comment") || (!allows(identity, "tasks.read") && !allows(identity, "tasks.manage")))) {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			if request.Method == http.MethodGet {
				if personal {
					http.Error(writer, "method not allowed", 405)
					return
				}
				registry.Lock()
				defer registry.Unlock()
				result := []centralComment{}
				for _, c := range comments {
					if c.SubjectType == "task" {
						result = append(result, c)
					}
				}
				sort.Slice(result, func(i, j int) bool { return result[i].ID < result[j].ID })
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method != http.MethodPost {
				http.Error(writer, "method not allowed", 405)
				return
			}
			defer request.Body.Close()
			var input struct {
				ClientID    string         `json:"clientId"`
				OperationID string         `json:"operationId"`
				Comment     centralComment `json:"comment"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ClientID) == "" || len(input.ClientID) > 128 || strings.TrimSpace(input.OperationID) == "" || len(input.OperationID) > 128 || strings.TrimSpace(input.Comment.ID) == "" || len(input.Comment.ID) > 256 || input.Comment.SubjectType != "task" || strings.TrimSpace(input.Comment.SubjectID) == "" || len(input.Comment.SubjectID) > 256 || len(strings.TrimSpace(input.Comment.Body)) == 0 || len(input.Comment.Body) > 16000 || input.Comment.ImportedBy != 0 {
				http.Error(writer, "invalid task comment sync request", 400)
				return
			}
			if strings.TrimSpace(input.Comment.AuthorID) == "" || len(input.Comment.AuthorID) > 256 {
				http.Error(writer, "invalid author identity", 400)
				return
			}
			if _, err := time.Parse(time.RFC3339Nano, input.Comment.CreatedAt); err != nil {
				http.Error(writer, "invalid comment timestamp", 400)
				return
			}
			canonical, _ := json.Marshal(input)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			task, ok := tasks[input.Comment.SubjectID]
			if !ok {
				http.Error(writer, "task missing", 409)
				return
			}
			if personal && (!task.Active || !strings.HasPrefix(input.Comment.ID, input.ClientID+":comment:") || (!allows(identity, "tasks.manage") && task.AssigneeID != identity.EmployeeID && task.CreatedBy != identity.EmployeeID) || input.Comment.AuthorID != approvalEmployee(strconv.FormatInt(identity.EmployeeID, 10))) {
				http.Error(writer, "task or comment author not accessible", 403)
				return
			}
			count := 0
			author, parseErr := strconv.ParseInt(input.Comment.AuthorID, 10, 64)
			numeric := parseErr == nil && author > 0 && strconv.FormatInt(author, 10) == input.Comment.AuthorID
			for _, e := range employees {
				if (numeric && e.AccountEmployeeID == author) || (!numeric && e.ID == input.Comment.AuthorID && e.AccountEmployeeID > 0) {
					count++
				}
			}
			if count != 1 {
				http.Error(writer, "author account has no unique employee mapping", 409)
				return
			}
			if _, ok := comments[input.Comment.ID]; ok {
				http.Error(writer, "comment identity already exists", 409)
				return
			}
			input.Comment.ImportedBy = identity.EmployeeID
			payload, _ := json.Marshal(input.Comment)
			event := syncEvent{Topic: "comment.created", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			comments[input.Comment.ID] = input.Comment
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(event); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		if request.URL.Path == "/api/v1/desktop-task-recurrence-snapshot" && request.Method == http.MethodGet {
			if !allows(identity, "tasks.manage") || !allows(identity, "sync.events.read") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			cursor, err := centralMirror.latestCursor()
			if err != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			links := make([]map[string]string, 0, len(taskSuccessors))
			for source, successor := range taskSuccessors {
				if _, exists := tasks[successor]; !exists {
					continue
				}
				if _, exists := tasks[source]; !exists {
					continue
				}
				links = append(links, map[string]string{"sourceId": source, "successorId": successor})
			}
			sort.Slice(links, func(i, j int) bool { return links[i]["sourceId"] < links[j]["sourceId"] })
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(struct {
				Cursor int64               `json:"cursor"`
				Links  []map[string]string `json:"links"`
			}{cursor, links})
			return
		}
		if request.URL.Path == "/api/v1/desktop-maintenance-recurrence-snapshot" && request.Method == http.MethodGet {
			if !allows(identity, "facility.manage") || !allows(identity, "sync.events.read") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			cursor, err := centralMirror.latestCursor()
			if err != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			links := make([]map[string]string, 0, len(maintenanceSuccessors))
			for source, successor := range maintenanceSuccessors {
				if _, exists := maintenanceOrders[successor]; !exists {
					continue
				}
				if _, exists := maintenanceOrders[source]; !exists {
					continue
				}
				links = append(links, map[string]string{"sourceId": source, "successorId": successor})
			}
			sort.Slice(links, func(i, j int) bool { return links[i]["sourceId"] < links[j]["sourceId"] })
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(struct {
				Cursor int64               `json:"cursor"`
				Links  []map[string]string `json:"links"`
			}{cursor, links})
			return
		}
		if request.URL.Path == "/api/v1/desktop-project-assignment-snapshot" && request.Method == http.MethodGet {
			if !allows(identity, "tasks.manage") || !allows(identity, "projects.manage") || !allows(identity, "sync.events.read") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			cursor, err := centralMirror.latestCursor()
			if err != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			edges := []map[string]string{}
			for project, items := range projectAssignments {
				for task := range items {
					edges = append(edges, map[string]string{"projectId": project, "taskId": task})
				}
			}
			sort.Slice(edges, func(i, j int) bool {
				if edges[i]["projectId"] == edges[j]["projectId"] {
					return edges[i]["taskId"] < edges[j]["taskId"]
				}
				return edges[i]["projectId"] < edges[j]["projectId"]
			})
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(struct {
				Cursor      int64               `json:"cursor"`
				Assignments []map[string]string `json:"assignments"`
			}{cursor, edges})
			return
		}
		if request.URL.Path == "/api/v1/desktop-attachment-snapshot" && request.Method == http.MethodGet {
			if !allows(identity, "sync.events.read") || (!allows(identity, "tasks.manage") && !allows(identity, "tickets.manage")) {
				http.Error(writer, "forbidden", http.StatusForbidden)
				return
			}
			clientID := request.URL.Query().Get("clientId")
			if strings.TrimSpace(clientID) == "" || len(clientID) > 128 {
				http.Error(writer, "invalid client", http.StatusBadRequest)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", http.StatusServiceUnavailable)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			cursor, err := centralMirror.latestCursor()
			if err != nil {
				http.Error(writer, "persistence failure", http.StatusInternalServerError)
				return
			}
			result := []centralAttachment{}
			for _, item := range attachments {
				if !strings.HasPrefix(item.ID, clientID+":") || !allows(identity, item.SubjectType+"s.manage") {
					continue
				}
				if n, err := strconv.ParseInt(item.UploadedBy, 10, 64); err == nil && n > 0 && strconv.FormatInt(n, 10) == item.UploadedBy {
					item.UploadedBy = approvalEmployee(item.UploadedBy)
					if item.UploadedBy == "" {
						http.Error(writer, "attachment uploader mapping required", http.StatusConflict)
						return
					}
				}
				result = append(result, item)
			}
			sort.Slice(result, func(i, j int) bool { return result[i].ID < result[j].ID })
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(struct {
				Cursor      int64               `json:"cursor"`
				Attachments []centralAttachment `json:"attachments"`
			}{cursor, result})
			return
		}
		if request.URL.Path == "/api/v1/project-assignment-sync" {
			if !allows(identity, "tasks.manage") || !allows(identity, "projects.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			if request.Method == http.MethodGet {
				registry.Lock()
				defer registry.Unlock()
				result := []map[string]string{}
				for projectID, edges := range projectAssignments {
					for taskID := range edges {
						result = append(result, map[string]string{"projectId": projectID, "taskId": taskID})
					}
				}
				sort.Slice(result, func(i, j int) bool {
					if result[i]["projectId"] == result[j]["projectId"] {
						return result[i]["taskId"] < result[j]["taskId"]
					}
					return result[i]["projectId"] < result[j]["projectId"]
				})
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method != http.MethodPost {
				http.Error(writer, "method not allowed", 405)
				return
			}
			defer request.Body.Close()
			var input struct {
				ClientID    string `json:"clientId"`
				OperationID string `json:"operationId"`
				ProjectID   string `json:"projectId"`
				TaskID      string `json:"taskId"`
				Present     *bool  `json:"present"`
				Expected    *bool  `json:"expected"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ClientID) == "" || len(input.ClientID) > 128 || strings.TrimSpace(input.OperationID) == "" || len(input.OperationID) > 128 || strings.TrimSpace(input.ProjectID) == "" || len(input.ProjectID) > 256 || strings.TrimSpace(input.TaskID) == "" || len(input.TaskID) > 256 || input.Present == nil || input.Expected == nil || *input.Present == *input.Expected {
				http.Error(writer, "invalid project assignment sync request", 400)
				return
			}
			canonical, _ := json.Marshal(input)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			if _, exists := projects[input.ProjectID]; !exists {
				http.Error(writer, "project missing", 409)
				return
			}
			if _, exists := tasks[input.TaskID]; !exists {
				http.Error(writer, "task missing", 409)
				return
			}
			current := projectAssignments[input.ProjectID] != nil && projectAssignments[input.ProjectID][input.TaskID]
			if current != *input.Expected {
				http.Error(writer, "project assignment changed on server", 409)
				return
			}
			topic := "central.project_assignment.removed"
			if *input.Present {
				topic = "central.project_assignment.added"
			}
			payload, _ := json.Marshal(map[string]string{"projectId": input.ProjectID, "taskId": input.TaskID})
			event := syncEvent{Topic: topic, Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			if projectAssignments[input.ProjectID] == nil {
				projectAssignments[input.ProjectID] = make(map[string]bool)
			}
			if *input.Present {
				projectAssignments[input.ProjectID][input.TaskID] = true
			} else {
				delete(projectAssignments[input.ProjectID], input.TaskID)
			}
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(event); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		if request.URL.Path == "/api/v1/dependency-sync" {
			if !allows(identity, "tasks.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			if request.Method == http.MethodGet {
				registry.Lock()
				defer registry.Unlock()
				result := []map[string]string{}
				for taskID, edges := range dependencies {
					for dependencyID := range edges {
						result = append(result, map[string]string{"taskId": taskID, "dependencyId": dependencyID})
					}
				}
				sort.Slice(result, func(i, j int) bool {
					if result[i]["taskId"] == result[j]["taskId"] {
						return result[i]["dependencyId"] < result[j]["dependencyId"]
					}
					return result[i]["taskId"] < result[j]["taskId"]
				})
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method != http.MethodPost {
				http.Error(writer, "method not allowed", 405)
				return
			}
			defer request.Body.Close()
			var input struct {
				ClientID     string `json:"clientId"`
				OperationID  string `json:"operationId"`
				TaskID       string `json:"taskId"`
				DependencyID string `json:"dependencyId"`
				Present      *bool  `json:"present"`
				Expected     *bool  `json:"expected"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ClientID) == "" || len(input.ClientID) > 128 || strings.TrimSpace(input.OperationID) == "" || len(input.OperationID) > 128 || strings.TrimSpace(input.TaskID) == "" || len(input.TaskID) > 256 || strings.TrimSpace(input.DependencyID) == "" || len(input.DependencyID) > 256 || input.TaskID == input.DependencyID || input.Present == nil || input.Expected == nil || *input.Present == *input.Expected {
				http.Error(writer, "invalid dependency sync request", 400)
				return
			}
			canonical, _ := json.Marshal(input)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			task, exists := tasks[input.TaskID]
			if !exists {
				http.Error(writer, "task missing", 409)
				return
			}
			dependency, exists := tasks[input.DependencyID]
			if !exists {
				http.Error(writer, "dependency task missing", 409)
				return
			}
			current := dependencies[input.TaskID] != nil && dependencies[input.TaskID][input.DependencyID]
			if current != *input.Expected {
				http.Error(writer, "dependency changed on server", 409)
				return
			}
			if *input.Present {
				if task.Active && task.Status == "done" && dependency.Active && dependency.Status != "done" {
					http.Error(writer, "completed task cannot depend on an open task", 409)
					return
				}
				seen := map[string]bool{}
				stack := []string{input.DependencyID}
				for len(stack) > 0 {
					current := stack[len(stack)-1]
					stack = stack[:len(stack)-1]
					if current == input.TaskID {
						http.Error(writer, "dependency cycle", 409)
						return
					}
					if seen[current] {
						continue
					}
					seen[current] = true
					for next := range dependencies[current] {
						stack = append(stack, next)
					}
				}
			}
			topic := "central.task_dependency.removed"
			if *input.Present {
				topic = "central.task_dependency.added"
			}
			payload, _ := json.Marshal(map[string]string{"taskId": input.TaskID, "dependencyId": input.DependencyID})
			event := syncEvent{Topic: topic, Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			if dependencies[input.TaskID] == nil {
				dependencies[input.TaskID] = make(map[string]bool)
			}
			if *input.Present {
				dependencies[input.TaskID][input.DependencyID] = true
			} else {
				delete(dependencies[input.TaskID], input.DependencyID)
			}
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(event); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		if request.URL.Path == "/api/v1/desktop-subtask-context" && request.Method == http.MethodGet {
			if !allows(identity, "tasks.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			employeeID := request.URL.Query().Get("employee")
			if len(employeeID) > 256 {
				http.Error(writer, "invalid employee", 400)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			account := int64(0)
			if employeeID != "" {
				value, ok := employees[employeeID]
				if !ok || !value.Active || value.AccountEmployeeID <= 0 {
					http.Error(writer, "employee account mapping unavailable", 409)
					return
				}
				account = value.AccountEmployeeID
				for otherID, other := range employees {
					if otherID != employeeID && other.Active && other.AccountEmployeeID == account {
						http.Error(writer, "employee account mapping ambiguous", 409)
						return
					}
				}
			}
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(map[string]any{"employeeId": identity.EmployeeID, "assigneeAccountId": account, "desktopSubtaskSync": 1})
			return
		}
		// Fence a never-accepted Linux subtask creation before identity mapping.
		if request.URL.Path == "/api/v1/subtask-sync-abandon" && request.Method == http.MethodPost {
			if !allows(identity, "tasks.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			defer request.Body.Close()
			var input struct {
				Operation subtaskSyncOperation `json:"operation"`
				Remote    centralSubtask       `json:"remote"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil {
				http.Error(writer, "invalid request", 400)
				return
			}
			op := input.Operation
			if strings.TrimSpace(op.ClientID) == "" || len(op.ClientID) > 128 || strings.TrimSpace(op.OperationID) == "" || len(op.OperationID) > 128 || op.Action != "create" || op.Expected != nil || op.AssigneeEmployeeID != nil || op.ExpectedAssigneeEmployeeID != nil || op.Subtask.ID == "" || len(op.Subtask.ID) > 256 || !strings.HasPrefix(op.Subtask.ID, op.ClientID+":subtask:") || strings.TrimSpace(op.Subtask.Title) == "" || op.Subtask.TaskID == "" || !op.Subtask.Active || input.Remote.ID == "" || input.Remote.ID == op.Subtask.ID || input.Remote.TaskID != op.Subtask.TaskID {
				http.Error(writer, "invalid subtask operation", 400)
				return
			}
			canonical, _ := json.Marshal(op)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			abandoned := "abandoned:" + fingerprint
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, op.ClientID, op.OperationID, fingerprint) {
			case 1:
				http.Error(writer, "operation already accepted", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			switch centralMirror.roomReceipt(identity.EmployeeID, op.ClientID, op.OperationID, abandoned) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			remote, exists := subtasks[input.Remote.ID]
			_, ownExists := subtasks[op.Subtask.ID]
			if ownExists || !exists || remote != input.Remote || !strings.EqualFold(strings.TrimSpace(remote.Title), strings.TrimSpace(op.Subtask.Title)) {
				http.Error(writer, "subtask conflict changed", 409)
				return
			}
			payload, _ := json.Marshal(map[string]any{"clientId": op.ClientID, "operationId": op.OperationID, "actorId": identity.EmployeeID, "original": op.Subtask, "remote": remote})
			event := syncEvent{Topic: "central.subtask_sync.abandoned", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, op.ClientID, op.OperationID, abandoned, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		if request.URL.Path == "/api/v1/subtask-sync" || request.URL.Path == "/api/v1/desktop-subtask-sync" {
			desktop := request.URL.Path == "/api/v1/desktop-subtask-sync"
			if !allows(identity, "tasks.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			if request.Method == http.MethodGet && !desktop {
				registry.Lock()
				defer registry.Unlock()
				result := []centralSubtask{}
				for _, sub := range subtasks {
					result = append(result, sub)
				}
				sort.Slice(result, func(i, j int) bool { return result[i].ID < result[j].ID })
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method != http.MethodPost {
				http.Error(writer, "method not allowed", 405)
				return
			}
			defer request.Body.Close()
			var input subtaskSyncOperation
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ClientID) == "" || len(input.ClientID) > 128 || strings.TrimSpace(input.OperationID) == "" || len(input.OperationID) > 128 || strings.TrimSpace(input.Subtask.ID) == "" || len(input.Subtask.ID) > 256 || strings.TrimSpace(input.Subtask.TaskID) == "" || len(input.Subtask.TaskID) > 256 || strings.TrimSpace(input.Subtask.Title) == "" || !validTaskStatus(input.Subtask.Status) || input.Subtask.AssigneeID < 0 || input.Subtask.DueAt < 0 || (input.Action != "create" && input.Action != "update" && input.Action != "archive") || (input.Action == "create" && !input.Subtask.Active) || (input.Action == "archive" && input.Subtask.Active) {
				http.Error(writer, "invalid subtask sync request", 400)
				return
			}
			if desktop {
				if input.AssigneeEmployeeID == nil || (input.Expected != nil && input.ExpectedAssigneeEmployeeID == nil) || (input.Expected == nil && input.ExpectedAssigneeEmployeeID != nil) {
					http.Error(writer, "desktop assignee reference required", 400)
					return
				}
			} else if input.AssigneeEmployeeID != nil || input.ExpectedAssigneeEmployeeID != nil {
				http.Error(writer, "desktop references not accepted", 400)
				return
			}
			canonical, _ := json.Marshal(input)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			if desktop {
				matches := func(id string, account int64) bool {
					if account == 0 {
						return id == ""
					}
					if id == "" {
						return false
					}
					employee, ok := employees[id]
					if !ok || !employee.Active || employee.AccountEmployeeID != account {
						return false
					}
					for otherID, other := range employees {
						if otherID != id && other.Active && other.AccountEmployeeID == account {
							return false
						}
					}
					return true
				}
				if !matches(*input.AssigneeEmployeeID, input.Subtask.AssigneeID) || (input.Expected != nil && !matches(*input.ExpectedAssigneeEmployeeID, input.Expected.AssigneeID)) {
					http.Error(writer, "subtask employee account mapping changed or unavailable", 409)
					return
				}
			}
			parent, found := tasks[input.Subtask.TaskID]
			if !found {
				http.Error(writer, "parent task missing", 409)
				return
			}
			existing, found := subtasks[input.Subtask.ID]
			if input.Action == "create" {
				if found || input.Expected != nil {
					http.Error(writer, "subtask exists or unexpected base", 409)
					return
				}
			} else if !found || input.Expected == nil || *input.Expected != existing || existing.TaskID != input.Subtask.TaskID || (input.Action == "archive" && !existing.Active) {
				http.Error(writer, "subtask changed on server", 409)
				return
			}
			if parent.Active && parent.Status == "done" && input.Subtask.Active && input.Subtask.Status != "done" && input.Subtask.Status != "cancelled" {
				http.Error(writer, "completed task cannot have open subtasks", 409)
				return
			}
			topic := "central.task_subtask.saved"
			if input.Action == "archive" {
				topic = "central.task_subtask.archived"
			}
			payload, _ := json.Marshal(input.Subtask)
			event := syncEvent{Topic: topic, Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			subtasks[input.Subtask.ID] = input.Subtask
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(event); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		if request.URL.Path == "/api/v1/task-recurrence-reserve" && request.Method == http.MethodPost {
			if !allows(identity, "tasks.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			var input struct {
				ClientID    string            `json:"clientId"`
				OperationID string            `json:"operationId"`
				Original    taskSyncOperation `json:"original"`
				SuccessorID string            `json:"successorId"`
			}
			defer request.Body.Close()
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || input.ClientID == "" || len(input.ClientID) > 128 || input.OperationID == "" || len(input.OperationID) > 128 || input.Original.ClientID != input.ClientID || input.Original.OperationID == "" || len(input.Original.OperationID) > 128 || input.OperationID == input.Original.OperationID || input.Original.RecurrenceSuccessorID != "" || (input.Original.Action != "create" && input.Original.Action != "update") || input.Original.Task.ID == "" || input.Original.Task.Status != "done" || input.Original.Task.RecurrenceDays <= 0 || len(input.SuccessorID) > 256 || !strings.HasPrefix(input.SuccessorID, input.ClientID+":task:") || input.SuccessorID == input.Original.Task.ID {
				http.Error(writer, "invalid recurrence reservation", 400)
				return
			}
			originalTask := input.Original.Task
			if strings.TrimSpace(input.ClientID) == "" || strings.TrimSpace(input.OperationID) == "" || strings.TrimSpace(input.Original.OperationID) == "" || strings.TrimSpace(originalTask.ID) == "" || len(originalTask.ID) > 256 || strings.ContainsRune(input.ClientID, 0) || strings.ContainsRune(input.SuccessorID, 0) || strings.TrimSpace(originalTask.Title) == "" || strings.TrimSpace(originalTask.Department) == "" || (originalTask.Priority != "Niedrig" && originalTask.Priority != "Mittel" && originalTask.Priority != "Hoch" && originalTask.Priority != "Kritisch") || originalTask.AssigneeID <= 0 || originalTask.CreatedBy <= 0 || originalTask.DueAt < 0 || originalTask.RecurrenceDays > 3660 || !originalTask.Active {
				http.Error(writer, "invalid original task", 400)
				return
			}
			canonical, _ := json.Marshal(input)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			original, _ := json.Marshal(input.Original)
			originalHash := sha256.Sum256(original)
			originalFingerprint := hex.EncodeToString(originalHash[:])
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			if input.Original.Expected != nil && (input.Original.Expected.ID != input.Original.Task.ID || input.Original.Expected.CreatedBy != input.Original.Task.CreatedBy) {
				http.Error(writer, "invalid task base", 400)
				return
			}
			source, exists := tasks[input.Original.Task.ID]
			receipt := centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.Original.OperationID, originalFingerprint)
			if receipt < 0 {
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			if receipt == 2 {
				http.Error(writer, "original operation identity reused", 409)
				return
			}
			accepted := receipt == 1
			if input.Original.Action == "create" {
				if input.Original.Expected != nil || ((!accepted && exists) || (accepted && (!exists || source != input.Original.Task))) {
					http.Error(writer, "source task changed", 409)
					return
				}
			} else if input.Original.Expected == nil || !exists || ((!accepted && source != *input.Original.Expected) || (accepted && source != input.Original.Task)) {
				http.Error(writer, "source task changed", 409)
				return
			}
			if !accepted {
				for id := range dependencies[input.Original.Task.ID] {
					if dep, exists := tasks[id]; exists && dep.Active && dep.Status != "done" {
						http.Error(writer, "unfinished task dependency", 409)
						return
					}
				}
				for _, sub := range subtasks {
					if sub.TaskID == input.Original.Task.ID && sub.Active && sub.Status != "done" && sub.Status != "cancelled" {
						http.Error(writer, "unfinished subtask", 409)
						return
					}
				}
			}
			if input.Original.Action == "create" && input.Original.Task.CreatedBy != identity.EmployeeID {
				http.Error(writer, "creator identity mismatch", 403)
				return
			}
			if existing := taskSuccessors[input.Original.Task.ID]; existing != "" && existing != input.SuccessorID {
				http.Error(writer, "different recurrence successor", 409)
				return
			}
			if taskSuccessors[input.Original.Task.ID] == input.SuccessorID {
				if _, bound := taskReservationOwners[input.SuccessorID]; !bound && !accepted {
					http.Error(writer, "ownerless reservation requires original receipt", 409)
					return
				}
			}
			if _, exists := tasks[input.SuccessorID]; exists {
				http.Error(writer, "successor already exists", 409)
				return
			}
			if owner, bound := taskReservationOwners[input.SuccessorID]; bound && (owner.ClientID != input.ClientID || owner.ActorID != identity.EmployeeID) {
				http.Error(writer, "reservation belongs to another client", 409)
				return
			}
			for source, successor := range taskSuccessors {
				if source != input.Original.Task.ID && successor == input.SuccessorID {
					http.Error(writer, "successor already reserved", 409)
					return
				}
			}
			seen := map[string]bool{}
			for cursor := input.SuccessorID; cursor != ""; cursor = taskSuccessors[cursor] {
				if cursor == input.Original.Task.ID || seen[cursor] {
					http.Error(writer, "recurrence cycle", 409)
					return
				}
				seen[cursor] = true
			}
			payload, _ := json.Marshal(map[string]any{"sourceId": input.Original.Task.ID, "successorId": input.SuccessorID, "clientId": input.ClientID, "actorId": identity.EmployeeID})
			event := syncEvent{Topic: "central.task_recurrence.reserved", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint, event) != nil {
				http.Error(writer, "reservation persistence failure", 500)
				return
			}
			taskSuccessors[input.Original.Task.ID] = input.SuccessorID
			taskReservationOwners[input.SuccessorID] = recurrenceReservationOwner{input.ClientID, identity.EmployeeID}
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(event); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		// Fence a prepared task creation before its local identity is reassigned.
		if request.URL.Path == "/api/v1/task-sync-abandon" && request.Method == http.MethodPost {
			if !allows(identity, "tasks.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			defer request.Body.Close()
			var input struct {
				Operation taskSyncOperation `json:"operation"`
				Remote    centralTask       `json:"remote"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil {
				http.Error(writer, "invalid request", 400)
				return
			}
			op := input.Operation
			if strings.TrimSpace(op.ClientID) == "" || len(op.ClientID) > 128 || strings.TrimSpace(op.OperationID) == "" || len(op.OperationID) > 128 || op.Action != "create" || op.Expected != nil || op.RecurrenceSuccessorID != "" || op.Task.ID == "" || len(op.Task.ID) > 256 || op.Task.CreatedBy != identity.EmployeeID || !strings.HasPrefix(op.Task.ID, op.ClientID+":task:") || strings.TrimSpace(op.Task.Title) == "" || !op.Task.Active || input.Remote.ID == "" || input.Remote.ID == op.Task.ID {
				http.Error(writer, "invalid task operation", 400)
				return
			}
			canonical, _ := json.Marshal(op)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			abandoned := "abandoned:" + fingerprint
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, op.ClientID, op.OperationID, fingerprint) {
			case 1:
				http.Error(writer, "operation already accepted", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			switch centralMirror.roomReceipt(identity.EmployeeID, op.ClientID, op.OperationID, abandoned) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			remote, exists := tasks[input.Remote.ID]
			_, ownExists := tasks[op.Task.ID]
			if ownExists || !exists || remote != input.Remote || !strings.EqualFold(strings.TrimSpace(remote.Title), strings.TrimSpace(op.Task.Title)) {
				http.Error(writer, "task conflict changed", 409)
				return
			}
			payload, _ := json.Marshal(map[string]any{"clientId": op.ClientID, "operationId": op.OperationID, "actorId": identity.EmployeeID, "original": op.Task, "remote": remote})
			event := syncEvent{Topic: "central.task_sync.abandoned", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, op.ClientID, op.OperationID, abandoned, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		if request.URL.Path == "/api/v1/task-sync" && request.Method == http.MethodPost {
			if !allows(identity, "tasks.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			defer request.Body.Close()
			var input taskSyncOperation
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ClientID) == "" || len(input.ClientID) > 128 || strings.TrimSpace(input.OperationID) == "" || len(input.OperationID) > 128 || strings.TrimSpace(input.Task.ID) == "" || len(input.Task.ID) > 256 || strings.TrimSpace(input.Task.Title) == "" || strings.TrimSpace(input.Task.Department) == "" || (input.Task.Priority != "Niedrig" && input.Task.Priority != "Mittel" && input.Task.Priority != "Hoch" && input.Task.Priority != "Kritisch") || !validTaskStatus(input.Task.Status) || input.Task.AssigneeID <= 0 || input.Task.CreatedBy <= 0 || input.Task.DueAt < 0 || input.Task.RecurrenceDays < 0 || input.Task.RecurrenceDays > 3660 || (input.Action != "create" && input.Action != "update" && input.Action != "archive") || (input.Action == "archive" && input.Task.Active) || (input.Action == "create" && !input.Task.Active) {
				http.Error(writer, "invalid task sync request", 400)
				return
			}
			canonical, _ := json.Marshal(input)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				// Earlier experimental reservations had no stored owner. A retry
				// of the original operation proves both client and actor via its
				// durable fingerprint; never infer ownership from a name alone.
				if input.RecurrenceSuccessorID != "" && taskSuccessors[input.Task.ID] == input.RecurrenceSuccessorID {
					if _, exists := tasks[input.RecurrenceSuccessorID]; !exists {
						owner, bound := taskReservationOwners[input.RecurrenceSuccessorID]
						if bound && (owner.ClientID != input.ClientID || owner.ActorID != identity.EmployeeID) {
							http.Error(writer, "recurrence reservation belongs to another client", 409)
							return
						}
						if !bound {
							if !strings.HasPrefix(input.RecurrenceSuccessorID, input.ClientID+":task:") {
								http.Error(writer, "invalid legacy reservation identity", 409)
								return
							}
							claimOp := "recurrence-claim-" + fingerprint
							claimHash := sha256.Sum256([]byte(claimOp))
							claimFingerprint := hex.EncodeToString(claimHash[:])
							payload, _ := json.Marshal(map[string]any{"sourceId": input.Task.ID, "successorId": input.RecurrenceSuccessorID, "clientId": input.ClientID, "actorId": identity.EmployeeID})
							event := syncEvent{Topic: "central.task_recurrence.claimed", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
							if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, claimOp, claimFingerprint, event) != nil && centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, claimOp, claimFingerprint) != 1 {
								http.Error(writer, "reservation claim persistence failure", 500)
								return
							}
							taskReservationOwners[input.RecurrenceSuccessorID] = recurrenceReservationOwner{input.ClientID, identity.EmployeeID}
							if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
								if data, err := json.Marshal(event); err == nil {
									_, _ = file.Write(append(data, '\n'))
								}
								_ = file.Close()
							}
						}
					}
				}
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			existing, found := tasks[input.Task.ID]
			if input.Action == "create" {
				for _, reserved := range taskSuccessors {
					if reserved == input.Task.ID {
						owner, ok := taskReservationOwners[input.Task.ID]
						if !ok || owner.ClientID != input.ClientID || owner.ActorID != identity.EmployeeID {
							http.Error(writer, "recurrence successor reserved for another client", 409)
							return
						}
					}
				}
			}
			if input.RecurrenceSuccessorID != "" {
				if len(input.RecurrenceSuccessorID) > 256 || strings.TrimSpace(input.RecurrenceSuccessorID) != input.RecurrenceSuccessorID || input.RecurrenceSuccessorID == input.Task.ID || input.Task.Status != "done" || input.Task.RecurrenceDays <= 0 {
					http.Error(writer, "invalid recurrence reservation", 400)
					return
				}
				if successor := taskSuccessors[input.Task.ID]; successor != "" && successor != input.RecurrenceSuccessorID {
					http.Error(writer, "different recurrence successor", 409)
					return
				}
				if _, exists := tasks[input.RecurrenceSuccessorID]; exists && taskSuccessors[input.Task.ID] != input.RecurrenceSuccessorID {
					http.Error(writer, "successor already exists", 409)
					return
				}
				if _, exists := tasks[input.RecurrenceSuccessorID]; !exists && !strings.HasPrefix(input.RecurrenceSuccessorID, input.ClientID+":task:") {
					http.Error(writer, "successor identity belongs to another client", 409)
					return
				}
				if _, exists := tasks[input.RecurrenceSuccessorID]; !exists {
					if owner, ok := taskReservationOwners[input.RecurrenceSuccessorID]; ok && (owner.ClientID != input.ClientID || owner.ActorID != identity.EmployeeID) {
						http.Error(writer, "recurrence reservation belongs to another client", 409)
						return
					}
				}
				for source, successor := range taskSuccessors {
					if source != input.Task.ID && successor == input.RecurrenceSuccessorID {
						http.Error(writer, "successor already reserved", 409)
						return
					}
				}
				seen := map[string]bool{}
				for cursor := input.RecurrenceSuccessorID; cursor != ""; cursor = taskSuccessors[cursor] {
					if cursor == input.Task.ID || seen[cursor] {
						http.Error(writer, "recurrence cycle", 409)
						return
					}
					seen[cursor] = true
				}
			}
			if input.Action == "create" {
				if found || input.Expected != nil {
					http.Error(writer, "task exists or unexpected base", 409)
					return
				}
				if input.Task.CreatedBy != identity.EmployeeID {
					http.Error(writer, "creator identity mismatch", 403)
					return
				}
			} else {
				if !found || input.Expected == nil || *input.Expected != existing || (input.Action == "archive" && !existing.Active) {
					http.Error(writer, "task changed on server", 409)
					return
				}
				if input.Task.CreatedBy != existing.CreatedBy {
					http.Error(writer, "task creator cannot be changed", 409)
					return
				}
			}
			if input.Task.Active && input.Task.Status == "done" {
				for id := range dependencies[input.Task.ID] {
					dep, exists := tasks[id]
					if exists && dep.Active && dep.Status != "done" {
						http.Error(writer, "unfinished task dependency", 409)
						return
					}
				}
				for _, sub := range subtasks {
					if sub.TaskID == input.Task.ID && sub.Active && sub.Status != "done" && sub.Status != "cancelled" {
						http.Error(writer, "unfinished subtask", 409)
						return
					}
				}
			}
			topic := "task." + map[string]string{"create": "created", "update": "updated", "archive": "archived"}[input.Action]
			payload, _ := json.Marshal(struct {
				centralTask
				RecurrenceSuccessorID         string `json:"recurrenceSuccessorId,omitempty"`
				RecurrenceReservationClientID string `json:"recurrenceReservationClientId,omitempty"`
				RecurrenceReservationActorID  int64  `json:"recurrenceReservationActorId,omitempty"`
			}{input.Task, input.RecurrenceSuccessorID, func() string {
				if input.RecurrenceSuccessorID != "" {
					if _, exists := tasks[input.RecurrenceSuccessorID]; !exists {
						return input.ClientID
					}
				}
				return ""
			}(), func() int64 {
				if input.RecurrenceSuccessorID != "" {
					if _, exists := tasks[input.RecurrenceSuccessorID]; !exists {
						return identity.EmployeeID
					}
				}
				return 0
			}()})
			event := syncEvent{Topic: topic, Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			tasks[input.Task.ID] = input.Task
			if input.RecurrenceSuccessorID != "" {
				taskSuccessors[input.Task.ID] = input.RecurrenceSuccessorID
				if _, exists := tasks[input.RecurrenceSuccessorID]; !exists {
					taskReservationOwners[input.RecurrenceSuccessorID] = recurrenceReservationOwner{input.ClientID, identity.EmployeeID}
				}
			}
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(event); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		if request.URL.Path == "/api/v1/task-recurrence-link" && request.Method == http.MethodGet {
			if !allows(identity, "tasks.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			links := make([]map[string]string, 0, len(taskSuccessors))
			for source, successor := range taskSuccessors {
				if _, exists := tasks[successor]; !exists {
					continue
				}
				if _, exists := tasks[source]; !exists {
					continue
				}
				links = append(links, map[string]string{"sourceId": source, "successorId": successor})
			}
			sort.Slice(links, func(i, j int) bool { return links[i]["sourceId"] < links[j]["sourceId"] })
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(links)
			return
		}
		if request.URL.Path == "/api/v1/task-recurrence-link" && request.Method == http.MethodPost {
			if !allows(identity, "tasks.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			var input struct {
				ClientID    string `json:"clientId"`
				OperationID string `json:"operationId"`
				SourceID    string `json:"sourceId"`
				SuccessorID string `json:"successorId"`
			}
			defer request.Body.Close()
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ClientID) == "" || len(input.ClientID) > 128 || strings.TrimSpace(input.OperationID) == "" || len(input.OperationID) > 128 || strings.TrimSpace(input.SourceID) == "" || len(input.SourceID) > 256 || strings.TrimSpace(input.SuccessorID) == "" || len(input.SuccessorID) > 256 || input.SourceID == input.SuccessorID {
				http.Error(writer, "invalid recurrence link", 400)
				return
			}
			canonical, _ := json.Marshal(input)
			sum := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(sum[:])
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			if _, ok := tasks[input.SourceID]; !ok {
				http.Error(writer, "source task missing", 409)
				return
			}
			if _, ok := tasks[input.SuccessorID]; !ok {
				http.Error(writer, "successor task missing", 409)
				return
			}
			if existing := taskSuccessors[input.SourceID]; existing != "" && existing != input.SuccessorID {
				http.Error(writer, "different recurrence successor", 409)
				return
			}
			for source, successor := range taskSuccessors {
				if source != input.SourceID && successor == input.SuccessorID {
					http.Error(writer, "successor already linked", 409)
					return
				}
			}
			seen := map[string]bool{}
			for cursor := input.SuccessorID; cursor != ""; cursor = taskSuccessors[cursor] {
				if cursor == input.SourceID || seen[cursor] {
					http.Error(writer, "recurrence cycle", 409)
					return
				}
				seen[cursor] = true
			}
			payload, _ := json.Marshal(map[string]string{"sourceId": input.SourceID, "successorId": input.SuccessorID})
			event := syncEvent{Topic: "central.task_recurrence.linked", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			taskSuccessors[input.SourceID] = input.SuccessorID
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(event); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		if request.URL.Path == "/api/v1/maintenance-recurrence-link" && request.Method == http.MethodGet {
			if !allows(identity, "facility.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			links := make([]map[string]string, 0, len(maintenanceSuccessors))
			for source, successor := range maintenanceSuccessors {
				if _, exists := maintenanceOrders[successor]; !exists {
					continue
				}
				if _, exists := maintenanceOrders[source]; !exists {
					continue
				}
				links = append(links, map[string]string{"sourceId": source, "successorId": successor})
			}
			sort.Slice(links, func(i, j int) bool { return links[i]["sourceId"] < links[j]["sourceId"] })
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(links)
			return
		}
		if request.URL.Path == "/api/v1/maintenance-recurrence-link" && request.Method == http.MethodPost {
			if !allows(identity, "facility.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			var input struct {
				ClientID    string `json:"clientId"`
				OperationID string `json:"operationId"`
				SourceID    string `json:"sourceId"`
				SuccessorID string `json:"successorId"`
			}
			defer request.Body.Close()
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ClientID) == "" || len(input.ClientID) > 128 || strings.TrimSpace(input.OperationID) == "" || len(input.OperationID) > 128 || strings.TrimSpace(input.SourceID) == "" || len(input.SourceID) > 256 || strings.TrimSpace(input.SuccessorID) == "" || len(input.SuccessorID) > 256 || input.SourceID == input.SuccessorID {
				http.Error(writer, "invalid recurrence link", 400)
				return
			}
			canonical, _ := json.Marshal(input)
			sum := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(sum[:])
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			if _, ok := maintenanceOrders[input.SourceID]; !ok {
				http.Error(writer, "source maintenance missing", 409)
				return
			}
			if _, ok := maintenanceOrders[input.SuccessorID]; !ok {
				http.Error(writer, "successor maintenance missing", 409)
				return
			}
			if maintenanceSuccessors[input.SourceID] != input.SuccessorID && maintenanceOrders[input.SourceID].AssetID != maintenanceOrders[input.SuccessorID].AssetID {
				http.Error(writer, "maintenance assets differ", 409)
				return
			}
			if existing := maintenanceSuccessors[input.SourceID]; existing != "" && existing != input.SuccessorID {
				http.Error(writer, "different recurrence successor", 409)
				return
			}
			for source, successor := range maintenanceSuccessors {
				if source != input.SourceID && successor == input.SuccessorID {
					http.Error(writer, "successor already linked", 409)
					return
				}
			}
			seen := map[string]bool{}
			for cursor := input.SuccessorID; cursor != ""; cursor = maintenanceSuccessors[cursor] {
				if cursor == input.SourceID || seen[cursor] {
					http.Error(writer, "recurrence cycle", 409)
					return
				}
				seen[cursor] = true
			}
			payload, _ := json.Marshal(map[string]string{"sourceId": input.SourceID, "successorId": input.SuccessorID})
			event := syncEvent{Topic: "central.maintenance_recurrence.linked", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			maintenanceSuccessors[input.SourceID] = input.SuccessorID
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(event); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		// Fence a never-accepted employee create before an administrator links
		// the existing local person to a reviewed central person.
		if request.URL.Path == "/api/v1/employee-sync-abandon" && request.Method == http.MethodPost {
			if !allows(identity, "employees.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			var input struct {
				Operation employeeSyncOperation `json:"operation"`
				Remote    centralEmployee       `json:"remote"`
			}
			defer request.Body.Close()
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil {
				http.Error(writer, "invalid request", 400)
				return
			}
			op := input.Operation
			if strings.TrimSpace(op.ClientID) == "" || len(op.ClientID) > 128 || strings.TrimSpace(op.OperationID) == "" || len(op.OperationID) > 128 || op.Action != "create" || op.Expected != nil || op.Employee.ID == "" || len(op.Employee.ID) > 256 || op.Employee.Name == "" || op.Employee.Role == "" || op.Employee.Department == "" || !op.Employee.Active || op.Employee.AccountEmployeeID != 0 || (op.Employee.Presence != "office" && op.Employee.Presence != "remote" && op.Employee.Presence != "meeting" && op.Employee.Presence != "away") || input.Remote.ID == "" || input.Remote.ID == op.Employee.ID {
				http.Error(writer, "invalid employee operation", 400)
				return
			}
			canonical, _ := json.Marshal(op)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			abandoned := "abandoned:" + fingerprint
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, op.ClientID, op.OperationID, fingerprint) {
			case 1:
				http.Error(writer, "operation already accepted", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			switch centralMirror.roomReceipt(identity.EmployeeID, op.ClientID, op.OperationID, abandoned) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			remote, exists := employees[input.Remote.ID]
			_, ownExists := employees[op.Employee.ID]
			if ownExists || !exists || remote != input.Remote || !strings.EqualFold(remote.Name, op.Employee.Name) {
				http.Error(writer, "employee conflict changed", 409)
				return
			}
			payload, _ := json.Marshal(map[string]any{"clientId": op.ClientID, "operationId": op.OperationID, "actorId": identity.EmployeeID, "original": op.Employee, "remote": remote})
			event := syncEvent{Topic: "central.employee_sync.abandoned", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, op.ClientID, op.OperationID, abandoned, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		if request.URL.Path == "/api/v1/employee-sync" && request.Method == http.MethodPost {
			if !allows(identity, "employees.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			defer request.Body.Close()
			var input employeeSyncOperation
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ClientID) == "" || len(input.ClientID) > 128 || strings.TrimSpace(input.OperationID) == "" || len(input.OperationID) > 128 || strings.TrimSpace(input.Employee.ID) == "" || len(input.Employee.ID) > 256 || strings.TrimSpace(input.Employee.Name) == "" || strings.TrimSpace(input.Employee.Role) == "" || strings.TrimSpace(input.Employee.Department) == "" || (input.Employee.Presence != "office" && input.Employee.Presence != "remote" && input.Employee.Presence != "meeting" && input.Employee.Presence != "away") || (input.Action != "create" && input.Action != "update" && input.Action != "archive") || (input.Action == "archive" && input.Employee.Active) || (input.Action == "create" && !input.Employee.Active) {
				http.Error(writer, "invalid employee sync request", 400)
				return
			}
			canonical, _ := json.Marshal(input)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			existing, found := employees[input.Employee.ID]
			if input.Action == "create" {
				if found || input.Expected != nil || input.Employee.AccountEmployeeID != 0 {
					http.Error(writer, "employee exists or unexpected base", 409)
					return
				}
			} else if !found || input.Expected == nil || *input.Expected != existing || (input.Action == "archive" && !existing.Active) {
				http.Error(writer, "employee changed on server", 409)
				return
			}
			if found && input.Employee.AccountEmployeeID != existing.AccountEmployeeID {
				http.Error(writer, "account link cannot be changed by sync", 409)
				return
			}
			for id, v := range employees {
				if id != input.Employee.ID && input.Employee.Active && input.Employee.AccountEmployeeID != 0 && v.Active && v.AccountEmployeeID == input.Employee.AccountEmployeeID {
					http.Error(writer, "account already linked", 409)
					return
				}
				if id != input.Employee.ID && strings.EqualFold(v.Name, input.Employee.Name) {
					http.Error(writer, "employee name already exists", 409)
					return
				}
			}
			topic := "employee." + map[string]string{"create": "created", "update": "updated", "archive": "archived"}[input.Action]
			payload, _ := json.Marshal(input.Employee)
			event := syncEvent{Topic: topic, Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			employees[input.Employee.ID] = input.Employee
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(event); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		// Desktop payloads use shared employee object IDs; local numeric IDs
		// never cross this boundary. Repeated delivery is idempotent.
		if request.URL.Path == "/api/v1/desktop-leave-events" && request.Method == http.MethodPost {
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			defer request.Body.Close()
			var event syncEvent
			if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&event) != nil || (event.Topic != "desktop.leave.requested" && event.Topic != "desktop.leave.decided") {
				http.Error(writer, "invalid event", 400)
				return
			}
			var value struct {
				ID       string `json:"id"`
				Employee string `json:"employeeExternalId"`
				Decider  string `json:"deciderExternalId"`
				StartsAt int64  `json:"startsAt"`
				EndsAt   int64  `json:"endsAt"`
				Reason   string `json:"reason"`
				Status   string `json:"status"`
			}
			decoder := json.NewDecoder(strings.NewReader(event.Payload))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&value) != nil || strings.TrimSpace(value.ID) == "" || len(value.ID) > 256 || value.StartsAt <= 0 || value.EndsAt <= value.StartsAt || strings.TrimSpace(value.Reason) == "" {
				http.Error(writer, "invalid leave payload", 400)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			employeeRecord, exists := employees[value.Employee]
			employee, ok := approvalAccount(value.Employee)
			if !exists || !employeeRecord.Active || !ok || value.Employee == "" {
				http.Error(writer, "employee account link required", 409)
				return
			}
			employeeID, err := strconv.ParseInt(employee, 10, 64)
			if err != nil {
				http.Error(writer, "employee account link required", 409)
				return
			}
			deciderID := int64(0)
			if event.Topic == "desktop.leave.requested" {
				if !allows(identity, "leave.request") || employeeID != identity.EmployeeID {
					http.Error(writer, "forbidden", 403)
					return
				}
				if value.Status != "pending" || value.Decider != "" {
					http.Error(writer, "invalid leave request", 400)
					return
				}
			} else {
				deciderRecord, exists := employees[value.Decider]
				decider, ok := approvalAccount(value.Decider)
				if !exists || !deciderRecord.Active || !ok || value.Decider == "" {
					http.Error(writer, "decider account link required", 409)
					return
				}
				deciderID, err = strconv.ParseInt(decider, 10, 64)
				if err != nil {
					http.Error(writer, "decider account link required", 409)
					return
				}
				if !allows(identity, "leave.approve") || deciderID != identity.EmployeeID || deciderID == employeeID {
					http.Error(writer, "forbidden", 403)
					return
				}
				if value.Status != "approved" && value.Status != "rejected" {
					http.Error(writer, "invalid leave decision", 400)
					return
				}
			}
			leave := leaveRequest{ID: value.ID, EmployeeID: employeeID, StartsAt: value.StartsAt, EndsAt: value.EndsAt, Reason: value.Reason, Status: value.Status, DecidedBy: deciderID}
			canonical, _ := json.Marshal(struct {
				Topic string `json:"topic"`
				Value any    `json:"value"`
			}{event.Topic, value})
			digest := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(digest[:])
			// The content digest is the stable operation ID for this legacy
			// desktop outbox, which does not carry independent operation IDs.
			switch centralMirror.roomReceipt(identity.EmployeeID, "desktop-leave-v1", fingerprint, fingerprint) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			old, exists := leaves[leave.ID]
			if exists && leaveAccounts[leave.ID] && old == leave {
				writer.WriteHeader(http.StatusAccepted)
				return
			}
			topic := "leave.requested"
			if event.Topic == "desktop.leave.requested" {
				if exists {
					http.Error(writer, "leave already exists", 409)
					return
				}
			} else {
				if !exists || !leaveAccounts[leave.ID] || old.Status != "pending" || old.DecidedBy != 0 || old.EmployeeID != leave.EmployeeID || old.StartsAt != leave.StartsAt || old.EndsAt != leave.EndsAt || old.Reason != leave.Reason {
					http.Error(writer, "leave changed or identity unverified", 409)
					return
				}
				if original := leaveOriginalEmployees[leave.ID]; original != "" && original != value.Employee {
					http.Error(writer, "leave employee binding changed", 409)
					return
				}
				topic = "leave.decided"
			}
			employeeOrigin := leaveOriginalEmployees[leave.ID]
			if event.Topic == "desktop.leave.requested" {
				employeeOrigin = value.Employee
			}
			payload := encodeBoundLeaveEvent(leave, employeeOrigin, value.Decider)
			committed := syncEvent{Topic: topic, Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, "desktop-leave-v1", fingerprint, fingerprint, committed) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(committed); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			leaves[leave.ID] = leave
			leaveAccounts[leave.ID] = true
			if employeeOrigin != "" {
				leaveOriginalEmployees[leave.ID] = employeeOrigin
			}
			if value.Decider != "" {
				leaveOriginalDeciders[leave.ID] = value.Decider
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		// Legacy numeric identities require an explicit, audited mapping.
		if request.URL.Path == "/api/v1/legacy-leave-reconcile" && request.Method == http.MethodPost {
			if !allows(identity, "employees.manage") || !allows(identity, "leave.approve") {
				http.Error(writer, "forbidden", 403)
				return
			}
			defer request.Body.Close()
			var input struct {
				Expected leaveRequest `json:"expected"`
				Employee string       `json:"employeeExternalId"`
				Decider  string       `json:"deciderExternalId"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.Expected.ID) == "" || strings.TrimSpace(input.Employee) == "" {
				http.Error(writer, "invalid reconciliation", 400)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			original, found := leaves[input.Expected.ID]
			if found && leaveAccounts[input.Expected.ID] {
				if previous, ok := leaveReconciliations[input.Expected.ID]; ok && previous.ReconciledBy == identity.EmployeeID && previous.Original == input.Expected && previous.EmployeeExternalID == input.Employee && previous.DeciderExternalID == input.Decider {
					writer.Header().Set("Content-Type", "application/json")
					_ = json.NewEncoder(writer).Encode(original)
					return
				}
			}
			verifiedLegacy := found && leaveAccounts[input.Expected.ID] && (leaveOriginalEmployees[input.Expected.ID] == "" || (original.Status != "pending" && leaveOriginalDeciders[input.Expected.ID] == ""))
			if !found || (leaveAccounts[input.Expected.ID] && !verifiedLegacy) || original != input.Expected {
				http.Error(writer, "leave changed or already reconciled", 409)
				return
			}
			employee, found := employees[input.Employee]
			if !found || !employee.Active {
				http.Error(writer, "employee not linked", 409)
				return
			}
			account, ok := approvalAccount(input.Employee)
			if !ok {
				http.Error(writer, "employee account link required", 409)
				return
			}
			employeeID, err := strconv.ParseInt(account, 10, 64)
			if err != nil {
				http.Error(writer, "employee account link required", 409)
				return
			}
			deciderID := int64(0)
			if original.Status == "pending" {
				if input.Decider != "" || original.DecidedBy != 0 {
					http.Error(writer, "invalid pending identity", 409)
					return
				}
			} else {
				decider, found := employees[input.Decider]
				if !found || !decider.Active {
					http.Error(writer, "decider not linked", 409)
					return
				}
				account, ok := approvalAccount(input.Decider)
				if !ok {
					http.Error(writer, "decider account link required", 409)
					return
				}
				deciderID, err = strconv.ParseInt(account, 10, 64)
				if err != nil || deciderID == employeeID {
					http.Error(writer, "invalid decider account", 409)
					return
				}
			}
			if verifiedLegacy && (employeeID != original.EmployeeID || deciderID != original.DecidedBy || (leaveOriginalEmployees[original.ID] != "" && leaveOriginalEmployees[original.ID] != input.Employee) || (leaveOriginalDeciders[original.ID] != "" && leaveOriginalDeciders[original.ID] != input.Decider)) {
				http.Error(writer, "confirmed account or historical binding differs", 409)
				return
			}
			reconciled := original
			reconciled.EmployeeID = employeeID
			reconciled.DecidedBy = deciderID
			resolution := leaveReconciliationEvent{reconciled, "server-account-v1", original, identity.EmployeeID, input.Employee, input.Decider}
			payload, _ := json.Marshal(resolution)
			event := syncEvent{Topic: "leave.identity.reconciled", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if persistEvent(journal, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			leaves[reconciled.ID] = reconciled
			leaveAccounts[reconciled.ID] = true
			leaveOriginalEmployees[reconciled.ID] = input.Employee
			if input.Decider != "" {
				leaveOriginalDeciders[reconciled.ID] = input.Decider
			}
			leaveReconciled[reconciled.ID] = true
			leaveReconciliations[reconciled.ID] = resolution
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(reconciled)
			return
		}
		// Offline leave operations are accepted once and attributed to the
		// authenticated server identity. Decisions compare the full prior state.
		// Fence an unaccepted personal leave request before identity mapping.
		if request.URL.Path == "/api/v1/leave-sync-abandon" && request.Method == http.MethodPost {
			if !allows(identity, "leave.request") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			defer request.Body.Close()
			var input struct {
				Operation leaveSyncOperation `json:"operation"`
				Remote    leaveRequest       `json:"remote"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil {
				http.Error(writer, "invalid request", 400)
				return
			}
			op := input.Operation
			if strings.TrimSpace(op.ClientID) == "" || len(op.ClientID) > 128 || strings.TrimSpace(op.OperationID) == "" || len(op.OperationID) > 128 || op.Action != "request" || op.Expected != nil || op.DeciderExternalID != "" || op.Leave.ID == "" || len(op.Leave.ID) > 256 || !strings.HasPrefix(op.Leave.ID, op.ClientID+":leave:") || op.Leave.EmployeeID != identity.EmployeeID || op.Leave.StartsAt <= 0 || op.Leave.EndsAt <= op.Leave.StartsAt || strings.TrimSpace(op.Leave.Reason) == "" || op.Leave.Status != "pending" || op.Leave.DecidedBy != 0 || input.Remote.ID == "" || input.Remote.ID == op.Leave.ID {
				http.Error(writer, "invalid leave operation", 400)
				return
			}
			canonical, _ := json.Marshal(op)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			abandoned := "abandoned:" + fingerprint
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, op.ClientID, op.OperationID, fingerprint) {
			case 1:
				http.Error(writer, "operation already accepted", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			switch centralMirror.roomReceipt(identity.EmployeeID, op.ClientID, op.OperationID, abandoned) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			remote, exists := leaves[input.Remote.ID]
			_, ownExists := leaves[op.Leave.ID]
			employee := approvalEmployee(strconv.FormatInt(identity.EmployeeID, 10))
			origin := leaveOriginalEmployees[input.Remote.ID]
			if ownExists || !exists || remote != input.Remote || !leaveAccounts[input.Remote.ID] || employee == "" || origin == "" || origin != employee || (op.EmployeeExternalID != "" && op.EmployeeExternalID != employee) || remote.EmployeeID != op.Leave.EmployeeID || remote.StartsAt != op.Leave.StartsAt || remote.EndsAt != op.Leave.EndsAt || remote.Reason != op.Leave.Reason {
				http.Error(writer, "leave conflict or employee binding changed", 409)
				return
			}
			payload, _ := json.Marshal(map[string]any{"clientId": op.ClientID, "operationId": op.OperationID, "actorId": identity.EmployeeID, "original": op.Leave, "remote": remote})
			event := syncEvent{Topic: "central.leave_sync.abandoned", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, op.ClientID, op.OperationID, abandoned, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		if request.URL.Path == "/api/v1/leave-sync" && request.Method == http.MethodPost {
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			defer request.Body.Close()
			var input leaveSyncOperation
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ClientID) == "" || len(input.ClientID) > 128 || strings.TrimSpace(input.OperationID) == "" || len(input.OperationID) > 128 || strings.TrimSpace(input.Leave.ID) == "" || len(input.Leave.ID) > 256 || input.Leave.EmployeeID <= 0 || input.Leave.StartsAt <= 0 || input.Leave.EndsAt <= input.Leave.StartsAt || strings.TrimSpace(input.Leave.Reason) == "" || (input.Action != "request" && input.Action != "decide") {
				http.Error(writer, "invalid leave sync request", 400)
				return
			}
			if input.Action == "request" {
				if !allows(identity, "leave.request") || input.Leave.EmployeeID != identity.EmployeeID {
					http.Error(writer, "forbidden", 403)
					return
				}
				if input.Expected != nil || input.Leave.Status != "pending" || input.Leave.DecidedBy != 0 {
					http.Error(writer, "invalid leave request", 400)
					return
				}
			} else {
				if !allows(identity, "leave.approve") || input.Leave.DecidedBy != identity.EmployeeID || input.Leave.EmployeeID == identity.EmployeeID {
					http.Error(writer, "forbidden", 403)
					return
				}
				if input.Expected == nil || (input.Leave.Status != "approved" && input.Leave.Status != "rejected") {
					http.Error(writer, "invalid leave decision", 400)
					return
				}
			}
			canonical, _ := json.Marshal(input)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			if input.EmployeeExternalID != "" || input.DeciderExternalID != "" {
				if input.EmployeeExternalID == "" || len(input.EmployeeExternalID) > 256 || approvalEmployee(strconv.FormatInt(input.Leave.EmployeeID, 10)) != input.EmployeeExternalID || (input.Action == "request" && input.DeciderExternalID != "") || (input.Action == "decide" && (input.DeciderExternalID == "" || len(input.DeciderExternalID) > 256 || approvalEmployee(strconv.FormatInt(identity.EmployeeID, 10)) != input.DeciderExternalID)) {
					http.Error(writer, "employee account binding changed", 409)
					return
				}
			}
			existing, found := leaves[input.Leave.ID]
			topic := "leave.requested"
			if input.Action == "request" {
				if found {
					http.Error(writer, "leave already exists", 409)
					return
				}
			} else {
				if !found || !leaveAccounts[input.Leave.ID] || *input.Expected != existing || existing.Status != "pending" || existing.DecidedBy != 0 {
					http.Error(writer, "leave changed on server", 409)
					return
				}
				if original := leaveOriginalEmployees[input.Leave.ID]; original != "" && approvalEmployee(strconv.FormatInt(input.Leave.EmployeeID, 10)) != original {
					http.Error(writer, "leave employee binding changed", 409)
					return
				}
				if input.Leave.ID != existing.ID || input.Leave.EmployeeID != existing.EmployeeID || input.Leave.StartsAt != existing.StartsAt || input.Leave.EndsAt != existing.EndsAt || input.Leave.Reason != existing.Reason {
					http.Error(writer, "leave details cannot be changed by decision", 409)
					return
				}
				topic = "leave.decided"
			}
			verified := input.Action == "request" || leaveAccounts[input.Leave.ID]
			employeeOrigin := leaveOriginalEmployees[input.Leave.ID]
			if input.Action == "request" {
				employeeOrigin = approvalEmployee(strconv.FormatInt(input.Leave.EmployeeID, 10))
			}
			deciderOrigin := ""
			if input.Action == "decide" {
				deciderOrigin = approvalEmployee(strconv.FormatInt(identity.EmployeeID, 10))
			}
			payload := encodeBoundLeaveEvent(input.Leave, employeeOrigin, deciderOrigin)
			event := syncEvent{Topic: topic, Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			leaves[input.Leave.ID] = input.Leave
			leaveAccounts[input.Leave.ID] = verified
			if employeeOrigin != "" {
				leaveOriginalEmployees[input.Leave.ID] = employeeOrigin
			}
			if deciderOrigin != "" {
				leaveOriginalDeciders[input.Leave.ID] = deciderOrigin
			}
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(event); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		// A booking and its receipt commit together, so a lost response is safe to retry.
		// Fence an unaccepted create before a client discards its local queue.
		if request.URL.Path == "/api/v1/booking-sync-abandon" && request.Method == http.MethodPost {
			if !allows(identity, "rooms.book") && !allows(identity, "rooms.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			defer request.Body.Close()
			var input struct {
				ClientID    string          `json:"clientId"`
				OperationID string          `json:"operationId"`
				Action      string          `json:"action"`
				Booking     bookingPayload  `json:"booking"`
				Expected    *bookingPayload `json:"expected"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ClientID) == "" || len(input.ClientID) > 128 || strings.TrimSpace(input.OperationID) == "" || len(input.OperationID) > 128 || (input.Action != "create" && input.Action != "reschedule" && input.Action != "cancel") || input.Booking.ClientID == "" || input.Booking.BookingID <= 0 || input.Booking.UserID <= 0 || input.Booking.RoomID != 1 || input.Booking.RoomKey == "" || input.Booking.StartsAt <= 0 || input.Booking.EndsAt <= input.Booking.StartsAt || (input.Action == "create" && (input.Expected != nil || input.Booking.ClientID != input.ClientID || input.Booking.UserID != identity.EmployeeID || input.Booking.Status != "confirmed")) || (input.Action != "create" && (input.Expected == nil || (input.Booking.UserID != identity.EmployeeID && !allows(identity, "rooms.manage")))) {
				http.Error(writer, "invalid booking operation", 400)
				return
			}
			canonical, _ := json.Marshal(input)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			abandoned := "abandoned:" + fingerprint
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				http.Error(writer, "booking operation already accepted", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, abandoned) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			existing, exists := bookingHistory[bookingKey(input.Booking.ClientID, input.Booking.BookingID)]
			if input.Action == "create" {
				if exists {
					http.Error(writer, "booking already exists", 409)
					return
				}
				registry.purgeExpiredReservations(time.Now().Unix())
				if !registry.conflicts(input.ClientID, input.Booking.BookingID, 1, input.Booking.StartsAt, input.Booking.EndsAt, 0, input.Booking.RoomKey) {
					http.Error(writer, "conflict no longer exists", 409)
					return
				}
			} else {
				if !exists || existing == *input.Expected || existing.ClientID != input.Expected.ClientID || existing.BookingID != input.Expected.BookingID || existing.UserID != input.Expected.UserID || existing.UserID != input.Booking.UserID || existing.RoomKey != input.Expected.RoomKey || existing.RoomKey != input.Booking.RoomKey || existing.RoomID != input.Expected.RoomID || (existing.UserID != identity.EmployeeID && !allows(identity, "rooms.manage")) {
					http.Error(writer, "booking conflict no longer eligible", 409)
					return
				}
			}
			payload, _ := json.Marshal(map[string]any{"clientId": input.ClientID, "operationId": input.OperationID, "actorId": identity.EmployeeID, "bookingId": input.Booking.BookingID})
			event := syncEvent{Topic: "central.booking_sync.abandoned", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, abandoned, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		// A snapshot and its event cursor are sampled under the booking lock.
		// Clients must first process the normal feed to this cursor before
		// adopting the snapshot; they must never advance other domain cursors.
		if request.URL.Path == "/api/v1/legacy-booking-reconcile" && request.Method == http.MethodPost {
			if !allows(identity, "rooms.manage") || !allows(identity, "employees.manage") || identity.EmployeeID <= 0 {
				http.Error(writer, "forbidden", 403)
				return
			}
			var input struct {
				Expected    bookingPayload `json:"expected"`
				EmployeeKey string         `json:"employeeExternalId"`
				Attendees   int            `json:"attendees"`
			}
			defer request.Body.Close()
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || input.Expected.ClientID == "" || input.Expected.BookingID <= 0 || input.Expected.RoomKey == "" || input.EmployeeKey == "" || input.Attendees <= 0 {
				http.Error(writer, "invalid reconciliation", 400)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			key := bookingKey(input.Expected.ClientID, input.Expected.BookingID)
			original, found := bookingHistory[key]
			if prior, ok := bookingReconciliations[key]; ok && found && original == prior.Booking && prior.Original == input.Expected && prior.EmployeeKey == input.EmployeeKey && prior.ReconciledBy == identity.EmployeeID && prior.Booking.Attendees == input.Attendees {
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(original)
				return
			}
			if !found || original != input.Expected || original.UserID != 0 || original.RoomKey == "" || (original.Status != "" && original.Status != "confirmed" && original.Status != "cancelled") {
				http.Error(writer, "booking changed or already reconciled", 409)
				return
			}
			room, found := rooms[original.RoomKey]
			if !found || input.Attendees > room.Capacity {
				http.Error(writer, "room or capacity invalid", 409)
				return
			}
			employee, found := employees[input.EmployeeKey]
			if !found || !employee.Active || employee.AccountEmployeeID <= 0 || approvalEmployee(strconv.FormatInt(employee.AccountEmployeeID, 10)) != input.EmployeeKey {
				http.Error(writer, "employee account link required", 409)
				return
			}
			booking := original
			booking.RoomID = 1
			booking.UserID = employee.AccountEmployeeID
			booking.Attendees = input.Attendees
			if booking.Status == "" {
				booking.Status = "confirmed"
			}
			resolved := bookingIdentityReconciliation{Booking: booking, Original: original, EmployeeKey: input.EmployeeKey, ReconciledBy: identity.EmployeeID}
			payload, _ := json.Marshal(resolved)
			if persistEvent(journal, syncEvent{Topic: "central.booking.identity.reconciled", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			bookingHistory[key] = booking
			bookingReconciliations[key] = resolved
			if booking.Status == "cancelled" {
				delete(registry.active, key)
			} else {
				registry.active[key] = booking
			}
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(booking)
			return
		}
		if request.URL.Path == "/api/v1/desktop-booking-snapshot" && request.Method == http.MethodGet {
			if (!allows(identity, "rooms.book") && !allows(identity, "rooms.manage")) || !allows(identity, "sync.events.read") {
				http.Error(writer, "forbidden", http.StatusForbidden)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", http.StatusServiceUnavailable)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			cursor, err := centralMirror.latestCursor()
			if err != nil {
				http.Error(writer, "persistence failure", http.StatusInternalServerError)
				return
			}
			bookings := []bookingPayload{}
			for _, booking := range bookingHistory {
				if booking.RoomKey != "" {
					bookings = append(bookings, booking)
				}
			}
			sort.Slice(bookings, func(i, j int) bool {
				if bookings[i].ClientID == bookings[j].ClientID {
					return bookings[i].BookingID < bookings[j].BookingID
				}
				return bookings[i].ClientID < bookings[j].ClientID
			})
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(struct {
				Cursor   int64            `json:"cursor"`
				Bookings []bookingPayload `json:"bookings"`
			}{cursor, bookings})
			return
		}
		if request.URL.Path == "/api/v1/booking-sync" {
			if !allows(identity, "rooms.book") && !allows(identity, "rooms.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if centralMirror == nil {
				http.Error(writer, "central database required", 503)
				return
			}
			if request.Method == http.MethodGet {
				registry.Lock()
				defer registry.Unlock()
				result := []bookingPayload{}
				for _, v := range bookingHistory {
					if v.RoomKey != "" {
						result = append(result, v)
					}
				}
				sort.Slice(result, func(i, j int) bool {
					if result[i].ClientID == result[j].ClientID {
						return result[i].BookingID < result[j].BookingID
					}
					return result[i].ClientID < result[j].ClientID
				})
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method != http.MethodPost {
				http.Error(writer, "method not allowed", 405)
				return
			}
			defer request.Body.Close()
			var input struct {
				ClientID    string          `json:"clientId"`
				OperationID string          `json:"operationId"`
				Action      string          `json:"action"`
				Booking     bookingPayload  `json:"booking"`
				Expected    *bookingPayload `json:"expected"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ClientID) == "" || len(input.ClientID) > 128 || strings.TrimSpace(input.OperationID) == "" || len(input.OperationID) > 128 || strings.TrimSpace(input.Booking.ClientID) == "" || len(input.Booking.ClientID) > 128 || input.Booking.BookingID <= 0 || input.Booking.RoomID != 1 || strings.TrimSpace(input.Booking.RoomKey) == "" || len(input.Booking.RoomKey) > 256 || input.Booking.UserID <= 0 || (input.Action != "block" && input.Booking.Reason != "") || (input.Action != "create" && input.Action != "block" && input.Action != "reschedule" && input.Action != "cancel") {
				http.Error(writer, "invalid booking sync request", 400)
				return
			}
			canonical, _ := json.Marshal(input)
			hash := sha256.Sum256(canonical)
			fingerprint := hex.EncodeToString(hash[:])
			registry.Lock()
			defer registry.Unlock()
			switch centralMirror.roomReceipt(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint) {
			case 1:
				writer.WriteHeader(http.StatusAccepted)
				return
			case 2:
				http.Error(writer, "operation identity reused", 409)
				return
			case -1:
				http.Error(writer, "receipt storage failure", 500)
				return
			}
			room, found := rooms[input.Booking.RoomKey]
			if !found || (!room.Active && input.Action != "cancel") {
				http.Error(writer, "active room missing", 409)
				return
			}
			key := bookingKey(input.Booking.ClientID, input.Booking.BookingID)
			old, exists := bookingHistory[key]
			if input.Action == "create" || input.Action == "block" {
				if input.Action == "block" && !allows(identity, "rooms.manage") {
					http.Error(writer, "forbidden", 403)
					return
				}
				if input.Booking.ClientID != input.ClientID {
					http.Error(writer, "booking client mismatch", 400)
					return
				}
				if input.Booking.UserID != identity.EmployeeID {
					http.Error(writer, "booking owner mismatch", 403)
					return
				}
				if exists || input.Expected != nil || (input.Action == "create" && input.Booking.Status != "confirmed") || (input.Action == "block" && (input.Booking.Status != "blocked" || strings.TrimSpace(input.Booking.Reason) == "" || len(input.Booking.Reason) > 4000)) {
					http.Error(writer, "booking exists or invalid base", 409)
					return
				}
			} else {
				if !exists || input.Expected == nil || *input.Expected != old || old.Status != "confirmed" || (old.UserID != identity.EmployeeID && !allows(identity, "rooms.manage")) || old.UserID != input.Booking.UserID || old.RoomKey != input.Booking.RoomKey {
					http.Error(writer, "booking changed on server", 409)
					return
				}
				if input.Action == "cancel" {
					expected := old
					expected.Status = "cancelled"
					if input.Booking != expected {
						http.Error(writer, "cancellation cannot modify booking", 400)
						return
					}
				} else if input.Booking.Status != "confirmed" {
					http.Error(writer, "invalid status", 400)
					return
				}
			}
			if input.Booking.StartsAt <= 0 || input.Booking.EndsAt <= input.Booking.StartsAt || input.Booking.EndsAt > 253402300799 || input.Booking.Attendees <= 0 || (input.Action != "cancel" && input.Booking.Attendees > room.Capacity) {
				http.Error(writer, "invalid time or capacity", 400)
				return
			}
			registry.purgeExpiredReservations(time.Now().Unix())
			if input.Action != "cancel" && registry.conflicts(input.Booking.ClientID, input.Booking.BookingID, 1, input.Booking.StartsAt, input.Booking.EndsAt, 0, input.Booking.RoomKey) {
				http.Error(writer, "booking conflict", 409)
				return
			}
			topic := map[string]string{"create": "booking.created", "block": "booking.created", "reschedule": "booking.rescheduled", "cancel": "booking.cancelled"}[input.Action]
			payload, _ := json.Marshal(input.Booking)
			event := syncEvent{Topic: topic, Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if centralMirror.appendRoomSync(identity.EmployeeID, input.ClientID, input.OperationID, fingerprint, event) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			if input.Action == "cancel" {
				delete(registry.active, key)
			} else {
				registry.active[key] = input.Booking
			}
			bookingHistory[key] = input.Booking
			if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
				if data, err := json.Marshal(event); err == nil {
					_, _ = file.Write(append(data, '\n'))
				}
				_ = file.Close()
			}
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		if request.URL.Path == "/api/v1/room-bookings" {
			if !allows(identity, "rooms.read") && !allows(identity, "rooms.book") && !allows(identity, "rooms.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if request.Method != http.MethodGet && !allows(identity, "rooms.book") && !allows(identity, "rooms.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			roomKey := request.URL.Query().Get("roomKey")
			registry.Lock()
			defer registry.Unlock()
			room, ok := rooms[roomKey]
			if !ok || !room.Active {
				http.NotFound(writer, request)
				return
			}
			registry.purgeExpiredReservations(time.Now().Unix())
			if request.Method == http.MethodGet {
				result := []bookingPayload{}
				for _, v := range bookingHistory {
					if v.RoomKey == roomKey {
						if v.Status == "" {
							v.Status = "confirmed"
						}
						result = append(result, v)
					}
				}
				sort.Slice(result, func(i, j int) bool { return result[i].StartsAt < result[j].StartsAt })
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method != http.MethodPost && request.Method != http.MethodPatch && request.Method != http.MethodDelete {
				http.Error(writer, "method not allowed", 405)
				return
			}
			var input struct {
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
			defer request.Body.Close()
			if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil {
				http.Error(writer, "invalid request", 400)
				return
			}
			var existing bookingPayload
			if request.Method != http.MethodPost {
				var ok bool
				existing, ok = registry.active[bookingKey(input.ClientID, input.BookingID)]
				if !ok || existing.RoomKey != roomKey {
					http.NotFound(writer, request)
					return
				}
				if !allows(identity, "rooms.manage") && (existing.UserID == 0 || existing.UserID != identity.EmployeeID) {
					http.Error(writer, "forbidden", 403)
					return
				}
			}
			if request.Method == http.MethodDelete && existing.Status == "blocked" && !allows(identity, "rooms.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if request.Method == http.MethodDelete {
				existing.Status = "cancelled"
				payload, _ := json.Marshal(existing)
				if persistEvent(journal, syncEvent{Topic: "booking.cancelled", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
					http.Error(writer, "persistence failure", 500)
					return
				}
				key := bookingKey(existing.ClientID, existing.BookingID)
				delete(registry.active, key)
				bookingHistory[key] = existing
				writer.WriteHeader(204)
				return
			}
			if input.StartsAt <= 0 || input.EndsAt <= input.StartsAt || input.EndsAt > 253402300799 || input.Attendees <= 0 || input.Attendees > room.Capacity {
				http.Error(writer, "invalid time or capacity", 400)
				return
			}
			if input.Blocked && (!allows(identity, "rooms.manage") || strings.TrimSpace(input.Reason) == "") {
				http.Error(writer, "forbidden or missing reason", 403)
				return
			}
			n := 1
			interval := int64(0)
			if request.Method == http.MethodPost {
				n = input.Occurrences
				if n < 1 || n > 104 || input.IntervalDays < 1 || input.IntervalDays > 366 {
					http.Error(writer, "invalid series", 400)
					return
				}
				interval = int64(input.IntervalDays) * 86400
				if n > 1 && input.EndsAt-input.StartsAt > interval {
					http.Error(writer, "series overlaps itself", 409)
					return
				}
			}
			if input.EndsAt+int64(n-1)*interval > 253402300799 {
				http.Error(writer, "invalid time", 400)
				return
			}
			if request.Method == http.MethodPatch && existing.Status == "blocked" {
				http.Error(writer, "blocked interval cannot be rescheduled", 409)
				return
			}
			now := time.Now().UTC()
			base := now.UnixNano()
			if base <= 0 {
				http.Error(writer, "clock error", 500)
				return
			}
			events := []syncEvent{}
			values := []bookingPayload{}
			for i := 0; i < n; i++ {
				v := existing
				if request.Method == http.MethodPost {
					v = bookingPayload{ClientID: "central", BookingID: base + int64(i), RoomID: 1, RoomKey: roomKey, UserID: identity.EmployeeID, Status: "confirmed"}
					if input.Blocked {
						v.Status = "blocked"
						v.Reason = input.Reason
					}
				}
				if request.Method == http.MethodPost {
					if _, exists := bookingHistory[bookingKey(v.ClientID, v.BookingID)]; exists {
						http.Error(writer, "booking id collision", 409)
						return
					}
				}
				v.StartsAt = input.StartsAt + int64(i)*interval
				v.EndsAt = input.EndsAt + int64(i)*interval
				v.Attendees = input.Attendees
				if registry.conflicts(v.ClientID, v.BookingID, v.RoomID, v.StartsAt, v.EndsAt, 0, v.RoomKey) {
					http.Error(writer, "booking conflict", 409)
					return
				}
				payload, _ := json.Marshal(v)
				topic := "booking.created"
				if request.Method == http.MethodPatch {
					topic = "booking.rescheduled"
				}
				events = append(events, syncEvent{Topic: topic, Payload: string(payload), At: now.Format(time.RFC3339Nano)})
				values = append(values, v)
			}
			if persistRelatedEvents(journal, events) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			for _, v := range values {
				key := bookingKey(v.ClientID, v.BookingID)
				registry.active[key] = v
				bookingHistory[key] = v
			}
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(values)
			return
		}
		if request.URL.Path == "/api/v1/identity" && request.Method == http.MethodGet {
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(map[string]any{"employeeId": identity.EmployeeID, "canManageRooms": allows(identity, "rooms.manage"), "canBookRooms": allows(identity, "rooms.book") || allows(identity, "rooms.manage"), "canManageEmployees": allows(identity, "employees.manage"), "canManageTasks": allows(identity, "tasks.manage"), "canReadTasks": allows(identity, "tasks.read") || allows(identity, "tasks.manage"), "canManageTickets": allows(identity, "tickets.manage"), "canReadTickets": allows(identity, "tickets.read") || allows(identity, "tickets.manage"), "canCollaborate": allows(identity, "collaboration.comment"), "canManageFacility": allows(identity, "facility.manage"), "canManageProjects": allows(identity, "projects.manage"), "canRequestLeave": allows(identity, "leave.request"), "canApproveLeave": allows(identity, "leave.approve")})
			return
		}
		if request.URL.Path == "/api/v1/capabilities" && request.Method == http.MethodGet {
			writer.Header().Set("Content-Type", "application/json")
			_, _ = writer.Write([]byte(`{"personalPresenceSync":1,"canonicalRoomBooking":1,"desktopBookingDetails":1,"legacyBookingReconciliation":1,"bookingSync":1,"bookingBlockSync":1,"roomSync":1,"roomIdentityReconciliation":1,"employeeIdentityReconciliation":1,"employeeSync":1,"leaveSync":1,"leaveRequestReconciliation":1,"leaveIdentityVerified":1,"leaveIdentityBinding":1,"leaveSnapshotIdentityBinding":1,"leaveDecisionCompare":1,"taskSync":1,"taskIdentityReconciliation":1,"taskRecurrenceLink":1,"taskRecurrenceReservation":1,"taskRecurrencePreflight":1,"subtaskSync":1,"subtaskIdentityReconciliation":1,"centralSubtaskCompare":1,"centralSubtaskArchiveCompare":1,"dependencySync":1,"projectSync":1,"projectAssignmentSync":1,"taskProjectCreate":1,"taskProjectUpdate":1,"taskCommentSync":1,"personalTaskCommentSync":1,"personalCommentPull":1,"ticketSync":1,"ticketCommentSync":1,"personalTicketCommentSync":1,"attachmentSync":1,"personalAttachmentSync":1,"personalAttachmentArchive":1,"personalAttachmentPull":1,"personalAttachmentStates":1,"attachmentRestoreSync":1,"organizationSync":1,"facilityAssetSync":1,"maintenanceSync":1,"maintenanceRecurrenceLink":1,"maintenanceRecurrenceReservation":1,"approvalSync":1}`))
			return
		}
		if strings.HasPrefix(request.URL.Path, "/api/v1/attachments/") || strings.HasPrefix(request.URL.Path, "/api/v1/personal-attachment-objects/") {
			personal := strings.HasPrefix(request.URL.Path, "/api/v1/personal-attachment-objects/")
			sha := strings.TrimPrefix(request.URL.Path, "/api/v1/attachments/")
			if personal {
				sha = strings.TrimPrefix(request.URL.Path, "/api/v1/personal-attachment-objects/")
			}
			if len(sha) != 64 {
				http.Error(writer, "invalid attachment id", http.StatusBadRequest)
				return
			}
			if _, err := hex.DecodeString(sha); err != nil {
				http.Error(writer, "invalid attachment id", http.StatusBadRequest)
				return
			}
			if !personal && !allows(identity, "sync.events.write") && !allows(identity, "tasks.manage") && !allows(identity, "tickets.manage") {
				http.Error(writer, "forbidden", http.StatusForbidden)
				return
			}
			if personal {
				kind, subject, viewer := request.URL.Query().Get("subjectType"), request.URL.Query().Get("subjectId"), request.URL.Query().Get("viewerEmployeeId")
				if request.Method != http.MethodPut || !allows(identity, "collaboration.comment") || (kind != "task" && kind != "ticket") || viewer == "" || len(viewer) > 256 {
					http.Error(writer, "forbidden", 403)
					return
				}
				registry.Lock()
				visible := false
				if kind == "task" && (allows(identity, "tasks.read") || allows(identity, "tasks.manage")) {
					v, ok := tasks[subject]
					visible = ok && v.Active && (allows(identity, "tasks.manage") || v.AssigneeID == identity.EmployeeID || v.CreatedBy == identity.EmployeeID)
				}
				if kind == "ticket" && (allows(identity, "tickets.read") || allows(identity, "tickets.manage")) {
					v, ok := tickets[subject]
					visible = ok && v.Active && (allows(identity, "tickets.manage") || v.AssigneeID == identity.EmployeeID || v.CreatedBy == identity.EmployeeID)
				}
				bound := approvalEmployee(strconv.FormatInt(identity.EmployeeID, 10)) == viewer
				registry.Unlock()
				if !visible || !bound {
					http.Error(writer, "subject or employee account not accessible", 403)
					return
				}
			}
			path := filepath.Join(attachmentRoot, sha)
			if request.Method == http.MethodGet {
				if _, err := os.Stat(path); err != nil {
					http.NotFound(writer, request)
					return
				}
				writer.Header().Set("Content-Type", "application/octet-stream")
				writer.Header().Set("Content-Disposition", "attachment; filename=\""+sha+"\"")
				file, err := os.Open(path)
				if err != nil {
					http.Error(writer, "storage failure", 500)
					return
				}
				defer file.Close()
				_, _ = io.Copy(writer, file)
				return
			}
			if request.Method != http.MethodPut {
				writer.Header().Set("Allow", "GET, PUT")
				http.Error(writer, "method not allowed", http.StatusMethodNotAllowed)
				return
			}
			if request.Header.Get("X-Aura-SHA256") != sha {
				http.Error(writer, "hash header mismatch", http.StatusBadRequest)
				return
			}
			if request.ContentLength > 100*1024*1024 {
				http.Error(writer, "attachment too large", http.StatusRequestEntityTooLarge)
				return
			}
			temp, err := os.CreateTemp(attachmentRoot, ".upload-*")
			if err != nil {
				http.Error(writer, "storage failure", 500)
				return
			}
			tempName := temp.Name()
			defer os.Remove(tempName)
			hash := sha256.New()
			written, err := io.Copy(io.MultiWriter(temp, hash), http.MaxBytesReader(writer, request.Body, 100*1024*1024+1))
			closeErr := temp.Close()
			if err != nil || closeErr != nil || written > 100*1024*1024 {
				http.Error(writer, "invalid attachment body", http.StatusBadRequest)
				return
			}
			if hex.EncodeToString(hash.Sum(nil)) != sha {
				http.Error(writer, "attachment hash mismatch", http.StatusUnprocessableEntity)
				return
			}
			if _, err := os.Stat(path); err == nil {
				writer.WriteHeader(http.StatusNoContent)
				return
			}
			if err := os.Rename(tempName, path); err != nil {
				http.Error(writer, "storage failure", 500)
				return
			}
			writer.WriteHeader(http.StatusCreated)
			return
		}
		if request.URL.Path == "/api/v1/leave-requests" && request.Method == http.MethodGet {
			registry.Lock()
			defer registry.Unlock()
			type leaveView struct {
				leaveRequest
				IdentityVerified       bool   `json:"identityVerified"`
				IdentityOriginVerified bool   `json:"identityOriginVerified"`
				EmployeeExternalID     string `json:"employeeExternalId"`
				DeciderExternalID      string `json:"deciderExternalId"`
			}
			result := []leaveView{}
			for _, leave := range leaves {
				verified := leaveAccounts[leave.ID]
				currentEmployee := approvalEmployee(strconv.FormatInt(leave.EmployeeID, 10))
				originalEmployee := leaveOriginalEmployees[leave.ID]
				boundToCurrent := originalEmployee == "" || originalEmployee == currentEmployee
				if allows(identity, "leave.approve") || (verified && leave.EmployeeID == identity.EmployeeID && boundToCurrent) {
					decider := ""
					if leave.DecidedBy > 0 {
						decider = approvalEmployee(strconv.FormatInt(leave.DecidedBy, 10))
						if original := leaveOriginalDeciders[leave.ID]; original != "" && original != decider {
							decider = ""
						}
					}
					if !boundToCurrent {
						currentEmployee = ""
					}
					originVerified := leaveOriginalEmployees[leave.ID] != "" && (leave.Status == "pending" || leaveOriginalDeciders[leave.ID] != "")
					result = append(result, leaveView{leave, verified, originVerified, currentEmployee, decider})
				}
			}
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(result)
			return
		}
		if request.URL.Path == "/api/v1/personal-attachment-states" && request.Method == http.MethodPost {
			defer request.Body.Close()
			var input struct {
				SubjectType      string              `json:"subjectType"`
				SubjectID        string              `json:"subjectId"`
				ViewerEmployeeID string              `json:"viewerEmployeeId"`
				Known            []centralAttachment `json:"known"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 8<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || (input.SubjectType != "task" && input.SubjectType != "ticket") || input.SubjectID == "" || input.ViewerEmployeeID == "" || len(input.Known) > 10000 {
				http.Error(writer, "invalid attachment states request", 400)
				return
			}
			permission := "tasks"
			if input.SubjectType == "ticket" {
				permission = "tickets"
			}
			if !allows(identity, permission+".read") && !allows(identity, permission+".manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			if approvalEmployee(strconv.FormatInt(identity.EmployeeID, 10)) != input.ViewerEmployeeID {
				http.Error(writer, "employee account binding changed", 409)
				return
			}
			visible := false
			if input.SubjectType == "task" {
				v, ok := tasks[input.SubjectID]
				visible = ok && v.Active && (allows(identity, "tasks.manage") || v.AssigneeID == identity.EmployeeID || v.CreatedBy == identity.EmployeeID)
			} else {
				v, ok := tickets[input.SubjectID]
				visible = ok && v.Active && (allows(identity, "tickets.manage") || v.AssigneeID == identity.EmployeeID || v.CreatedBy == identity.EmployeeID)
			}
			if !visible {
				http.Error(writer, "subject not found", 404)
				return
			}
			type state struct {
				ID                 string `json:"id"`
				Active             bool   `json:"active"`
				UploaderExternalID string `json:"uploaderExternalId"`
			}
			result := make([]state, 0, len(input.Known))
			seen := map[string]bool{}
			for _, known := range input.Known {
				current, ok := attachments[known.ID]
				if !ok || known.ID == "" || seen[known.ID] || known.SubjectType != input.SubjectType || known.SubjectID != input.SubjectID {
					http.Error(writer, "known attachment changed", 409)
					return
				}
				seen[known.ID] = true
				expected := current
				expected.Active = known.Active
				if expected != known {
					http.Error(writer, "known attachment changed", 409)
					return
				}
				external := ""
				if n, err := strconv.ParseInt(current.UploadedBy, 10, 64); err == nil && n > 0 && strconv.FormatInt(n, 10) == current.UploadedBy {
					external = approvalEmployee(current.UploadedBy)
				} else if _, ok := approvalAccount(current.UploadedBy); ok {
					external = current.UploadedBy
				}
				result = append(result, state{current.ID, current.Active, external})
			}
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(result)
			return
		}
		if request.URL.Path == "/api/v1/subject-attachments" || request.URL.Path == "/api/v1/personal-subject-attachments" {
			personal := request.URL.Path == "/api/v1/personal-subject-attachments"
			if personal && request.Method != http.MethodGet {
				http.Error(writer, "method not allowed", 405)
				return
			}
			kind, subjectID, attachmentID := request.URL.Query().Get("subjectType"), request.URL.Query().Get("subjectId"), request.URL.Query().Get("id")
			if (kind != "task" && kind != "ticket") || strings.TrimSpace(subjectID) == "" {
				http.Error(writer, "invalid subject", 400)
				return
			}
			permission := "tasks"
			if kind == "ticket" {
				permission = "tickets"
			}
			if !allows(identity, permission+".read") && !allows(identity, permission+".manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if request.Method != http.MethodGet && !allows(identity, permission+".manage") && !allows(identity, "collaboration.comment") {
				http.Error(writer, "forbidden", 403)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			if personal {
				viewer := request.URL.Query().Get("viewerEmployeeId")
				if viewer == "" || len(viewer) > 256 || approvalEmployee(strconv.FormatInt(identity.EmployeeID, 10)) != viewer {
					http.Error(writer, "employee account binding changed", 409)
					return
				}
			}
			visible := false
			if kind == "task" {
				v, ok := tasks[subjectID]
				visible = ok && v.Active && (allows(identity, "tasks.manage") || v.AssigneeID == identity.EmployeeID || v.CreatedBy == identity.EmployeeID)
			} else {
				v, ok := tickets[subjectID]
				visible = ok && v.Active && (allows(identity, "tickets.manage") || v.AssigneeID == identity.EmployeeID || v.CreatedBy == identity.EmployeeID)
			}
			if !visible {
				http.Error(writer, "subject not found", 404)
				return
			}
			if request.Method == http.MethodGet && attachmentID == "" {
				includeArchived := request.URL.Query().Get("includeArchived") == "1"
				if personal && includeArchived {
					http.Error(writer, "forbidden", 403)
					return
				}
				if includeArchived && !allows(identity, permission+".manage") {
					http.Error(writer, "forbidden", 403)
					return
				}
				result := []centralAttachment{}
				for _, v := range attachments {
					if (v.Active || includeArchived) && v.SubjectType == kind && v.SubjectID == subjectID {
						result = append(result, v)
					}
				}
				sort.Slice(result, func(i, j int) bool { return result[i].ID < result[j].ID })
				writer.Header().Set("Content-Type", "application/json")
				if personal {
					type personalAttachment struct {
						centralAttachment
						UploaderExternalID string `json:"uploaderExternalId"`
					}
					views := make([]personalAttachment, 0, len(result))
					for _, v := range result {
						external := ""
						if n, err := strconv.ParseInt(v.UploadedBy, 10, 64); err == nil && n > 0 && strconv.FormatInt(n, 10) == v.UploadedBy {
							external = approvalEmployee(v.UploadedBy)
						} else if _, ok := approvalAccount(v.UploadedBy); ok {
							external = v.UploadedBy
						}
						views = append(views, personalAttachment{v, external})
					}
					_ = json.NewEncoder(writer).Encode(views)
					return
				}
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method == http.MethodPost && attachmentID != "" {
				if !allows(identity, permission+".manage") {
					http.Error(writer, "forbidden", 403)
					return
				}
				defer request.Body.Close()
				var expected centralAttachment
				decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
				decoder.DisallowUnknownFields()
				if decoder.Decode(&expected) != nil {
					http.Error(writer, "invalid attachment", 400)
					return
				}
				var trailing any
				if decoder.Decode(&trailing) != io.EOF {
					http.Error(writer, "invalid attachment", 400)
					return
				}
				v, ok := attachments[attachmentID]
				if !ok || v.Active || v.SubjectType != kind || v.SubjectID != subjectID || v != expected {
					http.Error(writer, "attachment changed on server", 409)
					return
				}
				path := filepath.Join(attachmentRoot, v.SHA256)
				info, err := os.Lstat(path)
				if err != nil || !info.Mode().IsRegular() || info.Size() != v.SizeBytes {
					http.Error(writer, "attachment object missing or invalid", 409)
					return
				}
				f, err := os.Open(path)
				if err != nil {
					http.Error(writer, "attachment object unavailable", 409)
					return
				}
				h := sha256.New()
				n, readErr := io.Copy(h, io.LimitReader(f, 100*1024*1024+1))
				closeErr := f.Close()
				if readErr != nil || closeErr != nil || n != v.SizeBytes || hex.EncodeToString(h.Sum(nil)) != v.SHA256 {
					http.Error(writer, "attachment object integrity mismatch", 409)
					return
				}
				v.Active = true
				payload, _ := json.Marshal(v)
				if persistEvent(journal, syncEvent{Topic: "attachment.restored", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
					http.Error(writer, "persistence failure", 500)
					return
				}
				attachments[v.ID] = v
				attachmentRestored[v.ID] = true
				writer.WriteHeader(http.StatusNoContent)
				return
			}
			if request.Method == http.MethodGet || request.Method == http.MethodDelete {
				v, ok := attachments[attachmentID]
				if !ok || !v.Active || v.SubjectType != kind || v.SubjectID != subjectID {
					http.Error(writer, "attachment not found", 404)
					return
				}
				if request.Method == http.MethodGet {
					f, err := os.Open(filepath.Join(attachmentRoot, v.SHA256))
					if err != nil {
						http.Error(writer, "file not found", 404)
						return
					}
					defer f.Close()
					writer.Header().Set("Content-Type", "application/octet-stream")
					_, _ = io.Copy(writer, f)
					return
				}
				v.Active = false
				payload, _ := json.Marshal(v)
				if persistEvent(journal, syncEvent{Topic: "attachment.archived", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
					http.Error(writer, "persistence failure", 500)
					return
				}
				attachments[v.ID] = v
				writer.WriteHeader(http.StatusNoContent)
				return
			}
			if request.Method != http.MethodPut {
				http.Error(writer, "method not allowed", 405)
				return
			}
			name := request.URL.Query().Get("name")
			if strings.TrimSpace(name) == "" || len(name) > 255 || strings.ContainsAny(name, "/\\\x00") || name == "." || name == ".." {
				http.Error(writer, "invalid file name", 400)
				return
			}
			defer request.Body.Close()
			temp, err := os.CreateTemp(attachmentRoot, ".subject-upload-*")
			if err != nil {
				http.Error(writer, "storage failure", 500)
				return
			}
			defer os.Remove(temp.Name())
			hash := sha256.New()
			size, err := io.Copy(io.MultiWriter(temp, hash), io.LimitReader(request.Body, 100*1024*1024+1))
			closeErr := temp.Close()
			if err != nil || closeErr != nil || size > 100*1024*1024 {
				http.Error(writer, "invalid or oversized file", 400)
				return
			}
			digest := hex.EncodeToString(hash.Sum(nil))
			if err = os.Rename(temp.Name(), filepath.Join(attachmentRoot, digest)); err != nil {
				http.Error(writer, "storage failure", 500)
				return
			}
			now := time.Now().UTC()
			v := centralAttachment{ID: fmt.Sprintf("attachment-%d", now.UnixNano()), SubjectType: kind, SubjectID: subjectID, OriginalName: name, SizeBytes: size, SHA256: digest, UploadedBy: strconv.FormatInt(identity.EmployeeID, 10), Active: true}
			payload, _ := json.Marshal(v)
			if persistEvent(journal, syncEvent{Topic: "attachment.created", Payload: string(payload), At: now.Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			attachments[v.ID] = v
			writer.Header().Set("Content-Type", "application/json")
			writer.WriteHeader(201)
			_ = json.NewEncoder(writer).Encode(v)
			return
		}
		// Portable approvals bind requester and decision maker to the authenticated identity.
		if request.URL.Path == "/api/v1/task-approvals" {
			if !allows(identity, "tasks.read") && !allows(identity, "tasks.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			actor := strconv.FormatInt(identity.EmployeeID, 10)
			registry.Lock()
			defer registry.Unlock()
			if request.Method == http.MethodGet {
				result := []map[string]any{}
				for _, stored := range approvals {
					v, mapped := projectApproval(stored)
					if mapped && v.Active && v.ApproverID == actor {
						title := ""
						if task, ok := tasks[v.TaskID]; ok && task.Active {
							title = task.Title
						}
						result = append(result, map[string]any{"id": v.ID, "taskId": v.TaskID, "taskTitle": title, "requesterId": v.RequestedBy, "approverId": v.ApproverID, "status": v.Status, "decidedBy": v.DecidedBy})
					}
				}
				sort.Slice(result, func(i, j int) bool { return result[i]["id"].(string) < result[j]["id"].(string) })
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method == http.MethodPost {
				if !allows(identity, "tasks.manage") {
					http.Error(writer, "forbidden", 403)
					return
				}
				defer request.Body.Close()
				var input struct {
					TaskID     string `json:"taskId"`
					ApproverID int64  `json:"approverId"`
				}
				if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil || input.ApproverID <= 0 || input.ApproverID == identity.EmployeeID {
					http.Error(writer, "invalid approver", 400)
					return
				}
				identities, identityErr := configuredIdentities()
				if identityErr != nil {
					http.Error(writer, "identity configuration invalid", 500)
					return
				}
				allowedApprover := identities == nil && input.ApproverID == 1
				for _, candidate := range identities {
					if candidate.EmployeeID == input.ApproverID && (allows(candidate, "tasks.read") || allows(candidate, "tasks.manage")) {
						allowedApprover = true
						break
					}
				}
				if !allowedApprover {
					http.Error(writer, "approver account unavailable or lacks task access", 400)
					return
				}
				task, ok := tasks[input.TaskID]
				if !ok || !task.Active {
					http.NotFound(writer, request)
					return
				}
				v := centralTaskApproval{ID: fmt.Sprintf("approval-%d", time.Now().UnixNano()), TaskID: task.ID, RequestedBy: actor, ApproverID: strconv.FormatInt(input.ApproverID, 10), Status: "pending", Active: true}
				if requesterID, approverID := approvalEmployee(v.RequestedBy), approvalEmployee(v.ApproverID); requesterID != "" && approverID != "" {
					v.RequestedBy, v.ApproverID = requesterID, approverID
				}
				payload, _ := json.Marshal(v)
				if persistEvent(journal, syncEvent{Topic: "task.approval.requested", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
					http.Error(writer, "persistence failure", 500)
					return
				}
				approvals[v.ID] = v
				writer.Header().Set("Content-Type", "application/json")
				writer.WriteHeader(201)
				responseApproval, _ := projectApproval(v)
				_ = json.NewEncoder(writer).Encode(responseApproval)
				return
			}
			if request.Method == http.MethodPatch {
				id := request.URL.Query().Get("id")
				v, ok := approvals[id]
				projected, mapped := projectApproval(v)
				if !ok || !mapped || !v.Active || projected.ApproverID != actor {
					http.NotFound(writer, request)
					return
				}
				if projected.RequestedBy == actor || v.Status != "pending" {
					http.Error(writer, "approval not pending", 409)
					return
				}
				task, ok := tasks[v.TaskID]
				if !ok || !task.Active {
					http.Error(writer, "task not active", 409)
					return
				}
				defer request.Body.Close()
				var input struct {
					Status string `json:"status"`
				}
				if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil || (input.Status != "approved" && input.Status != "rejected") {
					http.Error(writer, "invalid decision", 400)
					return
				}
				v.Status = input.Status
				v.DecidedBy = v.ApproverID
				payload, _ := json.Marshal(v)
				if persistEvent(journal, syncEvent{Topic: "task.approval.decided", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
					http.Error(writer, "persistence failure", 500)
					return
				}
				approvals[id] = v
				writer.Header().Set("Content-Type", "application/json")
				responseApproval, _ := projectApproval(v)
				_ = json.NewEncoder(writer).Encode(responseApproval)
				return
			}
			http.Error(writer, "method not allowed", 405)
			return
		}
		if (request.URL.Path == "/api/v1/comments" && (request.Method == http.MethodGet || request.Method == http.MethodPost)) || (request.URL.Path == "/api/v1/personal-comments" && request.Method == http.MethodGet) {
			kind, id := request.URL.Query().Get("subjectType"), request.URL.Query().Get("subjectId")
			if (kind != "task" && kind != "ticket") || strings.TrimSpace(id) == "" {
				http.Error(writer, "invalid subject", 400)
				return
			}
			permission := "tasks"
			if kind == "ticket" {
				permission = "tickets"
			}
			if !allows(identity, permission+".read") && !allows(identity, permission+".manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if request.Method == http.MethodPost && !allows(identity, "collaboration.comment") && !allows(identity, permission+".manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			if request.URL.Path == "/api/v1/personal-comments" {
				expected := request.URL.Query().Get("viewerEmployeeId")
				if expected == "" || len(expected) > 256 || approvalEmployee(strconv.FormatInt(identity.EmployeeID, 10)) != expected {
					http.Error(writer, "employee account binding changed", 409)
					return
				}
			}
			visible := false
			if kind == "task" {
				v, ok := tasks[id]
				visible = ok && v.Active && (allows(identity, "tasks.manage") || v.AssigneeID == identity.EmployeeID || v.CreatedBy == identity.EmployeeID)
			} else {
				v, ok := tickets[id]
				visible = ok && v.Active && (allows(identity, "tickets.manage") || v.AssigneeID == identity.EmployeeID || v.CreatedBy == identity.EmployeeID)
			}
			if !visible {
				http.Error(writer, "subject not found", 404)
				return
			}
			writer.Header().Set("Content-Type", "application/json")
			if request.Method == http.MethodGet {
				result := []centralComment{}
				for _, c := range comments {
					if c.SubjectType == kind && c.SubjectID == id {
						result = append(result, c)
					}
				}
				sort.Slice(result, func(i, j int) bool {
					if result[i].CreatedAt == result[j].CreatedAt {
						return result[i].ID < result[j].ID
					}
					return result[i].CreatedAt < result[j].CreatedAt
				})
				if request.URL.Path == "/api/v1/personal-comments" {
					type personalComment struct {
						centralComment
						AuthorExternalID string `json:"authorExternalId"`
					}
					views := make([]personalComment, 0, len(result))
					for _, c := range result {
						external := ""
						if number, err := strconv.ParseInt(c.AuthorID, 10, 64); err == nil && number > 0 && strconv.FormatInt(number, 10) == c.AuthorID {
							external = approvalEmployee(c.AuthorID)
						} else if employee, ok := employees[c.AuthorID]; ok && employee.Active {
							if _, ok := approvalAccount(c.AuthorID); ok {
								external = c.AuthorID
							}
						}
						views = append(views, personalComment{c, external})
					}
					_ = json.NewEncoder(writer).Encode(views)
					return
				}
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			defer request.Body.Close()
			var input struct {
				Body string `json:"body"`
			}
			if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil || len(strings.TrimSpace(input.Body)) == 0 || len(input.Body) > 16000 {
				http.Error(writer, "invalid comment", 400)
				return
			}
			now := time.Now().UTC()
			c := centralComment{ID: fmt.Sprintf("comment-%d", now.UnixNano()), SubjectType: kind, SubjectID: id, AuthorID: strconv.FormatInt(identity.EmployeeID, 10), Body: input.Body, CreatedAt: now.Format(time.RFC3339Nano)}
			payload, _ := json.Marshal(c)
			if persistEvent(journal, syncEvent{Topic: "comment.created", Payload: string(payload), At: c.CreatedAt}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			comments[c.ID] = c
			writer.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(writer).Encode(c)
			return
		}
		if request.URL.Path == "/api/v1/organizations" {
			if !allows(identity, "employees.read") && !allows(identity, "employees.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if request.Method != http.MethodGet && !allows(identity, "employees.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			if request.Method == http.MethodGet {
				result := []centralOrganization{}
				for _, v := range organizations {
					if v.Active {
						result = append(result, v)
					}
				}
				sort.Slice(result, func(i, j int) bool { return result[i].Name < result[j].Name })
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method != http.MethodPost && request.Method != http.MethodPut && request.Method != http.MethodDelete {
				http.Error(writer, "method not allowed", 405)
				return
			}
			id := request.URL.Query().Get("id")
			var v centralOrganization
			topic := "organization.created"
			if request.Method != http.MethodPost {
				var ok bool
				v, ok = organizations[id]
				if !ok || !v.Active {
					http.NotFound(writer, request)
					return
				}
			}
			if request.Method != http.MethodDelete {
				defer request.Body.Close()
				var input struct {
					Name      string `json:"name"`
					Code      string `json:"code"`
					ManagerID int64  `json:"managerId"`
				}
				if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil || strings.TrimSpace(input.Name) == "" || strings.TrimSpace(input.Code) == "" || input.ManagerID < 0 {
					http.Error(writer, "invalid organization", 400)
					return
				}
				for otherID, other := range organizations {
					if otherID != id && (strings.EqualFold(other.Code, input.Code) || strings.EqualFold(other.Name, input.Name)) {
						http.Error(writer, "organization name or code already exists", 409)
						return
					}
				}
				if request.Method == http.MethodPost {
					id = fmt.Sprintf("organization-%d", time.Now().UnixNano())
					v = centralOrganization{ID: id, Active: true}
				} else {
					topic = "organization.updated"
				}
				v.Name = input.Name
				v.Code = input.Code
				v.ManagerID = input.ManagerID
			} else {
				v.Active = false
				topic = "organization.archived"
			}
			payload, _ := json.Marshal(v)
			if persistEvent(journal, syncEvent{Topic: topic, Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			organizations[id] = v
			writer.Header().Set("Content-Type", "application/json")
			if request.Method == http.MethodPost {
				writer.WriteHeader(201)
			}
			_ = json.NewEncoder(writer).Encode(v)
			return
		}
		if request.URL.Path == "/api/v1/my-presence" {
			if request.Method != http.MethodGet && request.Method != http.MethodPatch {
				http.Error(writer, "method not allowed", 405)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			id := approvalEmployee(strconv.FormatInt(identity.EmployeeID, 10))
			if id == "" {
				http.Error(writer, "employee account link required", 409)
				return
			}
			employee := employees[id]
			if request.Method == http.MethodPatch {
				defer request.Body.Close()
				var input struct {
					Presence   string `json:"presence"`
					Expected   string `json:"expected"`
					EmployeeID string `json:"employeeId,omitempty"`
				}
				decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 4096))
				decoder.DisallowUnknownFields()
				valid := func(v string) bool { return v == "office" || v == "remote" || v == "meeting" || v == "away" }
				if decoder.Decode(&input) != nil || !valid(input.Presence) || !valid(input.Expected) {
					http.Error(writer, "invalid presence", 400)
					return
				}
				if input.EmployeeID != "" && input.EmployeeID != id {
					http.Error(writer, "employee identity changed", 409)
					return
				}
				if employee.Presence != input.Expected {
					http.Error(writer, "presence changed", 409)
					return
				}
				if employee.Presence != input.Presence {
					employee.Presence = input.Presence
					payload, _ := json.Marshal(employee)
					if persistEvent(journal, syncEvent{Topic: "employee.presence.changed", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
						http.Error(writer, "persistence failure", 500)
						return
					}
					employees[id] = employee
				}
			}
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(map[string]string{"presence": employee.Presence, "employeeId": id, "name": employee.Name, "role": employee.Role})
			return
		}
		if request.URL.Path == "/api/v1/employee-accounts" && request.Method == http.MethodGet {
			if !allows(identity, "employees.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			identities, err := configuredIdentities()
			if err != nil {
				http.Error(writer, "identity configuration invalid", 500)
				return
			}
			ids := map[int64]bool{}
			for _, v := range identities {
				ids[v.EmployeeID] = true
			}
			if identities == nil {
				ids[1] = true
			}
			registry.Lock()
			defer registry.Unlock()
			result := []map[string]any{}
			for id := range ids {
				linkedID, name := "", ""
				for _, e := range employees {
					if e.Active && e.AccountEmployeeID == id {
						linkedID = e.ID
						name = e.Name
						break
					}
				}
				result = append(result, map[string]any{"employeeId": id, "linkedEmployeeId": linkedID, "linkedName": name})
			}
			sort.Slice(result, func(i, j int) bool { return result[i]["employeeId"].(int64) < result[j]["employeeId"].(int64) })
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(result)
			return
		}
		if request.URL.Path == "/api/v1/employee-account-link" && request.Method == http.MethodPut {
			if !allows(identity, "employees.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			defer request.Body.Close()
			var input struct {
				AccountEmployeeID int64 `json:"accountEmployeeId"`
			}
			if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil || input.AccountEmployeeID < 0 {
				http.Error(writer, "invalid account", 400)
				return
			}
			identities, err := configuredIdentities()
			if err != nil {
				http.Error(writer, "identity configuration invalid", 500)
				return
			}
			valid := input.AccountEmployeeID == 0 || (identities == nil && input.AccountEmployeeID == 1)
			for _, v := range identities {
				if v.EmployeeID == input.AccountEmployeeID {
					valid = true
				}
			}
			if !valid {
				http.Error(writer, "unknown server account", 400)
				return
			}
			id := request.URL.Query().Get("id")
			registry.Lock()
			defer registry.Unlock()
			employee, ok := employees[id]
			if !ok || !employee.Active {
				http.NotFound(writer, request)
				return
			}
			if input.AccountEmployeeID != 0 {
				for otherID, e := range employees {
					if otherID != id && e.Active && e.AccountEmployeeID == input.AccountEmployeeID {
						http.Error(writer, "server account already linked", 409)
						return
					}
				}
			}
			employee.AccountEmployeeID = input.AccountEmployeeID
			payload, _ := json.Marshal(employee)
			if persistEvent(journal, syncEvent{Topic: "employee.updated", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			employees[id] = employee
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(employee)
			return
		}
		if request.URL.Path == "/api/v1/employees" {
			if !allows(identity, "employees.read") && !allows(identity, "employees.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if request.Method != http.MethodGet && !allows(identity, "employees.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			if request.Method == http.MethodGet {
				result := []centralEmployee{}
				for _, v := range employees {
					if v.Active || (request.URL.Query().Get("includeArchived") == "true" && allows(identity, "employees.manage")) {
						result = append(result, v)
					}
				}
				sort.Slice(result, func(i, j int) bool { return result[i].Name < result[j].Name })
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method != http.MethodPost && request.Method != http.MethodPut && request.Method != http.MethodDelete {
				http.Error(writer, "method not allowed", 405)
				return
			}
			id := request.URL.Query().Get("id")
			var employee centralEmployee
			topic := "employee.created"
			if request.Method != http.MethodPost {
				var ok bool
				employee, ok = employees[id]
				if !ok || !employee.Active {
					http.Error(writer, "employee not found", 404)
					return
				}
			}
			if request.Method == http.MethodPost || request.Method == http.MethodPut {
				defer request.Body.Close()
				var input struct {
					Name       string `json:"name"`
					Role       string `json:"role"`
					Department string `json:"department"`
					Presence   string `json:"presence"`
				}
				if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil || strings.TrimSpace(input.Name) == "" || strings.TrimSpace(input.Role) == "" || strings.TrimSpace(input.Department) == "" || (input.Presence != "office" && input.Presence != "remote" && input.Presence != "meeting" && input.Presence != "away") {
					http.Error(writer, "invalid employee", 400)
					return
				}
				if request.Method == http.MethodPost {
					id = fmt.Sprintf("employee-%d", time.Now().UnixNano())
					employee = centralEmployee{ID: id, Active: true}
				} else {
					topic = "employee.updated"
				}
				employee.Name = input.Name
				employee.Role = input.Role
				employee.Department = input.Department
				employee.Presence = input.Presence
			} else {
				employee.Active = false
				topic = "employee.archived"
			}
			payload, _ := json.Marshal(employee)
			if persistEvent(journal, syncEvent{Topic: topic, Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			employees[id] = employee
			writer.Header().Set("Content-Type", "application/json")
			if request.Method == http.MethodPost {
				writer.WriteHeader(201)
			}
			_ = json.NewEncoder(writer).Encode(employee)
			return
		}
		if request.URL.Path == "/api/v1/task-subtasks" {
			if !allows(identity, "tasks.read") && !allows(identity, "tasks.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if request.Method != http.MethodGet && !allows(identity, "tasks.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			taskID, id := request.URL.Query().Get("taskId"), request.URL.Query().Get("id")
			registry.Lock()
			defer registry.Unlock()
			task, found := tasks[taskID]
			if !found || !task.Active || (!allows(identity, "tasks.manage") && task.AssigneeID != identity.EmployeeID && task.CreatedBy != identity.EmployeeID) {
				http.NotFound(writer, request)
				return
			}
			if request.Method == http.MethodGet {
				result := []centralSubtask{}
				for _, sub := range subtasks {
					if sub.TaskID == taskID && sub.Active {
						result = append(result, sub)
					}
				}
				sort.Slice(result, func(i, j int) bool { return result[i].ID < result[j].ID })
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method != http.MethodPost && request.Method != http.MethodPut && request.Method != http.MethodDelete {
				http.Error(writer, "method not allowed", 405)
				return
			}
			var sub centralSubtask
			if request.Method != http.MethodPost {
				var exists bool
				sub, exists = subtasks[id]
				if !exists || !sub.Active || sub.TaskID != taskID {
					http.NotFound(writer, request)
					return
				}
			}
			topic := "central.task_subtask.saved"
			if request.Method == http.MethodDelete {
				defer request.Body.Close()
				var input struct {
					Expected *centralSubtask `json:"expected"`
				}
				decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
				decoder.DisallowUnknownFields()
				if err := decoder.Decode(&input); err != nil && err != io.EOF {
					http.Error(writer, "invalid subtask", 400)
					return
				}
				if input.Expected != nil && *input.Expected != sub {
					http.Error(writer, "subtask changed on server", 409)
					return
				}
				sub.Active = false
				topic = "central.task_subtask.archived"
			} else {
				defer request.Body.Close()
				var input struct {
					Title      string          `json:"title"`
					Status     string          `json:"status"`
					AssigneeID int64           `json:"assigneeId"`
					DueAt      int64           `json:"dueAt"`
					Expected   *centralSubtask `json:"expected"`
				}
				decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
				decoder.DisallowUnknownFields()
				if decoder.Decode(&input) != nil || strings.TrimSpace(input.Title) == "" || !validTaskStatus(input.Status) || input.AssigneeID < 0 || input.DueAt < 0 {
					http.Error(writer, "invalid subtask", 400)
					return
				}
				if input.Expected != nil && (request.Method != http.MethodPut || *input.Expected != sub) {
					http.Error(writer, "subtask changed on server", 409)
					return
				}
				if task.Status == "done" && input.Status != "done" && input.Status != "cancelled" {
					http.Error(writer, "completed task cannot have open subtasks", 409)
					return
				}
				if request.Method == http.MethodPost {
					sub = centralSubtask{ID: fmt.Sprintf("subtask-%d", time.Now().UnixNano()), TaskID: taskID, Active: true}
					for n := 1; ; n++ {
						if _, exists := subtasks[sub.ID]; !exists {
							break
						}
						sub.ID = fmt.Sprintf("subtask-%d-%d", time.Now().UnixNano(), n)
					}
				}
				sub.Title = input.Title
				sub.Status = input.Status
				sub.AssigneeID = input.AssigneeID
				sub.DueAt = input.DueAt
			}
			data, _ := json.Marshal(sub)
			if persistEvent(journal, syncEvent{Topic: topic, Payload: string(data), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			subtasks[sub.ID] = sub
			writer.Header().Set("Content-Type", "application/json")
			if request.Method == http.MethodPost {
				writer.WriteHeader(201)
			}
			_ = json.NewEncoder(writer).Encode(sub)
			return
		}
		if request.URL.Path == "/api/v1/task-dependencies" {
			if !allows(identity, "tasks.read") && !allows(identity, "tasks.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if request.Method != http.MethodGet && !allows(identity, "tasks.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			taskID, dependencyID := request.URL.Query().Get("taskId"), request.URL.Query().Get("dependencyId")
			registry.Lock()
			defer registry.Unlock()
			task, found := tasks[taskID]
			if !found || !task.Active || (!allows(identity, "tasks.manage") && task.AssigneeID != identity.EmployeeID && task.CreatedBy != identity.EmployeeID) {
				http.NotFound(writer, request)
				return
			}
			if request.Method == http.MethodGet {
				result := []map[string]string{}
				for id := range dependencies[taskID] {
					dep, ok := tasks[id]
					if !ok {
						continue
					}
					title := "Nicht freigegebene Aufgabe"
					if allows(identity, "tasks.manage") || dep.AssigneeID == identity.EmployeeID || dep.CreatedBy == identity.EmployeeID {
						title = dep.Title
					}
					if !dep.Active {
						title += " (archiviert; blockiert nicht)"
					}
					result = append(result, map[string]string{"id": id, "title": title})
				}
				sort.Slice(result, func(i, j int) bool { return result[i]["id"] < result[j]["id"] })
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method != http.MethodPost && request.Method != http.MethodDelete {
				http.Error(writer, "method not allowed", 405)
				return
			}
			dependency, exists := tasks[dependencyID]
			if dependencyID == "" || taskID == dependencyID || (request.Method == http.MethodPost && (!exists || !dependency.Active)) {
				http.Error(writer, "invalid dependency", 400)
				return
			}
			exists = dependencies[taskID] != nil && dependencies[taskID][dependencyID]
			if request.Method == http.MethodPost && exists {
				http.Error(writer, "dependency already exists", 409)
				return
			}
			if request.Method == http.MethodDelete && !exists {
				http.Error(writer, "dependency not found", 404)
				return
			}
			if request.Method == http.MethodPost {
				if task.Status == "done" && dependency.Active && dependency.Status != "done" {
					http.Error(writer, "completed task cannot depend on an open task", 409)
					return
				}
				seen := map[string]bool{}
				stack := []string{dependencyID}
				for len(stack) > 0 {
					current := stack[len(stack)-1]
					stack = stack[:len(stack)-1]
					if current == taskID {
						http.Error(writer, "dependency cycle", 409)
						return
					}
					if seen[current] {
						continue
					}
					seen[current] = true
					for next := range dependencies[current] {
						stack = append(stack, next)
					}
				}
			}
			topic := "central.task_dependency.added"
			if request.Method == http.MethodDelete {
				topic = "central.task_dependency.removed"
			}
			data, _ := json.Marshal(map[string]string{"taskId": taskID, "dependencyId": dependencyID})
			if persistEvent(journal, syncEvent{Topic: topic, Payload: string(data), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			if dependencies[taskID] == nil {
				dependencies[taskID] = make(map[string]bool)
			}
			if request.Method == http.MethodPost {
				dependencies[taskID][dependencyID] = true
			} else {
				delete(dependencies[taskID], dependencyID)
			}
			writer.WriteHeader(http.StatusNoContent)
			return
		}
		if request.URL.Path == "/api/v1/tasks" && request.Method == http.MethodGet {
			if !allows(identity, "tasks.read") && !allows(identity, "tasks.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			result := []centralTask{}
			for _, task := range tasks {
				if (task.Active || (request.URL.Query().Get("includeArchived") == "true" && allows(identity, "tasks.manage"))) && (allows(identity, "tasks.manage") || task.AssigneeID == identity.EmployeeID || task.CreatedBy == identity.EmployeeID) {
					result = append(result, task)
				}
			}
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(result)
			return
		}
		if request.URL.Path == "/api/v1/projects" && request.Method == http.MethodGet {
			if !allows(identity, "projects.read") && !allows(identity, "projects.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			result := []centralProject{}
			for _, project := range projects {
				if allows(identity, "projects.manage") || project.OwnerID == identity.EmployeeID {
					result = append(result, project)
				}
			}
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(result)
			return
		}
		if request.URL.Path == "/api/v1/rooms" && request.Method == http.MethodGet {
			if !allows(identity, "rooms.read") && !allows(identity, "rooms.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			includeArchived := request.URL.Query().Get("includeArchived") == "true" && allows(identity, "rooms.manage")
			registry.Lock()
			defer registry.Unlock()
			if request.URL.Query().Get("availability") != "true" {
				result := []centralRoom{}
				for _, room := range rooms {
					if room.Active || includeArchived {
						result = append(result, room)
					}
				}
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			type roomAvailability struct {
				centralRoom
				Occupied     *bool  `json:"occupied,omitempty"`
				Availability string `json:"availability"`
			}
			result := []roomAvailability{}
			now := time.Now().Unix()
			occupied := map[string]string{}
			unknown := false
			for _, booking := range registry.active {
				if booking.StartsAt <= now && now < booking.EndsAt {
					if booking.RoomKey == "" {
						unknown = true
					} else {
						if booking.Status == "blocked" {
							occupied[booking.RoomKey] = "blocked"
						} else if occupied[booking.RoomKey] != "blocked" {
							occupied[booking.RoomKey] = "booked"
						}
					}
				}
			}
			for _, room := range rooms {
				if room.Active || includeArchived {
					var state *bool
					availability := occupied[room.ID]
					if availability != "" {
						value := true
						state = &value
					} else if !unknown {
						value := false
						state = &value
						availability = "free"
					} else {
						availability = "unknown"
					}
					result = append(result, roomAvailability{room, state, availability})
				}
			}
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(result)
			return
		}
		if request.URL.Path == "/api/v1/maintenance" {
			if !allows(identity, "facility.read") && !allows(identity, "facility.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			if request.Method != http.MethodGet && !allows(identity, "facility.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			assetID, id := request.URL.Query().Get("assetId"), request.URL.Query().Get("id")
			registry.Lock()
			defer registry.Unlock()
			asset, ok := assets[assetID]
			if !ok || !asset.Active {
				http.NotFound(writer, request)
				return
			}
			if request.Method == http.MethodGet {
				result := []centralMaintenanceOrder{}
				for _, v := range maintenanceOrders {
					if v.Active && v.AssetID == assetID {
						result = append(result, v)
					}
				}
				sort.Slice(result, func(i, j int) bool { return result[i].ID < result[j].ID })
				writer.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(writer).Encode(result)
				return
			}
			if request.Method != http.MethodPost && request.Method != http.MethodPut && request.Method != http.MethodDelete && request.Method != http.MethodPatch {
				http.Error(writer, "method not allowed", 405)
				return
			}
			var v centralMaintenanceOrder
			if request.Method != http.MethodPost {
				v, ok = maintenanceOrders[id]
				if !ok || !v.Active || v.AssetID != assetID {
					http.NotFound(writer, request)
					return
				}
			}
			topic := "facility.maintenance.created"
			var follow *centralMaintenanceOrder
			if request.Method == http.MethodPost || request.Method == http.MethodPut {
				defer request.Body.Close()
				var input struct {
					Title          string `json:"title"`
					DueAt          int64  `json:"dueAt"`
					RecurrenceDays int    `json:"recurrenceDays"`
				}
				if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil || strings.TrimSpace(input.Title) == "" || input.DueAt < 0 || input.RecurrenceDays < 0 || input.RecurrenceDays > 3660 {
					http.Error(writer, "invalid maintenance", 400)
					return
				}
				if request.Method == http.MethodPost {
					v = centralMaintenanceOrder{ID: fmt.Sprintf("maintenance-%d", time.Now().UnixNano()), AssetID: assetID, Status: "open", Active: true}
				} else {
					topic = "facility.maintenance.updated"
				}
				v.Title = input.Title
				v.DueAt = input.DueAt
				v.RecurrenceDays = input.RecurrenceDays
			} else if request.Method == http.MethodDelete {
				v.Active = false
				topic = "facility.maintenance.archived"
			} else {
				defer request.Body.Close()
				var input struct {
					Status string `json:"status"`
				}
				if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil || (input.Status != "open" && input.Status != "in_progress" && input.Status != "completed" && input.Status != "cancelled") || v.Status == input.Status {
					http.Error(writer, "invalid status", 400)
					return
				}
				if input.Status == "completed" && maintenanceSuccessors[v.ID] != "" {
					if _, exists := maintenanceOrders[maintenanceSuccessors[v.ID]]; !exists {
						http.Error(writer, "recurrence successor pending", 409)
						return
					}
				}
				v.Status = input.Status
				topic = "facility.maintenance.status.changed"
				if v.Status == "completed" && v.RecurrenceDays > 0 && maintenanceSuccessors[v.ID] == "" {
					next := v
					next.ID = fmt.Sprintf("maintenance-%d", time.Now().UnixNano())
					for n := 1; ; n++ {
						if _, exists := maintenanceOrders[next.ID]; !exists {
							if _, reserved := maintenanceReservationOwners[next.ID]; !reserved {
								break
							}
						}
						next.ID = fmt.Sprintf("maintenance-%d-%d", time.Now().UnixNano(), n)
					}
					next.Status = "open"
					base := v.DueAt
					if base == 0 {
						base = time.Now().Unix()
					}
					delta := int64(v.RecurrenceDays) * 86400
					if base > int64(^uint64(0)>>1)-delta {
						http.Error(writer, "next maintenance due time overflow", 400)
						return
					}
					next.DueAt = base + delta
					follow = &next
				}
			}
			now := time.Now().UTC().Format(time.RFC3339Nano)
			payload, _ := json.Marshal(v)
			events := []syncEvent{{Topic: topic, Payload: string(payload), At: now}}
			if follow != nil {
				payload, _ = json.Marshal(follow)
				events = append(events, syncEvent{Topic: "facility.maintenance.created", Payload: string(payload), At: now})
				link, _ := json.Marshal(map[string]string{"sourceId": v.ID, "successorId": follow.ID})
				events = append(events, syncEvent{Topic: "central.maintenance_recurrence.linked", Payload: string(link), At: now})
			}
			if persistRelatedEvents(journal, events) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			maintenanceOrders[v.ID] = v
			if follow != nil {
				maintenanceOrders[follow.ID] = *follow
				maintenanceSuccessors[v.ID] = follow.ID
			}
			writer.Header().Set("Content-Type", "application/json")
			if request.Method == http.MethodPost {
				writer.WriteHeader(201)
			}
			_ = json.NewEncoder(writer).Encode(v)
			return
		}
		if request.URL.Path == "/api/v1/facility-assets" && request.Method == http.MethodGet {
			if !allows(identity, "facility.read") && !allows(identity, "facility.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			includeArchived := request.URL.Query().Get("includeArchived") == "true"
			registry.Lock()
			result := make([]centralFacilityAsset, 0, len(assets))
			for _, asset := range assets {
				if includeArchived || asset.Active {
					result = append(result, asset)
				}
			}
			registry.Unlock()
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(result)
			return
		}
		if request.URL.Path == "/api/v1/rooms" && request.Method == http.MethodPost {
			if !allows(identity, "rooms.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			defer request.Body.Close()
			var input struct {
				Name      string `json:"name"`
				Location  string `json:"location"`
				Capacity  int    `json:"capacity"`
				Equipment string `json:"equipment"`
			}
			if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil || strings.TrimSpace(input.Name) == "" || strings.TrimSpace(input.Location) == "" || input.Capacity < 1 {
				http.Error(writer, "invalid room", 400)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			room := centralRoom{ID: fmt.Sprintf("room-%d", time.Now().UnixNano()), Name: input.Name, Location: input.Location, Capacity: input.Capacity, Equipment: input.Equipment, Active: true}
			payload, _ := json.Marshal(room)
			if persistEvent(journal, syncEvent{Topic: "room.created", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			rooms[room.ID] = room
			writer.Header().Set("Content-Type", "application/json")
			writer.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(writer).Encode(room)
			return
		}
		if strings.HasPrefix(request.URL.Path, "/api/v1/rooms/") && request.Method == http.MethodPut {
			if !allows(identity, "rooms.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			id := strings.TrimPrefix(request.URL.Path, "/api/v1/rooms/")
			if id == "" || strings.Contains(id, "/") {
				http.NotFound(writer, request)
				return
			}
			defer request.Body.Close()
			var input struct {
				Name      string `json:"name"`
				Location  string `json:"location"`
				Capacity  int    `json:"capacity"`
				Equipment string `json:"equipment"`
			}
			if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil || strings.TrimSpace(input.Name) == "" || strings.TrimSpace(input.Location) == "" || input.Capacity < 1 {
				http.Error(writer, "invalid room", 400)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			room, found := rooms[id]
			if !found || !room.Active {
				http.NotFound(writer, request)
				return
			}
			room.Name = input.Name
			room.Location = input.Location
			room.Capacity = input.Capacity
			room.Equipment = input.Equipment
			payload, _ := json.Marshal(room)
			if persistEvent(journal, syncEvent{Topic: "room.updated", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			rooms[id] = room
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(room)
			return
		}
		if strings.HasPrefix(request.URL.Path, "/api/v1/rooms/") && strings.HasSuffix(request.URL.Path, "/archive") && request.Method == http.MethodPost {
			if !allows(identity, "rooms.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			id := strings.TrimSuffix(strings.TrimPrefix(request.URL.Path, "/api/v1/rooms/"), "/archive")
			if id == "" || strings.Contains(id, "/") {
				http.NotFound(writer, request)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			room, found := rooms[id]
			if !found || !room.Active {
				http.NotFound(writer, request)
				return
			}
			room.Active = false
			payload, _ := json.Marshal(room)
			if persistEvent(journal, syncEvent{Topic: "room.archived", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			rooms[id] = room
			writer.WriteHeader(http.StatusNoContent)
			return
		}
		if request.URL.Path == "/api/v1/project-status" && request.Method == http.MethodPost {
			if !allows(identity, "projects.manage") {
				http.Error(writer, "forbidden", http.StatusForbidden)
				return
			}
			defer request.Body.Close()
			var input struct {
				ID       string `json:"id"`
				Expected string `json:"expected"`
				Status   string `json:"status"`
			}
			decoder := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20))
			decoder.DisallowUnknownFields()
			if decoder.Decode(&input) != nil || strings.TrimSpace(input.ID) == "" || !validProjectStatus(input.Expected) || !validProjectStatus(input.Status) || input.Expected == input.Status {
				http.Error(writer, "invalid project status", http.StatusBadRequest)
				return
			}
			var trailing any
			if decoder.Decode(&trailing) != io.EOF {
				http.Error(writer, "invalid project status", http.StatusBadRequest)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			project, found := projects[input.ID]
			if !found || !project.Active {
				http.Error(writer, "project not found", http.StatusNotFound)
				return
			}
			if project.Status != input.Expected {
				http.Error(writer, "project status changed", http.StatusConflict)
				return
			}
			project.Status = input.Status
			payload, _ := json.Marshal(project)
			if persistEvent(journal, syncEvent{Topic: "project.updated", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", http.StatusInternalServerError)
				return
			}
			projects[input.ID] = project
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(project)
			return
		}
		if request.URL.Path == "/api/v1/projects" && request.Method == http.MethodPost {
			if !allows(identity, "projects.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			defer request.Body.Close()
			var input struct {
				Name        string `json:"name"`
				Department  string `json:"department"`
				Description string `json:"description"`
				Status      string `json:"status"`
				OwnerID     int64  `json:"ownerId"`
			}
			if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil || strings.TrimSpace(input.Name) == "" || strings.TrimSpace(input.Department) == "" || !validProjectStatus(input.Status) {
				http.Error(writer, "invalid project", 400)
				return
			}
			if input.OwnerID <= 0 {
				input.OwnerID = identity.EmployeeID
			}
			registry.Lock()
			defer registry.Unlock()
			project := centralProject{ID: fmt.Sprintf("project-%d", time.Now().UnixNano()), Name: input.Name, Department: input.Department, Description: input.Description, Status: input.Status, OwnerID: input.OwnerID, Active: true}
			payload, _ := json.Marshal(project)
			if persistEvent(journal, syncEvent{Topic: "project.created", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			projects[project.ID] = project
			writer.Header().Set("Content-Type", "application/json")
			writer.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(writer).Encode(project)
			return
		}
		if strings.HasPrefix(request.URL.Path, "/api/v1/projects/") && request.Method == http.MethodPut {
			if !allows(identity, "projects.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			id := strings.TrimPrefix(request.URL.Path, "/api/v1/projects/")
			if id == "" || strings.Contains(id, "/") {
				http.NotFound(writer, request)
				return
			}
			defer request.Body.Close()
			var input struct {
				Name        string `json:"name"`
				Department  string `json:"department"`
				Description string `json:"description"`
				Status      string `json:"status"`
				OwnerID     int64  `json:"ownerId"`
			}
			if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil || strings.TrimSpace(input.Name) == "" || strings.TrimSpace(input.Department) == "" || !validProjectStatus(input.Status) || input.OwnerID <= 0 {
				http.Error(writer, "invalid project", 400)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			project, found := projects[id]
			if !found || !project.Active {
				http.NotFound(writer, request)
				return
			}
			project.Name = input.Name
			project.Department = input.Department
			project.Description = input.Description
			project.Status = input.Status
			project.OwnerID = input.OwnerID
			payload, _ := json.Marshal(project)
			if persistEvent(journal, syncEvent{Topic: "project.updated", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			projects[id] = project
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(project)
			return
		}
		if strings.HasPrefix(request.URL.Path, "/api/v1/projects/") && strings.HasSuffix(request.URL.Path, "/archive") && request.Method == http.MethodPost {
			if !allows(identity, "projects.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			id := strings.TrimSuffix(strings.TrimPrefix(request.URL.Path, "/api/v1/projects/"), "/archive")
			if id == "" || strings.Contains(id, "/") {
				http.NotFound(writer, request)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			project, found := projects[id]
			if !found || !project.Active {
				http.NotFound(writer, request)
				return
			}
			project.Active = false
			payload, _ := json.Marshal(project)
			if persistEvent(journal, syncEvent{Topic: "project.archived", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			projects[id] = project
			writer.WriteHeader(http.StatusNoContent)
			return
		}
		// Edits keep server-owned status and creator, so a stale form cannot change workflow state.
		if (strings.HasPrefix(request.URL.Path, "/api/v1/tasks/") || strings.HasPrefix(request.URL.Path, "/api/v1/tickets/")) && (request.Method == http.MethodPut || (request.Method == http.MethodPost && strings.HasSuffix(request.URL.Path, "/archive"))) {
			taskKind := strings.HasPrefix(request.URL.Path, "/api/v1/tasks/")
			prefix := "/api/v1/tickets/"
			permission := "tickets.manage"
			if taskKind {
				prefix = "/api/v1/tasks/"
				permission = "tasks.manage"
			}
			if !allows(identity, permission) {
				http.Error(writer, "forbidden", 403)
				return
			}
			id := strings.TrimPrefix(request.URL.Path, prefix)
			if request.Method == http.MethodPost {
				id = strings.TrimSuffix(id, "/archive")
			}
			if id == "" || strings.Contains(id, "/") {
				http.NotFound(writer, request)
				return
			}
			var input struct {
				Title           string  `json:"title"`
				Department      string  `json:"department"`
				Category        string  `json:"category"`
				Priority        string  `json:"priority"`
				AssigneeID      int64   `json:"assigneeId"`
				DueAt           *int64  `json:"dueAt"`
				RecurrenceDays  *int    `json:"recurrenceDays"`
				ExpectedProject *string `json:"expectedProjectId"`
				ProjectID       *string `json:"projectId"`
			}
			if request.Method == http.MethodPut {
				defer request.Body.Close()
				if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil || strings.TrimSpace(input.Title) == "" || (taskKind && strings.TrimSpace(input.Department) == "") || (!taskKind && strings.TrimSpace(input.Category) == "") || input.AssigneeID <= 0 || (input.DueAt != nil && *input.DueAt < 0) || (input.RecurrenceDays != nil && (*input.RecurrenceDays < 0 || *input.RecurrenceDays > 3660)) || (input.Priority != "Niedrig" && input.Priority != "Mittel" && input.Priority != "Hoch" && input.Priority != "Kritisch") {
					http.Error(writer, "invalid entry", 400)
					return
				}
			}
			registry.Lock()
			defer registry.Unlock()
			if input.ExpectedProject != nil || input.ProjectID != nil {
				if !taskKind || request.Method != http.MethodPut || input.ExpectedProject == nil || input.ProjectID == nil || len(*input.ExpectedProject) > 256 || len(*input.ProjectID) > 256 || strings.TrimSpace(*input.ExpectedProject) != *input.ExpectedProject || strings.TrimSpace(*input.ProjectID) != *input.ProjectID {
					http.Error(writer, "invalid project assignment", 400)
					return
				}
				if !allows(identity, "projects.manage") {
					http.Error(writer, "forbidden", 403)
					return
				}
				current := ""
				count := 0
				for project, edges := range projectAssignments {
					if edges[id] {
						current = project
						count++
					}
				}
				if count > 1 || current != *input.ExpectedProject {
					http.Error(writer, "project assignment changed on server", 409)
					return
				}
				if *input.ProjectID != "" && *input.ProjectID != current {
					project, found := projects[*input.ProjectID]
					if !found || !project.Active {
						http.Error(writer, "project unavailable", 409)
						return
					}
				}
			}
			var value any
			topic := ""
			if taskKind {
				v, ok := tasks[id]
				if !ok || !v.Active {
					http.NotFound(writer, request)
					return
				}
				if request.Method == http.MethodPut {
					v.Title = input.Title
					v.Department = input.Department
					v.Priority = input.Priority
					v.AssigneeID = input.AssigneeID
					if input.DueAt != nil {
						v.DueAt = *input.DueAt
					}
					if input.RecurrenceDays != nil {
						v.RecurrenceDays = *input.RecurrenceDays
					}
					topic = "task.updated"
				} else {
					v.Active = false
					topic = "task.archived"
				}
				value = v
			} else {
				v, ok := tickets[id]
				if !ok || !v.Active {
					http.NotFound(writer, request)
					return
				}
				if request.Method == http.MethodPut {
					v.Title = input.Title
					v.Category = input.Category
					v.Priority = input.Priority
					v.AssigneeID = input.AssigneeID
					topic = "ticket.updated"
				} else {
					v.Active = false
					topic = "ticket.archived"
				}
				value = v
			}
			payload, _ := json.Marshal(value)
			now := time.Now().UTC().Format(time.RFC3339Nano)
			events := []syncEvent{{Topic: topic, Payload: string(payload), At: now}}
			if input.ExpectedProject != nil && *input.ExpectedProject != *input.ProjectID {
				for _, change := range []struct{ project, topic string }{{*input.ExpectedProject, "central.project_assignment.removed"}, {*input.ProjectID, "central.project_assignment.added"}} {
					if change.project != "" {
						data, _ := json.Marshal(map[string]string{"projectId": change.project, "taskId": id})
						events = append(events, syncEvent{Topic: change.topic, Payload: string(data), At: now})
					}
				}
			}
			if persistRelatedEvents(journal, events) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			if taskKind {
				tasks[id] = value.(centralTask)
				if input.ExpectedProject != nil && *input.ExpectedProject != *input.ProjectID {
					if *input.ExpectedProject != "" {
						delete(projectAssignments[*input.ExpectedProject], id)
					}
					if *input.ProjectID != "" {
						if projectAssignments[*input.ProjectID] == nil {
							projectAssignments[*input.ProjectID] = make(map[string]bool)
						}
						projectAssignments[*input.ProjectID][id] = true
					}
				}
			} else {
				tickets[id] = value.(centralTicket)
			}
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(value)
			return
		}
		if request.URL.Path == "/api/v1/tasks" && request.Method == http.MethodPost {
			if !allows(identity, "tasks.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			defer request.Body.Close()
			var input struct {
				Title          string `json:"title"`
				Department     string `json:"department"`
				Priority       string `json:"priority"`
				AssigneeID     int64  `json:"assigneeId"`
				DueAt          *int64 `json:"dueAt"`
				RecurrenceDays *int   `json:"recurrenceDays"`
				ProjectID      string `json:"projectId"`
			}
			if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil || strings.TrimSpace(input.Title) == "" || strings.TrimSpace(input.Department) == "" || (input.DueAt != nil && *input.DueAt < 0) || (input.RecurrenceDays != nil && (*input.RecurrenceDays < 0 || *input.RecurrenceDays > 3660)) || (input.Priority != "Niedrig" && input.Priority != "Mittel" && input.Priority != "Hoch" && input.Priority != "Kritisch") {
				http.Error(writer, "invalid task", 400)
				return
			}
			if input.AssigneeID <= 0 {
				input.AssigneeID = identity.EmployeeID
			}
			registry.Lock()
			defer registry.Unlock()
			if input.ProjectID != "" {
				if !allows(identity, "projects.manage") {
					http.Error(writer, "forbidden", 403)
					return
				}
				project, found := projects[input.ProjectID]
				if !found || !project.Active || strings.TrimSpace(input.ProjectID) != input.ProjectID || len(input.ProjectID) > 256 {
					http.Error(writer, "project unavailable", 409)
					return
				}
			}
			task := centralTask{ID: fmt.Sprintf("task-%d", time.Now().UnixNano()), Title: input.Title, Department: input.Department, Priority: input.Priority, Status: "open", AssigneeID: input.AssigneeID, CreatedBy: identity.EmployeeID, Active: true}
			for n := 1; ; n++ {
				_, exists := tasks[task.ID]
				_, reserved := taskReservationOwners[task.ID]
				if !exists && !reserved {
					break
				}
				task.ID = fmt.Sprintf("task-%d-%d", time.Now().UnixNano(), n)
			}
			if input.DueAt != nil {
				task.DueAt = *input.DueAt
			}
			if input.RecurrenceDays != nil {
				task.RecurrenceDays = *input.RecurrenceDays
			}
			payload, _ := json.Marshal(task)
			now := time.Now().UTC().Format(time.RFC3339Nano)
			events := []syncEvent{{Topic: "task.created", Payload: string(payload), At: now}}
			if input.ProjectID != "" {
				assignment, _ := json.Marshal(map[string]string{"projectId": input.ProjectID, "taskId": task.ID})
				events = append(events, syncEvent{Topic: "central.project_assignment.added", Payload: string(assignment), At: now})
			}
			if persistRelatedEvents(journal, events) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			tasks[task.ID] = task
			if input.ProjectID != "" {
				if projectAssignments[input.ProjectID] == nil {
					projectAssignments[input.ProjectID] = make(map[string]bool)
				}
				projectAssignments[input.ProjectID][task.ID] = true
			}
			writer.Header().Set("Content-Type", "application/json")
			writer.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(writer).Encode(task)
			return
		}
		if strings.HasPrefix(request.URL.Path, "/api/v1/tasks/") && strings.HasSuffix(request.URL.Path, "/status") && request.Method == http.MethodPost {
			if !allows(identity, "tasks.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			id := strings.TrimSuffix(strings.TrimPrefix(request.URL.Path, "/api/v1/tasks/"), "/status")
			defer request.Body.Close()
			var input struct {
				Status string `json:"status"`
			}
			if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil {
				http.Error(writer, "invalid status", 400)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			task, found := tasks[id]
			if !found || !task.Active {
				http.NotFound(writer, request)
				return
			}
			if !validTaskTransition(task.Status, input.Status) {
				http.Error(writer, "invalid task transition", 409)
				return
			}
			if input.Status == "done" && taskSuccessors[id] != "" {
				if _, exists := tasks[taskSuccessors[id]]; !exists {
					http.Error(writer, "recurrence successor pending", 409)
					return
				}
			}
			if input.Status == "done" {
				for dependencyID := range dependencies[id] {
					dependency, exists := tasks[dependencyID]
					if exists && dependency.Active && dependency.Status != "done" {
						http.Error(writer, "unfinished task dependency", 409)
						return
					}
				}
			}
			if input.Status == "done" {
				for _, sub := range subtasks {
					if sub.TaskID == id && sub.Active && sub.Status != "done" && sub.Status != "cancelled" {
						http.Error(writer, "unfinished subtask", 409)
						return
					}
				}
			}
			task.Status = input.Status
			payload, _ := json.Marshal(task)
			now := time.Now().UTC()
			events := []syncEvent{{Topic: "task.status.changed", Payload: string(payload), At: now.Format(time.RFC3339Nano)}}
			var next centralTask
			var nextSubtasks []centralSubtask
			var nextProjects []string
			if input.Status == "done" && task.RecurrenceDays > 0 && taskSuccessors[id] == "" {
				base := task.DueAt
				if base == 0 {
					base = now.Unix()
				}
				delta := int64(task.RecurrenceDays) * 86400
				if base > int64(^uint64(0)>>1)-delta {
					http.Error(writer, "next due time overflow", 400)
					return
				}
				next = task
				next.ID = fmt.Sprintf("task-%d", time.Now().UnixNano())
				for n := 1; ; n++ {
					_, reserved := taskReservationOwners[next.ID]
					if _, exists := tasks[next.ID]; !exists && !reserved {
						break
					}
					next.ID = fmt.Sprintf("task-%d-%d", now.UnixNano(), n)
				}
				next.Status = "open"
				next.DueAt = base + delta
				data, _ := json.Marshal(next)
				events = append(events, syncEvent{Topic: "task.created", Payload: string(data), At: now.Format(time.RFC3339Nano)})
				link, _ := json.Marshal(map[string]string{"sourceId": id, "successorId": next.ID})
				events = append(events, syncEvent{Topic: "central.task_recurrence.linked", Payload: string(link), At: now.Format(time.RFC3339Nano)})
				for projectID, edges := range projectAssignments {
					if edges[id] {
						payload, _ := json.Marshal(map[string]string{"projectId": projectID, "taskId": next.ID})
						events = append(events, syncEvent{Topic: "central.project_assignment.added", Payload: string(payload), At: now.Format(time.RFC3339Nano)})
						nextProjects = append(nextProjects, projectID)
					}
				}
				for _, sub := range subtasks {
					if sub.TaskID != id || !sub.Active || sub.Status == "cancelled" {
						continue
					}
					copy := sub
					copy.TaskID = next.ID
					copy.ID = fmt.Sprintf("subtask-%d-%d", now.UnixNano(), len(nextSubtasks))
					copy.Status = "open"
					if copy.DueAt > 0 {
						if copy.DueAt > int64(^uint64(0)>>1)-delta {
							http.Error(writer, "subtask due time overflow", 400)
							return
						}
						copy.DueAt += delta
					}
					if _, exists := subtasks[copy.ID]; exists {
						http.Error(writer, "subtask identity collision", 409)
						return
					}
					child, _ := json.Marshal(copy)
					events = append(events, syncEvent{Topic: "central.task_subtask.saved", Payload: string(child), At: now.Format(time.RFC3339Nano)})
					nextSubtasks = append(nextSubtasks, copy)
				}
			}
			if persistRelatedEvents(journal, events) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			tasks[id] = task
			if next.ID != "" {
				tasks[next.ID] = next
				taskSuccessors[id] = next.ID
				for _, projectID := range nextProjects {
					projectAssignments[projectID][next.ID] = true
				}
				for _, sub := range nextSubtasks {
					subtasks[sub.ID] = sub
				}
			}
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(task)
			return
		}
		if request.URL.Path == "/api/v1/tickets" && request.Method == http.MethodGet {
			if !allows(identity, "tickets.read") && !allows(identity, "tickets.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			result := []centralTicket{}
			for _, ticket := range tickets {
				if ticket.Active && (allows(identity, "tickets.manage") || ticket.AssigneeID == identity.EmployeeID || ticket.CreatedBy == identity.EmployeeID) {
					result = append(result, ticket)
				}
			}
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(result)
			return
		}
		if request.URL.Path == "/api/v1/tickets" && request.Method == http.MethodPost {
			if !allows(identity, "tickets.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			defer request.Body.Close()
			var input struct {
				Title      string `json:"title"`
				Category   string `json:"category"`
				Priority   string `json:"priority"`
				AssigneeID int64  `json:"assigneeId"`
			}
			if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil || strings.TrimSpace(input.Title) == "" || strings.TrimSpace(input.Category) == "" || (input.Priority != "Niedrig" && input.Priority != "Mittel" && input.Priority != "Hoch" && input.Priority != "Kritisch") {
				http.Error(writer, "invalid ticket", 400)
				return
			}
			if input.AssigneeID <= 0 {
				input.AssigneeID = identity.EmployeeID
			}
			registry.Lock()
			defer registry.Unlock()
			ticket := centralTicket{ID: fmt.Sprintf("ticket-%d", time.Now().UnixNano()), Title: input.Title, Category: input.Category, Priority: input.Priority, Status: "open", AssigneeID: input.AssigneeID, CreatedBy: identity.EmployeeID, Active: true}
			payload, _ := json.Marshal(ticket)
			if persistEvent(journal, syncEvent{Topic: "ticket.created", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			tickets[ticket.ID] = ticket
			writer.Header().Set("Content-Type", "application/json")
			writer.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(writer).Encode(ticket)
			return
		}
		if strings.HasPrefix(request.URL.Path, "/api/v1/tickets/") && strings.HasSuffix(request.URL.Path, "/status") && request.Method == http.MethodPost {
			if !allows(identity, "tickets.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			id := strings.TrimSuffix(strings.TrimPrefix(request.URL.Path, "/api/v1/tickets/"), "/status")
			defer request.Body.Close()
			var input struct {
				Status string `json:"status"`
			}
			if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil {
				http.Error(writer, "invalid status", 400)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			ticket, found := tickets[id]
			if !found || !ticket.Active {
				http.NotFound(writer, request)
				return
			}
			if !validTicketTransition(ticket.Status, input.Status) {
				http.Error(writer, "invalid ticket transition", 409)
				return
			}
			ticket.Status = input.Status
			payload, _ := json.Marshal(ticket)
			if persistEvent(journal, syncEvent{Topic: "ticket.status.changed", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			tickets[id] = ticket
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(ticket)
			return
		}
		if request.URL.Path == "/api/v1/leave-requests" && request.Method == http.MethodPost {
			if !allows(identity, "leave.request") {
				http.Error(writer, "forbidden", http.StatusForbidden)
				return
			}
			defer request.Body.Close()
			var input struct {
				StartsAt int64  `json:"startsAt"`
				EndsAt   int64  `json:"endsAt"`
				Reason   string `json:"reason"`
			}
			if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil || input.EndsAt <= input.StartsAt || strings.TrimSpace(input.Reason) == "" {
				http.Error(writer, "invalid leave request", http.StatusBadRequest)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			leave := leaveRequest{ID: fmt.Sprintf("leave-%d", time.Now().UnixNano()), EmployeeID: identity.EmployeeID, StartsAt: input.StartsAt, EndsAt: input.EndsAt, Reason: input.Reason, Status: "pending"}
			employeeOrigin := approvalEmployee(strconv.FormatInt(identity.EmployeeID, 10))
			payload := encodeBoundLeaveEvent(leave, employeeOrigin, "")
			if persistEvent(journal, syncEvent{Topic: "leave.requested", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			leaves[leave.ID] = leave
			leaveAccounts[leave.ID] = true
			if employeeOrigin != "" {
				leaveOriginalEmployees[leave.ID] = employeeOrigin
			}
			writer.Header().Set("Content-Type", "application/json")
			writer.WriteHeader(http.StatusCreated)
			_ = json.NewEncoder(writer).Encode(leave)
			return
		}
		if strings.HasPrefix(request.URL.Path, "/api/v1/leave-requests/") && strings.HasSuffix(request.URL.Path, "/decision") && request.Method == http.MethodPost {
			if !allows(identity, "leave.approve") {
				http.Error(writer, "forbidden", 403)
				return
			}
			id := strings.TrimSuffix(strings.TrimPrefix(request.URL.Path, "/api/v1/leave-requests/"), "/decision")
			defer request.Body.Close()
			var input struct {
				Decision                   string        `json:"decision"`
				Expected                   *leaveRequest `json:"expected"`
				ExpectedEmployeeExternalID string        `json:"expectedEmployeeExternalId"`
			}
			if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil || (input.Decision != "approved" && input.Decision != "rejected") {
				http.Error(writer, "invalid decision", 400)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			leave, found := leaves[id]
			if !found {
				http.NotFound(writer, request)
				return
			}
			if !leaveAccounts[id] {
				http.Error(writer, "leave identity requires reconciliation", 409)
				return
			}
			if original := leaveOriginalEmployees[id]; original != "" && approvalEmployee(strconv.FormatInt(leave.EmployeeID, 10)) != original {
				http.Error(writer, "leave employee binding changed", 409)
				return
			}
			if leave.EmployeeID == identity.EmployeeID {
				http.Error(writer, "self approval forbidden", 403)
				return
			}
			if leave.Status != "pending" {
				http.Error(writer, "leave request already decided", 409)
				return
			}
			if input.Expected != nil && (*input.Expected != leave || input.ExpectedEmployeeExternalID != approvalEmployee(strconv.FormatInt(leave.EmployeeID, 10))) {
				http.Error(writer, "leave request changed on server", http.StatusConflict)
				return
			}
			leave.Status = input.Decision
			leave.DecidedBy = identity.EmployeeID
			verified := leaveAccounts[id]
			deciderOrigin := approvalEmployee(strconv.FormatInt(identity.EmployeeID, 10))
			payload := encodeBoundLeaveEvent(leave, leaveOriginalEmployees[id], deciderOrigin)
			if persistEvent(journal, syncEvent{Topic: "leave.decided", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			leaves[id] = leave
			leaveAccounts[id] = verified
			if deciderOrigin != "" {
				leaveOriginalDeciders[id] = deciderOrigin
			}
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(leave)
			return
		}
		if request.URL.Path == "/api/v1/sync/changes" && request.Method == http.MethodGet {
			cursor := 0
			if raw := request.URL.Query().Get("cursor"); raw != "" {
				var err error
				cursor, err = strconv.Atoi(raw)
				if err != nil || cursor < 0 {
					http.Error(writer, "invalid cursor", http.StatusBadRequest)
					return
				}
			}
			var events []syncEvent
			var next int
			var err error
			if centralMirror != nil {
				events, next, err = centralMirror.eventsAfter(cursor)
			} else {
				events, next, err = changesSince(journal, cursor)
			}
			if err != nil {
				http.Error(writer, "persistence failure", http.StatusInternalServerError)
				return
			}
			// Translate historic account IDs for the native desktop without rewriting the journal.
			if request.URL.Query().Get("desktop") == "1" {
				registry.Lock()
				needed := map[string]centralEmployee{}
				unresolved := false
				unresolvedReason := "desktop employee account link required"
				translate := func(value string) (string, bool) {
					if value == "" {
						return "", true
					}
					n, err := strconv.ParseInt(value, 10, 64)
					if err != nil || n <= 0 || strconv.FormatInt(n, 10) != value {
						return value, true
					}
					id := approvalEmployee(value)
					if id == "" {
						return "", false
					}
					needed[id] = employees[id]
					return id, true
				}
				for i := range events {
					if events[i].Topic == "attachment.created" || events[i].Topic == "attachment.archived" || events[i].Topic == "attachment.restored" {
						var a centralAttachment
						if json.Unmarshal([]byte(events[i].Payload), &a) != nil || a.UploadedBy == "" {
							unresolved = true
							break
						}
						var ok bool
						a.UploadedBy, ok = translate(a.UploadedBy)
						if !ok {
							unresolved = true
							break
						}
						payload, _ := json.Marshal(a)
						events[i].Payload = string(payload)
						continue
					}
					if events[i].Topic == "comment.created" {
						var c centralComment
						if json.Unmarshal([]byte(events[i].Payload), &c) != nil || c.AuthorID == "" {
							unresolved = true
							break
						}
						if c.CreatedAt == "" {
							c.CreatedAt = events[i].At
						}
						var ok bool
						c.AuthorID, ok = translate(c.AuthorID)
						if !ok {
							unresolved = true
							break
						}
						payload, _ := json.Marshal(c)
						events[i].Payload = string(payload)
						continue
					}
					if events[i].Topic == "leave.requested" || events[i].Topic == "leave.decided" || events[i].Topic == "leave.identity.reconciled" {
						var marked leaveAccountEvent
						parsed := json.Unmarshal([]byte(events[i].Payload), &marked) == nil
						if parsed && marked.IdentityKind != "server-account-v1" && leaveReconciled[marked.ID] {
							events[i].Topic = "central.leave.legacy.superseded"
							continue
						}
						if events[i].Topic == "leave.identity.reconciled" {
							if marked.Status == "pending" {
								events[i].Topic = "leave.requested"
							} else {
								events[i].Topic = "leave.decided"
							}
						}
						if !parsed || marked.IdentityKind != "server-account-v1" || marked.EmployeeID <= 0 || marked.DecidedBy < 0 {
							unresolvedReason = "legacy leave identity requires explicit reconciliation"
							unresolved = true
							break
						}
						employee := approvalEmployee(strconv.FormatInt(marked.EmployeeID, 10))
						employeeOrigin := marked.EmployeeExternalID
						if employeeOrigin == "" {
							employeeOrigin = leaveOriginalEmployees[marked.ID]
						}
						if employee == "" || (employeeOrigin != "" && employeeOrigin != employee) {
							unresolved = true
							break
						}
						needed[employee] = employees[employee]
						decider := ""
						if marked.DecidedBy > 0 {
							decider = approvalEmployee(strconv.FormatInt(marked.DecidedBy, 10))
							deciderOrigin := marked.DeciderExternalID
							if deciderOrigin == "" {
								deciderOrigin = leaveOriginalDeciders[marked.ID]
							}
							if decider == "" || (deciderOrigin != "" && deciderOrigin != decider) {
								unresolved = true
								break
							}
							needed[decider] = employees[decider]
						}
						payload, _ := json.Marshal(struct {
							leaveRequest
							EmployeeExternalID string `json:"employeeExternalId"`
							DeciderExternalID  string `json:"deciderExternalId"`
						}{marked.leaveRequest, employee, decider})
						events[i].Payload = string(payload)
						continue
					}
					if events[i].Topic == "central.task_subtask.saved" || events[i].Topic == "central.task_subtask.archived" {
						var sub centralSubtask
						if json.Unmarshal([]byte(events[i].Payload), &sub) != nil || sub.AssigneeID < 0 {
							unresolved = true
							break
						}
						assignee := ""
						if sub.AssigneeID > 0 {
							assignee = approvalEmployee(strconv.FormatInt(sub.AssigneeID, 10))
							if assignee == "" {
								unresolved = true
								break
							}
							needed[assignee] = employees[assignee]
						}
						payload, _ := json.Marshal(struct {
							centralSubtask
							AssigneeEmployeeID string `json:"assigneeEmployeeId"`
						}{sub, assignee})
						events[i].Payload = string(payload)
						continue
					}
					if events[i].Topic != "task.approval.requested" && events[i].Topic != "task.approval.decided" {
						continue
					}
					var a centralTaskApproval
					if json.Unmarshal([]byte(events[i].Payload), &a) != nil {
						unresolved = true
						break
					}
					var ok bool
					a.RequestedBy, ok = translate(a.RequestedBy)
					if !ok {
						unresolved = true
						break
					}
					a.ApproverID, ok = translate(a.ApproverID)
					if !ok {
						unresolved = true
						break
					}
					a.DecidedBy, ok = translate(a.DecidedBy)
					if !ok {
						unresolved = true
						break
					}
					payload, _ := json.Marshal(a)
					events[i].Payload = string(payload)
				}
				ids := make([]string, 0, len(needed))
				for id := range needed {
					ids = append(ids, id)
				}
				sort.Strings(ids)
				prerequisites := make([]syncEvent, 0, len(ids))
				for _, id := range ids {
					payload, _ := json.Marshal(needed[id])
					prerequisites = append(prerequisites, syncEvent{Topic: "employee.updated", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)})
				}
				registry.Unlock()
				if unresolved {
					http.Error(writer, unresolvedReason, http.StatusConflict)
					return
				}
				if len(prerequisites) > 0 {
					events = append(prerequisites, events...)
					events = append(events, prerequisites...)
				}
			}
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(struct {
				Events     []syncEvent `json:"events"`
				NextCursor int         `json:"nextCursor"`
			}{events, next})
			return
		}
		if request.URL.Path == "/bookings/reserve" && request.Method == http.MethodPost {
			defer request.Body.Close()
			var reservation bookingReservation
			if err := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&reservation); err != nil || reservation.ReservationID >= 0 || reservation.RoomID <= 0 || reservation.EndsAt <= reservation.StartsAt {
				http.Error(writer, "invalid booking reservation", http.StatusBadRequest)
				return
			}
			if reservation.RoomKey != "" && !allows(identity, "rooms.book") && !allows(identity, "rooms.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			if reservation.RoomKey != "" {
				room, ok := rooms[reservation.RoomKey]
				if !ok || !room.Active {
					http.Error(writer, "room not found", 404)
					return
				}
				if reservation.EmployeeKey != "" || reservation.Attendees != 0 {
					if reservation.Attendees <= 0 || reservation.Attendees > room.Capacity || identity.EmployeeID <= 0 || approvalEmployee(strconv.FormatInt(identity.EmployeeID, 10)) != reservation.EmployeeKey {
						http.Error(writer, "invalid booking identity or capacity", 403)
						return
					}
					reservation.ActorID = identity.EmployeeID
				}
			} else if reservation.EmployeeKey != "" || reservation.Attendees != 0 {
				http.Error(writer, "room identity required", 400)
				return
			}
			registry.purgeExpiredReservations(time.Now().Unix())
			if prior, ok := registry.active[bookingKey(reservation.ClientID, reservation.BookingID)]; ok && prior.RoomKey != "" && reservation.RoomKey == "" {
				http.Error(writer, "canonical room identity required", 409)
				return
			}
			if prior, found := registry.active[bookingKey(reservation.ClientID, reservation.BookingID)]; found && prior.RoomKey != "" && prior.RoomKey != reservation.RoomKey {
				http.Error(writer, "booking room mismatch", 409)
				return
			}
			if prior, found := registry.active[bookingKey(reservation.ClientID, reservation.BookingID)]; found && prior.UserID > 0 {
				if reservation.ActorID != prior.UserID || reservation.EmployeeKey == "" {
					http.Error(writer, "booking owner mismatch", 403)
					return
				}
			} else if found && reservation.ActorID > 0 {
				http.Error(writer, "legacy booking owner unverified", 409)
				return
			}
			key := reservationKey(reservation.ClientID, reservation.ReservationID)
			if existing, found := registry.reservations[key]; found {
				if existing.BookingID == reservation.BookingID && existing.RoomID == reservation.RoomID && existing.RoomKey == reservation.RoomKey && existing.StartsAt == reservation.StartsAt && existing.EndsAt == reservation.EndsAt && existing.EmployeeKey == reservation.EmployeeKey && existing.Attendees == reservation.Attendees && existing.ActorID == reservation.ActorID {
					writer.WriteHeader(http.StatusNoContent)
					return
				}
				http.Error(writer, "reservation id collision", http.StatusConflict)
				return
			}
			if registry.conflicts(reservation.ClientID, reservation.BookingID, reservation.RoomID, reservation.StartsAt, reservation.EndsAt, reservation.ReservationID, reservation.RoomKey) {
				http.Error(writer, "booking conflict", http.StatusConflict)
				return
			}
			reservation.ExpiresAt = time.Now().Add(90 * time.Second).Unix()
			registry.reservations[key] = reservation
			writer.WriteHeader(http.StatusNoContent)
			return
		}
		if request.URL.Path == "/bookings/release" && request.Method == http.MethodPost {
			defer request.Body.Close()
			var release struct {
				ClientID      string `json:"clientId"`
				ReservationID int64  `json:"reservationId"`
			}
			if err := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&release); err != nil || release.ReservationID >= 0 {
				http.Error(writer, "invalid booking release", http.StatusBadRequest)
				return
			}
			registry.Lock()
			registry.purgeExpiredReservations(time.Now().Unix())
			key := reservationKey(release.ClientID, release.ReservationID)
			held, found := registry.reservations[key]
			if found && held.ActorID > 0 && held.ActorID != identity.EmployeeID {
				registry.Unlock()
				http.Error(writer, "booking owner mismatch", 403)
				return
			}
			delete(registry.reservations, key)
			registry.Unlock()
			if !found {
				http.NotFound(writer, request)
				return
			}
			writer.WriteHeader(http.StatusNoContent)
			return
		}
		if request.URL.Path == "/bookings/commit" && request.Method == http.MethodPost {
			defer request.Body.Close()
			var commit bookingCommit
			if err := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&commit); err != nil || commit.ReservationID >= 0 || commit.BookingID <= 0 || (commit.Topic != "booking.created" && commit.Topic != "booking.rescheduled") {
				http.Error(writer, "invalid booking commit", http.StatusBadRequest)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			registry.purgeExpiredReservations(time.Now().Unix())
			reservation, found := registry.reservations[reservationKey(commit.ClientID, commit.ReservationID)]
			if !found {
				http.Error(writer, "booking reservation expired", http.StatusConflict)
				return
			}
			if reservation.BookingID != 0 && reservation.BookingID != commit.BookingID {
				http.Error(writer, "booking identity mismatch", http.StatusConflict)
				return
			}
			if prior, exists := bookingHistory[bookingKey(commit.ClientID, commit.BookingID)]; exists && prior.UserID > 0 && reservation.ActorID == 0 {
				http.Error(writer, "verified booking requires identity", 403)
				return
			}
			if reservation.ActorID > 0 {
				if reservation.ActorID != identity.EmployeeID || approvalEmployee(strconv.FormatInt(identity.EmployeeID, 10)) != reservation.EmployeeKey {
					http.Error(writer, "booking identity changed", 403)
					return
				}
				room, found := rooms[reservation.RoomKey]
				if !found || !room.Active || reservation.Attendees > room.Capacity {
					http.Error(writer, "room or capacity changed", 409)
					return
				}
				_, exists := bookingHistory[bookingKey(commit.ClientID, commit.BookingID)]
				if (commit.Topic == "booking.created" && (exists || reservation.BookingID != 0)) || (commit.Topic == "booking.rescheduled" && (!exists || reservation.BookingID != commit.BookingID)) {
					http.Error(writer, "booking identity changed", 409)
					return
				}
			}
			booking := bookingPayload{ClientID: commit.ClientID, BookingID: commit.BookingID, RoomID: reservation.RoomID, RoomKey: reservation.RoomKey, StartsAt: reservation.StartsAt, EndsAt: reservation.EndsAt}
			if reservation.ActorID > 0 {
				booking.RoomID = 1 // The shared room key, not a device-local number, identifies the room.
				booking.UserID = reservation.ActorID
				booking.Attendees = reservation.Attendees
				booking.Status = "confirmed"
			}
			if registry.conflicts(commit.ClientID, commit.BookingID, booking.RoomID, booking.StartsAt, booking.EndsAt, commit.ReservationID, booking.RoomKey) {
				http.Error(writer, "booking conflict", http.StatusConflict)
				return
			}
			payload, _ := json.Marshal(booking)
			event := syncEvent{Topic: commit.Topic, Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
			if err := persistEvent(journal, event); err != nil {
				http.Error(writer, "persistence failure", http.StatusInternalServerError)
				return
			}
			delete(registry.reservations, reservationKey(commit.ClientID, commit.ReservationID))
			registry.active[bookingKey(booking.ClientID, booking.BookingID)] = booking
			bookingHistory[bookingKey(booking.ClientID, booking.BookingID)] = booking
			writer.WriteHeader(http.StatusAccepted)
			return
		}
		if request.URL.Path == "/bookings/check" && request.Method == http.MethodPost {
			defer request.Body.Close()
			var check bookingCheck
			if err := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&check); err != nil || check.RoomID <= 0 || check.EndsAt <= check.StartsAt {
				http.Error(writer, "invalid booking check", http.StatusBadRequest)
				return
			}
			registry.Lock()
			defer registry.Unlock()
			registry.purgeExpiredReservations(time.Now().Unix())
			if registry.conflicts(check.ClientID, check.BookingID, check.RoomID, check.StartsAt, check.EndsAt, 0, check.RoomKey) {
				http.Error(writer, "booking conflict", http.StatusConflict)
				return
			}
			writer.WriteHeader(http.StatusNoContent)
			return
		}
		desktopAttachment := request.URL.Path == "/api/v1/desktop-attachment-events"
		if (request.URL.Path != "/events" && !desktopAttachment) || request.Method != http.MethodPost {
			http.NotFound(writer, request)
			return
		}
		defer request.Body.Close()
		var event syncEvent
		if err := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&event); err != nil || strings.TrimSpace(event.Topic) == "" {
			http.Error(writer, "invalid event", http.StatusBadRequest)
			return
		}
		if desktopAttachment && (event.Topic != "attachment.archived" || strings.TrimSpace(event.ClientID) == "" || len(event.ClientID) > 128 || strings.TrimSpace(event.OperationID) == "" || len(event.OperationID) > 128) {
			http.Error(writer, "invalid attachment operation", http.StatusBadRequest)
			return
		}
		if desktopAttachment && centralMirror == nil {
			http.Error(writer, "central database required", http.StatusServiceUnavailable)
			return
		}
		if event.Topic == "leave.identity.reconciled" {
			http.Error(writer, "use reconciliation endpoint", 403)
			return
		}
		if event.Topic == "attachment.restored" {
			http.Error(writer, "use attachment sync endpoint", http.StatusForbidden)
			return
		}
		if strings.HasPrefix(event.Topic, "central.project_assignment.") || strings.HasPrefix(event.Topic, "central.task_dependency.") || strings.HasPrefix(event.Topic, "central.task_subtask.") || strings.HasPrefix(event.Topic, "central.task_recurrence.") || strings.HasPrefix(event.Topic, "central.maintenance_recurrence.") || event.Topic == "central.booking.identity.reconciled" || event.Topic == "central.room_sync.abandoned" || event.Topic == "central.employee_sync.abandoned" || event.Topic == "central.attachment_sync.abandoned" {
			http.Error(writer, "use task dependency endpoint", 403)
			return
		}
		if event.At == "" {
			event.At = time.Now().UTC().Format(time.RFC3339Nano)
		}
		isCollaborative := event.Topic == "comment.created" || event.Topic == "attachment.created" || event.Topic == "attachment.archived"
		if !allows(identity, eventPermission(event.Topic)) && !(isCollaborative && (allows(identity, "tasks.manage") || allows(identity, "tickets.manage") || allows(identity, "sync.events.write"))) {
			http.Error(writer, "forbidden", http.StatusForbidden)
			return
		}
		registry.Lock()
		registry.purgeExpiredReservations(time.Now().Unix())
		attachmentReceiptClient, attachmentFingerprint := "", ""
		var booking bookingPayload
		isBooking := event.Topic == "booking.created" || event.Topic == "booking.rescheduled"
		if isBooking {
			if err := json.Unmarshal([]byte(event.Payload), &booking); err != nil || booking.BookingID <= 0 || booking.RoomID <= 0 || booking.EndsAt <= booking.StartsAt {
				registry.Unlock()
				http.Error(writer, "invalid booking payload", http.StatusBadRequest)
				return
			}
			if existing, found := registry.active[bookingKey(booking.ClientID, booking.BookingID)]; found && existing.RoomKey != "" {
				if booking.RoomKey == "" {
					booking.RoomKey = existing.RoomKey
				} else if booking.RoomKey != existing.RoomKey {
					registry.Unlock()
					http.Error(writer, "booking room identity mismatch", 409)
					return
				}
			}
			if prior, exists := bookingHistory[bookingKey(booking.ClientID, booking.BookingID)]; exists && prior.UserID > 0 {
				if _, active := registry.active[bookingKey(booking.ClientID, booking.BookingID)]; !active {
					registry.Unlock()
					http.Error(writer, "verified booking is not active", 409)
					return
				}
			}
			if existing, alreadyCommitted := registry.active[bookingKey(booking.ClientID, booking.BookingID)]; alreadyCommitted {
				sameRoom := (existing.RoomKey != "" && existing.RoomKey == booking.RoomKey) || (existing.RoomKey == "" && booking.RoomKey == "" && existing.RoomID == booking.RoomID)
				if sameRoom && existing.StartsAt == booking.StartsAt && existing.EndsAt == booking.EndsAt {
					registry.Unlock()
					writer.WriteHeader(http.StatusAccepted)
					return
				}
				if existing.UserID > 0 {
					registry.Unlock()
					http.Error(writer, "verified booking requires reservation", 409)
					return
				}
			}
			if registry.conflicts(booking.ClientID, booking.BookingID, booking.RoomID, booking.StartsAt, booking.EndsAt, 0, booking.RoomKey) {
				registry.Unlock()
				http.Error(writer, "booking conflict", http.StatusConflict)
				return
			}
		}
		if event.Topic == "booking.cancelled" {
			var cancelled bookingPayload
			if json.Unmarshal([]byte(event.Payload), &cancelled) != nil || cancelled.BookingID <= 0 {
				registry.Unlock()
				http.Error(writer, "invalid booking cancellation", 400)
				return
			}
			if prior, found := bookingHistory[bookingKey(cancelled.ClientID, cancelled.BookingID)]; found && prior.UserID > 0 && prior.UserID != identity.EmployeeID && !allows(identity, "rooms.manage") {
				registry.Unlock()
				http.Error(writer, "booking owner mismatch", 403)
				return
			}
		}
		if isBooking && booking.RoomKey != "" {
			payload, _ := json.Marshal(booking)
			event.Payload = string(payload)
		}
		var project centralProject
		isProject := event.Topic == "project.created" || event.Topic == "project.updated" || event.Topic == "project.archived"
		if isProject {
			if err := json.Unmarshal([]byte(event.Payload), &project); err != nil || strings.TrimSpace(project.ID) == "" || strings.TrimSpace(project.Name) == "" || strings.TrimSpace(project.Department) == "" || project.OwnerID <= 0 || !validProjectStatus(project.Status) {
				registry.Unlock()
				http.Error(writer, "invalid project payload", http.StatusBadRequest)
				return
			}
			if event.Topic == "project.archived" {
				project.Active = false
			}
		}
		var task centralTask
		isTask := event.Topic == "task.created" || event.Topic == "task.updated" || event.Topic == "task.status.changed" || event.Topic == "task.archived"
		if isTask {
			if err := json.Unmarshal([]byte(event.Payload), &task); err != nil || strings.TrimSpace(task.ID) == "" || strings.TrimSpace(task.Title) == "" || strings.TrimSpace(task.Department) == "" || task.AssigneeID <= 0 || task.CreatedBy <= 0 || !validTaskStatus(task.Status) || (task.Priority != "Niedrig" && task.Priority != "Mittel" && task.Priority != "Hoch" && task.Priority != "Kritisch") {
				registry.Unlock()
				http.Error(writer, "invalid task payload", http.StatusBadRequest)
				return
			}
			if _, reserved := taskReservationOwners[task.ID]; reserved {
				if _, exists := tasks[task.ID]; !exists {
					registry.Unlock()
					http.Error(writer, "recurrence successor requires reserved task sync", 409)
					return
				}
			}
			if task.Status == "done" && event.Topic != "task.archived" {
				for dependencyID := range dependencies[task.ID] {
					dependency, exists := tasks[dependencyID]
					if exists && dependency.Active && dependency.Status != "done" {
						registry.Unlock()
						http.Error(writer, "unfinished task dependency", 409)
						return
					}
				}
			}
			if task.Status == "done" && event.Topic != "task.archived" {
				for _, sub := range subtasks {
					if sub.TaskID == task.ID && sub.Active && sub.Status != "done" && sub.Status != "cancelled" {
						registry.Unlock()
						http.Error(writer, "unfinished subtask", 409)
						return
					}
				}
			}
			// Legacy desktop events omitted schedules. Preserve the last known values.
			var schedule struct {
				DueAt          *int64 `json:"dueAt"`
				RecurrenceDays *int   `json:"recurrenceDays"`
			}
			if json.Unmarshal([]byte(event.Payload), &schedule) != nil {
				registry.Unlock()
				http.Error(writer, "invalid task schedule", 400)
				return
			}
			if existing, found := tasks[task.ID]; found {
				if schedule.DueAt == nil {
					task.DueAt = existing.DueAt
				}
				if schedule.RecurrenceDays == nil {
					task.RecurrenceDays = existing.RecurrenceDays
				}
			}
			if task.DueAt < 0 || task.RecurrenceDays < 0 || task.RecurrenceDays > 3660 {
				registry.Unlock()
				http.Error(writer, "invalid task schedule", 400)
				return
			}
			if event.Topic == "task.archived" {
				task.Active = false
			} else if !task.Active {
				task.Active = true
			}
		}
		if isTask {
			payload, _ := json.Marshal(task)
			event.Payload = string(payload)
		}
		var approval centralTaskApproval
		isApproval := event.Topic == "task.approval.requested" || event.Topic == "task.approval.decided"
		if isApproval {
			if err := json.Unmarshal([]byte(event.Payload), &approval); err != nil || strings.TrimSpace(approval.ID) == "" || strings.TrimSpace(approval.TaskID) == "" || strings.TrimSpace(approval.RequestedBy) == "" || strings.TrimSpace(approval.ApproverID) == "" || approval.RequestedBy == approval.ApproverID || (approval.Status != "pending" && approval.Status != "approved" && approval.Status != "rejected") {
				registry.Unlock()
				http.Error(writer, "invalid task approval payload", http.StatusBadRequest)
				return
			}
			if event.Topic == "task.approval.requested" && (approval.Status != "pending" || approval.DecidedBy != "") {
				registry.Unlock()
				http.Error(writer, "invalid task approval request", http.StatusConflict)
				return
			}
			if event.Topic == "task.approval.decided" && ((approval.Status != "approved" && approval.Status != "rejected") || approval.DecidedBy != approval.ApproverID) {
				registry.Unlock()
				http.Error(writer, "invalid task approval decision", http.StatusConflict)
				return
			}
			if !approval.Active {
				registry.Unlock()
				http.Error(writer, "invalid task approval state", http.StatusConflict)
				return
			}
			existing, found := approvals[approval.ID]
			if found {
				if existing.TaskID != approval.TaskID || existing.RequestedBy != approval.RequestedBy || existing.ApproverID != approval.ApproverID || !existing.Active {
					registry.Unlock()
					http.Error(writer, "task approval identity conflict", http.StatusConflict)
					return
				}
				// A retry can arrive after a decision or after the task is archived.
				// Acknowledge it without appending an older event or reopening it.
				if event.Topic == "task.approval.requested" || (existing.Status == approval.Status && existing.DecidedBy == approval.DecidedBy) {
					registry.Unlock()
					writer.WriteHeader(http.StatusAccepted)
					return
				}
				if existing.Status != "pending" {
					registry.Unlock()
					http.Error(writer, "task approval decision conflict", http.StatusConflict)
					return
				}
			} else if event.Topic == "task.approval.decided" {
				registry.Unlock()
				http.Error(writer, "unknown task approval", http.StatusConflict)
				return
			}
			if task, found := tasks[approval.TaskID]; !found || !task.Active {
				registry.Unlock()
				http.Error(writer, "active task missing", http.StatusConflict)
				return
			}
		}
		var comment centralComment
		isComment := event.Topic == "comment.created"
		if isComment {
			if err := json.Unmarshal([]byte(event.Payload), &comment); err != nil || strings.TrimSpace(comment.ID) == "" || (comment.SubjectType != "task" && comment.SubjectType != "ticket") || strings.TrimSpace(comment.SubjectID) == "" || strings.TrimSpace(comment.AuthorID) == "" || len(strings.TrimSpace(comment.Body)) == 0 || len(comment.Body) > 16000 || comment.ImportedBy != 0 {
				registry.Unlock()
				http.Error(writer, "invalid comment payload", http.StatusBadRequest)
				return
			}
			if comment.CreatedAt != "" {
				if _, err := time.Parse(time.RFC3339Nano, comment.CreatedAt); err != nil {
					registry.Unlock()
					http.Error(writer, "invalid comment timestamp", http.StatusBadRequest)
					return
				}
			}
			if !allows(identity, "collaboration.comment") && !allows(identity, "sync.events.write") && !allows(identity, comment.SubjectType+"s.manage") {
				registry.Unlock()
				http.Error(writer, "forbidden", http.StatusForbidden)
				return
			}
			if existing, exists := comments[comment.ID]; exists {
				registry.Unlock()
				if existing.SubjectType != comment.SubjectType || existing.SubjectID != comment.SubjectID || existing.AuthorID != comment.AuthorID || existing.Body != comment.Body || (comment.CreatedAt != "" && existing.CreatedAt != comment.CreatedAt) {
					http.Error(writer, "comment identity already belongs to different content", http.StatusConflict)
					return
				}
				writer.WriteHeader(http.StatusAccepted)
				return
			}
			if comment.SubjectType == "task" {
				if task, found := tasks[comment.SubjectID]; !found || !task.Active {
					registry.Unlock()
					http.Error(writer, "active comment task missing", http.StatusConflict)
					return
				}
			} else if ticket, found := tickets[comment.SubjectID]; !found || !ticket.Active {
				registry.Unlock()
				http.Error(writer, "active comment ticket missing", http.StatusConflict)
				return
			}
		}
		var attachment centralAttachment
		isAttachment := event.Topic == "attachment.created" || event.Topic == "attachment.archived"
		if isAttachment {
			if err := json.Unmarshal([]byte(event.Payload), &attachment); err != nil || strings.TrimSpace(attachment.ID) == "" || (attachment.SubjectType != "task" && attachment.SubjectType != "ticket") || strings.TrimSpace(attachment.SubjectID) == "" || strings.TrimSpace(attachment.OriginalName) == "" || attachment.SizeBytes < 0 || len(attachment.SHA256) != 64 || strings.TrimSpace(attachment.UploadedBy) == "" {
				registry.Unlock()
				http.Error(writer, "invalid attachment payload", http.StatusBadRequest)
				return
			}
			if !allows(identity, "sync.events.write") && !allows(identity, attachment.SubjectType+"s.manage") {
				registry.Unlock()
				http.Error(writer, "forbidden", http.StatusForbidden)
				return
			}
			if attachment.ImportedBy != 0 {
				registry.Unlock()
				http.Error(writer, "invalid imported-by field", http.StatusBadRequest)
				return
			}
			if _, err := hex.DecodeString(attachment.SHA256); err != nil {
				registry.Unlock()
				http.Error(writer, "invalid attachment hash", http.StatusBadRequest)
				return
			}
			if desktopAttachment {
				attachmentReceiptClient = "desktop-attachment:" + event.ClientID
				canonical, _ := json.Marshal(struct{ Topic, Payload string }{event.Topic, event.Payload})
				sum := sha256.Sum256(canonical)
				attachmentFingerprint = hex.EncodeToString(sum[:])
				switch centralMirror.roomReceipt(identity.EmployeeID, attachmentReceiptClient, event.OperationID, attachmentFingerprint) {
				case 1:
					registry.Unlock()
					writer.WriteHeader(http.StatusAccepted)
					return
				case 2:
					registry.Unlock()
					http.Error(writer, "operation identity reused", http.StatusConflict)
					return
				case -1:
					registry.Unlock()
					http.Error(writer, "receipt storage failure", http.StatusInternalServerError)
					return
				}
				switch centralMirror.anyReceipt(attachmentReceiptClient, event.OperationID) {
				case 1:
					registry.Unlock()
					http.Error(writer, "operation belongs to another account", http.StatusConflict)
					return
				case -1:
					registry.Unlock()
					http.Error(writer, "receipt storage failure", http.StatusInternalServerError)
					return
				}
			}
			if event.Topic == "attachment.created" && !attachment.Active {
				registry.Unlock()
				http.Error(writer, "invalid attachment state", http.StatusConflict)
				return
			}
			if existing, found := attachments[attachment.ID]; found {
				candidate := attachment
				candidate.ImportedBy = existing.ImportedBy
				candidate.Active = existing.Active
				if candidate != existing {
					registry.Unlock()
					http.Error(writer, "attachment metadata changed", http.StatusConflict)
					return
				}
				// An old creation retry must not reactivate an archived attachment.
				if event.Topic == "attachment.created" || !existing.Active {
					if desktopAttachment && centralMirror.recordReceipt(identity.EmployeeID, attachmentReceiptClient, event.OperationID, attachmentFingerprint) != nil {
						registry.Unlock()
						http.Error(writer, "receipt storage failure", http.StatusInternalServerError)
						return
					}
					registry.Unlock()
					writer.WriteHeader(http.StatusAccepted)
					return
				}
				if !desktopAttachment && attachmentRestored[attachment.ID] {
					registry.Unlock()
					http.Error(writer, "legacy attachment archive after restore requires reconciliation", http.StatusConflict)
					return
				}
				if desktopAttachment {
					var base struct {
						Cursor *int64 `json:"baseCursor"`
					}
					if json.Unmarshal([]byte(event.Payload), &base) != nil || base.Cursor == nil || *base.Cursor < 0 {
						registry.Unlock()
						http.Error(writer, "attachment archive requires a known server cursor", http.StatusConflict)
						return
					}
					latest, err := centralMirror.latestCursor()
					if err != nil || *base.Cursor > latest {
						registry.Unlock()
						http.Error(writer, "invalid attachment base cursor", http.StatusConflict)
						return
					}
					changed, err := centralMirror.attachmentChangedAfter(*base.Cursor, attachment.ID)
					if err != nil {
						registry.Unlock()
						http.Error(writer, "persistence failure", http.StatusInternalServerError)
						return
					}
					if changed {
						registry.Unlock()
						http.Error(writer, "attachment changed since desktop cursor", http.StatusConflict)
						return
					}
				}
				attachment = existing
			} else if event.Topic == "attachment.archived" {
				registry.Unlock()
				http.Error(writer, "attachment missing", http.StatusConflict)
				return
			}
			if attachment.SubjectType == "task" {
				if task, found := tasks[attachment.SubjectID]; !found || !task.Active {
					registry.Unlock()
					http.Error(writer, "active attachment task missing", http.StatusConflict)
					return
				}
			} else if ticket, found := tickets[attachment.SubjectID]; !found || !ticket.Active {
				registry.Unlock()
				http.Error(writer, "active attachment ticket missing", http.StatusConflict)
				return
			}
			if event.Topic == "attachment.archived" {
				attachment.Active = false
				payload, _ := json.Marshal(attachment)
				event.Payload = string(payload)
			}
		}
		var ticket centralTicket
		isTicket := event.Topic == "ticket.created" || event.Topic == "ticket.updated" || event.Topic == "ticket.status.changed" || event.Topic == "ticket.archived"
		if isTicket {
			if err := json.Unmarshal([]byte(event.Payload), &ticket); err != nil || strings.TrimSpace(ticket.ID) == "" || strings.TrimSpace(ticket.Title) == "" || strings.TrimSpace(ticket.Category) == "" || ticket.AssigneeID <= 0 || ticket.CreatedBy <= 0 || !validTicketStatus(ticket.Status) || (ticket.Priority != "Niedrig" && ticket.Priority != "Mittel" && ticket.Priority != "Hoch" && ticket.Priority != "Kritisch") {
				registry.Unlock()
				http.Error(writer, "invalid ticket payload", http.StatusBadRequest)
				return
			}
			if event.Topic == "ticket.archived" {
				ticket.Active = false
			} else if !ticket.Active {
				ticket.Active = true
			}
		}
		var room centralRoom
		isRoom := event.Topic == "room.created" || event.Topic == "room.updated" || event.Topic == "room.archived"
		if isRoom {
			if err := json.Unmarshal([]byte(event.Payload), &room); err != nil || strings.TrimSpace(room.ID) == "" || strings.TrimSpace(room.Name) == "" || strings.TrimSpace(room.Location) == "" || room.Capacity < 1 {
				registry.Unlock()
				http.Error(writer, "invalid room payload", http.StatusBadRequest)
				return
			}
			if event.Topic == "room.archived" {
				room.Active = false
			} else if !room.Active {
				room.Active = true
			}
		}
		var employee centralEmployee
		isEmployee := event.Topic == "employee.created" || event.Topic == "employee.updated" || event.Topic == "employee.presence.changed" || event.Topic == "employee.archived"
		if isEmployee {
			if err := json.Unmarshal([]byte(event.Payload), &employee); err != nil || strings.TrimSpace(employee.ID) == "" || strings.TrimSpace(employee.Name) == "" || strings.TrimSpace(employee.Role) == "" || strings.TrimSpace(employee.Department) == "" || (employee.Presence != "office" && employee.Presence != "remote" && employee.Presence != "meeting" && employee.Presence != "away") {
				registry.Unlock()
				http.Error(writer, "invalid employee payload", http.StatusBadRequest)
				return
			}
			if event.Topic == "employee.archived" {
				employee.Active = false
			} else if !employee.Active {
				employee.Active = true
			}
			// Legacy desktop events cannot change account links. Preserve the
			// authoritative link instead of dropping it on employee edits.
			employee.AccountEmployeeID = 0
			if old, found := employees[employee.ID]; found {
				employee.AccountEmployeeID = old.AccountEmployeeID
			}
			if employee.Active && employee.AccountEmployeeID > 0 {
				for id, other := range employees {
					if id != employee.ID && other.Active && other.AccountEmployeeID == employee.AccountEmployeeID {
						registry.Unlock()
						http.Error(writer, "server account already linked", 409)
						return
					}
				}
			}
			payload, _ := json.Marshal(employee)
			event.Payload = string(payload)
		}
		var organization centralOrganization
		isOrganization := event.Topic == "organization.created" || event.Topic == "organization.updated" || event.Topic == "organization.archived"
		if isOrganization {
			if err := json.Unmarshal([]byte(event.Payload), &organization); err != nil || strings.TrimSpace(organization.ID) == "" || strings.TrimSpace(organization.Name) == "" || strings.TrimSpace(organization.Code) == "" || organization.ManagerID < 0 {
				registry.Unlock()
				http.Error(writer, "invalid organization payload", http.StatusBadRequest)
				return
			}
			if event.Topic == "organization.archived" {
				organization.Active = false
			} else if !organization.Active {
				organization.Active = true
			}
		}
		var leave leaveRequest
		isLeave := event.Topic == "leave.requested" || event.Topic == "leave.decided"
		if isLeave {
			if err := json.Unmarshal([]byte(event.Payload), &leave); err != nil || strings.TrimSpace(leave.ID) == "" || leave.EmployeeID <= 0 || leave.StartsAt <= 0 || leave.EndsAt <= leave.StartsAt || strings.TrimSpace(leave.Reason) == "" || (leave.Status != "pending" && leave.Status != "approved" && leave.Status != "rejected") {
				registry.Unlock()
				http.Error(writer, "invalid leave payload", http.StatusBadRequest)
				return
			}
			if leaveAccounts[leave.ID] {
				registry.Unlock()
				http.Error(writer, "verified leave requires identity-aware synchronization", 409)
				return
			}
			// Legacy events carry local numeric IDs; never accept a claimed account marker.
			sanitized, _ := json.Marshal(leave)
			event.Payload = string(sanitized)
			if event.Topic == "leave.requested" && leave.Status != "pending" {
				registry.Unlock()
				http.Error(writer, "invalid leave request state", http.StatusConflict)
				return
			}
			if event.Topic == "leave.decided" && (leave.Status == "pending" || leave.DecidedBy <= 0 || leave.DecidedBy == leave.EmployeeID) {
				registry.Unlock()
				http.Error(writer, "invalid leave decision", http.StatusConflict)
				return
			}
		}
		var asset centralFacilityAsset
		isAsset := event.Topic == "facility.asset.created" || event.Topic == "facility.asset.updated" || event.Topic == "facility.asset.archived"
		if isAsset {
			if err := json.Unmarshal([]byte(event.Payload), &asset); err != nil || strings.TrimSpace(asset.ID) == "" || strings.TrimSpace(asset.Name) == "" || strings.TrimSpace(asset.Category) == "" || strings.TrimSpace(asset.Location) == "" || (asset.Status != "operational" && asset.Status != "maintenance" && asset.Status != "out_of_service") {
				registry.Unlock()
				http.Error(writer, "invalid facility asset payload", http.StatusBadRequest)
				return
			}
			if event.Topic == "facility.asset.archived" {
				asset.Active = false
			} else if !asset.Active {
				asset.Active = true
			}
		}
		var maintenance centralMaintenanceOrder
		isMaintenance := event.Topic == "facility.maintenance.created" || event.Topic == "facility.maintenance.updated" || event.Topic == "facility.maintenance.status.changed" || event.Topic == "facility.maintenance.archived"
		if isMaintenance {
			if err := json.Unmarshal([]byte(event.Payload), &maintenance); err != nil || strings.TrimSpace(maintenance.ID) == "" || strings.TrimSpace(maintenance.AssetID) == "" || strings.TrimSpace(maintenance.Title) == "" || maintenance.DueAt < 0 || maintenance.RecurrenceDays < 0 || maintenance.RecurrenceDays > 3660 || (maintenance.Status != "open" && maintenance.Status != "in_progress" && maintenance.Status != "completed" && maintenance.Status != "cancelled") {
				registry.Unlock()
				http.Error(writer, "invalid maintenance payload", http.StatusBadRequest)
				return
			}
			if _, reserved := maintenanceReservationOwners[maintenance.ID]; reserved {
				if _, exists := maintenanceOrders[maintenance.ID]; !exists {
					registry.Unlock()
					http.Error(writer, "recurrence successor requires reserved maintenance sync", 409)
					return
				}
			}
			if child := maintenanceSuccessors[maintenance.ID]; child != "" {
				if _, exists := maintenanceOrders[child]; !exists {
					if existing, found := maintenanceOrders[maintenance.ID]; found && existing.AssetID != maintenance.AssetID {
						registry.Unlock()
						http.Error(writer, "cannot change reserved maintenance asset", 409)
						return
					}
					if event.Topic != "facility.maintenance.archived" && maintenance.Status == "completed" {
						registry.Unlock()
						http.Error(writer, "recurrence successor pending", 409)
						return
					}
				}
			}
			asset, found := assets[maintenance.AssetID]
			if !found || !asset.Active {
				registry.Unlock()
				http.Error(writer, "active facility asset missing", http.StatusConflict)
				return
			}
			if event.Topic == "facility.maintenance.archived" {
				maintenance.Active = false
			} else if !maintenance.Active {
				maintenance.Active = true
			}
		}
		var persistErr error
		if desktopAttachment {
			persistErr = centralMirror.appendRoomSync(identity.EmployeeID, attachmentReceiptClient, event.OperationID, attachmentFingerprint, event)
			if persistErr == nil {
				if file, err := os.OpenFile(journal, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600); err == nil {
					if data, err := json.Marshal(event); err == nil {
						_, _ = file.Write(append(data, '\n'))
					}
					_ = file.Close()
				}
			}
		} else {
			persistErr = persistEvent(journal, event)
		}
		if persistErr != nil {
			registry.Unlock()
			http.Error(writer, "persistence failure", http.StatusInternalServerError)
			return
		}
		if isBooking {
			registry.active[bookingKey(booking.ClientID, booking.BookingID)] = booking
			bookingHistory[bookingKey(booking.ClientID, booking.BookingID)] = booking
		}
		if event.Topic == "booking.cancelled" {
			var cancelled bookingPayload
			if json.Unmarshal([]byte(event.Payload), &cancelled) == nil {
				key := bookingKey(cancelled.ClientID, cancelled.BookingID)
				if prior, ok := bookingHistory[key]; ok {
					prior.Status = "cancelled"
					bookingHistory[key] = prior
				}
				delete(registry.active, key)
			}
		}
		if isProject {
			projects[project.ID] = project
		}
		if isTask {
			tasks[task.ID] = task
		}
		if isApproval {
			approvals[approval.ID] = approval
		}
		if isAttachment {
			attachments[attachment.ID] = attachment
		}
		if isComment {
			if comment.CreatedAt == "" {
				comment.CreatedAt = event.At
			}
			comments[comment.ID] = comment
		}
		if isTicket {
			tickets[ticket.ID] = ticket
		}
		if isRoom {
			rooms[room.ID] = room
		}
		if isEmployee {
			employees[employee.ID] = employee
		}
		if isOrganization {
			organizations[organization.ID] = organization
		}
		if isLeave {
			leaves[leave.ID] = leave
			leaveAccounts[leave.ID] = false
		}
		if isAsset {
			assets[asset.ID] = asset
		}
		if isMaintenance {
			maintenanceOrders[maintenance.ID] = maintenance
		}
		registry.Unlock()
		writer.Header().Set("Content-Type", "application/json")
		writer.WriteHeader(http.StatusAccepted)
		_, _ = writer.Write([]byte(`{"accepted":true}`))
	})
}

func runServer() error {
	journal, token, address := os.Getenv("AURA_SYNC_JOURNAL"), os.Getenv("AURA_SYNC_TOKEN"), os.Getenv("AURA_SYNC_LISTEN")
	if journal == "" || token == "" {
		return fmt.Errorf("AURA_SYNC_JOURNAL und AURA_SYNC_TOKEN müssen gesetzt sein")
	}
	if _, err := configuredIdentities(); err != nil {
		return err
	}
	if os.Getenv("AURA_SYNC_REQUIRE_IDENTITIES") == "1" && os.Getenv("AURA_SYNC_IDENTITIES") == "" {
		return fmt.Errorf("AURA_SYNC_IDENTITIES ist für den produktiven Betrieb erforderlich")
	}
	if address == "" {
		address = "127.0.0.1:8787"
	}
	database := os.Getenv("AURA_CENTRAL_DB")
	if database == "" {
		database = journal + ".sqlite"
	}
	store, err := openCentralStore(database)
	if err != nil {
		return err
	}
	centralMirror = store
	defer func() { centralMirror = nil; store.close() }()
	certificate, key := os.Getenv("AURA_SYNC_TLS_CERT"), os.Getenv("AURA_SYNC_TLS_KEY")
	if (certificate == "") != (key == "") {
		return fmt.Errorf("AURA_SYNC_TLS_CERT und AURA_SYNC_TLS_KEY müssen gemeinsam gesetzt sein")
	}
	if certificate != "" {
		return http.ListenAndServeTLS(address, certificate, key, syncHandler(journal, token))
	}
	if os.Getenv("AURA_SYNC_REQUIRE_TLS") == "1" {
		return fmt.Errorf("TLS ist erforderlich: AURA_SYNC_TLS_CERT und AURA_SYNC_TLS_KEY setzen")
	}
	return http.ListenAndServe(address, syncHandler(journal, token))
}

// sendDesktopSubtask sends the already frozen operation without a second,
// nontransactional file outbox. The caller retains it until a 202 response.
func sendDesktopSubtask(payload string) int {
	return sendDesktopSyncOperation(payload, "/api/v1/desktop-subtask-sync")
}
func sendDesktopDependency(payload string) int {
	return sendDesktopSyncOperation(payload, "/api/v1/dependency-sync")
}
func sendDesktopProjectAssignment(payload string) int {
	return sendDesktopSyncOperation(payload, "/api/v1/project-assignment-sync")
}

//export aura_sync_desktop_task_recurrence
func aura_sync_desktop_task_recurrence(payload *C.char) C.int {
	if payload == nil {
		return 1
	}
	return C.int(sendDesktopSyncOperation(C.GoString(payload), "/api/v1/task-recurrence-link"))
}

//export aura_sync_desktop_maintenance_recurrence
func aura_sync_desktop_maintenance_recurrence(payload *C.char) C.int {
	if payload == nil {
		return 1
	}
	return C.int(sendDesktopSyncOperation(C.GoString(payload), "/api/v1/maintenance-recurrence-link"))
}
func sendDesktopSyncOperation(payload, path string) int {
	if len(payload) == 0 || len(payload) > 1<<20 || !json.Valid([]byte(payload)) {
		return 1
	}
	endpoint, token := os.Getenv("AURA_SYNC_URL"), os.Getenv("AURA_SYNC_TOKEN")
	if endpoint == "" {
		return 2
	}
	if token == "" {
		return 6
	}
	request, err := http.NewRequest(http.MethodPost, strings.TrimRight(endpoint, "/")+path, strings.NewReader(payload))
	if err != nil {
		return 7
	}
	request.Header.Set("Authorization", "Bearer "+token)
	request.Header.Set("Content-Type", "application/json")
	client, err := centralHTTPClient(5 * time.Second)
	if err != nil {
		return 7
	}
	response, err := client.Do(request)
	if err != nil {
		return 7
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusAccepted {
		return 0
	}
	if response.StatusCode == http.StatusConflict {
		return 9
	}
	return 8
}

func desktopSubtaskContext(employeeID string) (int64, int64, int) {
	if len(employeeID) > 256 {
		return 0, 0, 1
	}
	endpoint, token := os.Getenv("AURA_SYNC_URL"), os.Getenv("AURA_SYNC_TOKEN")
	if endpoint == "" {
		return 0, 0, 2
	}
	if token == "" {
		return 0, 0, 6
	}
	request, err := http.NewRequest(http.MethodGet, strings.TrimRight(endpoint, "/")+"/api/v1/desktop-subtask-context?employee="+url.QueryEscape(employeeID), nil)
	if err != nil {
		return 0, 0, 7
	}
	request.Header.Set("Authorization", "Bearer "+token)
	client, err := centralHTTPClient(5 * time.Second)
	if err != nil {
		return 0, 0, 7
	}
	response, err := client.Do(request)
	if err != nil {
		return 0, 0, 7
	}
	defer response.Body.Close()
	if response.StatusCode == http.StatusConflict {
		return 0, 0, 9
	}
	if response.StatusCode != http.StatusOK {
		return 0, 0, 8
	}
	var result struct {
		EmployeeID int64 `json:"employeeId"`
		AssigneeID int64 `json:"assigneeAccountId"`
		Capability int   `json:"desktopSubtaskSync"`
	}
	if json.NewDecoder(io.LimitReader(response.Body, 65536)).Decode(&result) != nil || result.EmployeeID <= 0 || result.AssigneeID < 0 || result.Capability != 1 || (employeeID != "" && result.AssigneeID == 0) || (employeeID == "" && result.AssigneeID != 0) {
		return 0, 0, 8
	}
	return result.EmployeeID, result.AssigneeID, 0
}

//export aura_desktop_subtask_context
func aura_desktop_subtask_context(employeeID *C.char, actor *C.longlong, assignee *C.longlong) C.int {
	if employeeID == nil || actor == nil || assignee == nil {
		return 1
	}
	a, b, code := desktopSubtaskContext(C.GoString(employeeID))
	if code != 0 {
		return C.int(code)
	}
	*actor = C.longlong(a)
	*assignee = C.longlong(b)
	return 0
}

//export aura_sync_desktop_project_assignment
func aura_sync_desktop_project_assignment(payload *C.char) C.int {
	if payload == nil {
		return 1
	}
	return C.int(sendDesktopProjectAssignment(C.GoString(payload)))
}

//export aura_sync_desktop_dependency
func aura_sync_desktop_dependency(payload *C.char) C.int {
	if payload == nil {
		return 1
	}
	return C.int(sendDesktopDependency(C.GoString(payload)))
}

//export aura_sync_desktop_subtask
func aura_sync_desktop_subtask(payload *C.char) C.int {
	if payload == nil {
		return 1
	}
	return C.int(sendDesktopSubtask(C.GoString(payload)))
}

//export aura_desktop_leave_protocol
func aura_desktop_leave_protocol() C.int { return 1 }

//export aura_publish_event
func aura_publish_event(topic *C.char, payload *C.char) C.int {
	if topic == nil || payload == nil {
		return 1
	}
	event := syncEvent{Topic: C.GoString(topic), Payload: C.GoString(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
	result := publishEvent(event)
	setDesktopSyncStatus(result)
	if result != 0 {
		return C.int(result)
	}
	eventLog.Lock()
	eventLog.latest = "Persistiert: " + event.Topic + " @ " + event.At
	eventLog.Unlock()
	return 0
}

//export aura_cancel_desktop_booking
func aura_cancel_desktop_booking(payload *C.char) C.int {
	if payload == nil {
		return 1
	}
	return C.int(cancelDesktopBooking(C.GoString(payload)))
}

//export aura_upload_attachment
func aura_upload_attachment(path *C.char) C.int {
	if path == nil {
		return 1
	}
	return C.int(uploadAttachmentFile(C.GoString(path)))
}

//export aura_download_attachment
func aura_download_attachment(sha256 *C.char, destination *C.char) C.int {
	if sha256 == nil || destination == nil {
		return 1
	}
	return C.int(downloadAttachmentFile(C.GoString(sha256), C.GoString(destination)))
}

//export aura_preflight_booking
func aura_preflight_booking(bookingID C.longlong, roomID C.longlong, startsAt C.longlong, endsAt C.longlong) C.int {
	if roomID <= 0 || endsAt <= startsAt {
		return 1
	}
	return C.int(preflightBooking(int64(bookingID), int64(roomID), int64(startsAt), int64(endsAt)))
}

//export aura_reserve_booking
func aura_reserve_booking(reservationID C.longlong, bookingID C.longlong, roomID C.longlong, startsAt C.longlong, endsAt C.longlong) C.int {
	return C.int(reserveBooking(int64(reservationID), int64(bookingID), int64(roomID), int64(startsAt), int64(endsAt)))
}

//export aura_reserve_booking_with_room_key
func aura_reserve_booking_with_room_key(reservationID C.longlong, bookingID C.longlong, roomID C.longlong, startsAt C.longlong, endsAt C.longlong, roomKey *C.char) C.int {
	if roomKey == nil {
		return 1
	}
	return C.int(reserveBookingWithRoomKey(int64(reservationID), int64(bookingID), int64(roomID), int64(startsAt), int64(endsAt), C.GoString(roomKey)))
}

//export aura_reserve_booking_with_details
func aura_reserve_booking_with_details(reservationID C.longlong, bookingID C.longlong, roomID C.longlong, startsAt C.longlong, endsAt C.longlong, roomKey *C.char, employeeKey *C.char, attendees C.int) C.int {
	if roomKey == nil || employeeKey == nil || C.GoString(employeeKey) == "" || attendees <= 0 {
		return 1
	}
	return C.int(reserveBookingWithDetails(int64(reservationID), int64(bookingID), int64(roomID), int64(startsAt), int64(endsAt), C.GoString(roomKey), C.GoString(employeeKey), int(attendees)))
}

//export aura_commit_booking
func aura_commit_booking(reservationID C.longlong, bookingID C.longlong, reschedule C.int) C.int {
	return C.int(commitBooking(int64(reservationID), int64(bookingID), reschedule != 0))
}

//export aura_release_booking
func aura_release_booking(reservationID C.longlong) C.int {
	return C.int(releaseBooking(int64(reservationID)))
}

//export aura_service_status
func aura_service_status(buffer *C.char, bufferSize C.int) C.int {
	if buffer == nil || bufferSize <= 1 {
		return 1
	}
	eventLog.RLock()
	value := eventLog.latest
	eventLog.RUnlock()
	bytes := []byte(value)
	max := int(bufferSize) - 1
	if len(bytes) > max {
		bytes = bytes[:max]
	}
	destination := unsafe.Slice((*byte)(unsafe.Pointer(buffer)), int(bufferSize))
	copy(destination, bytes)
	destination[len(bytes)] = 0
	return 0
}

//export aura_flush_outbox
func aura_flush_outbox() C.int { code := flushOutbox(); setDesktopSyncStatus(code); return C.int(code) }

//export aura_desktop_attachment_protocol
func aura_desktop_attachment_protocol() C.int { return 1 }

//export aura_inspect_desktop_attachment_archive
func aura_inspect_desktop_attachment_archive(buffer *C.char, size C.int) C.int {
	if buffer == nil || size <= 1 {
		return 1
	}
	review, code := inspectDesktopAttachmentArchive()
	if code != 0 {
		return C.int(code)
	}
	value, err := json.Marshal(review)
	if err != nil {
		return 3
	}
	if len(value) >= int(size) {
		return 10
	}
	destination := unsafe.Slice((*byte)(unsafe.Pointer(buffer)), int(size))
	copy(destination, value)
	destination[len(value)] = 0
	return 0
}

//export aura_prepare_desktop_attachment_archive_abandon
func aura_prepare_desktop_attachment_archive_abandon(value *C.char) C.int {
	if value == nil {
		return 1
	}
	var review desktopAttachmentReview
	if json.Unmarshal([]byte(C.GoString(value)), &review) != nil {
		return 3
	}
	code := abandonDesktopAttachmentArchive(review, false)
	setDesktopSyncStatus(code)
	return C.int(code)
}

//export aura_abandon_desktop_attachment_archive
func aura_abandon_desktop_attachment_archive(value *C.char) C.int {
	if value == nil {
		return 1
	}
	var review desktopAttachmentReview
	if json.Unmarshal([]byte(C.GoString(value)), &review) != nil {
		return 3
	}
	code := abandonDesktopAttachmentArchive(review, true)
	setDesktopSyncStatus(code)
	return C.int(code)
}

//export aura_publish_desktop_attachment
func aura_publish_desktop_attachment(path, metadata *C.char) C.int {
	if path == nil || metadata == nil {
		return 1
	}
	name, raw := C.GoString(path), C.GoString(metadata)
	var attachment centralAttachment
	if strings.TrimSpace(name) == "" || json.Unmarshal([]byte(raw), &attachment) != nil || attachment.ID == "" || !attachment.Active || len(attachment.SHA256) != 64 || attachment.SizeBytes < 0 || attachment.SizeBytes > 100<<20 {
		return 1
	}
	payload, err := json.Marshal(desktopAttachmentUpload{Path: name, Metadata: raw})
	if err != nil {
		return 3
	}
	code := publishEvent(syncEvent{Topic: "desktop.attachment.upload", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)})
	setDesktopSyncStatus(code)
	return C.int(code)
}

//export aura_desktop_task_recurrence_snapshot
func aura_desktop_task_recurrence_snapshot(buffer *C.char, bufferSize C.int, cursor *C.longlong) C.int {
	if buffer == nil || bufferSize <= 1 || cursor == nil {
		return 1
	}
	links, at, code := desktopTaskRecurrenceSnapshot()
	if code != 0 {
		return C.int(code)
	}
	encoded, err := json.Marshal(links)
	if err != nil {
		return 3
	}
	if len(encoded) >= int(bufferSize) {
		return 10
	}
	destination := unsafe.Slice((*byte)(unsafe.Pointer(buffer)), int(bufferSize))
	copy(destination, encoded)
	destination[len(encoded)] = 0
	*cursor = C.longlong(at)
	return 0
}

//export aura_desktop_maintenance_recurrence_snapshot
func aura_desktop_maintenance_recurrence_snapshot(buffer *C.char, bufferSize C.int, cursor *C.longlong) C.int {
	if buffer == nil || bufferSize <= 1 || cursor == nil {
		return 1
	}
	links, at, code := desktopMaintenanceRecurrenceSnapshot()
	if code != 0 {
		return C.int(code)
	}
	encoded, err := json.Marshal(links)
	if err != nil {
		return 3
	}
	if len(encoded) >= int(bufferSize) {
		return 10
	}
	destination := unsafe.Slice((*byte)(unsafe.Pointer(buffer)), int(bufferSize))
	copy(destination, encoded)
	destination[len(encoded)] = 0
	*cursor = C.longlong(at)
	return 0
}

//export aura_desktop_project_assignment_snapshot
func aura_desktop_project_assignment_snapshot(buffer *C.char, bufferSize C.int, cursor *C.longlong) C.int {
	if buffer == nil || bufferSize <= 1 || cursor == nil {
		return 1
	}
	assignments, at, code := desktopProjectAssignmentSnapshot()
	if code != 0 {
		return C.int(code)
	}
	encoded, err := json.Marshal(assignments)
	if err != nil {
		return 3
	}
	if len(encoded) >= int(bufferSize) {
		return 10
	}
	destination := unsafe.Slice((*byte)(unsafe.Pointer(buffer)), int(bufferSize))
	copy(destination, encoded)
	destination[len(encoded)] = 0
	*cursor = C.longlong(at)
	return 0
}

//export aura_desktop_booking_snapshot
func aura_desktop_booking_snapshot(buffer *C.char, bufferSize C.int, cursor *C.longlong) C.int {
	if buffer == nil || bufferSize <= 1 || cursor == nil {
		return 1
	}
	bookings, at, code := desktopBookingSnapshot()
	if code != 0 {
		return C.int(code)
	}
	encoded, err := json.Marshal(bookings)
	if err != nil {
		return 3
	}
	if len(encoded) >= int(bufferSize) {
		return 10
	}
	destination := unsafe.Slice((*byte)(unsafe.Pointer(buffer)), int(bufferSize))
	copy(destination, encoded)
	destination[len(encoded)] = 0
	*cursor = C.longlong(at)
	return 0
}

//export aura_desktop_attachment_snapshot
func aura_desktop_attachment_snapshot(buffer *C.char, bufferSize C.int, cursor *C.longlong) C.int {
	if buffer == nil || bufferSize <= 1 || cursor == nil {
		return 1
	}
	attachments, at, code := desktopAttachmentSnapshot()
	if code != 0 {
		return C.int(code)
	}
	events := make([]syncEvent, 0, len(attachments))
	for _, attachment := range attachments {
		payload, err := json.Marshal(attachment)
		if err != nil {
			return 3
		}
		topic := "attachment.archived"
		if attachment.Active {
			topic = "attachment.restored"
		}
		events = append(events, syncEvent{Topic: topic, Payload: string(payload)})
	}
	encoded, err := json.Marshal(events)
	if err != nil {
		return 3
	}
	if len(encoded) >= int(bufferSize) {
		return 10
	}
	destination := unsafe.Slice((*byte)(unsafe.Pointer(buffer)), int(bufferSize))
	copy(destination, encoded)
	destination[len(encoded)] = 0
	*cursor = C.longlong(at)
	return 0
}

//export aura_fetch_changes
func aura_fetch_changes(cursor C.longlong, buffer *C.char, bufferSize C.int, nextCursor *C.longlong) C.int {
	if cursor < 0 || buffer == nil || bufferSize <= 1 || nextCursor == nil {
		return 1
	}
	events, next, code := fetchChanges(int(cursor))
	if code != 0 {
		return C.int(code)
	}
	encoded, err := json.Marshal(events)
	if err != nil {
		return 3
	}
	if len(encoded) >= int(bufferSize) {
		return 10
	}
	destination := unsafe.Slice((*byte)(unsafe.Pointer(buffer)), int(bufferSize))
	copy(destination, encoded)
	destination[len(encoded)] = 0
	*nextCursor = C.longlong(next)
	return 0
}

func main() {
	if len(os.Args) > 1 && os.Args[1] == "--server" {
		if err := runServer(); err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(2)
		}
	}
}
