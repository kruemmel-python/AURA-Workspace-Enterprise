// AURA Workspace Enterprise portable command client.
// It is UI-neutral so Linux and Android adapters use the same central protocol.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
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
	action := flag.String("action", "sync", "sync|rooms|tasks|projects|tickets|leave|facility-assets|create-leave|leave-decision|create-task|task-status|create-ticket|ticket-status|create-room|archive-room|create-project|archive-project|create-facility-asset|update-facility-asset|archive-facility-asset|create-comment|request-task-approval|decide-task-approval|upload-attachment|download-attachment|publish-event")
	cursor := flag.Int("cursor", 0, "cursor for sync")
	starts := flag.Int64("starts-at", 0, "leave start as Unix seconds")
	ends := flag.Int64("ends-at", 0, "leave end as Unix seconds")
	reason := flag.String("reason", "", "leave reason")
	title := flag.String("title", "", "task or ticket title")
	department := flag.String("department", "", "department")
	category := flag.String("category", "", "ticket category")
	priority := flag.String("priority", "Mittel", "Niedrig|Mittel|Hoch|Kritisch")
	status := flag.String("status", "", "workflow status")
	id := flag.String("id", "", "central object id")
	assignee := flag.Int64("assignee-id", 0, "central assignee employee id")
	name := flag.String("name", "", "room or project name")
	location := flag.String("location", "", "room location")
	assetCategory := flag.String("asset-category", "", "facility asset category")
	assetStatus := flag.String("asset-status", "operational", "operational|maintenance|out_of_service")
	capacity := flag.Int("capacity", 0, "room capacity")
	equipment := flag.String("equipment", "", "room equipment")
	description := flag.String("description", "", "project description")
	owner := flag.Int64("owner-id", 0, "central project owner id")
	topic := flag.String("topic", "", "enterprise event topic")
	payload := flag.String("payload", "", "JSON event payload")
	subjectType := flag.String("subject-type", "", "task|ticket")
	subjectID := flag.String("subject-id", "", "stable central task or ticket id")
	authorID := flag.String("author-id", "", "stable central employee id")
	body := flag.String("body", "", "comment body")
	requesterID := flag.String("requested-by", "", "stable central requester employee id")
	approverID := flag.String("approver-id", "", "stable central approver employee id")
	decisionBy := flag.String("decided-by", "", "stable central deciding employee id")
	filePath := flag.String("file", "", "source or destination file path for attachment actions")
	flag.Parse()
	if strings.TrimSpace(*url) == "" || strings.TrimSpace(*token) == "" {
		fail(fmt.Errorf("-url/-token (or AURA_SYNC_URL/AURA_SYNC_TOKEN) are required"))
	}
	client := portable.Client{BaseURL: *url, Token: *token}
	ctx := context.Background()
	switch *action {
	case "sync":
		value, err := client.Changes(ctx, *cursor)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "rooms":
		value, err := client.Rooms(ctx, true)
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
	case "leave":
		value, err := client.Leaves(ctx)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "facility-assets":
		value, err := client.FacilityAssets(ctx, true)
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
		value, err := client.DecideLeave(ctx, *id, *status)
		if err != nil {
			fail(err)
		}
		emit(value)
	case "create-task":
		if strings.TrimSpace(*title) == "" || strings.TrimSpace(*department) == "" || *assignee <= 0 {
			fail(fmt.Errorf("-title, -department and -assignee-id are required"))
		}
		value, err := client.CreateTask(ctx, *title, *department, *priority, *assignee)
		if err != nil {
			fail(err)
		}
		emit(value)
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
