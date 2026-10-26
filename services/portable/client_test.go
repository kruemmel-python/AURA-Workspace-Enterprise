package portable

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestPortableClientUsesBearerAndDecodesChangeFeed(t *testing.T) {
	server:=httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){if r.Header.Get("Authorization")!="Bearer portable-token"{http.Error(w,"unauthorized",401);return};if r.URL.Path!="/api/v1/sync/changes"||r.URL.Query().Get("cursor")!="4"{http.Error(w,"wrong route",400);return};w.Header().Set("Content-Type","application/json");_,_=w.Write([]byte(`{"events":[{"topic":"task.created","payload":"{}","at":"2026-10-26T10:00:00Z"}],"nextCursor":5}`))}));defer server.Close()
	feed,err:=(Client{BaseURL:server.URL,Token:"portable-token"}).Changes(context.Background(),4);if err!=nil{t.Fatal(err)};if feed.NextCursor!=5||len(feed.Events)!=1||feed.Events[0].Topic!="task.created"{t.Fatalf("unexpected feed: %#v",feed)}
}

func TestPortableClientReturnsServerFailure(t *testing.T) {
	server:=httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){http.Error(w,"forbidden",http.StatusForbidden)}));defer server.Close()
	if _,err:=(Client{BaseURL:server.URL,Token:"t"}).Tasks(context.Background());err==nil{t.Fatal("forbidden response was accepted")}
}

func TestPortableClientCreatesRoom(t *testing.T) {
	server:=httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){if r.Method!=http.MethodPost||r.URL.Path!="/api/v1/rooms"||r.Header.Get("Authorization")!="Bearer room-token"{http.Error(w,"wrong request",400);return};w.Header().Set("Content-Type","application/json");_,_=w.Write([]byte(`{"id":"room-1","name":"Alpha","location":"Berlin","capacity":12,"equipment":"Display","active":true}`))}));defer server.Close()
	room,err:=(Client{BaseURL:server.URL,Token:"room-token"}).CreateRoom(context.Background(),"Alpha","Berlin",12,"Display");if err!=nil{t.Fatal(err)};if room.ID!="room-1"||!room.Active||room.Capacity!=12{t.Fatalf("unexpected room: %#v",room)}
}
