package main

/*
#include <stdlib.h>
*/
import "C"

import (
	"bufio"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
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
	Topic   string `json:"topic"`
	Payload string `json:"payload"`
	At      string `json:"at"`
}

type bookingPayload struct {
	ClientID  string `json:"clientId,omitempty"`
	BookingID int64  `json:"bookingId"`
	RoomID    int64  `json:"roomId"`
	StartsAt  int64  `json:"startsAt"`
	EndsAt    int64  `json:"endsAt"`
}

type bookingCheck struct {
	ClientID  string `json:"clientId,omitempty"`
	BookingID int64  `json:"bookingId"`
	RoomID    int64  `json:"roomId"`
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
type centralTask struct {
	ID         string `json:"id"`
	Title      string `json:"title"`
	Department string `json:"department"`
	Priority   string `json:"priority"`
	Status     string `json:"status"`
	AssigneeID int64  `json:"assigneeId"`
	CreatedBy  int64  `json:"createdBy"`
	Active     bool   `json:"active"`
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
	ID         string `json:"id"`
	Name       string `json:"name"`
	Role       string `json:"role"`
	Department string `json:"department"`
	Presence   string `json:"presence"`
	Active     bool   `json:"active"`
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
	ID          string `json:"id"`
	SubjectType string `json:"subjectType"`
	SubjectID   string `json:"subjectId"`
	AuthorID    string `json:"authorId"`
	Body        string `json:"body"`
}
type centralAttachment struct {
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
	return (from == "open" && (to == "in_progress" || to == "cancelled")) || (from == "in_progress" && (to == "done" || to == "cancelled" || to == "open"))
}
func validTaskStatus(status string) bool {
	return status == "open" || status == "in_progress" || status == "done" || status == "cancelled"
}
func validTicketTransition(from, to string) bool {
	return (from == "open" && (to == "in_progress" || to == "closed")) || (from == "in_progress" && (to == "resolved" || to == "closed" || to == "open")) || (from == "resolved" && (to == "closed" || to == "in_progress"))
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
		return identity, found
	}
	if presented != token {
		return serverIdentity{}, false
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
	StartsAt      int64  `json:"startsAt"`
	EndsAt        int64  `json:"endsAt"`
	ExpiresAt     int64  `json:"expiresAt,omitempty"`
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

func (registry *bookingRegistry) conflicts(clientID string, bookingID, roomID, startsAt, endsAt, reservationID int64) bool {
	for existingID, existing := range registry.active {
		if existingID != bookingKey(clientID, bookingID) && existing.RoomID == roomID && existing.StartsAt < endsAt && existing.EndsAt > startsAt {
			return true
		}
	}
	for existingID, existing := range registry.reservations {
		if existingID != reservationKey(clientID, reservationID) && existing.RoomID == roomID && existing.StartsAt < endsAt && existing.EndsAt > startsAt {
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
	journal := os.Getenv("AURA_SYNC_JOURNAL")
	if journal == "" {
		return 2
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
			return 6
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
	body, err := json.Marshal(event)
	if err != nil {
		return 3
	}
	request, err := http.NewRequest(http.MethodPost, strings.TrimRight(endpoint, "/")+"/events", strings.NewReader(string(body)))
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
	if response.StatusCode != http.StatusAccepted {
		return 8
	}
	return 0
}

// uploadAttachmentFile copies a content-addressed object to Central without
// introducing a second local cache.  The caller already owns the durable
// workspace copy; a failed upload can therefore be retried by the user
// without losing the original evidence file.
func uploadAttachmentFile(path string) int {
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
	hash := sha256.New()
	if _, err = io.Copy(hash, file); err != nil {
		return 1
	}
	digest := hex.EncodeToString(hash.Sum(nil))
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
	request, err := http.NewRequest(http.MethodGet, strings.TrimRight(endpoint, "/")+"/api/v1/sync/changes?cursor="+strconv.Itoa(cursor), nil)
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

// flushOutbox replays events in write order and leaves the failed event plus
// every following event intact.  This preserves causal order across an
// interrupted reconnect.
func flushOutbox() int {
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
	for index, line := range lines {
		if strings.TrimSpace(line) == "" {
			continue
		}
		var event syncEvent
		if json.Unmarshal([]byte(line), &event) != nil {
			remaining = append(remaining, lines[index:]...)
			break
		}
		if code := sendCentralEvent(endpoint, token, event); code != 0 {
			remaining = append(remaining, lines[index:]...)
			break
		}
	}
	value := ""
	if len(remaining) > 0 {
		value = strings.Join(remaining, "\n") + "\n"
	}
	if err := os.WriteFile(journal, []byte(value), 0600); err != nil {
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
	leaves := make(map[string]leaveRequest)
	tasks := make(map[string]centralTask)
	tickets := make(map[string]centralTicket)
	rooms := make(map[string]centralRoom)
	projects := make(map[string]centralProject)
	employees := make(map[string]centralEmployee)
	organizations := make(map[string]centralOrganization)
	assets := make(map[string]centralFacilityAsset)
	maintenanceOrders := make(map[string]centralMaintenanceOrder)
	approvals := make(map[string]centralTaskApproval)
	comments := make(map[string]centralComment)
	if previous, err := authoritativeEvents(journal); err == nil {
		for _, prior := range previous {
			var booking bookingPayload
			if (prior.Topic == "booking.created" || prior.Topic == "booking.rescheduled") && json.Unmarshal([]byte(prior.Payload), &booking) == nil && booking.BookingID > 0 && booking.EndsAt > booking.StartsAt {
				registry.active[bookingKey(booking.ClientID, booking.BookingID)] = booking
			}
			if prior.Topic == "booking.cancelled" && json.Unmarshal([]byte(prior.Payload), &booking) == nil {
				delete(registry.active, bookingKey(booking.ClientID, booking.BookingID))
			}
			var leave leaveRequest
			if (prior.Topic == "leave.requested" || prior.Topic == "leave.decided") && json.Unmarshal([]byte(prior.Payload), &leave) == nil && leave.ID != "" {
				leaves[leave.ID] = leave
			}
			var task centralTask
			if (prior.Topic == "task.created" || prior.Topic == "task.updated" || prior.Topic == "task.status.changed" || prior.Topic == "task.archived") && json.Unmarshal([]byte(prior.Payload), &task) == nil && task.ID != "" {
				if prior.Topic != "task.archived" && !task.Active {
					task.Active = true // Compatibility with event records written before the active flag existed.
				}
				tasks[task.ID] = task
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
			}
			var approval centralTaskApproval
			if (prior.Topic == "task.approval.requested" || prior.Topic == "task.approval.decided") && json.Unmarshal([]byte(prior.Payload), &approval) == nil && approval.ID != "" {
				approvals[approval.ID] = approval
			}
			var comment centralComment
			if prior.Topic == "comment.created" && json.Unmarshal([]byte(prior.Payload), &comment) == nil && comment.ID != "" {
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
		if strings.HasPrefix(request.URL.Path, "/api/v1/attachments/") {
			sha := strings.TrimPrefix(request.URL.Path, "/api/v1/attachments/")
			if len(sha) != 64 {
				http.Error(writer, "invalid attachment id", http.StatusBadRequest)
				return
			}
			if _, err := hex.DecodeString(sha); err != nil {
				http.Error(writer, "invalid attachment id", http.StatusBadRequest)
				return
			}
			if !allows(identity, "sync.events.write") && !allows(identity, "tasks.manage") && !allows(identity, "tickets.manage") {
				http.Error(writer, "forbidden", http.StatusForbidden)
				return
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
			result := []leaveRequest{}
			for _, leave := range leaves {
				if allows(identity, "leave.approve") || leave.EmployeeID == identity.EmployeeID {
					result = append(result, leave)
				}
			}
			writer.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(writer).Encode(result)
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
				if task.Active && (allows(identity, "tasks.manage") || task.AssigneeID == identity.EmployeeID || task.CreatedBy == identity.EmployeeID) {
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
		if request.URL.Path == "/api/v1/tasks" && request.Method == http.MethodPost {
			if !allows(identity, "tasks.manage") {
				http.Error(writer, "forbidden", 403)
				return
			}
			defer request.Body.Close()
			var input struct {
				Title      string `json:"title"`
				Department string `json:"department"`
				Priority   string `json:"priority"`
				AssigneeID int64  `json:"assigneeId"`
			}
			if json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&input) != nil || strings.TrimSpace(input.Title) == "" || strings.TrimSpace(input.Department) == "" || (input.Priority != "Niedrig" && input.Priority != "Mittel" && input.Priority != "Hoch" && input.Priority != "Kritisch") {
				http.Error(writer, "invalid task", 400)
				return
			}
			if input.AssigneeID <= 0 {
				input.AssigneeID = identity.EmployeeID
			}
			registry.Lock()
			defer registry.Unlock()
			task := centralTask{ID: fmt.Sprintf("task-%d", time.Now().UnixNano()), Title: input.Title, Department: input.Department, Priority: input.Priority, Status: "open", AssigneeID: input.AssigneeID, CreatedBy: identity.EmployeeID, Active: true}
			payload, _ := json.Marshal(task)
			if persistEvent(journal, syncEvent{Topic: "task.created", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			tasks[task.ID] = task
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
			if !found {
				http.NotFound(writer, request)
				return
			}
			if !validTaskTransition(task.Status, input.Status) {
				http.Error(writer, "invalid task transition", 409)
				return
			}
			task.Status = input.Status
			payload, _ := json.Marshal(task)
			if persistEvent(journal, syncEvent{Topic: "task.status.changed", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			tasks[id] = task
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
			if !found {
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
			payload, _ := json.Marshal(leave)
			if persistEvent(journal, syncEvent{Topic: "leave.requested", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			leaves[leave.ID] = leave
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
				Decision string `json:"decision"`
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
			if leave.EmployeeID == identity.EmployeeID {
				http.Error(writer, "self approval forbidden", 403)
				return
			}
			if leave.Status != "pending" {
				http.Error(writer, "leave request already decided", 409)
				return
			}
			leave.Status = input.Decision
			leave.DecidedBy = identity.EmployeeID
			payload, _ := json.Marshal(leave)
			if persistEvent(journal, syncEvent{Topic: "leave.decided", Payload: string(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}) != nil {
				http.Error(writer, "persistence failure", 500)
				return
			}
			leaves[id] = leave
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
			registry.Lock()
			defer registry.Unlock()
			registry.purgeExpiredReservations(time.Now().Unix())
			key := reservationKey(reservation.ClientID, reservation.ReservationID)
			if existing, found := registry.reservations[key]; found {
				if existing.BookingID == reservation.BookingID && existing.RoomID == reservation.RoomID && existing.StartsAt == reservation.StartsAt && existing.EndsAt == reservation.EndsAt {
					writer.WriteHeader(http.StatusNoContent)
					return
				}
				http.Error(writer, "reservation id collision", http.StatusConflict)
				return
			}
			if registry.conflicts(reservation.ClientID, reservation.BookingID, reservation.RoomID, reservation.StartsAt, reservation.EndsAt, reservation.ReservationID) {
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
			_, found := registry.reservations[key]
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
			booking := bookingPayload{ClientID: commit.ClientID, BookingID: commit.BookingID, RoomID: reservation.RoomID, StartsAt: reservation.StartsAt, EndsAt: reservation.EndsAt}
			if registry.conflicts(commit.ClientID, commit.BookingID, booking.RoomID, booking.StartsAt, booking.EndsAt, commit.ReservationID) {
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
			if registry.conflicts(check.ClientID, check.BookingID, check.RoomID, check.StartsAt, check.EndsAt, 0) {
				http.Error(writer, "booking conflict", http.StatusConflict)
				return
			}
			writer.WriteHeader(http.StatusNoContent)
			return
		}
		if request.URL.Path != "/events" || request.Method != http.MethodPost {
			http.NotFound(writer, request)
			return
		}
		defer request.Body.Close()
		var event syncEvent
		if err := json.NewDecoder(http.MaxBytesReader(writer, request.Body, 1<<20)).Decode(&event); err != nil || strings.TrimSpace(event.Topic) == "" {
			http.Error(writer, "invalid event", http.StatusBadRequest)
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
		var booking bookingPayload
		isBooking := event.Topic == "booking.created" || event.Topic == "booking.rescheduled"
		if isBooking {
			if err := json.Unmarshal([]byte(event.Payload), &booking); err != nil || booking.BookingID <= 0 || booking.RoomID <= 0 || booking.EndsAt <= booking.StartsAt {
				registry.Unlock()
				http.Error(writer, "invalid booking payload", http.StatusBadRequest)
				return
			}
			if existing, alreadyCommitted := registry.active[bookingKey(booking.ClientID, booking.BookingID)]; alreadyCommitted && existing.RoomID == booking.RoomID && existing.StartsAt == booking.StartsAt && existing.EndsAt == booking.EndsAt {
				registry.Unlock()
				writer.WriteHeader(http.StatusAccepted)
				return
			}
			if registry.conflicts(booking.ClientID, booking.BookingID, booking.RoomID, booking.StartsAt, booking.EndsAt, 0) {
				registry.Unlock()
				http.Error(writer, "booking conflict", http.StatusConflict)
				return
			}
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
			if event.Topic == "task.archived" {
				task.Active = false
			} else if !task.Active {
				task.Active = true
			}
		}
		var approval centralTaskApproval
		isApproval := event.Topic == "task.approval.requested" || event.Topic == "task.approval.decided"
		if isApproval {
			if err := json.Unmarshal([]byte(event.Payload), &approval); err != nil || strings.TrimSpace(approval.ID) == "" || strings.TrimSpace(approval.TaskID) == "" || strings.TrimSpace(approval.RequestedBy) == "" || strings.TrimSpace(approval.ApproverID) == "" || approval.RequestedBy == approval.ApproverID || (approval.Status != "pending" && approval.Status != "approved" && approval.Status != "rejected") {
				registry.Unlock()
				http.Error(writer, "invalid task approval payload", http.StatusBadRequest)
				return
			}
			if task, found := tasks[approval.TaskID]; !found || !task.Active {
				registry.Unlock()
				http.Error(writer, "active task missing", http.StatusConflict)
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
			if event.Topic == "task.approval.decided" {
				if existing, found := approvals[approval.ID]; !found || existing.Status != "pending" || existing.TaskID != approval.TaskID || existing.ApproverID != approval.ApproverID {
					registry.Unlock()
					http.Error(writer, "unknown or closed task approval", http.StatusConflict)
					return
				}
			}
			if !approval.Active {
				registry.Unlock()
				http.Error(writer, "invalid task approval state", http.StatusConflict)
				return
			}
		}
		var comment centralComment
		isComment := event.Topic == "comment.created"
		if isComment {
			if err := json.Unmarshal([]byte(event.Payload), &comment); err != nil || strings.TrimSpace(comment.ID) == "" || (comment.SubjectType != "task" && comment.SubjectType != "ticket") || strings.TrimSpace(comment.SubjectID) == "" || strings.TrimSpace(comment.AuthorID) == "" || len(strings.TrimSpace(comment.Body)) == 0 || len(comment.Body) > 16000 {
				registry.Unlock()
				http.Error(writer, "invalid comment payload", http.StatusBadRequest)
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
			if _, exists := comments[comment.ID]; exists {
				registry.Unlock()
				writer.WriteHeader(http.StatusAccepted)
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
			if _, err := hex.DecodeString(attachment.SHA256); err != nil {
				registry.Unlock()
				http.Error(writer, "invalid attachment hash", http.StatusBadRequest)
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
			if event.Topic == "attachment.created" && !attachment.Active {
				registry.Unlock()
				http.Error(writer, "invalid attachment state", http.StatusConflict)
				return
			}
			if event.Topic == "attachment.archived" {
				attachment.Active = false
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
		if err := persistEvent(journal, event); err != nil {
			registry.Unlock()
			http.Error(writer, "persistence failure", http.StatusInternalServerError)
			return
		}
		if isBooking {
			registry.active[bookingKey(booking.ClientID, booking.BookingID)] = booking
		}
		if event.Topic == "booking.cancelled" {
			var cancelled bookingPayload
			if json.Unmarshal([]byte(event.Payload), &cancelled) == nil {
				delete(registry.active, bookingKey(cancelled.ClientID, cancelled.BookingID))
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
		if isComment {
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

//export aura_publish_event
func aura_publish_event(topic *C.char, payload *C.char) C.int {
	if topic == nil || payload == nil {
		return 1
	}
	event := syncEvent{Topic: C.GoString(topic), Payload: C.GoString(payload), At: time.Now().UTC().Format(time.RFC3339Nano)}
	result := publishEvent(event)
	if result != 0 {
		return C.int(result)
	}
	eventLog.Lock()
	eventLog.latest = "Persistiert: " + event.Topic + " @ " + event.At
	eventLog.Unlock()
	return 0
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
func aura_flush_outbox() C.int { return C.int(flushOutbox()) }

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
