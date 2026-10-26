// Package portable is the platform-neutral AURA API adapter.  It contains no
// Win32, SQLite, or cgo dependencies and can therefore be embedded by Linux
// and Android front ends as well as command-line integration tooling.
package portable

import (
	"bytes"
	"context"
	sha256pkg "crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

type Client struct {
	BaseURL, Token string
	HTTP           *http.Client
}
type LeaveRequest struct {
	ID         string `json:"id"`
	EmployeeID int64  `json:"employeeId"`
	StartsAt   int64  `json:"startsAt"`
	EndsAt     int64  `json:"endsAt"`
	Reason     string `json:"reason"`
	Status     string `json:"status"`
	DecidedBy  int64  `json:"decidedBy,omitempty"`
}
type Task struct {
	ID         string `json:"id"`
	Title      string `json:"title"`
	Department string `json:"department"`
	Priority   string `json:"priority"`
	Status     string `json:"status"`
	AssigneeID int64  `json:"assigneeId"`
	CreatedBy  int64  `json:"createdBy"`
}
type Ticket struct {
	ID         string `json:"id"`
	Title      string `json:"title"`
	Category   string `json:"category"`
	Priority   string `json:"priority"`
	Status     string `json:"status"`
	AssigneeID int64  `json:"assigneeId"`
	CreatedBy  int64  `json:"createdBy"`
}
type Room struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	Location  string `json:"location"`
	Capacity  int    `json:"capacity"`
	Equipment string `json:"equipment"`
	Active    bool   `json:"active"`
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
type FacilityAsset struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Category string `json:"category"`
	Location string `json:"location"`
	Status   string `json:"status"`
	Active   bool   `json:"active"`
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
		httpClient = &http.Client{Timeout: 15 * time.Second}
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
func (client Client) DecideLeave(ctx context.Context, id, decision string) (LeaveRequest, error) {
	var result LeaveRequest
	err := client.request(ctx, http.MethodPost, "/api/v1/leave-requests/"+id+"/decision", map[string]string{"decision": decision}, &result)
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
func (client Client) CreateTask(ctx context.Context, title, department, priority string, assigneeID int64) (Task, error) {
	var result Task
	err := client.request(ctx, http.MethodPost, "/api/v1/tasks", map[string]any{"title": title, "department": department, "priority": priority, "assigneeId": assigneeID}, &result)
	return result, err
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
		httpClient = &http.Client{Timeout: 15 * time.Second}
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
		httpClient = &http.Client{Timeout: 15 * time.Second}
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
